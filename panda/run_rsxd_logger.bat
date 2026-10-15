@echo off
REM ===========================================================================
REM  RSXD SCAN LOGGER  --  double-click launcher
REM  Starts the app from this folder no matter where the shortcut was clicked.
REM ===========================================================================
cd /d "%~dp0"

REM pythonw = no console window. Fall back to python if pythonw is missing.
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw rsxd_logger.py
    exit /b 0
)

python rsxd_logger.py
if errorlevel 1 (
    echo.
    echo The app exited with an error. The message above says why.
    pause
)
