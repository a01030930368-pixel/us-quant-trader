"""Mintory 업데이트: GitHub의 최신 코드를 받아 이 폴더에 덮어쓴다.

    python scripts/update.py          # update.bat이 실행
    python scripts/update.py --check  # 새 버전이 있는지만 확인

- .env, paper/(매매 기록), backups/, reports/, data_cache/는 절대 건드리지 않는다.
- 저장소가 비공개라 .env 의 GITHUB_TOKEN(읽기 전용 토큰)으로 받는다.
- 지난번 업데이트 때 설치했는데 새 버전에서 사라진 파일은 지운다.
- requirements.txt가 바뀌면 pip install을 다시 한다.
- 자동 실행기가 매매 중이면 업데이트하지 않는다. 매매 중이 아니면 자동 실행기가
  바뀐 코드를 알아채고 스스로 다시 켜진다.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import webbrowser
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.mintory_agent import agent_alive, read_state  # noqa: E402

REPO = "a01030930368-pixel/us-quant-trader"
BRANCH = "main"
API = "https://api.github.com"
MANIFEST = ROOT / ".mintory-update.json"
KEEP = {".env", "paper", "backups", "reports", "data_cache", ".venv", ".git", MANIFEST.name}
TOKEN_PAGE = "https://github.com/settings/personal-access-tokens/new"

TOKEN_HELP = f"""
업데이트하려면 GitHub 읽기 전용 토큰이 한 번 필요합니다. (저장소가 비공개라서요)

  1. 방금 열린 GitHub 페이지에서 로그인합니다.
     (안 열렸으면 주소창에 {TOKEN_PAGE} 입력)
  2. Token name: mintory-update
     Expiration: 가장 긴 기간 선택
  3. Repository access: "Only select repositories" → us-quant-trader 선택
  4. Permissions → Repository permissions → Contents: "Read-only"
  5. 맨 아래 "Generate token" → 나온 토큰(github_pat_ 로 시작)을 복사
  6. 이 폴더의 .env 파일을 메모장으로 열고 맨 아래에 한 줄 추가 후 저장:
        GITHUB_TOKEN=복사한토큰
  7. update.bat을 다시 더블클릭
"""


def load_env(path: Path = ROOT / ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def is_kept(rel: str) -> bool:
    """True for paths the updater must never write or delete."""
    return rel.replace("\\", "/").split("/", 1)[0] in KEEP


def read_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gh(session: requests.Session, path: str, **headers) -> requests.Response:
    r = session.get(f"{API}{path}", headers=headers, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("GitHub이 토큰을 거절했습니다. 토큰이 만료됐거나 잘못 복사됐을 수 있어요.\n"
                         ".env 의 GITHUB_TOKEN 줄을 지우고 update.bat을 다시 열면 새로 만드는 방법이 나옵니다.")
    if r.status_code == 404:
        raise SystemExit(f"저장소({REPO})를 찾지 못했습니다. 토큰을 만들 때 Repository access에서 "
                         "us-quant-trader를 선택했는지, Contents가 Read-only인지 확인하세요.")
    r.raise_for_status()
    return r


def unpack(data: bytes) -> dict[str, bytes]:
    """Zipball -> {relative path: content}, without the top folder and kept paths."""
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = info.filename.split("/", 1)[1] if "/" in info.filename else ""
            if rel and not is_kept(rel) and ".." not in rel.split("/"):
                files[rel] = z.read(info)
    return files


def apply(files: dict[str, bytes], old_files: list[str], root: Path = ROOT) -> tuple[list[str], list[str]]:
    """Write changed files, delete ones the last update installed that are gone now."""
    changed = []
    for rel, content in sorted(files.items()):
        target = root / rel
        if target.exists() and target.read_bytes() == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".mintory-new")
        tmp.write_bytes(content)
        os.replace(tmp, target)
        changed.append(rel)
    removed = []
    for rel in old_files:
        if rel not in files and not is_kept(rel) and (root / rel).is_file():
            (root / rel).unlink()
            removed.append(rel)
    return changed, removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="새 버전이 있는지만 확인")
    args = ap.parse_args()

    env = load_env()
    token = env.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(TOKEN_HELP)
        webbrowser.open(TOKEN_PAGE)
        return 1
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "X-GitHub-Api-Version": "2022-11-28"})

    repo = env.get("MINTORY_REPO", REPO)
    sha = gh(session, f"/repos/{repo}/commits/{BRANCH}", Accept="application/vnd.github.sha").text.strip()
    manifest = read_manifest()
    if manifest.get("sha") == sha:
        print(f"이미 최신 버전입니다. ({sha[:7]})")
        return 0
    if args.check:
        print(f"새 버전이 있습니다: {sha[:7]} (지금 버전: {manifest.get('sha', '알 수 없음')[:7]})")
        return 0
    if agent_alive() and read_state().get("agent") == "trading":
        print("지금 자동 실행기가 매매 중입니다. 몇 분 뒤에 다시 update.bat을 열어주세요.")
        return 1

    print(f"최신 버전({sha[:7]})을 받는 중...")
    files = unpack(gh(session, f"/repos/{repo}/zipball/{sha}").content)
    old_req = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    changed, removed = apply(files, manifest.get("files", []))
    MANIFEST.write_text(json.dumps({"sha": sha, "files": sorted(files)}, indent=1), encoding="utf-8")

    for rel in changed:
        print(f"  바뀜  {rel}")
    for rel in removed:
        print(f"  지움  {rel}")
    if files.get("requirements.txt", old_req) != old_req:
        print("필요한 파이썬 패키지를 설치하는 중...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")])
    print(f"\n업데이트 완료: {sha[:7]} (파일 {len(changed)}개 바뀜, {len(removed)}개 지움)")
    print(".env 와 paper 폴더(매매 기록)는 그대로입니다.")
    if agent_alive():
        print("자동 실행기는 1분 안에 새 코드로 다시 켜집니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
