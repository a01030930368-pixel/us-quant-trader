@echo off
rem Mintory: start the background auto-runner (once), then open the Mintory window.
rem Opening refreshes the Kiwoom mock balance (no orders).
chcp 65001 >nul
cd /d "%~dp0"
python scripts\mintory_agent.py --install-startup >nul
where pythonw >nul 2>nul && (start "" pythonw scripts\mintory_agent.py) || (start "" /min python scripts\mintory_agent.py)
python scripts\mintory_agent.py --open
if errorlevel 1 pause
