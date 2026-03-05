#!/usr/bin/env python3
"""
Launcher para a Interface Gráfica do Sistema de Censura Digital
(modelo estável: mesmo processo, como no censura-digital funcional)
"""

import sys
import os
import platform

def main():
    print()
    print("  ALECE PLAY - SISTEMA DE CENSURA DIGITAL")
    print("  " + "=" * 42)
    print()
    v = sys.version_info
    if v < (3, 9):
        print(f"  Python {v.major}.{v.minor} detectado - requer 3.9+")
        return
    print(f"  Python {v.major}.{v.minor}.{v.micro}  |  {platform.system()} {platform.release()}")
    print()

    for f in ("gravador_censura_digital.py", "interface_censura_digital.py", "stream_manager.py", "processador_audio.py"):
        if not os.path.exists(f):
            print(f"  ERRO: {f} nao encontrado. Execute no diretorio do projeto.")
            return

    print("  Iniciando interface grafica...")
    print()
    try:
        from interface_censura_digital import main as interface_main
        interface_main()
    except Exception as e:
        print(f"  ERRO: {e}")
        import traceback
        traceback.print_exc()
        input("\n  Pressione Enter para sair.")

if __name__ == "__main__":
    main()
