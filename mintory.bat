@echo off
rem Mintory: read the latest Kiwoom mock balance (no orders) and open the dashboard.
chcp 65001 >nul
cd /d "%~dp0"
python scripts\run_trader.py --mode kiwoom --check --open
if errorlevel 1 pause
