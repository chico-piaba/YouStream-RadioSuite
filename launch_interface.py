#!/usr/bin/env python3
"""
Launcher para a Interface Gráfica do Sistema de Censura Digital
Detecta dependências e fornece instruções para instalação.
"""

import sys
import os
import subprocess
import platform

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def check_python_version():
    v = sys.version_info
    if v < (3, 9):
        print(f"  Python {v.major}.{v.minor} detectado - requer 3.9+")
        return False
    print(f"  Python {v.major}.{v.minor}.{v.micro} OK")
    return True


def check_dependencies():
    results = {}

    # Tkinter
    try:
        import tkinter
        results["tkinter"] = True
    except ImportError:
        results["tkinter"] = False

    # PyAudio
    try:
        import pyaudio
        results["pyaudio"] = True
    except ImportError:
        results["pyaudio"] = False

    # NumPy
    try:
        import numpy
        results["numpy"] = True
    except ImportError:
        results["numpy"] = False

    # Pillow
    try:
        from PIL import Image
        results["Pillow"] = True
    except ImportError:
        results["Pillow"] = False

    return results


def print_install_instructions(missing):
    print()
    if "pyaudio" in missing:
        if IS_WINDOWS:
            print("  PyAudio no Windows:")
            print("    pip install pyaudio")
            print()
            print("  Se falhar, tente:")
            print("    pip install pipwin")
            print("    pipwin install pyaudio")
            print()
            print("  Ou baixe o .whl de:")
            print("    https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio")
        elif IS_MAC:
            print("  PyAudio no macOS:")
            print("    brew install portaudio")
            print("    pip install pyaudio")
        else:
            print("  PyAudio no Linux:")
            print("    sudo apt install portaudio19-dev python3-pyaudio")
            print("    pip install pyaudio")
        print()

    if "tkinter" in missing:
        if IS_WINDOWS:
            print("  Tkinter: reinstale Python do python.org marcando 'tcl/tk'")
        elif IS_MAC:
            print("  Tkinter no macOS:")
            print("    brew install python-tk@3.12")
        else:
            print("  Tkinter no Linux:")
            print("    sudo apt install python3-tk")
        print()

    other = [m for m in missing if m not in ("pyaudio", "tkinter")]
    if other:
        print(f"  Instalar via pip: pip install {' '.join(other)}")
        print()

    print("  Ou instale tudo de uma vez:")
    print("    pip install -r requirements.txt")


def main():
    print()
    print("  ALECE PLAY - SISTEMA DE CENSURA DIGITAL")
    print("  " + "=" * 42)
    print()

    if not check_python_version():
        return

    print(f"  Sistema: {platform.system()} {platform.release()}")
    print()

    required_files = [
        "gravador_censura_digital.py",
        "interface_censura_digital.py",
        "stream_manager.py",
        "processador_audio.py",
    ]
    for f in required_files:
        if not os.path.exists(f):
            print(f"  ERRO: {f} nao encontrado!")
            print("  Certifique-se de estar no diretorio do projeto.")
            return

    deps = check_dependencies()
    missing = [name for name, ok in deps.items() if not ok]

    for name, ok in deps.items():
        status = "OK" if ok else "FALTANDO"
        print(f"  {name:12s} ... {status}")

    if missing:
        print()
        print("  Dependencias faltantes detectadas!")
        print_install_instructions(missing)

        answer = input("\n  Tentar instalar automaticamente? (s/n): ").strip().lower()
        if answer in ("s", "sim", "y", "yes"):
            print("\n  Instalando...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                    check=True,
                )
                print("  Instalacao concluida!")
            except subprocess.CalledProcessError:
                print("  Falha na instalacao automatica.")
                print("  Siga as instrucoes acima para instalar manualmente.")
                return
        else:
            return

    print()
    print("  Iniciando interface grafica...")
    print()

    try:
        from interface_censura_digital import main as interface_main
        interface_main()
    except Exception as e:
        print(f"  ERRO ao iniciar: {e}")
        print()
        print("  Se o erro persistir, execute diretamente:")
        print(f"    {sys.executable} interface_censura_digital.py")


if __name__ == "__main__":
    main()
