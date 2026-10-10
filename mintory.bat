@echo off
rem Mintory: start the background auto-runner (once), then open the Mintory window.
rem Opening refreshes the Kiwoom mock balance (no orders).
chcp 65001 >nul
cd /d "%~dp0"
call scripts\pyfind.bat || (pause & exit /b 1)
%PY% scripts\mintory_agent.py --install-startup >nul
set "PYW="
for /f "delims=" %%i in ('%PY% -c "import sys,pathlib;p=pathlib.Path(sys.executable).with_name('pythonw.exe');print(p if p.exists() else '')"') do set "PYW=%%i"
if defined PYW goto quiet
start "" /min %PY% scripts\mintory_agent.py
goto open
:quiet
start "" "%PYW%" scripts\mintory_agent.py
:open
%PY% scripts\mintory_agent.py --open
if errorlevel 1 pause
