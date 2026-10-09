@echo off
rem Run by Windows Task Scheduler every evening: trades on rebalance days,
rem otherwise only records the day, then refreshes the Mintory dashboard.
chcp 65001 >nul
cd /d "%~dp0"
if not exist paper\logs mkdir paper\logs
python scripts\run_trader.py --mode kiwoom >> paper\logs\run.log 2>&1
