#!/usr/bin/env python3
"""
Stream Manager para o Sistema de Censura Digital.
Gerencia streaming de áudio via RTMP e Icecast usando subprocessos FFmpeg.

Cada protocolo recebe dados PCM raw via queue própria, alimentada pelo
método feed_audio(), e os encaminha ao FFmpeg em thread dedicada.
"""
from __future__ import annotations

import logging
import platform
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Callable, Dict, Any

FEED_QUEUE_MAXSIZE = 2000


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

        self._on_status_callback: Optional[Callable[[str, str], None]] = None

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
        self.logger.info(f"[{protocol.upper()}] {message}")
        if self._on_status_callback:
            try:
                self._on_status_callback(protocol, message)
            except Exception:
                pass

    # ── FFmpeg helpers ────────────────────────────────────────────

    def _build_base_input_args(self) -> list[str]:
        return [
            self.ffmpeg_cmd,
            "-hide_banner",
            "-loglevel", "error",
            "-f", self.pcm_format,
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-i", "pipe:0",
        ]

    def _feed_loop(self, protocol: str):
        """Thread dedicada: lê da queue e escreve no stdin do FFmpeg."""
        q = self._rtmp_queue if protocol == "rtmp" else self._icecast_queue
        proc_attr = f"_{protocol}_process"
        proc: Optional[subprocess.Popen] = getattr(self, proc_attr, None)
        if proc is None or q is None:
            return

        while getattr(self, f"_{protocol}_active", False):
            if proc.poll() is not None:
                break
            try:
                data = q.get(timeout=1.0)
                proc.stdin.write(data)
                proc.stdin.flush()
            except queue.Empty:
                continue
            except (BrokenPipeError, OSError):
                break

    def _monitor_process(self, protocol: str):
        """Thread que espera o processo FFmpeg terminar e reporta status."""
        proc: Optional[subprocess.Popen] = getattr(
            self, f"_{protocol}_process", None
        )
        if proc is None:
            return

        proc.wait()
        stderr_output = ""
        try:
            stderr_output = proc.stderr.read().decode(errors="replace").strip()
        except Exception:
            pass

        still_active = getattr(self, f"_{protocol}_active", False)
        if still_active:
            setattr(self, f"_{protocol}_active", False)
            msg = f"FFmpeg encerrou inesperadamente (code {proc.returncode})"
            if stderr_output:
                msg += f": {stderr_output[:300]}"
            self._notify(protocol, msg)

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
            self._notify("rtmp", f"Streaming iniciado → {effective_url}")

            threading.Thread(
                target=self._feed_loop, args=("rtmp",), daemon=True
            ).start()
            threading.Thread(
                target=self._monitor_process, args=("rtmp",), daemon=True
            ).start()
            return True

        except FileNotFoundError:
            self._notify("rtmp", "FFmpeg não encontrado no sistema")
            return False
        except Exception as e:
            self._notify("rtmp", f"Erro ao iniciar: {e}")
            return False

    def stop_rtmp(self):
        self._rtmp_active = False
        proc = self._rtmp_process
        if proc:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._rtmp_process = None
            self._rtmp_queue = None
            self._notify("rtmp", "Streaming parado")

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
            self._notify(
                "icecast",
                f"Streaming iniciado → {effective_host}:{effective_port}/{effective_mount}",
            )

            threading.Thread(
                target=self._feed_loop, args=("icecast",), daemon=True
            ).start()
            threading.Thread(
                target=self._monitor_process, args=("icecast",), daemon=True
            ).start()
            return True

        except FileNotFoundError:
            self._notify("icecast", "FFmpeg não encontrado no sistema")
            return False
        except Exception as e:
            self._notify("icecast", f"Erro ao iniciar: {e}")
            return False

    def stop_icecast(self):
        self._icecast_active = False
        proc = self._icecast_process
        if proc:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._icecast_process = None
            self._icecast_queue = None
            self._notify("icecast", "Streaming parado")

    # ── Common ────────────────────────────────────────────────────

    def feed_audio(self, data: bytes):
        """Distribui dados PCM para as queues de cada protocolo ativo."""
        if self._rtmp_active and self._rtmp_queue is not None:
            try:
                self._rtmp_queue.put_nowait(data)
            except queue.Full:
                pass

        if self._icecast_active and self._icecast_queue is not None:
            try:
                self._icecast_queue.put_nowait(data)
            except queue.Full:
                pass

    def stop_all(self):
        self.stop_rtmp()
        self.stop_icecast()

    def get_status(self) -> Dict[str, Any]:
        return {
            "rtmp_active": self._rtmp_active,
            "icecast_active": self._icecast_active,
            "rtmp_url": self.rtmp_url,
            "icecast_host": self.icecast_host,
            "icecast_port": self.icecast_port,
            "icecast_mount": self.icecast_mount,
        }
