"""기록 백업: paper 폴더(잔고·매매·수익 기록)를 날짜별 zip 으로 매일 한 부씩 남긴다.

자동 실행기가 매일 아침 7시(미국장 마감 뒤) 한 번 만든다.
- 기본 위치: 프로젝트의 backups/paper-YYYY-MM-DD.zip (최근 KEEP 개만 남김)
- .env 의 BACKUP_DIR 에 폴더(예: OneDrive 폴더)를 적으면 거기에도 한 부 더 복사한다.
  PC 디스크가 고장 나도 기록이 남게 하려는 것.
되돌리기: py scripts/restore_backup.py (목록) / py scripts/restore_backup.py 2026-10-12
"""
from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

KEEP = 60
SKIP_DIRS = {"logs"}          # logs are large and not needed to restore
PREFIX = "paper-"


def _files(paper: Path) -> list[Path]:
    return sorted(p for p in paper.rglob("*")
                  if p.is_file() and not (set(p.relative_to(paper).parts[:-1]) & SKIP_DIRS))


def make(paper: Path, dest: Path, day: str, keep: int = KEEP) -> Path | None:
    """Zip paper/ into dest/paper-<day>.zip (replacing that day's copy). None if nothing to save."""
    files = _files(paper) if paper.exists() else []
    if not files:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{PREFIX}{day}.zip"
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(paper).as_posix())
    tmp.replace(out)
    for old in listing(dest)[keep:]:
        old.unlink(missing_ok=True)
    return out


def listing(dest: Path) -> list[Path]:
    """Backups in dest, newest first."""
    return sorted(dest.glob(f"{PREFIX}????-??-??.zip"), reverse=True) if dest.exists() else []


def daily(paper: Path, dest: Path, day: str, extra: str | None = None) -> list[Path]:
    """Today's backup in dest, plus a copy in the BACKUP_DIR folder when set."""
    out = make(paper, dest, day)
    if out is None:
        return []
    saved = [out]
    if extra:
        try:
            other = Path(extra).expanduser()
            other.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, other / out.name)
            for old in listing(other)[KEEP:]:
                old.unlink(missing_ok=True)
            saved.append(other / out.name)
        except OSError:
            pass  # the local copy is enough; the caller logs that only one was saved
    return saved


def restore(zip_path: Path, paper: Path, dest: Path, today: str) -> Path | None:
    """Replace paper/ with a backup. The current records are saved first as paper-<today>.zip."""
    data = io.BytesIO(zip_path.read_bytes())  # read first: today's save may overwrite this file
    before = make(paper, dest, today) if paper.exists() else None
    with zipfile.ZipFile(data) as z:
        names = z.namelist()
        if any(n.startswith("/") or ".." in Path(n).parts for n in names):
            raise ValueError("백업 파일 안의 경로가 이상해요")
        for f in _files(paper) if paper.exists() else []:
            f.unlink()
        z.extractall(paper)
    return before
