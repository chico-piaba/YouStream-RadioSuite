#!/usr/bin/env python3
"""
Launcher para a Interface Gráfica do Sistema de Censura Digital
Detecta dependências e fornece instruções para instalação.
Roda a interface em subprocesso com timeout para evitar travamento no Windows.
"""

import sys
import os
import subprocess
import platform
import traceback
import threading

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
STARTUP_TIMEOUT = 25


def check_python_version():
    v = sys.version_info
    if v < (3, 9):
        print(f"  Python {v.major}.{v.minor} detectado - requer 3.9+")
        return False
    print(f"  Python {v.major}.{v.minor}.{v.micro} OK")
    return True


IMPORT_TIMEOUT = 8


def _check_module_subprocess(module_stmt):
    """Roda import em subprocesso com timeout (evita travamento do PyAudio no Windows)."""
    code = f"""
import sys
try:
    {module_stmt}
    sys.exit(0)
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
"""
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=IMPORT_TIMEOUT,
        )
        return r.returncode == 0, (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, "timeout (import travou)"
    except Exception as e:
        return False, str(e)


def check_dependencies():
    results = {}
    errors = {}

    modules = [
        ("tkinter", "import tkinter"),
        ("numpy", "import numpy"),
        ("Pillow", "from PIL import Image"),
        ("pyaudio", "import pyaudio"),
    ]

    for name, stmt in modules:
        ok, err = _check_module_subprocess(stmt)
        results[name] = ok
        if not ok and err:
            errors[name] = err

    return results, errors


def print_install_instructions(missing):
    print()
    if "pyaudio" in missing or "sounddevice" in str(missing).lower():
        if IS_WINDOWS:
            print("  No Windows, use sounddevice (mais estavel que PyAudio):")
            print("    pip install sounddevice")
            print()
            print("  Ou tente PyAudio:")
            print("    pip install pyaudio")
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

    if not os.path.exists("run_interface.py"):
        print("  ERRO: run_interface.py nao encontrado!")
        return

    # Roda interface em subprocesso com timeout (evita travar se PyAudio travar no Windows)
    print("  Iniciando interface grafica...")
    print()
    proc = subprocess.Popen(
        [sys.executable, "run_interface.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=os.getcwd(),
    )
    ready = [False]

    def read_ready():
        try:
            line = proc.stdout.readline()
            if line and "READY" in line:
                ready[0] = True
        except Exception:
            pass

    t = threading.Thread(target=read_ready, daemon=True)
    t.start()

    for _ in range(STARTUP_TIMEOUT):
        t.join(timeout=1)
        if ready[0]:
            proc.wait()
            return
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            if err:
                print(f"  ERRO: {err.strip()}")
            break

    if not ready[0] and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("  A interface travou ao carregar (timeout).")
        print("  No Windows, o PyAudio pode travar. Verifique as instrucoes abaixo.")
        print()

    # Se falhou ou travou, roda diagnostico de dependencias
    print("  Verificando dependencias...")
    deps, errors = check_dependencies()
    missing = [name for name, ok in deps.items() if not ok]

    for name, ok in deps.items():
        status = "OK" if ok else "FALTANDO"
        line = f"  {name:12s} ... {status}"
        if name in errors:
            line += f"  ({errors[name]})"
        print(line)

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
                print("  Instalacao concluida! Execute novamente: python launch_interface.py")
            except subprocess.CalledProcessError:
                print("  Falha na instalacao automatica.")
                print("  Siga as instrucoes acima para instalar manualmente.")
        return
    else:
        print()
        print("  Dependencias OK. O erro pode ser outro.")
        print(f"  Tente executar: {sys.executable} interface_censura_digital.py")


if __name__ == "__main__":
    main()
