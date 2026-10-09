"""바탕화면에 Mintory 아이콘(바로가기)을 만든다. 윈도우 전용, 한 번만 실행하면 된다.

    python dashboard/make_shortcut.py

아이콘을 더블클릭하면 mintory.bat(없으면 dashboard.bat)이 실행되어 대시보드가 앱 창으로 열린다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ICON = HERE / "mintory.ico"


def launcher() -> Path:
    for name in ("mintory.bat", "dashboard.bat"):
        if (ROOT / name).exists():
            return ROOT / name
    raise FileNotFoundError("mintory.bat 이나 dashboard.bat 을 찾지 못했습니다.")


def ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> int:
    if os.name != "nt":
        print("바탕화면 아이콘은 윈도우에서만 만들 수 있어요.")
        return 1
    target = launcher()
    # OneDrive로 옮겨진 바탕화면도 찾도록 윈도우에 직접 경로를 묻는다.
    script = (
        "$d = [Environment]::GetFolderPath('Desktop');"
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Mintory.lnk'));"
        f"$s.TargetPath = {ps_quote(str(target))};"
        f"$s.WorkingDirectory = {ps_quote(str(ROOT))};"
        f"$s.IconLocation = {ps_quote(str(ICON))};"
        "$s.WindowStyle = 7;"  # 검은 실행 창은 최소화된 채로
        "$s.Description = 'Mintory 모의투자 대시보드';"
        "$s.Save();"
        "Write-Output (Join-Path $d 'Mintory.lnk')"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("[오류] 바로가기를 만들지 못했습니다:", r.stderr.strip(), file=sys.stderr)
        return 1
    print(f"바탕화면에 Mintory 아이콘을 만들었어요: {r.stdout.strip()}")
    print("아이콘을 우클릭해서 '작업 표시줄에 고정'을 누르면 작업 표시줄에서도 열 수 있어요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
