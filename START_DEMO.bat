@echo off
cd /d "%~dp0"
set IGNORE_MARKET_HOURS=1
set BAR_SECONDS=20
start "" http://localhost:8765
py -3 server.py --demo --no-eod
pause
