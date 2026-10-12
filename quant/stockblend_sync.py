"""Mintory가 4종 혼합 모의 장부를 GitHub에서 받아 온다 (paper/stockblend/).

장부는 GitHub Actions(stockblend-paper)가 매일 밤 paper-stockblend 브랜치에 쌓는다.
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


def sync(dest: Path, token: str | None, repo: str = REPO, session=None) -> str | None:
    """Download the ledger files into `dest`. Returns None on success, else why not."""
    if not token:
        return ".env 에 GITHUB_TOKEN 이 없어 4종 혼합 장부를 받지 못했습니다."
    import requests

    s = session or requests.Session()
    got: dict[str, bytes] = {}
    for name in FILES:
        try:
            r = s.get(f"{API}/repos/{repo}/contents/{FOLDER}/{name}", params={"ref": BRANCH},
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw",
                               "X-GitHub-Api-Version": "2022-11-28"}, timeout=30)
        except requests.RequestException as e:
            return f"4종 혼합 장부를 받지 못했습니다: {e}"
        if r.status_code == 404:
            return "4종 혼합 장부가 아직 없습니다 (첫 계산 전)."
        if r.status_code != 200:
            return f"4종 혼합 장부를 받지 못했습니다 (GitHub {r.status_code})."
        got[name] = r.content
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in got.items():  # write only when every file arrived
        tmp = dest / (name + ".new")
        tmp.write_bytes(content)
        os.replace(tmp, dest / name)
    return None
