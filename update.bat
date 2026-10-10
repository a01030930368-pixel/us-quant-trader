@echo off
rem Mintory update: download the latest code from GitHub into this folder.
rem .env and paper\ (your trading records) are never touched.
chcp 65001 >nul
cd /d "%~dp0"
call scripts\pyfind.bat || (pause & exit /b 1)
%PY% scripts\update.py & pause & exit /b
