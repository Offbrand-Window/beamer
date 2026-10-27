@echo off
setlocal
cd /d "%~dp0"
if not exist win_app\.venv\Scripts\python.exe py -3.13 -m venv win_app\.venv
if errorlevel 1 goto failed
win_app\.venv\Scripts\python.exe -m pip install -r win_app\requirements-win.txt
if errorlevel 1 goto failed
win_app\.venv\Scripts\python.exe win_app\kvm_bridge_win.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup failed. Install Python 3.13 for Windows, then run this file again.
pause
exit /b 1
