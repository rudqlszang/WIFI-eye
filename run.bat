@echo off
REM ---------------------------------------------------------------------------
REM WIFI-eye - serve the phone survey page from this computer.
REM
REM No install step. Everything here is Python standard library, so there is no
REM requirements.txt, no virtualenv, and nothing to pip install.
REM
REM   run.bat                start on port 5010
REM   run.bat --port 5011    if something already holds 5010
REM   run.bat --no-adb       skip the optional RSSI probe entirely
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [WIFI-eye] Python not found on PATH.
    echo [WIFI-eye] Install Python 3.10+ from python.org, then run this again.
    pause
    exit /b 1
)

python survey.py %*
if errorlevel 1 pause
