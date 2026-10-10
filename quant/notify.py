"""휴대폰 알림 (텔레그램).

.env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 가 있으면 보낸다. 없으면 아무것도 안 한다.
설정은 notify_setup.bat (scripts/notify_setup.py) 이 도와준다.
매일 밤 요약을 끄려면 .env 에 NOTIFY_DAILY=0 을 넣는다.

알림 실패가 매매를 막으면 안 되므로 send()는 절대 예외를 던지지 않는다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
API = "https://api.telegram.org"
SIDE = {"buy": "매수", "sell": "매도"}
SIDE_ICON = {"buy": "🔴", "sell": "🔵"}      # Mintory colours: red = buy/up, blue = sell/down
STATUS = {"filled": "체결", "partial": "일부 체결", "pending": "체결 확인 중",
          "rejected": "거부", "cancelled": "취소"}
STATUS_ICON = {"filled": "✅", "partial": "🟡", "pending": "⏳", "rejected": "❌", "cancelled": "🚫"}
TITLES = {"auto": "🌙 자동 매매 실행", "run": "▶️ 지금 매매 실행", "refresh": "🔄 잔고 새로고침"}


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
            "daily": env.get("NOTIFY_DAILY", "1") != "0",
            "weekly": env.get("NOTIFY_WEEKLY", "1") != "0",
            "preview": env.get("NOTIFY_PREVIEW", "1") != "0"}


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


def trend(v: float) -> str:
    return "📈" if v > 0 else "📉" if v < 0 else "➖"


def order_line(t: dict) -> str:
    side, st = t.get("side"), t.get("status")
    px = f" @ {_usd(float(t['price']))}" if t.get("price") else ""
    extra = f" ({t['message']})" if st == "rejected" and t.get("message") else ""
    return (f"{SIDE_ICON.get(side, '·')} {SIDE.get(side, side)} {t.get('symbol')} {t.get('qty')}주{px}"
            f"\n    {STATUS_ICON.get(st, '·')} {STATUS.get(st, st or '')}{extra}")


def run_message(job: str, result: str, state: dict, new_trades: list[dict],
                equity: list[dict], daily: bool = True) -> str | None:
    """Text for one trader run, or None when nothing is worth a message."""
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

    summary: list[str] = []
    if equity:
        cur = float(equity[-1]["equity"])
        summary.append(f"💵 평가금액 {_usd(cur)}")
        if len(equity) >= 2 and float(equity[-2]["equity"]):
            day = cur / float(equity[-2]["equity"]) - 1
            summary.append(f"{trend(day)} 오늘 {_pct(day)}")
        initial = float(state.get("initial_capital") or 0)
        if initial:
            summary.append(f"📊 누적 {_pct(cur / initial - 1)}")

    if not (new_trades or problems or paused or (daily and summary) or job == "run"):
        return None
    blocks = [f"{TITLES.get(job, job)} · Mintory"]
    if paused:
        blocks.append("⏸ 일시정지 중이라 주문하지 않았어요.")
    if problems:
        blocks.append("⚠️ 확인이 필요해요\n" + "\n".join(f"· {p}" for p in problems))
    if new_trades:
        blocks.append("🛒 주문\n" + "\n".join(order_line(t) for t in new_trades))
    if summary:
        blocks.append("\n".join(summary))
    if not (new_trades or problems or paused) and job in ("auto", "run"):
        blocks.append("😴 오늘은 리밸런싱 날이 아니라 주문 없이 기록만 했어요.")
    return "\n\n".join(blocks)


WEEKDAY_KO = "월화수목금토일"


def _md(day: str) -> str:
    d = date.fromisoformat(day[:10])
    return f"{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"


def _pp(v: float) -> str:
    return f"{v * 100:+.2f}%p"


def weekly_message(week_end: str, state: dict, equity: list[dict], trades: list[dict]) -> str | None:
    """Saturday-morning summary of the US trading week that ended on `week_end` (a Friday)."""
    end = date.fromisoformat(week_end)
    start = end - timedelta(days=end.weekday())          # Monday
    week = [r for r in equity if start.isoformat() <= r["date"] <= week_end]
    if not week:
        return None
    before = [r for r in equity if r["date"] < start.isoformat()]
    initial = float(state.get("initial_capital") or 0)
    base = float(before[-1]["equity"]) if before else initial
    cur = float(week[-1]["equity"])
    blocks = [f"📅 Mintory 주간 리포트\n{_md(start.isoformat())} ~ {_md(week[-1]['date'])}"]

    head = [f"💵 평가금액 {_usd(cur)}"]
    wk = cur / base - 1 if base else None
    if wk is not None:
        head.append(f"{trend(wk)} 이번 주 {_pct(wk)} ({'+' if cur >= base else '-'}{_usd(abs(cur - base))})")
    if initial:
        head.append(f"📊 누적 {_pct(cur / initial - 1)}")
    blocks.append("\n".join(head))

    cmp_ = []
    ref = before[-1] if before else week[0]
    b0, b1 = ref.get("benchmark_close"), week[-1].get("benchmark_close")
    if wk is not None and b0 and b1:
        spy = float(b1) / float(b0) - 1
        cmp_.append(f"· {state.get('benchmark') or 'SPY'} {_pct(spy)} · 차이 {_pp(wk - spy)}")
    series = (state.get("backtest") or {}).get("series") or {}
    m0 = series.get(ref["date"]) if before else series.get(week[0]["date"])
    m1 = series.get(week[-1]["date"])
    if wk is not None and m0 and m1 and before:
        bt = float(m1) / float(m0) - 1
        cmp_.append(f"· 백테스트 {_pct(bt)} · 차이 {_pp(wk - bt)}")
    if cmp_:
        blocks.append("🆚 같은 기간 비교\n" + "\n".join(cmp_))

    done = [t for t in trades if start.isoformat() <= str(t.get("date", ""))[:10] <= week_end]
    if done:
        fee = sum(float(t.get("fee") or 0) for t in done)
        blocks.append(f"🛒 이번 주 매매 {len(done)}건 · 수수료 {_usd(fee)}\n"
                      + "\n".join(order_line(t) for t in done))
    else:
        blocks.append("🛒 이번 주 매매 없음")

    nxt = []
    order_day = (state.get("next_rebalance") or {}).get("order_date")
    if order_day:
        nxt.append(f"· 리밸런싱 주문일 {_md(order_day)}")
    pv = state.get("preview") or {}
    if pv.get("order_date") == order_day and pv.get("orders") is not None:
        nxt.append(f"· 지금 가격이면 예상 주문 {len(pv['orders'])}건" if pv["orders"] else "· 지금 가격이면 바뀌는 종목 없음")
    if any(str(a.get("message", "")).startswith("STOP 파일") for a in state.get("alerts") or []):
        nxt.append("· ⏸ 일시정지 중이라 주문하지 않아요")
    if nxt:
        blocks.append("🗓 다음 주\n" + "\n".join(nxt))
    return "\n\n".join(blocks)


def preview_message(preview: dict, paused: bool = False) -> str | None:
    """Heads-up the night before a rebalance: the orders it would place at today's prices."""
    orders = preview.get("orders") or []
    if not orders:
        return None
    lines = [f"{SIDE_ICON.get(o['side'], '·')} {SIDE.get(o['side'], o['side'])} {o['symbol']} {o['qty']}주 "
             f"(약 {_usd(float(o['value']))})" for o in orders]
    blocks = [f"📣 내일 밤 리밸런싱 예정\n🗓 {_md(preview['order_date'])} 23:45 자동 실행",
              "🛒 예상 주문\n" + "\n".join(lines),
              (f"💡 {_md(preview['as_of'])} 장 마감 가격 기준 예상이에요. 오늘 밤 미국장 마감 가격으로 "
               "다시 계산하니 수량은 조금 달라질 수 있어요.")]
    if paused:
        blocks.append("⏸ 지금 일시정지 중이라 이대로면 주문하지 않아요. /resume 으로 재개")
    return "\n\n".join(blocks)


def notify_preview(paper: Path, path: Path = ENV) -> bool:
    """Send the night-before heads-up from paper/state.json. Never raises."""
    try:
        if not configured(path) or not settings(path)["preview"]:
            return False
        state = json.loads((paper / "state.json").read_text(encoding="utf-8-sig"))
        text = preview_message(state.get("preview") or {}, (paper / "STOP").exists())
        return send(text, path) if text else False
    except Exception:  # noqa: BLE001 - a notification must never break the agent
        return False


def notify_weekly(week_end: str, paper: Path, path: Path = ENV) -> bool:
    """Build and send the weekly report. Never raises."""
    try:
        if not configured(path) or not settings(path)["weekly"]:
            return False
        try:
            state = json.loads((paper / "state.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            state = {}
        text = weekly_message(week_end, state, read_jsonl(paper / "equity.jsonl"),
                              read_jsonl(paper / "trades.jsonl"))
        return send(text, path) if text else False
    except Exception:  # noqa: BLE001 - a notification must never break the agent
        return False


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
