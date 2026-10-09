@echo off
rem Double-click to open the paper-trading dashboard in your browser.
chcp 65001 >nul
cd /d "%~dp0"
python dashboard\build.py %*
if errorlevel 1 pause
