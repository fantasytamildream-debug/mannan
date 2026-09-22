@echo off
cd /d "%~dp0"
if not exist config.env copy config.env.example config.env >nul && echo Fill in config.env, then run this again. && notepad config.env && exit /b
start "" http://localhost:8765
py -3 server.py
pause
