@echo off
chcp 65001 >nul
title Muse Register
cd /d "%~dp0"

set "PORT=8801"
set "PYTHONIOENCODING=utf-8"

where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=python"
    set "PYTHON_ARGS="
) else (
    where py >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=py"
        set "PYTHON_ARGS=-3"
    ) else (
        echo Python 3 was not found. Please install Python 3.10 or newer.
        pause
        exit /b 1
    )
)

netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo WebUI is already running: http://127.0.0.1:%PORT%
    start "" "http://127.0.0.1:%PORT%"
    exit /b 0
)

echo Starting Muse WebUI on port %PORT%...
echo Close this window to stop the service.
echo.

start "" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:%PORT%'"

%PYTHON% %PYTHON_ARGS% "%~dp0web.py" --host 127.0.0.1 --port %PORT%

echo.
echo WebUI has stopped. Check the error above if startup failed.
pause
