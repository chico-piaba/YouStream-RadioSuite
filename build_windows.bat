@echo off
REM --- Build do Censura Digital para Windows (executavel standalone) ---
setlocal

cd /d "%~dp0"

set "PYTHON=.\venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo.
    echo ERRO: venv nao encontrado. Crie primeiro:
    echo   python -m venv venv
    echo   venv\Scripts\pip install -r requirements.txt
    echo   venv\Scripts\pip install pyinstaller
    echo.
    exit /b 1
)

echo.
echo ===================================================
echo  Build Censura Digital - Executavel Windows
echo ===================================================
echo.

REM Instala PyInstaller se nao tiver
"%PYTHON%" -m pip install pyinstaller --quiet

REM Build
"%PYTHON%" -m PyInstaller build_windows.spec --noconfirm --clean

if errorlevel 1 (
    echo.
    echo ERRO: Build falhou.
    exit /b 1
)

echo.
echo ===================================================
echo  Build concluido!
echo ===================================================
echo.
echo  Pasta de saida: dist\CensuraDigital\
echo  Executavel:     dist\CensuraDigital\CensuraDigital.exe
echo.
echo  Para distribuir: zipa a pasta inteira dist\CensuraDigital\
echo  O usuario so precisa:
echo    - Extrair o zip
echo    - Ter FFmpeg no PATH (para streaming RTMP/Icecast)
echo    - Executar CensuraDigital.exe
echo.
pause
