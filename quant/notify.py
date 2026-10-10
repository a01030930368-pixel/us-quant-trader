"""휴대폰 알림 (텔레그램).

.env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 가 있으면 보낸다. 없으면 아무것도 안 한다.
설정은 notify_setup.bat (scripts/notify_setup.py) 이 도와준다.
매일 밤 요약을 끄려면 .env 에 NOTIFY_DAILY=0 을 넣는다.

알림 실패가 매매를 막으면 안 되므로 send()는 절대 예외를 던지지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
API = "https://api.telegram.org"
SIDE = {"buy": "매수", "sell": "매도"}
STATUS = {"filled": "체결", "partial": "일부 체결", "pending": "체결 확인 중",
          "rejected": "거부", "cancelled": "취소"}
TITLES = {"auto": "자동 매매 실행", "run": "지금 매매 실행", "refresh": "잔고 새로고침"}


def read_env(path: Path = ENV) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def settings(path: Path = ENV) -> dict:
    env = read_env(path)
    return {"token": env.get("TELEGRAM_BOT_TOKEN", ""), "chat_id": env.get("TELEGRAM_CHAT_ID", ""),
            "daily": env.get("NOTIFY_DAILY", "1") != "0"}


def configured(path: Path = ENV) -> bool:
    s = settings(path)
    return bool(s["token"] and s["chat_id"])


def send(text: str, path: Path = ENV) -> bool:
    """Send a Telegram message. Returns False (never raises) when not set up or it fails."""
    s = settings(path)
    if not (s["token"] and s["chat_id"]):
        return False
    try:
        r = requests.post(f"{API}/bot{s['token']}/sendMessage",
                          json={"chat_id": s["chat_id"], "text": text}, timeout=15)
        return r.ok
    except requests.RequestException:
        return False


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _pct(v: float) -> str:
    return f"{v * 100:+.2f}%"


def run_message(job: str, result: str, state: dict, new_trades: list[dict],
                equity: list[dict], daily: bool = True) -> str | None:
    """Text for one trader run, or None when nothing is worth a message."""
    lines: list[str] = []
    problems: list[str] = []
    paused = False
    if result != "ok":
        problems.append(f"실행에 실패했어요 ({result}). paper/logs/agent.log 를 확인해 주세요.")
    for a in state.get("alerts") or []:
        msg = a.get("message", "")
        if msg.startswith("STOP 파일"):
            paused = True
        elif a.get("level") in ("warn", "error"):
            problems.append(msg)

    if new_trades:
        lines.append("주문")
        for t in new_trades:
            px = f" @ {_usd(float(t['price']))}" if t.get("price") else ""
            st = STATUS.get(t.get("status"), t.get("status", ""))
            extra = f" ({t['message']})" if t.get("status") == "rejected" and t.get("message") else ""
            lines.append(f"· {SIDE.get(t.get('side'), t.get('side'))} {t.get('symbol')} "
                         f"{t.get('qty')}주{px} · {st}{extra}")

    summary = ""
    if equity:
        last = equity[-1]
        cur = float(last["equity"])
        parts = [f"평가금액 {_usd(cur)}"]
        if len(equity) >= 2 and float(equity[-2]["equity"]):
            parts.append(f"오늘 {_pct(cur / float(equity[-2]['equity']) - 1)}")
        initial = float(state.get("initial_capital") or 0)
        if initial:
            parts.append(f"누적 {_pct(cur / initial - 1)}")
        summary = " · ".join(parts)

    if not (lines or problems or paused or (daily and summary) or job == "run"):
        return None
    out = [f"Mintory · {TITLES.get(job, job)}"]
    if paused:
        out.append("⏸ 일시정지 중이라 주문하지 않았어요.")
    out += [f"⚠️ {p}" for p in problems]
    out += lines
    if summary:
        out.append(summary)
    if not (lines or problems or paused) and job in ("auto", "run"):
        out.append("오늘은 리밸런싱 날이 아니라 주문 없이 기록만 했어요.")
    return "\n".join(out)


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def notify_run(job: str, result: str, paper: Path, trades_before: int, path: Path = ENV) -> bool:
    """Build and send the message for a run that just finished. Never raises."""
    try:
        if not configured(path):
            return False
        try:
            state = json.loads((paper / "state.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            state = {}
        trades = read_jsonl(paper / "trades.jsonl")
        text = run_message(job, result, state, trades[trades_before:],
                           read_jsonl(paper / "equity.jsonl"), daily=settings(path)["daily"])
        return send(text, path) if text else False
    except Exception:  # noqa: BLE001 - a notification must never break the agent
        return False
