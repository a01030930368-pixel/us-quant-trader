"""기록 백업에서 되돌리기.

    py scripts/restore_backup.py              백업 목록 보기
    py scripts/restore_backup.py 2026-10-12   그날 백업으로 paper 폴더 되돌리기

되돌리기 전에 지금 기록을 오늘 날짜 백업으로 먼저 저장하므로, 잘못 골라도 다시 돌아올 수 있다.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quant import backup

PAPER = ROOT / "paper"
DEST = ROOT / "backups"


def main(argv: list[str]) -> int:
    found = backup.listing(DEST)
    if not argv:
        if not found:
            print("아직 백업이 없어요. Mintory가 매일 아침 7시에 하나씩 만들어요.")
            return 0
        print("백업 목록 (최신순):")
        for p in found:
            print(f"  {p.stem.removeprefix(backup.PREFIX)}   {p.stat().st_size / 1024:,.0f} KB")
        print("\n되돌리려면: py scripts/restore_backup.py 날짜")
        return 0
    zip_path = DEST / f"{backup.PREFIX}{argv[0]}.zip"
    if not zip_path.exists():
        print(f"{argv[0]} 백업이 없어요. 날짜 없이 실행하면 목록이 나와요.")
        return 1
    if input(f"paper 폴더를 {argv[0]} 백업으로 되돌릴까요? Mintory 자동 매매를 일시정지한 뒤 하세요. (y/n) ").strip().lower() != "y":
        print("취소했어요.")
        return 1
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    before = backup.restore(zip_path, PAPER, DEST, today)
    print(f"되돌렸어요. 바꾸기 전 기록은 {before.name if before else '(없음)'} 에 저장했어요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
