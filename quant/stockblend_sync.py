"""Mintory가 가상 계좌 장부를 GitHub에서 받아 온다.

- 4종 혼합: stockblend-paper 워크플로 -> paper-stockblend 브랜치 -> paper/stockblend/
- 저변동성 업종: lowvol-paper 워크플로 -> paper-lowvol 브랜치 -> paper/lowvol/

장부는 GitHub Actions가 매일 밤 각 브랜치에 쌓는다.
update.bat 과 같은 .env 의 GITHUB_TOKEN(읽기 전용)으로 받는다. 받지 못하면 예전 파일을 그대로 둔다.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = "a01030930368-pixel/us-quant-trader"
BRANCH = "paper-stockblend"
FOLDER = "paper_stockblend"
FILES = ("state.json", "equity.jsonl")
API = "https://api.github.com"
# 이름 -> (브랜치, 브랜치 안 폴더, paper/ 아래 받을 폴더)
LEDGERS = {"4종 혼합": (BRANCH, FOLDER, "stockblend"), "저변동성 업종": ("paper-lowvol", "paper_lowvol", "lowvol")}


def sync(dest: Path, token: str | None, repo: str = REPO, session=None, branch: str = BRANCH,
         folder: str = FOLDER, label: str = "4종 혼합") -> str | None:
    """Download the ledger files into `dest`. Returns None on success, else why not."""
    if not token:
        return f".env 에 GITHUB_TOKEN 이 없어 {label} 장부를 받지 못했습니다."
    import requests

    s = session or requests.Session()
    got: dict[str, bytes] = {}
    for name in FILES:
        try:
            r = s.get(f"{API}/repos/{repo}/contents/{folder}/{name}", params={"ref": branch},
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw",
                               "X-GitHub-Api-Version": "2022-11-28"}, timeout=30)
        except requests.RequestException as e:
            return f"{label} 장부를 받지 못했습니다: {e}"
        if r.status_code == 404:
            return f"{label} 장부가 아직 없습니다 (첫 계산 전)."
        if r.status_code != 200:
            return f"{label} 장부를 받지 못했습니다 (GitHub {r.status_code})."
        got[name] = r.content
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in got.items():  # write only when every file arrived
        tmp = dest / (name + ".new")
        tmp.write_bytes(content)
        os.replace(tmp, dest / name)
    return None
