#!/usr/bin/env python3
"""
Gravador de Censura Digital para Rádio
Sistema de gravação contínua com divisão em chunks organizados por horário.

Usa modo callback (PyAudio ou sounddevice) com queue.Queue para captura
não-bloqueante, watchdog para detecção de stalls, e fan-out para streaming.
No Windows, sounddevice é preferido (PyAudio pode travar).
"""
from __future__ import annotations

import struct
import wave
import os
import gc
import queue
import threading
import time
from datetime import datetime, date
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Callable

_NUMPY_AVAILABLE = None


def _have_numpy() -> bool:
    global _NUMPY_AVAILABLE
    if _NUMPY_AVAILABLE is None:
        try:
            import numpy
            _NUMPY_AVAILABLE = True
        except Exception:
            _NUMPY_AVAILABLE = False
    return _NUMPY_AVAILABLE


def _compute_rms(data: bytes) -> float:
    """Calcula RMS do PCM int16. Usa numpy se disponível, senão Python puro."""
    n = len(data) // 2
    if n == 0:
        return 0.0
    if _have_numpy():
        try:
            import numpy as np
            pcm = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
            return min(1.0, rms / 32768.0)
        except Exception:
            pass
    total = 0
    for i in range(n):
        s = struct.unpack_from("h", data, i * 2)[0]
        total += s * s
    rms = (total / n) ** 0.5
    return min(1.0, rms / 32768.0)


def _scale_monitor_pcm(data: bytes, volume: float) -> bytes:
    """Escala PCM int16 para monitor. Usa numpy se disponível, senão Python puro."""
    if _have_numpy():
        try:
            import numpy as np
            pcm = np.frombuffer(data, dtype=np.int16)
            scaled = (pcm * volume).astype(np.int16)
            return scaled.tobytes()
        except Exception:
            pass
    n = len(data) // 2
    result = bytearray(len(data))
    for i in range(n):
        s = struct.unpack_from("h", data, i * 2)[0]
        scaled = max(-32768, min(32767, int(s * volume)))
        struct.pack_into("h", result, i * 2, scaled)
    return bytes(result)

WATCHDOG_CHECK_INTERVAL = 2.0
STALL_THRESHOLD_SECONDS = 10.0
QUEUE_READ_TIMEOUT = 2.0
MAX_RESTART_ATTEMPTS = 5
AUDIO_QUEUE_MAXSIZE = 2000


