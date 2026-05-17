@echo off
REM ============================================================
REM  NSIGII Service Installer / Uninstaller
REM  OBINexus Constitutional Computing Framework
REM
REM  Run this script as Administrator.
REM  Usage:
REM    install_service.bat install    (default)
REM    install_service.bat uninstall
REM    install_service.bat debug
REM ============================================================

SET SCRIPT_DIR=%~dp0
SET SERVICE_PY=%SCRIPT_DIR%nsigii_service.py
SET PYTHON=python

IF "%1"=="uninstall" GOTO UNINSTALL
IF "%1"=="debug"     GOTO DEBUG

:INSTALL
echo.
echo [NSIGII] Checking for pywin32...
%PYTHON% -c "import win32serviceutil" 2>nul
IF %ERRORLEVEL% NEQ 0 (
    echo [NSIGII] pywin32 not found. Installing...
    %PYTHON% -m pip install pywin32 --quiet
    IF %ERRORLEVEL% NEQ 0 (
        echo [NSIGII] ERROR: Failed to install pywin32. Check your Python/pip setup.
        pause
        exit /b 1
    )
    REM Run post-install to register pywin32 with Windows
    %PYTHON% -m pywin32_postinstall -install 2>nul
)

echo [NSIGII] Installing NSIGII Windows Service...
%PYTHON% "%SERVICE_PY%" install
IF %ERRORLEVEL% NEQ 0 (
    echo [NSIGII] ERROR: Service install failed. Make sure you are running as Administrator.
    pause
    exit /b 1
)

echo [NSIGII] Starting NSIGII Service...
%PYTHON% "%SERVICE_PY%" start
IF %ERRORLEVEL% NEQ 0 (
    echo [NSIGII] WARNING: Service installed but could not start automatically.
    echo          Start it manually: sc start NSIGIIService
)

echo.
echo [NSIGII] Done.
echo   Service name : NSIGIIService
echo   Config file  : %SCRIPT_DIR%nsigii_config.json
echo   Log file     : C:\ProgramData\NSIGII\nsigii_service.log
echo   Manage via   : services.msc  or  sc.exe
echo.
pause
exit /b 0

:UNINSTALL
echo [NSIGII] Stopping and removing NSIGII Service...
%PYTHON% "%SERVICE_PY%" stop   2>nul
%PYTHON% "%SERVICE_PY%" remove
echo [NSIGII] Uninstalled.
pause
exit /b 0

:DEBUG
echo [NSIGII] Running in debug mode (no SCM). Press Ctrl-C to stop.
%PYTHON% "%SERVICE_PY%" debug
exit /b 0
