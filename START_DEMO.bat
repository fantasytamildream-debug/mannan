@echo off
cd /d "%~dp0"
start "" http://localhost:8765
py -3 live_bridge.py --demo
pause
