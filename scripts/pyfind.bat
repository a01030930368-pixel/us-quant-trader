@echo off
rem Finds a working Python 3 and stores the command in PY ("python" or "py -3").
rem The Microsoft Store "python" shortcut only prints "Python", so each candidate is test-run.
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if defined PY exit /b 0
echo.
echo  [Mintory] 이 PC에서 Python을 찾지 못했습니다.
echo.
echo  1. https://www.python.org/downloads/ 에서 Python을 내려받아 설치하세요.
echo  2. 설치 첫 화면 아래의 "Add python.exe to PATH" 를 꼭 체크하세요.
echo  3. 설치가 끝나면 이 창을 닫고 다시 실행하세요.
echo.
echo  (설정 ^> 앱 ^> 고급 앱 설정 ^> 앱 실행 별칭 에서 python.exe / python3.exe 를 끄면
echo   Microsoft Store 바로가기가 진짜 Python을 가리는 문제도 사라집니다.)
echo.
exit /b 1
