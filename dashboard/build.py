"""모의투자 기록을 읽어 대시보드 HTML 한 장을 만들고 브라우저로 연다.

    python dashboard/build.py            # paper/ 폴더의 실제 기록 (없으면 샘플)
    python dashboard/build.py --sample   # 샘플 데이터로 보기

추가 설치가 필요 없도록 표준 라이브러리만 쓴다. 기록 형식은 docs/paper-format.md 참고.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant import market_calendar, tax

TEMPLATE = HERE / "template.html"
SAMPLE_DIR = HERE / "sample"
PLACEHOLDER = "/*__DASHBOARD_DATA__*/"
SUPPORTED_SCHEMA = 1


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # 실행기가 쓰는 도중이면 마지막 줄이 잘려 있을 수 있다.
            print(f"[경고] {path.name} {n}번째 줄을 읽지 못해 건너뜁니다.", file=sys.stderr)
    return rows


def load(data_dir: Path) -> dict:
    state_path = data_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    version = state.get("schema_version", 1)
    if version > SUPPORTED_SCHEMA:
        print(f"[경고] 기록 형식 버전 {version}은 이 대시보드(버전 {SUPPORTED_SCHEMA})보다 새롭습니다. "
              "일부 항목이 안 보일 수 있어요.", file=sys.stderr)
    data = {
        "state": state,
        "trades": read_jsonl(data_dir / "trades.jsonl"),
        "equity": read_jsonl(data_dir / "equity.jsonl"),
        "flows": read_jsonl(data_dir / "flows.jsonl"),   # 입금·출금 (실행기가 감지)
    }
    # 4종 혼합 모의 장부 (별도 가상 계좌, 자동 실행기가 GitHub에서 받아 둠). 없으면 비교 화면을 숨긴다.
    blend_dir = data_dir / "stockblend"
    if (blend_dir / "state.json").exists():
        try:
            data["stockblend"] = {
                "state": json.loads((blend_dir / "state.json").read_text(encoding="utf-8-sig")),
                "equity": read_jsonl(blend_dir / "equity.jsonl"),
            }
        except (OSError, json.JSONDecodeError):
            print("[경고] 4종 혼합 장부를 읽지 못해 전략 비교는 건너뜁니다.", file=sys.stderr)
    # 자동 실행 도우미(mintory_agent)가 남기는 상태. 없거나 깨져 있으면 표시하지 않는다.
    agent_path = data_dir / "agent.json"
    if agent_path.exists():
        try:
            data["agent"] = json.loads(agent_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            print(f"[경고] {agent_path.name} 을 읽지 못해 자동 실행 상태는 건너뜁니다.", file=sys.stderr)
    return data


def render(data: dict, *, full_page: bool = True) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    body = TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, payload)
    if not full_page:
        return body
    return ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            "</head>\n<body>\n" + body + "\n</body>\n</html>\n")


def page(data_dir: Path | None, *, full_page: bool = True, extra: dict | None = None,
         quiet: bool = False) -> str:
    """기록 폴더를 읽어 대시보드 HTML을 만든다. 기록이 없으면(또는 None이면) 샘플로."""
    source = "paper"
    if data_dir is None:
        data_dir, source = SAMPLE_DIR, "sample"
    elif not (data_dir / "state.json").exists():
        if not quiet:
            print(f"아직 {data_dir} 에 모의투자 기록이 없어서 샘플 데이터로 보여드릴게요.")
        data_dir, source = SAMPLE_DIR, "sample"
    data = load(data_dir)
    try:
        shown = data_dir.resolve().relative_to(ROOT)
    except ValueError:
        shown = data_dir
    data["meta"] = {
        "source": source,
        "dir": str(shown).replace("\\", "/") + "/",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market": market_calendar.upcoming(datetime.now().astimezone().date() - timedelta(days=7)),  # 미국장 시계용 휴장일
    }
    try:  # 올해 양도세 추정 (환율 기록이 있을 때만)
        st = data["state"]
        data["tax"] = tax.estimate(data["trades"], st.get("positions") or [], datetime.now().astimezone().year,
                                   (st.get("fx") or {}).get("usdkrw"))
    except Exception as e:  # noqa: BLE001 - an estimate must never break the dashboard
        print(f"[경고] 양도세 추정을 계산하지 못했습니다: {e}", file=sys.stderr)
    data.update(extra or {})
    return render(data, full_page=full_page)


def _app_browsers() -> list[Path]:
    """Edge(윈도우 기본)와 크롬의 설치 위치 후보."""
    roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    return [Path(r) / rel for rel in rels for r in roots if r]


def open_page(path: Path) -> None:
    open_url(path.resolve().as_uri())


def open_url(uri: str) -> None:
    """윈도우에서는 주소창 없는 앱 창으로, 그 밖에는 기본 브라우저로 연다."""
    if os.name == "nt":
        for exe in _app_browsers():
            if exe.exists():
                try:
                    subprocess.Popen([str(exe), f"--app={uri}", "--start-maximized"])
                    return
                except OSError:
                    continue
    webbrowser.open(uri)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="모의투자 대시보드 만들기")
    ap.add_argument("--data", default=str(ROOT / "paper"), help="기록 폴더 (기본: paper/)")
    ap.add_argument("--sample", action="store_true", help="샘플 데이터로 보기")
    ap.add_argument("--out", default=str(ROOT / "reports" / "dashboard.html"), help="저장할 HTML 경로")
    ap.add_argument("--no-open", action="store_true", help="브라우저를 열지 않음")
    ap.add_argument("--fragment", action="store_true", help="<html> 껍데기 없이 본문만 저장")
    args = ap.parse_args(argv)

    data_dir = None if args.sample else Path(args.data)
    try:
        html = page(data_dir, full_page=not args.fragment)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[오류] {Path(args.data) / 'state.json'} 을 읽지 못했습니다: {e}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"대시보드 저장: {out}")
    if not args.no_open:
        open_page(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
