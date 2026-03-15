@echo off
cd /d "%~dp0"
start "" /b .venv\Scripts\pythonw.exe server_manager.py
