#!/usr/bin/env python3
"""
Stream Manager para o Sistema de Censura Digital.
Gerencia streaming de áudio via RTMP e Icecast usando subprocessos FFmpeg.

Cada protocolo recebe dados PCM raw via queue própria, alimentada pelo
método feed_audio(), e os encaminha ao FFmpeg em thread dedicada.

Inclui métricas de throughput, qualidade, reconexão automática e
leitura de stderr do FFmpeg em tempo real.
"""
from __future__ import annotations

import logging
import platform
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Dict, Any

FEED_QUEUE_MAXSIZE = 2000
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_BASE_DELAY = 2.0
METRICS_LOG_INTERVAL = 30.0


@dataclass
class StreamMetrics:
    """Métricas de throughput e qualidade por protocolo de streaming."""

    bytes_fed: int = 0
    frames_sent: int = 0
    frames_dropped: int = 0
    queue_high_watermark: int = 0
    started_at: float = 0.0
    last_feed_ts: float = 0.0
    reconnect_count: int = 0
    last_error: str = ""
    connected: bool = False
    target_bitrate_kbps: int = 0
    _last_log_bytes: int = field(default=0, repr=False)
    _last_log_ts: float = field(default=0.0, repr=False)

    @property
    def pcm_feed_kbps(self) -> float:
        """Taxa de dados PCM raw enviados ao FFmpeg (entrada, não saída de rede)."""
        elapsed = time.time() - self.started_at
        if elapsed <= 0:
            return 0.0
        return (self.bytes_fed * 8) / elapsed / 1000

    @property
    def instant_feed_kbps(self) -> float:
        """Taxa PCM instantânea (janela curta desde último log)."""
        now = time.time()
        dt = now - self._last_log_ts if self._last_log_ts > 0 else 0
        if dt <= 0:
            return self.pcm_feed_kbps
        delta_bytes = self.bytes_fed - self._last_log_bytes
        return (delta_bytes * 8) / dt / 1000

    @property
    def quality_score(self) -> float:
        """1.0 = perfeito, 0.0 = tudo descartado."""
        total = self.frames_sent + self.frames_dropped
        if total == 0:
            return 1.0
        return self.frames_sent / total

    @property
    def uptime_seconds(self) -> float:
        if self.started_at <= 0:
            return 0.0
        return time.time() - self.started_at

    def snapshot_for_log(self):
        self._last_log_bytes = self.bytes_fed
        self._last_log_ts = time.time()

    def reset(self):
        self.bytes_fed = 0
        self.frames_sent = 0
        self.frames_dropped = 0
        self.queue_high_watermark = 0
        self.started_at = time.time()
        self.last_feed_ts = 0.0
        self.last_error = ""
        self.connected = False
        self._last_log_bytes = 0
        self._last_log_ts = time.time()


