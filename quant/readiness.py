"""실계좌 전환 점검표: 모의투자를 실계좌로 바꾸기 전에 확인할 것들.

읽기만 한다. 실계좌 주문을 내거나 실행기를 실계좌로 바꾸지 않는다.
Mintory 화면의 "실계좌 점검"과 텔레그램 /check 가 이 결과를 보여준다.

각 항목은 {"id", "title", "status": "ok"|"warn"|"fail"|"todo", "detail"}.
- ok: 준비됨, warn: 확인 권장, fail: 전환 전에 꼭 고쳐야 함, todo: 아직 확인 안 함
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

MIN_DAYS = 20          # trading days of paper records before going live
MAX_GAP = 0.03         # |paper - backtest| cumulative gap that is still fine
LIVE_KEYS = ("KIWOOM_LIVE_APP_KEY", "KIWOOM_LIVE_SECRET_KEY")
KEY_CHECK = "live_key_check.json"


def _jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _num(env: dict, key: str) -> float | None:
    try:
        v = float(str(env.get(key, "")).replace(",", "").strip())
    except ValueError:
        return None
    return v


def wake_task_registered(name: str = "Mintory 자동 실행") -> bool | None:
    """True/False on Windows, None elsewhere (cannot tell)."""
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, timeout=15, check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def item(id_: str, title: str, status: str, detail: str) -> dict:
    return {"id": id_, "title": title, "status": status, "detail": detail}


def checklist(paper: Path, env: dict, agent: dict | None = None, today: date | None = None,
              wake: bool | None = None) -> list[dict]:
    today = today or datetime.now().astimezone().date()
    agent = agent if agent is not None else _json(paper / "agent.json")
    state = _json(paper / "state.json")
    equity = _jsonl(paper / "equity.jsonl")
    trades = _jsonl(paper / "trades.jsonl")
    out = []

    days = len(equity)
    out.append(item("days", "모의투자 기간", "ok" if days >= MIN_DAYS else "warn",
                    f"{days}거래일 기록 (권장 {MIN_DAYS}일 이상)"))

    series = (state.get("backtest") or {}).get("series") or {}
    if equity and series.get(equity[-1]["date"]) and series.get(equity[0]["date"]):
        real = float(equity[-1]["equity"]) / float(equity[0]["equity"]) - 1
        model = float(series[equity[-1]["date"]]) / float(series[equity[0]["date"]]) - 1
        gap = real - model
        out.append(item("gap", "백테스트와의 차이", "ok" if abs(gap) <= MAX_GAP else "warn",
                        f"실제 {real * 100:+.2f}% · 백테스트 {model * 100:+.2f}% · 차이 {gap * 100:+.2f}%p "
                        f"(±{MAX_GAP * 100:.0f}%p 안이면 정상)"))
    else:
        out.append(item("gap", "백테스트와의 차이", "todo", "아직 비교할 기록이 없어요"))

    res = agent.get("last_result")
    missed = agent.get("missed") or []
    if not agent.get("last_run"):
        out.append(item("runs", "자동 실행", "todo", "아직 자동 실행 기록이 없어요"))
    elif res != "ok" or missed:
        why = [f"마지막 실행 실패 ({res})"] if res != "ok" else []
        why += ["놓친 실행 " + ", ".join(missed)] if missed else []
        out.append(item("runs", "자동 실행", "warn", " · ".join(why)))
    else:
        out.append(item("runs", "자동 실행", "ok", f"마지막 실행 {agent['last_run'][:16].replace('T', ' ')} OK, 놓친 날 없음"))

    since = (today - timedelta(days=30)).isoformat()
    recent = [t for t in trades if str(t.get("date", "")) >= since]
    bad = [t for t in recent if t.get("status") in ("rejected", "cancelled")]
    stuck = [t for t in recent if t.get("status") == "pending" and str(t.get("date", "")) < (today - timedelta(days=3)).isoformat()]
    if bad or stuck:
        out.append(item("orders", "최근 30일 주문", "warn",
                        f"거부·취소 {len(bad)}건, 체결 확인 안 된 주문 {len(stuck)}건"))
    else:
        out.append(item("orders", "최근 30일 주문", "ok", f"{len(recent)}건 모두 정상"))

    tg = bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"))
    out.append(item("telegram", "텔레그램 알림", "ok" if tg else "fail",
                    "연결됨 (실계좌 주문 직전 알림이 여기로 와요)" if tg else "notify_setup.bat 으로 연결해 주세요"))

    limit = _num(env, "MAX_ORDER_USD")
    eq_now = float(equity[-1]["equity"]) if equity else None
    if not limit or limit <= 0:
        out.append(item("limit", "주문 한도 (MAX_ORDER_USD)", "fail",
                        ".env 에 주문 하나의 최대 금액(달러)을 적어야 실계좌 주문이 나가요"))
    else:
        note = f"${limit:,.0f}"
        if eq_now:
            note += f" (지금 평가금액의 {limit / eq_now * 100:.0f}%)"
        out.append(item("limit", "주문 한도 (MAX_ORDER_USD)", "ok", note))

    loss = _num(env, "MAX_DAILY_LOSS_PCT")
    loss = 10.0 if loss is None and "MAX_DAILY_LOSS_PCT" not in env else loss
    out.append(item("loss", "하루 손실 한도", "ok" if loss and loss > 0 else "warn",
                    f"{loss:g}% 넘게 빠진 날은 주문 쉼" if loss and loss > 0 else "꺼져 있어요 (MAX_DAILY_LOSS_PCT=0)"))

    keys = all(env.get(k) for k in LIVE_KEYS)
    out.append(item("keys", "실계좌 앱키", "ok" if keys else "fail",
                    ".env 에 입력됨" if keys else
                    "키움 실전투자용 앱키를 따로 발급받아 .env 의 KIWOOM_LIVE_APP_KEY, KIWOOM_LIVE_SECRET_KEY 에 넣어 주세요"))

    chk = _json(paper / KEY_CHECK)
    if not chk:
        out.append(item("api", "실계좌 연결 확인 (조회만)", "todo",
                        "앱키를 넣은 뒤 '실계좌 키 확인'을 눌러 주세요. 잔고만 조회하고 주문은 내지 않아요"))
    elif chk.get("ok"):
        out.append(item("api", "실계좌 연결 확인 (조회만)", "ok",
                        f"{chk.get('time', '')[:16].replace('T', ' ')} 확인 · 달러 예수금 ${chk.get('cash_usd', 0):,.2f}"))
    else:
        out.append(item("api", "실계좌 연결 확인 (조회만)", "fail",
                        f"{chk.get('time', '')[:16].replace('T', ' ')} 실패: {chk.get('error', '')}"))

    out.append(rehearsal_item(paper, today))

    if wake is not None:
        out.append(item("wake", "PC 깨우기 작업", "ok" if wake else "warn",
                        "평일 23:40에 PC를 깨워요" if wake else "Mintory를 한 번 다시 열면 등록돼요"))

    last = agent.get("backup_day")
    extra = " · BACKUP_DIR 에도 복사" if env.get("BACKUP_DIR") else " · BACKUP_DIR 를 정하면 다른 폴더에도 한 부 더"
    if not last:
        out.append(item("backup", "기록 백업", "todo", "아직 백업이 없어요. 매일 아침 7시에 만들어요"))
    else:
        fresh = last >= (today - timedelta(days=2)).isoformat()
        out.append(item("backup", "기록 백업", "ok" if fresh else "warn",
                        f"마지막 백업 {last}" + (extra if fresh else " · 며칠째 백업이 없어요")))

    paused = any(str(a.get("message", "")).startswith("STOP 파일") for a in state.get("alerts") or [])
    paused = paused or (paper / "STOP").exists()
    out.append(item("paused", "일시정지", "warn" if paused else "ok",
                    "일시정지 중이에요. 전환 전에 풀어 주세요" if paused else "꺼져 있음"))
    return out


def rehearsal_item(paper: Path, today: date) -> dict:
    """The latest live-account rehearsal (paper/rehearsal/state.json): real balances,
    the orders the strategy would have sent, and anything that would have blocked them."""
    title = "실계좌 리허설 (주문 없음)"
    st = _json(paper / "rehearsal" / "state.json")
    if not st:
        return item("rehearsal", title, "todo",
                    "실계좌 앱키를 넣으면 매일 밤 자동 실행 뒤에 실계좌 잔고로 리허설해요. '리허설 해보기'로 바로 해볼 수도 있어요")
    when = str(st.get("updated_at", ""))[:16].replace("T", " ")
    errors = [a.get("message", "") for a in st.get("alerts") or [] if a.get("level") == "error"]
    if errors:
        return item("rehearsal", title, "warn", f"{when} · 실계좌였다면 막혔어요: {errors[0]}")
    orders = st.get("planned_orders") or []
    plan = ", ".join(f"{'매수' if o.get('side') == 'buy' else '매도'} {o.get('symbol')} {o.get('qty')}주"
                     for o in orders[:4]) or "리밸런싱 날이 아니라 낼 주문 없음"
    stale = when[:10] < (today - timedelta(days=4)).isoformat()
    cash = st.get("cash")
    note = f"{when} · 달러 예수금 ${float(cash):,.2f} · {plan}" if cash is not None else f"{when} · {plan}"
    return item("rehearsal", title, "warn" if stale else "ok", note + (" · 며칠째 리허설이 없어요" if stale else ""))


def summary(items: list[dict]) -> dict:
    count = {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "warn", "fail", "todo")}
    ready = count["fail"] == 0 and count["todo"] == 0 and count["warn"] == 0
    return {"ready": ready, **count, "total": len(items)}


def check_live_keys(env: dict, paper: Path, broker_cls=None) -> dict:
    """Read-only: issue a token on the REAL server and read USD cash. Never orders."""
    rec: dict = {"time": datetime.now().astimezone().isoformat(timespec="seconds")}
    if not all(env.get(k) for k in LIVE_KEYS):
        rec.update(ok=False, error="실계좌 앱키가 .env 에 없어요")
    else:
        try:
            if broker_cls is None:
                from quant.trader.brokers import KiwoomBroker as broker_cls
            b = broker_cls(env[LIVE_KEYS[0]], env[LIVE_KEYS[1]], mock=False)
            rec.update(ok=True, cash_usd=round(float(b.cash_usd()), 2))
        except Exception as e:  # noqa: BLE001 - shown to the user as a failed check
            msg = str(e)
            for k in LIVE_KEYS:  # keys must never be stored or shown
                if env.get(k):
                    msg = msg.replace(env[k], "***")
            rec.update(ok=False, error=msg[:200])
    paper.mkdir(parents=True, exist_ok=True)
    (paper / KEY_CHECK).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec
