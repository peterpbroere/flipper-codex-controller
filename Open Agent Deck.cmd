@echo off
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Install Agent Deck first. See README.md in this folder.
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start-Companion.ps1"
