"""텔레그램 명령: 휴대폰에서 Mintory 잔고를 보고, 일시정지/재개한다.

자동 실행기(scripts/mintory_agent.py)가 켜져 있는 동안 봇에 온 메시지를 읽고 답한다.
.env 의 TELEGRAM_CHAT_ID 대화방(notify_setup.bat 으로 연결한 본인)에서 온 메시지만 따르고,
나머지는 무시한다. 주문을 내는 명령은 일부러 없다.

    /balance  잔고        보유 종목과 평가금액 (시세가 있으면 최신 시세로)
    /refresh  새로고침    키움 잔고를 새로 불러온 뒤 잔고 보내기 (주문 없음)
    /status   상태        다음 자동 실행, 마지막 실행 결과, 일시정지 여부
    /next     다음        다음 리밸런싱 날짜와 예상 주문
    /check    점검        실계좌 전환 점검표
    /tax      세금        올해 양도소득세 추정 (원화)
    /pause    일시정지    자동 매매가 주문을 내지 않게 멈춤
    /resume   재개        다시 주문
    /help     도움말 (/? 나 ? 도 됨)
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import requests

from quant import notify

COMMANDS = [
    ("balance", "잔고", "보유 종목과 평가금액"),
    ("refresh", "새로고침", "키움 잔고를 새로 불러오기 (주문 없음)"),
    ("status", "상태", "다음 자동 실행과 마지막 결과"),
    ("next", "다음", "다음 리밸런싱 예상 주문"),
    ("check", "점검", "실계좌 전환 점검표"),
    ("tax", "세금", "올해 양도소득세 추정 (원화)"),
    ("pause", "일시정지", "자동 매매 주문 멈추기"),
    ("resume", "재개", "자동 매매 주문 다시 하기"),
    ("help", "도움말", "명령 목록"),
]
ICONS = {"balance": "💰", "refresh": "🔄", "status": "📋", "next": "📣", "check": "🧾", "tax": "🧮", "pause": "⏸", "resume": "▶️", "help": "❓"}
ALIASES = {ko: en for en, ko, _ in COMMANDS} | {
    "start": "help", "?": "help", "양도세": "tax", "환율": "tax", "？": "help", "명령": "help", "명령어": "help", "도움": "help", "정지": "pause", "멈춰": "pause", "다시시작": "resume",
}
MAX_AGE = 600  # seconds; older messages (sent while the PC was off) are ignored
WEEKDAY_KO = "월화수목금토일"


@dataclass
class Actions:
    """What the agent lets the bot do."""
    state: Callable[[], dict]          # paper/state.json
    agent: Callable[[], dict]          # paper/agent.json
    paused: Callable[[], bool]
    set_paused: Callable[[bool], None]
    refresh: Callable[[], str | None]  # run a balance check; None when another run is going
    quotes: Callable[[list[str]], dict] | None = None
    readiness: Callable[[], dict] | None = None
    tax: Callable[[], dict | None] | None = None


def command(text: str) -> str | None:
    """'/balance@MintoryBot', '잔고', '/잔고' -> 'balance'."""
    word = (text or "").strip().split(maxsplit=1)[0] if (text or "").strip() else ""
    word = word.lstrip("/").split("@", 1)[0].lower()
    if word in ALIASES.values():
        return word
    return ALIASES.get(word)


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _won(v: float) -> str:
    return f"₩{round(v):,}" if v >= 0 else f"-₩{round(-v):,}"


def _won_signed(v: float) -> str:
    return ("+" if v > 0 else "") + _won(v)


def tax_text(t: dict | None, mode: str | None = None) -> str:
    if not t:
        return ("🧮 아직 환율 기록이 없어 계산하지 못했어요.\n\n"
                "오늘 밤 자동 실행(또는 Mintory 잔고 새로고침) 뒤에 다시 보내 주세요.")
    blocks = [f"🧮 {t['year']}년 양도소득세 추정\n💱 환율 {t['usdkrw']:,.1f}원 기준",
              "\n".join([f"📈 실현 손익 {_won_signed(t['realized_krw'])} ({_usd(t['realized_usd'])}, 매도 {t['sells']}건)",
                         f"➖ 기본공제 {_won(t['deduction_krw'])}",
                         f"🧾 과세표준 {_won(t['taxable_krw'])}",
                         f"💸 예상 세금 (22%) {_won(t['tax_krw'])}"]),
              "\n".join(["📦 지금 모두 판다면",
                         f"· 평가손익 {_won_signed(t['unrealized_krw'])}",
                         f"· 올해 예상 세금 {_won(t['tax_if_sold_krw'])}"])]
    note = "💡 1년 손익을 합쳐 250만 원을 뺀 뒤 22%, 다음 해 5월 신고. 참고용 추정이에요."
    if mode and mode != "kiwoom_live":
        note += "\n지금은 모의투자 기록이라 실제로 낼 세금은 없어요."
    blocks.append(note)
    return "\n\n".join(blocks)


def _when(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{t.month}/{t.day}({WEEKDAY_KO[t.weekday()]}) {t:%H:%M}"


def balance_text(state: dict, quotes: dict | None = None, paused: bool = False) -> str:
    if not state:
        return "💰 아직 잔고 기록이 없어요.\n\nMintory에서 잔고 새로고침을 한 번 눌러 주세요."
    q = (quotes or {}).get("quotes") or {}
    rows, total = [], float(state.get("cash") or 0)
    for p in state.get("positions") or []:
        sym, qty = p.get("symbol"), float(p.get("qty") or 0)
        live = q.get(sym) or {}
        price = float(live.get("price") or p.get("last_price") or 0)
        value = qty * price
        total += value
        avg = float(p.get("avg_price") or 0)
        bits = []
        if live.get("change") is not None:
            bits.append(f"{notify.trend(live['change'])} 오늘 {live['change'] * 100:+.2f}%")
        if avg:
            bits.append(f"손익 {(price / avg - 1) * 100:+.2f}%")
        line = f"· {sym} {qty:g}주 · {_usd(value)}"
        if bits:
            line += "\n    " + " · ".join(bits)
        rows.append((value, line))
    basis = (f"시세 {_when(quotes.get('as_of'))}" if q
             else f"기록 {_when(state.get('updated_at'))}")
    head = [f"💵 평가금액 {_usd(total)}"]
    fx = float((state.get("fx") or {}).get("usdkrw") or 0)
    if fx:
        head.append(f"🇰🇷 약 {_won(total * fx)} (환율 {fx:,.1f}원)")
    initial = float(state.get("initial_capital") or 0)
    if initial:
        head.append(f"📊 누적 {(total / initial - 1) * 100:+.2f}%")
    hold = ["📦 보유 종목"] + [r for _, r in sorted(rows, reverse=True)]
    hold.append(f"· 현금 · {_usd(float(state.get('cash') or 0))}")
    blocks = [f"💰 Mintory 잔고\n🕒 {basis} 기준", "\n".join(head), "\n".join(hold)]
    if paused:
        blocks.append("⏸ 일시정지 중 (주문 안 함)")
    return "\n\n".join(blocks)


def status_text(state: dict, agent: dict, paused: bool) -> str:
    lines = ["⏸ 일시정지 중 · 자동 매매가 주문을 내지 않아요\n    /resume 으로 재개"
             if paused else "▶️ 자동 매매 켜짐"]
    if agent.get("next_run"):
        lines.append(f"⏰ 다음 자동 실행 · {_when(agent['next_run'])}")
    if agent.get("last_run"):
        res = agent.get("last_result") or "-"
        ok = res == "ok"
        lines.append(f"{'✅' if ok else '❌'} 마지막 실행 · {_when(agent['last_run'])} · {'OK' if ok else res}")
    nxt = (state.get("next_rebalance") or {}).get("order_date")
    if nxt:
        lines.append(f"📅 다음 리밸런싱 주문일 · {nxt}")
    if agent.get("backup_day"):
        lines.append(f"🗂 마지막 기록 백업 · {agent['backup_day']}")
    blocks = ["📋 Mintory 상태", "\n".join(lines)]
    if agent.get("missed"):
        blocks.append("⚠️ 놓친 실행 · " + ", ".join(agent["missed"]))
    return "\n\n".join(blocks)


def next_text(state: dict, paused: bool = False) -> str:
    pv = state.get("preview") or {}
    if not pv.get("order_date"):
        return "📣 아직 미리보기가 없어요.\n\nMintory에서 잔고 새로고침을 한 번 하면 계산돼요."
    blocks = [f"📣 다음 리밸런싱\n🗓 {_when(pv['order_date'] + 'T23:45')} 자동 실행"]
    orders = pv.get("orders") or []
    if orders:
        blocks.append("🛒 예상 주문\n" + "\n".join(
            f"{notify.SIDE_ICON.get(o['side'], '·')} {notify.SIDE.get(o['side'], o['side'])} {o['symbol']} "
            f"{o['qty']}주 (약 {_usd(float(o['value']))})" for o in orders))
    else:
        blocks.append("✅ 지금 가격이면 바뀌는 종목이 없어요")
    blocks.append(f"💡 {notify._md(pv.get('as_of') or pv['order_date'])} 장 마감 가격 기준 예상이에요. 신호일 마감 가격으로 다시 계산해요.")
    if paused:
        blocks.append("⏸ 일시정지 중이라 이대로면 주문하지 않아요")
    return "\n\n".join(blocks)


STATUS_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "todo": "⬜"}


def readiness_text(report: dict) -> str:
    sm = report.get("summary") or {}
    head = ("🧾 실계좌 전환 점검표\n"
            + ("✅ 모두 준비됐어요" if sm.get("ready") else
               f"✅ {sm.get('ok', 0)} · ⚠️ {sm.get('warn', 0)} · ❌ {sm.get('fail', 0)} · ⬜ {sm.get('todo', 0)}"))
    rows = "\n".join(f"{STATUS_ICON.get(i['status'], '·')} {i['title']}\n    {i['detail']}"
                     for i in report.get("items") or [])
    return head + "\n\n" + rows + "\n\n💡 점검만 해요. 실계좌 전환은 직접 확인한 뒤에 따로 진행해요."


def help_text() -> str:
    rows = "\n".join(f"{ICONS[en]} /{en} ({ko})\n    {desc}" for en, ko, desc in COMMANDS)
    return ("🤖 Mintory 명령\n\n" + rows
            + "\n\n💡 한국어로 '잔고'처럼 보내도 되고, 언제든 /? 를 보내면 이 목록이 나와요.")


def handle(text: str, act: Actions) -> str:
    cmd = command(text)
    if cmd == "balance":
        state = act.state()
        syms = [p["symbol"] for p in state.get("positions") or [] if p.get("symbol")]
        quotes = act.quotes(syms) if act.quotes and syms else None
        return balance_text(state, quotes, act.paused())
    if cmd == "refresh":
        result = act.refresh()
        if result is None:
            return "⏳ 지금 다른 작업(매매 실행이나 새로고침) 중이에요.\n\n잠시 뒤에 다시 보내 주세요."
        reason = ((act.agent() or {}).get("last_job") or {}).get("reason") or ""
        head = "" if result == "ok" else ("⚠️ 잔고를 불러오지 못했어요" + (f"\n{reason}" if reason else "")
                                          + "\n\n마지막 기록을 보여 드려요.\n\n")
        return head + balance_text(act.state(), None, act.paused())
    if cmd == "next":
        return next_text(act.state(), act.paused())
    if cmd == "check":
        if not act.readiness:
            return "🧾 점검표를 불러오지 못했어요."
        return readiness_text(act.readiness())
    if cmd == "tax":
        return tax_text(act.tax() if act.tax else None, act.state().get("mode"))
    if cmd == "status":
        return status_text(act.state(), act.agent(), act.paused())
    if cmd == "pause":
        if act.paused():
            return "⏸ 이미 일시정지 중이에요.\n\n다시 하려면 /resume"
        act.set_paused(True)
        return "⏸ 일시정지했어요\n\n자동 매매가 주문을 내지 않아요.\n다시 하려면 /resume"
    if cmd == "resume":
        if not act.paused():
            return "▶️ 일시정지 상태가 아니에요.\n\n자동 매매는 그대로 켜져 있어요."
        act.set_paused(False)
        return "▶️ 다시 시작했어요\n\n다음 자동 실행부터 주문을 냅니다."
    return help_text()


def _api(token: str, method: str, **params) -> dict:
    r = requests.post(f"{notify.API}/bot{token}/{method}", json=params,
                      timeout=params.get("timeout", 0) + 15)
    r.raise_for_status()
    return r.json()


def serve(act: Actions, get_offset: Callable[[], int | None], set_offset: Callable[[int], None],
          log: Callable[[str], None] = lambda m: None, once: bool = False) -> None:
    """Answer the owner's messages forever (one round with once=True). Never raises."""
    menu_for, last_err = None, None
    while True:
        s = {"token": ""}
        try:
            s = notify.settings()
            if not (s["token"] and s["chat_id"]):
                if once:
                    return
                time.sleep(60)
                continue
            if menu_for != s["token"]:
                _api(s["token"], "setMyCommands",
                     commands=[{"command": en, "description": f"{ko} · {desc}"}
                               for en, ko, desc in COMMANDS])
                menu_for = s["token"]
            params = {"timeout": 0 if once else 25, "allowed_updates": ["message"]}
            if get_offset() is not None:
                params["offset"] = get_offset()
            for up in _api(s["token"], "getUpdates", **params).get("result", []):
                set_offset(up["update_id"] + 1)
                msg = up.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != str(s["chat_id"]):
                    continue
                if time.time() - msg.get("date", 0) > MAX_AGE or not msg.get("text"):
                    continue
                log(f"텔레그램 명령: {msg['text'][:40]}")
                notify.send(handle(msg["text"], act))
        except Exception as e:  # noqa: BLE001 - keep listening; a 409 means another copy polls
            err = str(e).replace(s["token"], "***") if s["token"] else str(e)
            if err != last_err:  # the bot token must never reach the log
                log(f"텔레그램 명령 오류: {err[:150]}")
                last_err = err
            if not once:
                time.sleep(30)
        if once:
            return
