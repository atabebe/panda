@echo off
REM ===========================================================================
REM  PANDA  --  double-click launcher
REM  Scan Log (phase 1) + the four analysis stages, one window.
REM ===========================================================================
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw panda_app.py
    exit /b 0
)

python panda_app.py
if errorlevel 1 (
    echo.
    echo PANDA exited with an error. The message above says why.
    pause
)
