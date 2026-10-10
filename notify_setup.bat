@echo off
rem Mintory: connect Telegram notifications (once). Saves the bot to .env.
chcp 65001 >nul
cd /d "%~dp0"
python scripts\notify_setup.py & pause & exit /b
