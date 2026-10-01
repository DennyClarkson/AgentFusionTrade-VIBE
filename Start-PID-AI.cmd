@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m fusion.pid_manager
pause