class CensuraDigital:
    def __init__(self, config_file: str = "config_censura.json"):
        self.config_file = config_file

        self.config: dict = {}
        self.setup_logging()
        self.load_config()
        self.setup_logging()

        from audio_backend import get_backend
        self._audio_backend = get_backend()

        # Recording state
        self.is_recording = False
        self.input_stream = None
        self.recording_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._stream_lock = threading.Lock()

        # Callback-based audio capture
        self._audio_queue: queue.Queue = queue.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)

        # Monitoring
        self.is_monitoring = False
        self.monitor_stream = None
        self.monitor_volume = 1.0

        # Chunk control
        self.current_chunk_start: Optional[datetime] = None
        self.chunk_counter = 0

        # Watchdog & metrics
        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_data_timestamp = 0.0
        self._stall_count = 0
        self._restart_attempts = 0
        self._total_frames = 0
        self._total_bytes = 0
        self._current_level = 0.0
        self._on_alert_callback: Optional[Callable[[str], None]] = None
        self._on_recording_failed_callback: Optional[Callable[[str], None]] = None

        # Streaming fan-out (set externally via set_stream_manager)
        self._stream_manager = None

        self.logger.info("Sistema de Censura Digital inicializado")

    # ── Configuration ─────────────────────────────────────────────

    def load_config(self):
        """Carrega configurações do arquivo JSON com deep-merge sobre defaults."""
        default_config = {
            "audio": {
                "format": "paInt16",
                "channels": 1,
                "rate": 44100,
                "chunk_size": 1024,
                "device_index": None,
            },
            "recording": {
                "chunk_duration_minutes": 15,
                "output_directory": "gravacoes_radio",
                "filename_prefix": "radio",
                "max_chunks_per_day": 96,
            },
            "logging": {
                "log_file": "censura_digital.log",
                "log_level": "INFO",
            },
        }

        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                for section in default_config:
                    if section in user_config and isinstance(user_config[section], dict):
                        default_config[section].update(user_config[section])
                for key in user_config:
                    if key not in default_config:
                        default_config[key] = user_config[key]
                self.config = default_config
            except Exception as e:
                self.logger.error(f"Erro ao carregar configuração: {e}")
                self.config = default_config
        else:
            self.config = default_config
            self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.logger.error(f"Erro ao salvar configuração: {e}")

    def setup_logging(self):
        log_config = self.config.get("logging", {})
        log_level_str = log_config.get("log_level", "INFO").upper()
        log_level = getattr(logging, log_level_str, logging.INFO)
        log_file = log_config.get("log_file")

        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        handlers = [logging.StreamHandler()]
        if log_file:
            try:
                handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
            except Exception as e:
                print(f"AVISO: Não foi possível criar o arquivo de log '{log_file}': {e}")

        logging.basicConfig(
            level=log_level,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=handlers,
        )
        self.logger = logging.getLogger(__name__)

    # ── Audio devices ─────────────────────────────────────────────

    def get_audio_devices(self) -> list[dict[str, Any]]:
        return self._audio_backend.get_devices()

    def list_audio_devices(self):
        devices = self.get_audio_devices()
        print("\n=== Dispositivos de Áudio Disponíveis ===")
        if not devices:
            print("Nenhum dispositivo de áudio encontrado.")
            return
        for dev in devices:
            i = dev["index"]
            print(f"Dispositivo {i}: {dev['name']}")
            print(f"  Canais de entrada: {dev['maxInputChannels']}")
            print(f"  Canais de saída: {dev['maxOutputChannels']}")
            print(f"  Taxa de amostragem padrão: {dev['defaultSampleRate']}")
            print()

    def _get_sample_width(self) -> int:
        """Largura da amostra em bytes (sempre 2 para int16)."""
        return 2

    # ── Stream management (callback mode) ─────────────────────────

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """Callback de áudio – deposita dados na queue (nunca bloqueia)."""
        try:
            self._audio_queue.put_nowait(in_data)
        except queue.Full:
            pass
        self._last_data_timestamp = time.time()

    def _open_streams(self) -> bool:
        """Abre streams em thread separada com timeout para evitar travar a UI.
        No Windows, sounddevice/PyAudio podem bloquear durante abertura do stream."""
        result_holder: list = [None]
        exc_holder: list = [None]

        def _do_open():
            try:
                self._drain_queue()
                audio_cfg = self.config["audio"]
                inp = self._audio_backend.open_input_stream(
                    channels=audio_cfg["channels"],
                    rate=audio_cfg["rate"],
                    chunk_size=audio_cfg["chunk_size"],
                    device_index=audio_cfg["device_index"],
                    callback=self._audio_callback,
                )
                mon = None
                if self.is_monitoring:
                    mon = self._audio_backend.open_output_stream(
                        channels=audio_cfg["channels"],
                        rate=audio_cfg["rate"],
                        chunk_size=audio_cfg["chunk_size"],
                    )
                result_holder[0] = (inp, mon)
            except Exception as e:
                exc_holder[0] = e

        opener = threading.Thread(target=_do_open, name="StreamOpener", daemon=True)
        opener.start()
        opener.join(timeout=15.0)
        if opener.is_alive():
            self.logger.error("Timeout ao abrir stream de áudio (15s). Verifique o dispositivo.")
            return False
        if exc_holder[0]:
            self.logger.error(f"Erro ao abrir streams: {exc_holder[0]}")
            return False
        inp, mon = result_holder[0]
        if inp is None:
            return False
        with self._stream_lock:
            self.input_stream = inp
            self.monitor_stream = mon
        self._last_data_timestamp = time.time()
        self.logger.info("Stream de entrada aberto (callback mode).")
        if mon:
            self.logger.info("Stream de monitoramento aberto.")
        return True

    def _close_streams(self):
        with self._stream_lock:
            for attr in ("input_stream", "monitor_stream"):
                stream = getattr(self, attr, None)
                if stream is not None:
                    try:
                        if stream.is_active():
                            stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)

    def _drain_queue(self):
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def _try_restart_stream(self):
        """Tenta fechar e reabrir o stream de entrada após stall detectado."""
        if self._restart_attempts >= MAX_RESTART_ATTEMPTS:
            msg = (
                f"Limite de {MAX_RESTART_ATTEMPTS} tentativas de reinício atingido. "
                "Parando gravação."
            )
            self.logger.critical(msg)
            if self._on_alert_callback:
                try:
                    self._on_alert_callback(msg)
                except Exception:
                    pass
            self._stop_event.set()
            return

        self._restart_attempts += 1
        self.logger.warning(
            f"Reiniciando stream (tentativa {self._restart_attempts}/{MAX_RESTART_ATTEMPTS})..."
        )

        with self._stream_lock:
            if self.input_stream:
                try:
                    self.input_stream.stop_stream()
                    self.input_stream.close()
                except Exception:
                    pass
                self.input_stream = None

            try:
                audio_cfg = self.config["audio"]
                self.input_stream = self._audio_backend.open_input_stream(
                    channels=audio_cfg["channels"],
                    rate=audio_cfg["rate"],
                    chunk_size=audio_cfg["chunk_size"],
                    device_index=audio_cfg["device_index"],
                    callback=self._audio_callback,
                )
                self._last_data_timestamp = time.time()
                self._restart_attempts = 0
                self.logger.info("Stream reiniciado com sucesso.")
            except Exception as e:
                self.logger.error(f"Falha ao reiniciar stream: {e}")

    # ── Directory / filename helpers ──────────────────────────────

    def create_output_directory(self, date_to_use: date) -> Path:
        base_dir = Path(self.config["recording"]["output_directory"])
        date_dir = base_dir / date_to_use.strftime("%Y") / date_to_use.strftime("%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        return date_dir

    def generate_filename(self, start_time: datetime) -> str:
        prefix = self.config["recording"]["filename_prefix"]
        timestamp = start_time.strftime("%Y%m%d_%H%M%S")
        return f"{prefix}_{timestamp}.wav"

    # ── Watchdog ──────────────────────────────────────────────────

    def _watchdog_loop(self):
        """Thread que monitora a saúde da captura de áudio."""
        while not self._stop_event.is_set():
            if self._last_data_timestamp > 0:
                age = time.time() - self._last_data_timestamp
                if age > STALL_THRESHOLD_SECONDS:
                    self._stall_count += 1
                    msg = (
                        f"ALERTA: Sem dados de áudio há {age:.1f}s "
                        f"(stall #{self._stall_count})"
                    )
                    self.logger.warning(msg)
                    if self._on_alert_callback:
                        try:
                            self._on_alert_callback(msg)
                        except Exception:
                            pass
            self._stop_event.wait(WATCHDOG_CHECK_INTERVAL)

    # ── Recording loop ────────────────────────────────────────────

    def recording_loop(self):
        """Loop principal: lê da queue, grava WAV, faz fan-out para streaming."""
        if not self._open_streams():
            self.is_recording = False
            self._stop_event.set()
            self.logger.error("Falha ao abrir streams de áudio. Abortando.")
            if self._on_recording_failed_callback:
                try:
                    self._on_recording_failed_callback(
                        "Não foi possível abrir o dispositivo de áudio. "
                        "Verifique se está correto e não está em uso."
                    )
                except Exception:
                    pass
            return

        audio_cfg = self.config["audio"]
        rate = audio_cfg["rate"]
        chunk_size = audio_cfg["chunk_size"]
        chunk_duration_seconds = self.config["recording"]["chunk_duration_minutes"] * 60
        max_frames_per_chunk = int(rate * chunk_duration_seconds)
        max_chunks = self.config["recording"].get("max_chunks_per_day", 96)
        last_chunk_date = None

        try:
            while not self._stop_event.is_set():
                current_date = datetime.now().date()
                if current_date != last_chunk_date:
                    if last_chunk_date is not None:
                        self.logger.info(
                            "Virada de dia detectada. Contador de chunks reiniciado."
                        )
                    self.chunk_counter = 0
                last_chunk_date = current_date

                if self.chunk_counter >= max_chunks:
                    self.logger.warning(
                        f"Limite diário de {max_chunks} chunks atingido. Pausando."
                    )
                    self._stop_event.wait(60)
                    continue

                self.chunk_counter += 1
                self.current_chunk_start = datetime.now()
                output_dir = self.create_output_directory(
                    self.current_chunk_start.date()
                )
                filename = self.generate_filename(self.current_chunk_start)
                output_path = output_dir / filename

                self.logger.info(f"Iniciando chunk #{self.chunk_counter}: {output_path}")

                try:
                    with wave.open(str(output_path), "wb") as wf:
                        wf.setnchannels(audio_cfg["channels"])
                        wf.setsampwidth(self._get_sample_width())
                        wf.setframerate(rate)

                        frames_written = 0
                        consecutive_timeouts = 0

                        while (
                            frames_written < max_frames_per_chunk
                            and not self._stop_event.is_set()
                        ):
                            try:
                                data = self._audio_queue.get(
                                    timeout=QUEUE_READ_TIMEOUT
                                )
                                consecutive_timeouts = 0

                                wf.writeframes(data)
                                frames_written += chunk_size
                                self._total_frames += chunk_size
                                self._total_bytes += len(data)

                                # Compute RMS level for VU meter
                                self._current_level = _compute_rms(data)

                                # Monitoring (playback)
                                if self.is_monitoring and self.monitor_stream:
                                    try:
                                        scaled = _scale_monitor_pcm(
                                            data, self.monitor_volume
                                        )
                                        self.monitor_stream.write(scaled)
                                    except Exception:
                                        pass

                                # Streaming fan-out
                                if self._stream_manager:
                                    self._stream_manager.feed_audio(data)

                            except queue.Empty:
                                consecutive_timeouts += 1
                                timeout_secs = (
                                    consecutive_timeouts * QUEUE_READ_TIMEOUT
                                )
                                if timeout_secs >= STALL_THRESHOLD_SECONDS:
                                    self._stall_count += 1
                                    self.logger.warning(
                                        f"Stall detectado: {timeout_secs:.0f}s sem dados "
                                        f"(stall #{self._stall_count})"
                                    )
                                    if self._on_alert_callback:
                                        try:
                                            self._on_alert_callback(
                                                f"Stall: {timeout_secs:.0f}s sem dados"
                                            )
                                        except Exception:
                                            pass
                                    self._try_restart_stream()
                                    consecutive_timeouts = 0

                    if output_path.exists():
                        file_size_mb = output_path.stat().st_size / (1024 * 1024)
                        self.logger.info(
                            f"Chunk #{self.chunk_counter} finalizado: "
                            f"{output_path.name} ({file_size_mb:.2f} MB)"
                        )

                except Exception as e:
                    self.logger.critical(f"Erro fatal ao gravar chunk: {e}")
                    self._stop_event.set()

                gc.collect()

        finally:
            self._close_streams()
            self.is_recording = False

    # ── Public API ────────────────────────────────────────────────

    def set_stream_manager(self, manager):
        """Registra um StreamManager para fan-out de áudio."""
        self._stream_manager = manager

    def set_alert_callback(self, callback: Optional[Callable[[str], None]]):
        """Registra callback para alertas do watchdog (chamado de thread separada)."""
        self._on_alert_callback = callback

    def set_recording_failed_callback(self, callback: Optional[Callable[[str], None]]):
        """Registra callback quando falha ao iniciar gravação (ex: timeout ao abrir stream)."""
        self._on_recording_failed_callback = callback

    def start_recording(self, enable_monitoring: bool = False) -> bool:
        if self.is_recording:
            self.logger.warning("Gravação já está em andamento")
            return False

        self.is_monitoring = enable_monitoring
        self.is_recording = True
        self._stop_event.clear()
        self.chunk_counter = 0
        self._stall_count = 0
        self._restart_attempts = 0
        self._total_frames = 0
        self._total_bytes = 0
        self._current_level = 0.0
        self._last_data_timestamp = 0.0
        self._drain_queue()

        self.recording_thread = threading.Thread(
            target=self.recording_loop, name="RecordingThread", daemon=True
        )
        self.recording_thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="WatchdogThread", daemon=True
        )
        self._watchdog_thread.start()

        self.logger.info(
            f"Gravação iniciada "
            f"(Monitoramento: {'Ligado' if self.is_monitoring else 'Desligado'})"
        )
        return True

    def stop_recording(self) -> bool:
        if not self.is_recording:
            self.logger.warning("Gravação não está em andamento")
            return False

        self.is_recording = False
        self._stop_event.set()

        if self.recording_thread:
            self.recording_thread.join(timeout=10)
            if self.recording_thread.is_alive():
                self.logger.warning(
                    "Thread de gravação não finalizou no tempo esperado. "
                    "Forçando fechamento dos streams."
                )
                self._close_streams()

        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=5)

        self.logger.info("Gravação interrompida")
        return True

    def set_monitor_volume(self, volume: float):
        self.monitor_volume = max(0.0, min(1.5, volume))
        self.logger.info(f"Volume do monitoramento: {self.monitor_volume:.2f}")

    def get_status(self) -> Dict[str, Any]:
        start_time_iso = None
        if self.current_chunk_start:
            start_time_iso = self.current_chunk_start.isoformat()

        return {
            "is_recording": self.is_recording,
            "is_monitoring": self.is_monitoring,
            "chunk_counter": self.chunk_counter,
            "current_chunk_start": start_time_iso,
            "config": self.config,
            "stall_count": self._stall_count,
            "total_frames": self._total_frames,
            "total_bytes": self._total_bytes,
            "current_level": self._current_level,
            "last_data_age": (
                round(time.time() - self._last_data_timestamp, 1)
                if self._last_data_timestamp > 0
                else None
            ),
        }

    # ── Context manager ───────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_recording()
        if self._audio_backend:
            try:
                self._audio_backend.terminate()
            except Exception:
                pass
        return False

    def __del__(self):
        if getattr(self, "_audio_backend", None):
            try:
                self._audio_backend.terminate()
            except Exception:
                pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sistema de Censura Digital para Rádio")
    parser.add_argument(
        "--config", default="config_censura.json", help="Arquivo de configuração"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="Lista dispositivos de áudio"
    )
    parser.add_argument(
        "--monitor", action="store_true", help="Ativa monitoramento de áudio"
    )
    args = parser.parse_args()

    with CensuraDigital(args.config) as censura:
        if args.list_devices:
            censura.list_audio_devices()
            return

        print("=== Sistema de Censura Digital (Linha de Comando) ===")
        print(
            f"Duração do chunk: "
            f"{censura.config['recording']['chunk_duration_minutes']} minutos"
        )
        print(f"Diretório de saída: {censura.config['recording']['output_directory']}")

        try:
            if censura.start_recording(enable_monitoring=args.monitor):
                print("Gravação iniciada. Pressione Ctrl+C para parar.")
                while censura.is_recording:
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\nInterrompendo gravação...")

        censura.stop_recording()
        print("Gravação finalizada.")


if __name__ == "__main__":
    main()
