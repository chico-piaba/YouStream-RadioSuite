#!/usr/bin/env python3
"""
Bootstrap: carrega a interface e sinaliza READY antes do mainloop.
Usado pelo launcher para detectar travamento no import (ex: PyAudio no Windows).
"""
import sys

if __name__ == "__main__":
    try:
        from interface_censura_digital import main
        print("READY", flush=True)
        main()
    except Exception as e:
        print(f"ERRO:{e}", file=sys.stderr, flush=True)
        sys.exit(1)
