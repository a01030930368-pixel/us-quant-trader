@echo off
rem Double-click once to put a Mintory icon on your desktop.
chcp 65001 >nul
cd /d "%~dp0"
python dashboard\make_shortcut.py
pause
