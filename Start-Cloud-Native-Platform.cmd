@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launch_platform.py
) else (
  python launch_platform.py
)
if errorlevel 1 pause
endlocal
