# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec para Censura Digital - Windows
Gera um executável que não requer Python instalado.
"""
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# Dados adicionais: config exemplo, logo (se existir)
datas = [
    ('config_censura_exemplo.json', '.'),
]
# Logo opcional
import os
if os.path.exists('aleceplay.png'):
    datas.append(('aleceplay.png', '.'))

# sounddevice precisa do PortAudio (DLL) - collect_all inclui tudo
sounddevice_datas, sounddevice_binaries, sounddevice_hidden = collect_all('sounddevice')

# PyAudio (opcional no Windows - sounddevice tem prioridade)
try:
    pyaudio_datas, pyaudio_binaries, pyaudio_hidden = collect_all('pyaudio')
except Exception:
    pyaudio_datas, pyaudio_binaries, pyaudio_hidden = [], [], ['pyaudio']

a = Analysis(
    ['interface_censura_digital.py'],
    pathex=[],
    binaries=sounddevice_binaries + pyaudio_binaries,
    datas=datas + sounddevice_datas + pyaudio_datas,
    hiddenimports=[
        'pyaudio',
        'sounddevice',
        'numpy',
        'numpy.core._methods',
        'numpy.lib.format',
        'PIL',
        'PIL._tkinter_finder',
        'tkcalendar',
        'tkcalendar.calendar_',
        'tkcalendar.dateentry',
        'audio_backend',
        'gravador_censura_digital',
        'stream_manager',
        'processador_audio',
    ] + sounddevice_hidden + pyaudio_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['runtime_hook_cwd.py'],
    excludes=[
        'matplotlib', 'scipy', 'pandas', 'tkinter.test',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir: binários vão para a pasta
    name='CensuraDigital',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI: sem janela de console
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CensuraDigital',
)
