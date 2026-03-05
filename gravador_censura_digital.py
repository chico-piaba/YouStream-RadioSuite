#!/usr/bin/env python3
"""
Gravador de Censura Digital para Rádio
Sistema de gravação contínua com divisão em chunks (modelo estável do censura-digital).
Usa PyAudio com leitura bloqueante; opcionalmente alimenta StreamManager para RTMP/Icecast.

Inclui: retry de stream de áudio, verificação de espaço em disco,
validação de WAV pós-gravação e métricas de gravação.
"""
from __future__ import annotations

import array
import math
import pyaudio
import shutil
import wave
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
import json
import logging
from pathlib import Path
from typing import Optional, Callable, Dict, Any

STREAM_RETRY_MAX = 3
STREAM_RETRY_DELAY = 2.0
MIN_DISK_SPACE_MB = 500
MIN_WAV_SIZE_BYTES = 1024


def _deep_merge(base: dict, override: dict) -> dict:
    """Faz merge recursivo: override sobrescreve base, dicts aninhados são mesclados."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class RecordingMetrics:
    """Métricas de saúde da gravação por chunk."""

    bytes_written: int = 0
    io_errors: int = 0
    stream_retries: int = 0
    chunks_completed: int = 0
    chunks_failed: int = 0
    expected_duration_s: float = 0.0
    actual_duration_s: float = 0.0

    @property
    def duration_accuracy(self) -> float:
        if self.expected_duration_s <= 0:
            return 1.0
        return min(self.actual_duration_s / self.expected_duration_s, 1.0)

    def reset_chunk(self):
        self.bytes_written = 0
        self.io_errors = 0
        self.expected_duration_s = 0.0
        self.actual_duration_s = 0.0


class CensuraDigital:
    def __init__(self, config_file: str = "config_censura.json"):
        self.config_file = config_file
        self.audio = None
        self.config = {}
        self.setup_logging()
        self.load_config()
        self.setup_logging()

        self.audio = pyaudio.PyAudio()

        self.is_recording = False
        self.input_stream = None
        self.recording_thread: Optional[threading.Thread] = None

        self.is_monitoring = False
        self.monitor_stream = None
        self.monitor_volume = 1.0

        self.current_chunk_start: Optional[datetime] = None
        self.chunk_counter = 0

        self._stream_manager = None
        self.current_level = 0.0

        self._alert_callback: Optional[Callable[[str], None]] = None
        self._recording_failed_callback: Optional[Callable[[str], None]] = None

        self._metrics = RecordingMetrics()
        self._stall_count = 0

        self.logger.info("Sistema de Censura Digital inicializado (PyAudio, leitura bloqueante)")

    def load_config(self):
        """Carrega configurações com deep merge para preservar streaming/interface."""
        default_config = {
            "audio": {
                "format": "paInt16",
                "channels": 1,
                "rate": 44100,
                "chunk_size": 1024,
                "device_index": None,
            },
            "recording": {
                "chunk_duration_minutes": 30,
                "output_directory": "gravacoes_radio",
                "filename_prefix": "radio",
                "max_chunks_per_day": 48,
            },
            "logging": {
                "log_file": "censura_digital.log",
                "log_level": "INFO",
            },
            "streaming": {"rtmp": {}, "icecast": {}},
            "interface": {},
        }
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                self.config = _deep_merge(default_config, user_config)
            except Exception as e:
                self.logger.error("Erro ao carregar configuração: %s", e)
                self.config = default_config
        else:
            self.config = default_config
            self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.logger.error("Erro ao salvar configuração: %s", e)

    def setup_logging(self):
        from logging.handlers import RotatingFileHandler

        log_config = self.config.get("logging", {})
        log_level_str = log_config.get("log_level", "INFO").upper()
        log_level = getattr(logging, log_level_str, logging.INFO)
        log_file = log_config.get("log_file")
        max_bytes = int(log_config.get("max_log_size_mb", 5)) * 1024 * 1024
        backup_count = int(log_config.get("log_backup_count", 5))

        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        log_format = (
            "%(asctime)s | %(levelname)-8s | %(threadName)-16s | "
            "%(name)s | %(message)s"
        )
        formatter = logging.Formatter(log_format)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        handlers: list[logging.Handler] = [console_handler]

        if log_file:
            try:
                file_handler = RotatingFileHandler(
                    log_file, maxBytes=max_bytes,
                    backupCount=backup_count, encoding="utf-8",
                )
                file_handler.setFormatter(formatter)
                handlers.append(file_handler)
            except Exception as e:
                print(f"AVISO: Não foi possível criar o arquivo de log '{log_file}': {e}")

        logging.basicConfig(level=log_level, handlers=handlers)
        self.logger = logging.getLogger(__name__)

    def get_audio_devices(self) -> list[dict]:
        devices = []
        for i in range(self.audio.get_device_count()):
            try:
                devices.append(self.audio.get_device_info_by_index(i))
            except Exception as e:
                self.logger.warning("Não foi possível obter informações do dispositivo %d: %s", i, e)
        return devices

    def list_audio_devices(self):
        devices = self.get_audio_devices()
        print("\n=== Dispositivos de Áudio Disponíveis ===")
        if not devices:
            print("Nenhum dispositivo de áudio encontrado.")
            return
        for d in devices:
            print(f"Dispositivo {d['index']}: {d['name']}")
            print(f"  Canais de entrada: {d['maxInputChannels']}  Saída: {d['maxOutputChannels']}  Taxa: {d['defaultSampleRate']} Hz")
            print()

    def get_audio_format(self):
        fmt = self.config["audio"].get("format", "paInt16")
        return getattr(pyaudio, fmt, pyaudio.paInt16)

    def _validate_device(self, device_index, channels) -> int | None:
        """Valida se o dispositivo suporta os canais pedidos. Retorna index validado ou None para fallback."""
        if device_index is None:
            return None
        try:
            dev_info = self.audio.get_device_info_by_index(device_index)
            max_in = dev_info.get("maxInputChannels", 0)
            if max_in < channels:
                self.logger.warning(
                    "Dispositivo %d (%s) suporta apenas %d canais de entrada, "
                    "mas config pede %d. Usando dispositivo padrão.",
                    device_index, dev_info.get("name", "?"), max_in, channels,
                )
                return None
            return device_index
        except Exception as e:
            self.logger.warning("Dispositivo %d inválido: %s. Usando dispositivo padrão.", device_index, e)
            return None

    # ── Disk space check ──────────────────────────────────────────

    def _check_disk_space(self, path: Path) -> bool:
        """Verifica se há espaço mínimo em disco antes de gravar."""
        try:
            usage = shutil.disk_usage(str(path))
            free_mb = usage.free / (1024 * 1024)
            if free_mb < MIN_DISK_SPACE_MB:
                msg = (
                    f"Espaço em disco insuficiente: {free_mb:.0f} MB livre "
                    f"(mínimo: {MIN_DISK_SPACE_MB} MB) em {path}"
                )
                self.logger.critical(msg)
                self._fire_alert(msg)
                return False
            if free_mb < MIN_DISK_SPACE_MB * 2:
                self.logger.warning(
                    "Espaço em disco baixo: %.0f MB livre em %s", free_mb, path,
                )
                self._fire_alert(f"Espaço em disco baixo: {free_mb:.0f} MB livre")
            return True
        except Exception as e:
            self.logger.warning("Não foi possível verificar espaço em disco: %s", e)
            return True

    # ── WAV validation ────────────────────────────────────────────

    def _validate_wav(self, path: Path, expected_duration_s: float) -> bool:
        """Valida integridade básica do WAV gravado."""
        if not path.exists():
            self.logger.error("WAV não encontrado após gravação: %s", path)
            return False

        file_size = path.stat().st_size
        if file_size < MIN_WAV_SIZE_BYTES:
            self.logger.error(
                "WAV muito pequeno (%d bytes): %s — possível corrupção",
                file_size, path.name,
            )
            return False

        try:
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                actual_duration = frames / rate if rate > 0 else 0
                ratio = actual_duration / expected_duration_s if expected_duration_s > 0 else 1.0

                if ratio < 0.5:
                    self.logger.warning(
                        "WAV %s: duração real (%.1fs) muito menor que esperada (%.1fs) — ratio %.1f%%",
                        path.name, actual_duration, expected_duration_s, ratio * 100,
                    )
                else:
                    self.logger.info(
                        "WAV validado: %s | %.1fs | %.1f MB | precisão %.0f%%",
                        path.name, actual_duration,
                        file_size / (1024 * 1024), ratio * 100,
                    )
            return True
        except Exception as e:
            self.logger.error("Falha ao validar WAV %s: %s", path.name, e)
            return False

    # ── Stream open / close / retry ───────────────────────────────

    def _open_streams(self) -> bool:
        import concurrent.futures

        ac = self.config["audio"]
        device_index = self._validate_device(ac.get("device_index"), ac["channels"])

        channels = ac["channels"]
        if device_index is None and ac.get("device_index") is not None:
            channels = 1
            self.logger.info("Fallback: usando 1 canal com dispositivo padrão.")

        def _do_open():
            self.input_stream = self.audio.open(
                format=self.get_audio_format(),
                channels=channels,
                rate=ac["rate"],
                input=True,
                input_device_index=device_index,
                frames_per_buffer=ac["chunk_size"],
            )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_do_open)
                future.result(timeout=10)
            self.logger.info("Stream de entrada aberto (device=%s, ch=%d).", device_index, channels)
        except concurrent.futures.TimeoutError:
            msg = (
                "Timeout ao abrir stream de áudio (10s). "
                "Possíveis causas: dispositivo travado, driver com problema, "
                "ou outro programa usando o áudio em modo exclusivo."
            )
            self.logger.error(msg)
            self._fire_recording_failed(msg)
            return False
        except Exception as e:
            self.logger.error("Erro ao abrir stream de entrada: %s", e)
            if device_index is not None:
                self.logger.info("Tentando fallback com dispositivo padrão...")
                try:
                    self.input_stream = self.audio.open(
                        format=self.get_audio_format(),
                        channels=1,
                        rate=ac["rate"],
                        input=True,
                        input_device_index=None,
                        frames_per_buffer=ac["chunk_size"],
                    )
                    self.logger.info("Fallback: stream aberto com dispositivo padrão (1 canal).")
                except Exception as e2:
                    msg = f"Fallback também falhou: {e2}"
                    self.logger.error(msg)
                    self._fire_recording_failed(msg)
                    return False
            else:
                self._fire_recording_failed(str(e))
                return False

        if self.is_monitoring:
            try:
                self.monitor_stream = self.audio.open(
                    format=self.get_audio_format(),
                    channels=channels,
                    rate=ac["rate"],
                    output=True,
                    frames_per_buffer=ac["chunk_size"],
                )
                self.logger.info("Stream de monitoramento aberto.")
            except Exception as e:
                self.logger.warning("Monitor de áudio não disponível: %s", e)

        return True

    def _reopen_input_stream(self) -> bool:
        """Tenta reabrir o stream de entrada após falha de I/O."""
        ac = self.config["audio"]
        device_index = self._validate_device(ac.get("device_index"), ac["channels"])
        channels = ac["channels"]
        if device_index is None and ac.get("device_index") is not None:
            channels = 1

        if self.input_stream:
            try:
                self.input_stream.stop_stream()
                self.input_stream.close()
            except Exception:
                pass
            self.input_stream = None

        for attempt in range(1, STREAM_RETRY_MAX + 1):
            self._metrics.stream_retries += 1
            self._stall_count += 1
            self.logger.warning(
                "Tentativa de reabertura do stream de áudio %d/%d...",
                attempt, STREAM_RETRY_MAX,
            )
            self._fire_alert(f"Reabrindo stream de áudio (tentativa {attempt}/{STREAM_RETRY_MAX})")
            time.sleep(STREAM_RETRY_DELAY)

            try:
                self.input_stream = self.audio.open(
                    format=self.get_audio_format(),
                    channels=channels,
                    rate=ac["rate"],
                    input=True,
                    input_device_index=device_index,
                    frames_per_buffer=ac["chunk_size"],
                )
                self.logger.info("Stream de áudio reaberto com sucesso na tentativa %d.", attempt)
                self._fire_alert("Stream de áudio recuperado")
                return True
            except Exception as e:
                self.logger.error("Tentativa %d falhou: %s", attempt, e)

        msg = f"Falha ao reabrir stream após {STREAM_RETRY_MAX} tentativas"
        self.logger.critical(msg)
        self._fire_alert(msg)
        return False

    def _close_streams(self):
        if self.input_stream:
            try:
                self.input_stream.stop_stream()
                self.input_stream.close()
            except Exception as exc:
                self.logger.debug("Erro ao fechar input_stream: %s", exc)
            self.input_stream = None
        if self.monitor_stream:
            try:
                self.monitor_stream.stop_stream()
                self.monitor_stream.close()
            except Exception as exc:
                self.logger.debug("Erro ao fechar monitor_stream: %s", exc)
            self.monitor_stream = None

    # ── Callbacks ─────────────────────────────────────────────────

    def _fire_alert(self, message: str):
        self.logger.warning("ALERTA: %s", message)
        if self._alert_callback:
            try:
                self._alert_callback(message)
            except Exception as exc:
                self.logger.debug("Erro no alert callback: %s", exc)

    def _fire_recording_failed(self, message: str):
        self.logger.error("RECORDING FAILED: %s", message)
        if self._recording_failed_callback:
            try:
                self._recording_failed_callback(message)
            except Exception as exc:
                self.logger.debug("Erro no recording_failed callback: %s", exc)

    # ── Output directory / filename ───────────────────────────────

    def create_output_directory(self, date_to_use: date) -> Path:
        base_dir = Path(self.config["recording"]["output_directory"])
        date_dir = base_dir / date_to_use.strftime("%Y") / date_to_use.strftime("%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        return date_dir

    def generate_filename(self, start_time: datetime) -> str:
        prefix = self.config["recording"]["filename_prefix"]
        return f"{prefix}_{start_time.strftime('%Y%m%d_%H%M%S')}.wav"

    # ── Recording loop ────────────────────────────────────────────

    def recording_loop(self):
        if not self._open_streams():
            self.is_recording = False
            self.logger.error("Falha ao abrir streams de áudio. Abortando.")
            self._fire_recording_failed("Falha ao abrir streams de áudio")
            return

        chunk_duration_seconds = self.config["recording"]["chunk_duration_minutes"] * 60
        max_chunks = self.config["recording"].get("max_chunks_per_day", 48)
        last_chunk_date = None
        ac = self.config["audio"]
        chunk_size = ac["chunk_size"]

        while self.is_recording:
            current_date = datetime.now().date()
            if current_date != last_chunk_date:
                self.chunk_counter = 0
            last_chunk_date = current_date

            if self.chunk_counter >= max_chunks:
                time.sleep(60)
                continue

            self.chunk_counter += 1
            self.current_chunk_start = datetime.now()
            output_dir = self.create_output_directory(self.current_chunk_start.date())

            if not self._check_disk_space(output_dir):
                self.is_recording = False
                self._fire_recording_failed(
                    f"Espaço em disco insuficiente em {output_dir}"
                )
                break

            output_path = output_dir / self.generate_filename(self.current_chunk_start)
            self.logger.info("Iniciando chunk #%d: %s", self.chunk_counter, output_path)
            self._metrics.reset_chunk()
            self._metrics.expected_duration_s = chunk_duration_seconds
            chunk_start_time = time.monotonic()

            try:
                with wave.open(str(output_path), "wb") as wf:
                    wf.setnchannels(ac["channels"])
                    wf.setsampwidth(self.audio.get_sample_size(self.get_audio_format()))
                    wf.setframerate(ac["rate"])
                    start_time = datetime.now()
                    consecutive_errors = 0

                    while (datetime.now() - start_time).total_seconds() < chunk_duration_seconds:
                        if not self.is_recording:
                            break
                        try:
                            data = self.input_stream.read(chunk_size, exception_on_overflow=False)
                            wf.writeframes(data)
                            self._metrics.bytes_written += len(data)
                            consecutive_errors = 0
                            self._update_level(data)
                            if self._stream_manager:
                                self._stream_manager.feed_audio(data)
                            if self.is_monitoring and self.monitor_stream:
                                scaled = self._scale_audio(data, self.monitor_volume)
                                self.monitor_stream.write(scaled)
                        except (IOError, OSError) as e:
                            self._metrics.io_errors += 1
                            consecutive_errors += 1
                            self.logger.error(
                                "Erro de I/O na leitura de áudio (#%d consecutivo): %s",
                                consecutive_errors, e,
                            )

                            if consecutive_errors >= 3:
                                if self._reopen_input_stream():
                                    consecutive_errors = 0
                                    continue
                                else:
                                    self.logger.critical(
                                        "Impossível recuperar stream de áudio. "
                                        "Encerrando chunk #%d.", self.chunk_counter,
                                    )
                                    self._fire_alert(
                                        "Stream de áudio perdido — chunk interrompido"
                                    )
                                    break
                            time.sleep(0.1)

                self._metrics.actual_duration_s = time.monotonic() - chunk_start_time

                if output_path.exists():
                    valid = self._validate_wav(output_path, self._metrics.actual_duration_s)
                    if valid:
                        self._metrics.chunks_completed += 1
                    else:
                        self._metrics.chunks_failed += 1

                    self.logger.info(
                        "Chunk #%d finalizado: %s | bytes=%d | erros_io=%d | "
                        "duração=%.1fs/%.1fs (%.0f%%)",
                        self.chunk_counter, output_path.name,
                        self._metrics.bytes_written, self._metrics.io_errors,
                        self._metrics.actual_duration_s,
                        self._metrics.expected_duration_s,
                        self._metrics.duration_accuracy * 100,
                    )

            except Exception as e:
                self._metrics.chunks_failed += 1
                self.logger.critical("Erro ao gravar chunk #%d: %s", self.chunk_counter, e)
                self._fire_alert(f"Erro crítico na gravação: {e}")

                if not self._reopen_input_stream():
                    self.is_recording = False
                    self._fire_recording_failed(
                        f"Erro fatal na gravação e falha ao recuperar: {e}"
                    )

            if not self.is_recording:
                break

        self._close_streams()

    def _update_level(self, data: bytes):
        """Calcula RMS normalizado (0.0-1.0) das amostras PCM int16."""
        samples = array.array("h", data)
        if not samples:
            return
        sum_sq = sum(s * s for s in samples)
        rms = math.sqrt(sum_sq / len(samples))
        self.current_level = min(rms / 32768.0, 1.0)

    @staticmethod
    def _scale_audio(data: bytes, volume: float) -> bytes:
        """Escala amostras PCM int16 pelo fator de volume, sem numpy."""
        if volume == 1.0:
            return data
        samples = array.array("h", data)
        for i in range(len(samples)):
            val = int(samples[i] * volume)
            samples[i] = max(-32768, min(32767, val))
        return samples.tobytes()

    def set_stream_manager(self, manager):
        self._stream_manager = manager

    def set_alert_callback(self, callback):
        self._alert_callback = callback

    def set_recording_failed_callback(self, callback):
        self._recording_failed_callback = callback

    def start_recording(self, enable_monitoring: bool = False) -> bool:
        if self.is_recording:
            return False
        self.is_monitoring = enable_monitoring
        self.is_recording = True
        self.chunk_counter = 0
        self._stall_count = 0
        self._metrics = RecordingMetrics()
        self.recording_thread = threading.Thread(target=self.recording_loop, name="RecordingThread")
        self.recording_thread.daemon = False
        self.recording_thread.start()
        self.logger.info(
            "Gravação iniciada (Monitor: %s)",
            "Ligado" if self.is_monitoring else "Desligado",
        )
        return True

    def stop_recording(self) -> bool:
        if not self.is_recording:
            return False
        self.is_recording = False
        try:
            if self.input_stream and self.input_stream.is_active():
                self.input_stream.stop_stream()
        except Exception as exc:
            self.logger.debug("Erro ao parar input_stream: %s", exc)
        if self.recording_thread:
            self.recording_thread.join(timeout=5)
        self._close_streams()
        self.logger.info(
            "Gravação interrompida | chunks_ok=%d | chunks_falha=%d | retries=%d",
            self._metrics.chunks_completed, self._metrics.chunks_failed,
            self._metrics.stream_retries,
        )
        return True

    def set_monitor_volume(self, volume: float):
        self.monitor_volume = max(0.0, min(1.5, volume))

    def get_status(self) -> Dict[str, Any]:
        start_iso = self.current_chunk_start.isoformat() if self.current_chunk_start else None
        return {
            "is_recording": self.is_recording,
            "is_monitoring": self.is_monitoring,
            "chunk_counter": self.chunk_counter,
            "current_chunk_start": start_iso,
            "current_level": self.current_level,
            "config": self.config,
            "stall_count": self._stall_count,
            "recording_metrics": {
                "bytes_written": self._metrics.bytes_written,
                "io_errors": self._metrics.io_errors,
                "stream_retries": self._metrics.stream_retries,
                "chunks_completed": self._metrics.chunks_completed,
                "chunks_failed": self._metrics.chunks_failed,
                "duration_accuracy": round(self._metrics.duration_accuracy, 3),
            },
        }

    def __del__(self):
        if getattr(self, "audio", None):
            try:
                self.audio.terminate()
            except Exception:
                pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sistema de Censura Digital para Rádio")
    parser.add_argument("--config", default="config_censura.json")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()
    censura = CensuraDigital(args.config)
    if args.list_devices:
        censura.list_audio_devices()
        return
    try:
        if censura.start_recording(enable_monitoring=args.monitor):
            while censura.is_recording:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        censura.stop_recording()


if __name__ == "__main__":
    main()
