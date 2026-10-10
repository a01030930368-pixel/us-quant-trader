"""출금 준비: 수익 일부를 빼고 싶을 때 쓰는 요청 파일(paper/withdraw.json).

사용자가 금액을 정하면(대시보드 '입출금' 창 또는 텔레그램 /withdraw) 이 파일이 생기고,
다음 실행에서 실행기가 그만큼을 현금으로 빼 둔다(보유 종목을 목표 비중대로 조금씩 매도).
준비가 끝나면 텔레그램으로 알리고, 사용자가 키움 앱에서 환전·출금하면 실행기가 출금을
알아채고 요청을 지운다. 남은 돈으로는 같은 전략을 계속한다.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from quant import tax

FILE = "withdraw.json"
UNITS = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000, "천": 1_000}


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def parse_amount(text: str, usdkrw: float | None) -> tuple[float, float | None]:
    """'300만', '3,000,000원', '300만원', '$2000', '2000달러', '2000 usd' ->
    (dollars, won or None). A bare number is won from 100,000 up, else dollars."""
    t = (text or "").strip().lower().replace(",", "").replace(" ", "")
    if not t:
        raise ValueError("금액을 알려 주세요. 예: 300만 또는 $2000")
    dollars = t.startswith("$") or t.endswith(("$", "달러", "usd", "불"))
    won = t.endswith(("원", "krw", "₩")) or t.startswith("₩") or any(u in t for u in UNITS)
    num = re.sub(r"[$₩원달러불]|usd|krw", "", t)
    value, rest = 0.0, num
    for unit, mult in UNITS.items():           # 1억2천만, 300만 ...
        if unit in rest:
            head, rest = rest.split(unit, 1)
            value += float(head or 1) * mult
    if rest:
        value += float(rest)
    if value <= 0:
        raise ValueError("0보다 큰 금액을 알려 주세요.")
    if not dollars and not won:
        won = value >= 100_000
    if won:
        if not usdkrw:
            raise ValueError("환율 기록이 아직 없어 원화 금액을 달러로 바꿀 수 없어요. 달러로 알려 주세요 (예: $2000).")
        return round(value / usdkrw, 2), round(value)
    return round(value, 2), None


def equity_of(state: dict) -> float:
    held = sum(float(p.get("qty") or 0) * float(p.get("last_price") or p.get("avg_price") or 0)
               for p in state.get("positions") or [])
    return float(state.get("cash") or 0) + held


def request(paper: Path, text: str, source: str = "app") -> dict:
    """Create (or replace) the withdrawal request. Returns it with a tax hint.
    Raises ValueError with a message for the user."""
    state = _json(paper / "state.json")
    fx = float((state.get("fx") or {}).get("usdkrw") or 0) or None
    usd, krw = parse_amount(text, fx)
    equity = equity_of(state)
    if equity and usd > equity:
        raise ValueError(f"계좌 평가금액(${equity:,.2f})보다 많이 뺄 수는 없어요.")
    req = {"amount_usd": usd, "amount_krw": krw, "fx": fx, "left_usd": usd, "source": source,
           "requested_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    paper.mkdir(parents=True, exist_ok=True)
    tmp = paper / f".{FILE}.tmp"
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, paper / FILE)
    return {**req, "tax_krw": tax_hint(paper, usd)}


def cancel(paper: Path) -> bool:
    p = paper / FILE
    if not p.exists():
        return False
    p.unlink()
    return True


def current(paper: Path) -> dict | None:
    req = _json(paper / FILE)
    return req if float(req.get("left_usd") or 0) > 0 else None


def tax_hint(paper: Path, usd: float) -> int | None:
    """Extra tax this year (won) if `usd` worth of holdings were sold now,
    pro rata across holdings. None without enough data."""
    est = tax.from_paper(paper)
    state = _json(paper / "state.json")
    held = equity_of(state) - float(state.get("cash") or 0)
    if not est or held <= 0:
        return None
    need = max(0.0, usd - max(0.0, float(state.get("cash") or 0) - 0.02 * equity_of(state)))
    gain = est["unrealized_krw"] * min(1.0, need / held)
    _, before = tax.tax_on(est["realized_krw"])
    _, after = tax.tax_on(est["realized_krw"] + gain)
    return round(after - before)


def flows(paper: Path) -> list[dict]:
    """Deposits and withdrawals the runner spotted, newest first."""
    out = []
    p = paper / "flows.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return sorted(out, key=lambda r: str(r.get("time", "")), reverse=True)


def _won(v: float) -> str:
    return f"₩{round(v):,}"


def status_text(req: dict | None, state: dict) -> str:
    if not req:
        return ("🏧 출금 준비 요청이 없어요.\n\n"
                "수익 일부를 빼고 싶으면 금액과 함께 보내 주세요.\n예: /withdraw 300만  ·  /withdraw $2000")
    left = float(req["left_usd"])
    won = f" (약 {_won(req['amount_krw'])})" if req.get("amount_krw") else ""
    w = state.get("withdraw") or {}
    if w.get("ready"):
        step = "✅ 현금으로 빼 뒀어요. 키움 앱(영웅문)에서 환전한 뒤 출금하세요."
    elif w.get("paused"):
        step = "⏸ 일시정지 중이라 아직 팔지 못했어요. /resume 하면 다음 실행 때 준비해요."
    else:
        step = "⏳ 다음 자동 실행(밤 11:45) 때 보유 종목을 조금씩 팔아 현금으로 빼 둘게요."
    return (f"🏧 출금 준비\n\n💵 ${left:,.2f}{won}\n\n{step}\n\n"
            "취소하려면 /withdraw 취소")


def request_text(req: dict) -> str:
    won = f" (약 {_won(req['amount_krw'])})" if req.get("amount_krw") else (
        f" (약 {_won(req['amount_usd'] * req['fx'])})" if req.get("fx") else "")
    lines = [f"🏧 출금 준비를 요청했어요\n\n💵 ${req['amount_usd']:,.2f}{won}",
             ("⏳ 다음 자동 실행(밤 11:45) 때 보유 종목을 목표 비중대로 조금씩 팔아 현금으로 빼 둘게요. "
              "준비되면 알려 드려요.")]
    if req.get("tax_krw"):
        lines.append(f"🧾 이번 매도로 올해 양도세가 약 {_won(req['tax_krw'])} 늘어날 수 있어요 (추정).")
    elif req.get("tax_krw") == 0:
        lines.append("🧾 이번 매도로 늘어나는 양도세는 없을 것 같아요 (공제 250만 원 안, 추정).")
    lines.append("취소하려면 /withdraw 취소")
    return "\n\n".join(lines)