class StreamManager:
    """Gerencia streaming RTMP e Icecast via FFmpeg."""

    def __init__(self, config: dict, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()

        self._rtmp_process: Optional[subprocess.Popen] = None
        self._icecast_process: Optional[subprocess.Popen] = None

        self._rtmp_queue: Optional[queue.Queue] = None
        self._icecast_queue: Optional[queue.Queue] = None

        self._rtmp_active = False
        self._icecast_active = False
        self._last_rtmp_status = "Inativo"
        self._last_icecast_status = "Inativo"

        self._on_status_callback: Optional[Callable[[str, str], None]] = None

        self._rtmp_metrics = StreamMetrics()
        self._icecast_metrics = StreamMetrics()

        self._rtmp_last_start_args: Dict[str, Any] = {}
        self._icecast_last_start_args: Dict[str, Any] = {}
        self._rtmp_user_stopped = False
        self._icecast_user_stopped = False

        self._metrics_thread: Optional[threading.Thread] = None
        self._metrics_running = False

        self._load_config(config)

    # ── Configuration ─────────────────────────────────────────────

    def _load_config(self, config: dict):
        streaming_cfg = config.get("streaming", {})
        audio_cfg = config.get("audio", {})

        self.sample_rate = int(audio_cfg.get("rate", 44100))
        self.channels = int(audio_cfg.get("channels", 1))

        fmt = audio_cfg.get("format", "paInt16")
        fmt_map = {
            "paInt16": "s16le",
            "paInt24": "s24le",
            "paInt32": "s32le",
            "paFloat32": "f32le",
        }
        self.pcm_format = fmt_map.get(fmt, "s16le")

        rtmp_cfg = streaming_cfg.get("rtmp", {})
        self.rtmp_url = rtmp_cfg.get("url", "")
        self.rtmp_bitrate = int(rtmp_cfg.get("audio_bitrate_kbps", 128))

        ice_cfg = streaming_cfg.get("icecast", {})
        self.icecast_host = ice_cfg.get("host", "localhost")
        self.icecast_port = int(ice_cfg.get("port", 8000))
        self.icecast_mount = ice_cfg.get("mount", "/live").lstrip("/")
        self.icecast_password = ice_cfg.get("source_password", "")
        self.icecast_bitrate = int(ice_cfg.get("audio_bitrate_kbps", 128))

        processing_cfg = config.get("processing", {})
        ffmpeg_path = processing_cfg.get("ffmpeg_path") or "ffmpeg"
        self.ffmpeg_cmd = self._resolve_ffmpeg(ffmpeg_path)

    def reload_config(self, config: dict):
        """Recarrega configuração (chamado quando o usuário salva novas settings)."""
        self._load_config(config)

    @staticmethod
    def _resolve_ffmpeg(preferred: str) -> str:
        if preferred and Path(preferred).exists():
            return str(Path(preferred))
        found = shutil.which("ffmpeg")
        if found:
            return found
        if platform.system().lower().startswith("win"):
            found = shutil.which("ffmpeg.exe")
            if found:
                return found
        return preferred or "ffmpeg"

    # ── Callbacks ─────────────────────────────────────────────────

    def set_status_callback(self, callback: Optional[Callable[[str, str], None]]):
        """Registra callback(protocol, message) para atualizações de status."""
        self._on_status_callback = callback

    def _notify(self, protocol: str, message: str):
        self.logger.info("[%s] %s", protocol.upper(), message)
        if protocol == "rtmp":
            self._last_rtmp_status = message
        else:
            self._last_icecast_status = message
        if self._on_status_callback:
            try:
                self._on_status_callback(protocol, message)
            except Exception as exc:
                self.logger.debug("Erro no status callback: %s", exc)

    # ── Metrics ───────────────────────────────────────────────────

    def _get_metrics(self, protocol: str) -> StreamMetrics:
        return self._rtmp_metrics if protocol == "rtmp" else self._icecast_metrics

    def _start_metrics_logger(self):
        if self._metrics_running:
            return
        self._metrics_running = True
        self._metrics_thread = threading.Thread(
            target=self._metrics_log_loop, daemon=True, name="MetricsLogger"
        )
        self._metrics_thread.start()

    def _stop_metrics_logger(self):
        self._metrics_running = False

    def _metrics_log_loop(self):
        while self._metrics_running:
            time.sleep(METRICS_LOG_INTERVAL)
            if not self._metrics_running:
                break
            for proto in ("rtmp", "icecast"):
                if not getattr(self, f"_{proto}_active", False):
                    continue
                m = self._get_metrics(proto)
                instant_kbps = m.instant_feed_kbps
                m.snapshot_for_log()
                q = getattr(self, f"_{proto}_queue", None)
                qsize = q.qsize() if q else 0
                qpct = (qsize / FEED_QUEUE_MAXSIZE * 100) if FEED_QUEUE_MAXSIZE > 0 else 0
                conn_str = "CONECTADO" if m.connected else "SEM CONEXÃO"
                self.logger.info(
                    "[%s METRICS] status=%s | target=%d kbps | "
                    "pcm_feed=%.0f kbps | sent=%d | dropped=%d | quality=%.1f%% | "
                    "queue=%d/%d (%.0f%%) | peak=%d | reconexões=%d | uptime=%.0fs",
                    proto.upper(), conn_str, m.target_bitrate_kbps,
                    instant_kbps,
                    m.frames_sent, m.frames_dropped,
                    m.quality_score * 100,
                    qsize, FEED_QUEUE_MAXSIZE, qpct,
                    m.queue_high_watermark,
                    m.reconnect_count, m.uptime_seconds,
                )

    # ── FFmpeg helpers ────────────────────────────────────────────

    def _build_base_input_args(self) -> list[str]:
        return [
            self.ffmpeg_cmd,
            "-hide_banner",
            "-loglevel", "info",
            "-f", self.pcm_format,
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-i", "pipe:0",
        ]

    def _feed_loop(self, protocol: str):
        """Thread dedicada: lê da queue e escreve no stdin do FFmpeg."""
        q = self._rtmp_queue if protocol == "rtmp" else self._icecast_queue
        proc: Optional[subprocess.Popen] = getattr(self, f"_{protocol}_process", None)
        metrics = self._get_metrics(protocol)
        if proc is None or q is None:
            return

        while getattr(self, f"_{protocol}_active", False):
            if proc.poll() is not None:
                break
            try:
                data = q.get(timeout=1.0)
                proc.stdin.write(data)
                proc.stdin.flush()
                metrics.bytes_fed += len(data)
                metrics.frames_sent += 1
            except queue.Empty:
                continue
            except (BrokenPipeError, OSError) as exc:
                metrics.last_error = str(exc)
                self.logger.warning("[%s] Feed loop interrompido: %s", protocol.upper(), exc)
                break

    # Markers that indicate FFmpeg successfully opened the output muxer
    _CONNECTED_MARKERS = ("output #0", "press [q]", "muxing overhead")
    _ERROR_MARKERS = ("error", "failed", "connection refused", "timeout",
                      "server returned", "unauthorized", "i/o error")

    def _stderr_reader(self, protocol: str):
        """Thread que lê stderr do FFmpeg em tempo real, detecta conexão e loga."""
        proc: Optional[subprocess.Popen] = getattr(self, f"_{protocol}_process", None)
        if proc is None or proc.stderr is None:
            return
        metrics = self._get_metrics(protocol)
        try:
            for raw_line in iter(proc.stderr.readline, b""):
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue

                lower = line.lower()

                if not metrics.connected and any(m in lower for m in self._CONNECTED_MARKERS):
                    metrics.connected = True
                    self._notify(protocol, "Conexão estabelecida com o servidor")
                    self.logger.info("[%s FFmpeg] %s", protocol.upper(), line)
                    continue

                is_error = any(m in lower for m in self._ERROR_MARKERS)
                if is_error:
                    self.logger.error("[%s FFmpeg] %s", protocol.upper(), line)
                    metrics.last_error = line[:300]
                    if not metrics.connected:
                        self._notify(protocol, f"Falha na conexão: {line[:120]}")
                elif "warning" in lower or "guessed" in lower:
                    self.logger.warning("[%s FFmpeg] %s", protocol.upper(), line)
                else:
                    self.logger.debug("[%s FFmpeg] %s", protocol.upper(), line)
        except (OSError, ValueError):
            pass

    def _monitor_process(self, protocol: str):
        """Thread que espera o processo FFmpeg terminar e tenta reconexão."""
        proc: Optional[subprocess.Popen] = getattr(
            self, f"_{protocol}_process", None
        )
        if proc is None:
            return

        proc.wait()
        metrics = self._get_metrics(protocol)

        still_active = getattr(self, f"_{protocol}_active", False)
        user_stopped = getattr(self, f"_{protocol}_user_stopped", False)

        if still_active and not user_stopped:
            msg = f"FFmpeg encerrou inesperadamente (code {proc.returncode})"
            if metrics.last_error:
                msg += f": {metrics.last_error[:200]}"
            self._notify(protocol, msg)

            if metrics.reconnect_count < MAX_RECONNECT_ATTEMPTS:
                self._attempt_reconnect(protocol)
            else:
                setattr(self, f"_{protocol}_active", False)
                self._notify(
                    protocol,
                    f"Máximo de reconexões atingido ({MAX_RECONNECT_ATTEMPTS}). Streaming parado.",
                )

    def _attempt_reconnect(self, protocol: str):
        """Tenta reiniciar o streaming com backoff exponencial."""
        metrics = self._get_metrics(protocol)
        metrics.reconnect_count += 1
        delay = RECONNECT_BASE_DELAY * (2 ** (metrics.reconnect_count - 1))
        delay = min(delay, 60.0)

        self._notify(
            protocol,
            f"Reconexão #{metrics.reconnect_count} em {delay:.0f}s...",
        )
        time.sleep(delay)

        if getattr(self, f"_{protocol}_user_stopped", False):
            return

        saved_args = getattr(self, f"_{protocol}_last_start_args", {})
        setattr(self, f"_{protocol}_active", False)

        if protocol == "rtmp":
            self.start_rtmp(**saved_args)
        else:
            self.start_icecast(**saved_args)

    # ── RTMP ──────────────────────────────────────────────────────

    def start_rtmp(self, url: str = "", bitrate: int = 0) -> bool:
        if self._rtmp_active:
            self.logger.warning("RTMP streaming já está ativo")
            return False

        effective_url = url or self.rtmp_url
        effective_bitrate = bitrate or self.rtmp_bitrate

        if not effective_url:
            self._notify("rtmp", "URL RTMP não configurada")
            return False

        self._rtmp_last_start_args = {"url": effective_url, "bitrate": effective_bitrate}
        self._rtmp_user_stopped = False

        cmd = self._build_base_input_args() + [
            "-c:a", "aac",
            "-b:a", f"{effective_bitrate}k",
            "-f", "flv",
            effective_url,
        ]

        try:
            self._rtmp_queue = queue.Queue(maxsize=FEED_QUEUE_MAXSIZE)
            self._rtmp_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._rtmp_active = True
            if self._rtmp_metrics.reconnect_count == 0:
                self._rtmp_metrics.reset()
            self._rtmp_metrics.target_bitrate_kbps = effective_bitrate
            self._notify("rtmp", f"Conectando → {effective_url} ({effective_bitrate} kbps)")

            threading.Thread(
                target=self._feed_loop, args=("rtmp",), daemon=True,
                name="RTMP-Feed",
            ).start()
            threading.Thread(
                target=self._stderr_reader, args=("rtmp",), daemon=True,
                name="RTMP-Stderr",
            ).start()
            threading.Thread(
                target=self._monitor_process, args=("rtmp",), daemon=True,
                name="RTMP-Monitor",
            ).start()
            self._start_metrics_logger()
            return True

        except FileNotFoundError:
            self._notify("rtmp", "FFmpeg não encontrado no sistema")
            return False
        except Exception as e:
            self._notify("rtmp", f"Erro ao iniciar: {e}")
            return False

    def stop_rtmp(self):
        self._rtmp_user_stopped = True
        self._rtmp_active = False
        proc = self._rtmp_process
        if proc:
            try:
                proc.stdin.close()
            except Exception as exc:
                self.logger.debug("Erro ao fechar stdin RTMP: %s", exc)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception as exc:
                    self.logger.debug("Erro ao matar processo RTMP: %s", exc)
            self._rtmp_process = None
            self._rtmp_queue = None
            self._notify("rtmp", "Streaming parado")

        if not self._icecast_active:
            self._stop_metrics_logger()

    # ── Icecast ───────────────────────────────────────────────────

    def start_icecast(
        self,
        host: str = "",
        port: int = 0,
        mount: str = "",
        password: str = "",
        bitrate: int = 0,
    ) -> bool:
        if self._icecast_active:
            self.logger.warning("Icecast streaming já está ativo")
            return False

        effective_host = host or self.icecast_host
        effective_port = port or self.icecast_port
        effective_mount = (mount or self.icecast_mount).lstrip("/")
        effective_password = password or self.icecast_password
        effective_bitrate = bitrate or self.icecast_bitrate

        if not effective_host:
            self._notify("icecast", "Host Icecast não configurado")
            return False

        self._icecast_last_start_args = {
            "host": effective_host, "port": effective_port,
            "mount": effective_mount, "password": effective_password,
            "bitrate": effective_bitrate,
        }
        self._icecast_user_stopped = False

        icecast_url = (
            f"icecast://source:{effective_password}"
            f"@{effective_host}:{effective_port}"
            f"/{effective_mount}"
        )

        cmd = self._build_base_input_args() + [
            "-c:a", "libmp3lame",
            "-b:a", f"{effective_bitrate}k",
            "-content_type", "audio/mpeg",
            "-f", "mp3",
            icecast_url,
        ]

        try:
            self._icecast_queue = queue.Queue(maxsize=FEED_QUEUE_MAXSIZE)
            self._icecast_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._icecast_active = True
            if self._icecast_metrics.reconnect_count == 0:
                self._icecast_metrics.reset()
            self._icecast_metrics.target_bitrate_kbps = effective_bitrate
            self._notify(
                "icecast",
                f"Conectando → {effective_host}:{effective_port}/{effective_mount} ({effective_bitrate} kbps)",
            )

            threading.Thread(
                target=self._feed_loop, args=("icecast",), daemon=True,
                name="Icecast-Feed",
            ).start()
            threading.Thread(
                target=self._stderr_reader, args=("icecast",), daemon=True,
                name="Icecast-Stderr",
            ).start()
            threading.Thread(
                target=self._monitor_process, args=("icecast",), daemon=True,
                name="Icecast-Monitor",
            ).start()
            self._start_metrics_logger()
            return True

        except FileNotFoundError:
            self._notify("icecast", "FFmpeg não encontrado no sistema")
            return False
        except Exception as e:
            self._notify("icecast", f"Erro ao iniciar: {e}")
            return False

    def stop_icecast(self):
        self._icecast_user_stopped = True
        self._icecast_active = False
        proc = self._icecast_process
        if proc:
            try:
                proc.stdin.close()
            except Exception as exc:
                self.logger.debug("Erro ao fechar stdin Icecast: %s", exc)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception as exc:
                    self.logger.debug("Erro ao matar processo Icecast: %s", exc)
            self._icecast_process = None
            self._icecast_queue = None
            self._notify("icecast", "Streaming parado")

        if not self._rtmp_active:
            self._stop_metrics_logger()

    # ── Common ────────────────────────────────────────────────────

    def feed_audio(self, data: bytes):
        """Distribui dados PCM para as queues de cada protocolo ativo."""
        now = time.time()
        data_len = len(data)

        if self._rtmp_active and self._rtmp_queue is not None:
            try:
                self._rtmp_queue.put_nowait(data)
                self._rtmp_metrics.last_feed_ts = now
                qsize = self._rtmp_queue.qsize()
                if qsize > self._rtmp_metrics.queue_high_watermark:
                    self._rtmp_metrics.queue_high_watermark = qsize
            except queue.Full:
                self._rtmp_metrics.frames_dropped += 1
                if self._rtmp_metrics.frames_dropped % 100 == 1:
                    self.logger.warning(
                        "[RTMP] Queue cheia — frame descartado (total: %d)",
                        self._rtmp_metrics.frames_dropped,
                    )

        if self._icecast_active and self._icecast_queue is not None:
            try:
                self._icecast_queue.put_nowait(data)
                self._icecast_metrics.last_feed_ts = now
                qsize = self._icecast_queue.qsize()
                if qsize > self._icecast_metrics.queue_high_watermark:
                    self._icecast_metrics.queue_high_watermark = qsize
            except queue.Full:
                self._icecast_metrics.frames_dropped += 1
                if self._icecast_metrics.frames_dropped % 100 == 1:
                    self.logger.warning(
                        "[ICECAST] Queue cheia — frame descartado (total: %d)",
                        self._icecast_metrics.frames_dropped,
                    )

    def stop_all(self):
        self.stop_rtmp()
        self.stop_icecast()
        self._stop_metrics_logger()

    def _build_metrics_dict(self, m: StreamMetrics, qsize: int) -> Dict[str, Any]:
        return {
            "connected": m.connected,
            "target_bitrate_kbps": m.target_bitrate_kbps,
            "pcm_feed_kbps": round(m.pcm_feed_kbps, 1),
            "quality_score": round(m.quality_score, 4),
            "frames_sent": m.frames_sent,
            "frames_dropped": m.frames_dropped,
            "bytes_fed": m.bytes_fed,
            "queue_size": qsize,
            "queue_max": FEED_QUEUE_MAXSIZE,
            "queue_peak": m.queue_high_watermark,
            "reconnect_count": m.reconnect_count,
            "uptime_seconds": round(m.uptime_seconds, 0),
            "last_error": m.last_error,
        }

    def get_status(self) -> Dict[str, Any]:
        rtmp_q = self._rtmp_queue
        ice_q = self._icecast_queue
        rtmp_qsize = rtmp_q.qsize() if rtmp_q else 0
        ice_qsize = ice_q.qsize() if ice_q else 0

        return {
            "rtmp_active": self._rtmp_active,
            "icecast_active": self._icecast_active,
            "rtmp_status": self._last_rtmp_status,
            "icecast_status": self._last_icecast_status,
            "rtmp_url": self.rtmp_url,
            "icecast_host": self.icecast_host,
            "icecast_port": self.icecast_port,
            "icecast_mount": self.icecast_mount,
            "rtmp_metrics": self._build_metrics_dict(self._rtmp_metrics, rtmp_qsize),
            "icecast_metrics": self._build_metrics_dict(self._icecast_metrics, ice_qsize),
        }
