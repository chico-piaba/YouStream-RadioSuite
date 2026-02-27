"""
Runtime hook PyInstaller: muda o CWD para a pasta do executável.
Assim config_censura.json e gravacoes_radio ficam ao lado do .exe.
"""
import os
import sys

if getattr(sys, "frozen", False):
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    try:
        os.chdir(exe_dir)
    except OSError:
        pass
