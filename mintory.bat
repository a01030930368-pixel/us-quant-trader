@echo off
rem Mintory: start the background auto-runner (once), refresh the Kiwoom mock
rem balance (no orders) and open the dashboard window.
chcp 65001 >nul
cd /d "%~dp0"
python scripts\mintory_agent.py --install-startup >nul
where pythonw >nul 2>nul && (start "" pythonw scripts\mintory_agent.py) || (start "" /min python scripts\mintory_agent.py)
python scripts\run_trader.py --mode kiwoom --check --open
if errorlevel 1 pause
