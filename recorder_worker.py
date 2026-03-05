#!/usr/bin/env python3
"""
Worker de gravação em processo separado.
Quando a gravação roda aqui, um crash de áudio/driver não fecha a interface.
A interface inicia este processo e sinaliza parada com censura_stop.flag.

Propaga métricas de streaming e gravação via censura_status.json.
"""
import argparse
import json
import logging
import os
import sys
import time
import tempfile

STATUS_FILE = "censura_status.json"
STOP_FILE = "censura_stop.flag"
RTMP_CMD_FILE = "stream_rtmp_cmd.json"
ICECAST_CMD_FILE = "stream_icecast_cmd.json"

logger = logging.getLogger("recorder_worker")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config_censura.json", help="Arquivo de configuração")
    parser.add_argument("--monitor", action="store_true", help="Ativar monitoramento de áudio")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"ERRO: {args.config} não encontrado.", file=sys.stderr)
        sys.exit(2)

    for f in (STOP_FILE, RTMP_CMD_FILE, ICECAST_CMD_FILE):
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    try:
        from gravador_censura_digital import CensuraDigital
        from stream_manager import StreamManager
    except Exception as e:
        print(f"ERRO ao importar: {e}", file=sys.stderr)
        sys.exit(3)

    censura = CensuraDigital(args.config)
    stream_manager = StreamManager(censura.config, logger=censura.logger)
    censura.set_stream_manager(stream_manager)

    if not censura.start_recording(enable_monitoring=args.monitor):
        _write_status({"is_recording": False, "error": "Falha ao iniciar gravação"})
        sys.exit(4)

    try:
        while True:
            _process_stream_commands(stream_manager)
            s = censura.get_status()
            st = stream_manager.get_status()
            status = {
                "is_recording": s.get("is_recording"),
                "chunk_counter": s.get("chunk_counter"),
                "current_chunk_start": s.get("current_chunk_start"),
                "current_level": s.get("current_level", 0.0),
                "stall_count": s.get("stall_count", 0),
                "rtmp_active": st.get("rtmp_active", False),
                "icecast_active": st.get("icecast_active", False),
                "rtmp_status": st.get("rtmp_status", "Inativo"),
                "icecast_status": st.get("icecast_status", "Inativo"),
                "rtmp_metrics": st.get("rtmp_metrics", {}),
                "icecast_metrics": st.get("icecast_metrics", {}),
                "recording_metrics": s.get("recording_metrics", {}),
                "worker_pid": os.getpid(),
            }
            _write_status(status)

            if not s.get("is_recording", True):
                logger.warning("Gravação parou internamente (possível falha recuperável)")
                break

            if os.path.exists(STOP_FILE):
                break
            time.sleep(1.0)
    finally:
        stream_manager.stop_all()
        censura.stop_recording()
        _write_status({"is_recording": False, "worker_pid": os.getpid()})
    sys.exit(0)


def _process_stream_commands(stream_manager):
    """Lê arquivos de comando RTMP/Icecast e executa no worker."""
    for path, protocol in ((RTMP_CMD_FILE, "rtmp"), (ICECAST_CMD_FILE, "icecast")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cmd = json.load(f)
        except Exception as exc:
            logger.debug("Falha ao ler comando %s: %s", path, exc)
            cmd = {}
        try:
            os.remove(path)
        except Exception as exc:
            logger.debug("Falha ao remover %s: %s", path, exc)
        action = cmd.get("action")
        if action == "start" and protocol == "rtmp":
            stream_manager.start_rtmp(
                url=cmd.get("url", "").strip(),
                bitrate=int(cmd.get("bitrate", 128)),
            )
        elif action == "stop" and protocol == "rtmp":
            stream_manager.stop_rtmp()
        elif action == "start" and protocol == "icecast":
            stream_manager.start_icecast(
                host=cmd.get("host", "").strip(),
                port=int(cmd.get("port", 8000)),
                mount=cmd.get("mount", "/live").strip().lstrip("/"),
                password=cmd.get("password", ""),
                bitrate=int(cmd.get("bitrate", 128)),
            )
        elif action == "stop" and protocol == "icecast":
            stream_manager.stop_icecast()


def _write_status(data: dict):
    """Escrita atômica via tempfile + rename para evitar race conditions."""
    try:
        dir_name = os.path.dirname(os.path.abspath(STATUS_FILE)) or "."
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix="status_", dir=dir_name,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=0)
            os.replace(tmp_path, STATUS_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as exc:
        logger.debug("Falha ao escrever status: %s", exc)


if __name__ == "__main__":
    main()
