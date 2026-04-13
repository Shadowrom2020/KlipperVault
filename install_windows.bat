@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

if defined PYTHON_BIN (
    set "PYTHON_CMD=%PYTHON_BIN%"
) else (
    where py >nul 2>&1
    if %ERRORLEVEL%==0 (
        set "PYTHON_CMD=py -3"
    ) else (
        set "PYTHON_CMD=python"
    )
)

if not defined VENV_DIR set "VENV_DIR=%USERPROFILE%\klippervault-venv"
if not defined REQUIREMENTS_FILE set "REQUIREMENTS_FILE=%APP_DIR%\requirements.txt"
set "CONFIG_DIR=%APPDATA%\KlipperVault"
set "DB_PATH=%LOCALAPPDATA%\KlipperVault\klipper_macros.db"

echo Installing KlipperVault (remote-only mode) on Windows

echo App dir: %APP_DIR%
echo Python: %PYTHON_CMD%
echo Venv: %VENV_DIR%

if not exist "%REQUIREMENTS_FILE%" (
    echo Requirements file not found: %REQUIREMENTS_FILE%
    exit /b 1
)

mkdir "%CONFIG_DIR%" >nul 2>&1
for %%I in ("%DB_PATH%") do set "DB_DIR=%%~dpI"
mkdir "%DB_DIR%" >nul 2>&1

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment...
    call %PYTHON_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create virtual environment. Ensure Python 3 with venv is installed.
        exit /b 1
    )
)

echo Installing Python dependencies...
call "%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1

call "%VENV_DIR%\Scripts\python.exe" -m pip install -r "%REQUIREMENTS_FILE%"
if errorlevel 1 exit /b 1

echo Settings are initialized automatically in the SQLite database on first start.
echo.
echo Install complete.
echo Config dir: %CONFIG_DIR%
echo Database: %DB_PATH%
echo Start KlipperVault with: "%VENV_DIR%\Scripts\python.exe" "%APP_DIR%\klipper_vault_gui.py"

exit /b 0
