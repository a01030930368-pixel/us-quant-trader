@echo off
rem Double-click once to put a Mintory icon on your desktop.
chcp 65001 >nul
cd /d "%~dp0"
call scripts\pyfind.bat || (pause & exit /b 1)
%PY% dashboard\make_shortcut.py
pause
