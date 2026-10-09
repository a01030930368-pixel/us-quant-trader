@echo off
rem Double-click once: registers run_daily.bat to run every day at 23:45.
chcp 65001 >nul
schtasks /Create /F /TN "Mintory daily" /SC DAILY /ST 23:45 /TR "\"%~dp0run_daily.bat\""
if errorlevel 1 (
  echo 등록에 실패했습니다. 이 창을 캡처해서 Claude에게 보내 주세요.
) else (
  echo 등록 완료: 매일 밤 11시 45분에 run_daily.bat 이 실행됩니다.
)
pause
