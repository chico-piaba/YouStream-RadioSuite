@echo off
REM --- SCRIPT PARA INICIAR A INTERFACE DO CENSURA DIGITAL (Windows) ---
setlocal ENABLEDELAYEDEXPANSION

REM Muda para o diretório onde este .bat está localizado
cd /d "%~dp0"

REM Caminho do executável do Python dentro do venv
set "PYTHON_EXE=.\venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo.
    echo ERRO: O executavel do Python nao foi encontrado em ".\venv\Scripts\"!
    echo.
    echo Crie o ambiente virtual e instale dependencias:
    echo   python -m venv venv
    echo   .\venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo  Verificando/instalando dependencias do projeto...
echo  Usando Python: %PYTHON_EXE%
echo ===================================================
echo.

REM Garante que o pip esteja acessivel no venv
call "%PYTHON_EXE%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo ERRO: pip nao encontrado no ambiente virtual.
    echo Tente reinstalar: python -m venv venv
    echo.
    pause
    exit /b 1
)

REM Atualiza pip silenciosamente (opcional)
call "%PYTHON_EXE%" -m pip install --upgrade pip >nul 2>&1

REM Instala requirements
echo Instalando requirements (pode demorar na primeira vez)...
call "%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERRO: Falha ao instalar dependencias a partir de requirements.txt
    echo Verifique sua conexao com a Internet e tente novamente.
    echo.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo  Iniciando a aplicacao Censura Digital (Modo Seguro)...
echo ===================================================
echo.

REM Executa a interface grafica segura (sem PyAudio) - recomendada para Windows
call "%PYTHON_EXE%" interface_censura_digital_safe.py

echo.
echo ===================================================
echo  A aplicacao foi fechada.
echo ===================================================
echo.
pause


