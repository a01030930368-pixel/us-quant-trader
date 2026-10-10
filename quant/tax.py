"""해외주식 양도소득세 추정 (원화).

한국 거주자의 해외주식 양도소득은 1년(1/1~12/31) 동안의 이익과 손실을 합쳐,
기본공제 250만 원을 뺀 금액에 22%(양도세 20% + 지방세 2%)를 매깁니다.
신고·납부는 다음 해 5월입니다.

여기 계산은 화면에 보여 주는 **추정치**입니다.
- 취득가액은 선입선출(먼저 산 주식부터 판 것으로 계산).
- 원화 환산은 매매 기록에 남은 그날 환율(fx)을 쓰고, 없으면 지금 환율로 대신합니다.
  실제 신고는 결제일 기준환율을 쓰므로 조금 다를 수 있어요.
- 수수료는 필요경비로 뺍니다.
"""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path

DEDUCTION_KRW = 2_500_000
RATE = 0.22
COUNTED = ("filled", "partial")


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def tax_on(gain_krw: float) -> tuple[float, float]:
    """(과세표준, 세금) for one year's net gain in won."""
    taxable = max(0.0, gain_krw - DEDUCTION_KRW)
    return taxable, taxable * RATE


def estimate(trades: list[dict], positions: list[dict], year: int, usdkrw: float | None) -> dict | None:
    """Realized gains of `year` (FIFO, in won) and the tax on them, plus what
    selling everything now would add. None without any exchange rate."""
    rows = sorted((t for t in trades if t.get("status") in COUNTED and _f(t.get("qty")) > 0),
                  key=lambda t: (str(t.get("date", "")), str(t.get("time", ""))))
    fx_now = _f(usdkrw) or next((_f(t.get("fx")) for t in reversed(rows) if _f(t.get("fx"))), 0.0)
    if not fx_now:
        return None
    guessed = False

    def fx(t: dict) -> float:
        nonlocal guessed
        v = _f(t.get("fx"))
        if not v:
            guessed = True
        return v or fx_now

    lots: dict[str, deque] = {}        # symbol -> [qty, cost per share in won, in dollars]
    by_sym: dict[str, float] = {}
    realized_usd = 0.0
    sells = 0
    for t in rows:
        sym, qty, px, rate = t.get("symbol"), _f(t["qty"]), _f(t.get("price")), fx(t)
        fee = _f(t.get("fee")) * rate
        book = lots.setdefault(sym, deque())
        if t.get("side") == "buy":
            book.append([qty, (qty * px * rate + fee) / qty, px + _f(t.get("fee")) / qty])
            continue
        left, cost, cost_usd = qty, 0.0, 0.0
        while left > 1e-9 and book:
            lot = book[0]
            take = min(left, lot[0])
            cost += take * lot[1]
            cost_usd += take * lot[2]
            lot[0] -= take
            left -= take
            if lot[0] <= 1e-9:
                book.popleft()
        if left > 1e-9:                 # bought before the records start: no known cost
            guessed = True
            cost += left * px * rate
            cost_usd += left * px
        if str(t.get("date", ""))[:4] != str(year):
            continue
        gain = qty * px * rate - fee - cost
        by_sym[sym] = by_sym.get(sym, 0.0) + gain
        realized_usd += qty * px - _f(t.get("fee")) - cost_usd
        sells += 1

    realized = sum(by_sym.values())
    taxable, tax = tax_on(realized)
    unrealized = 0.0
    for p in positions or []:
        sym, qty = p.get("symbol"), _f(p.get("qty"))
        price = _f(p.get("last_price")) or _f(p.get("avg_price"))
        book = lots.get(sym) or deque()
        if abs(sum(lot[0] for lot in book) - qty) < 1e-6:
            cost = sum(lot[0] * lot[1] for lot in book)
        else:                           # records do not cover this holding: use the broker's average
            guessed = True
            cost = qty * _f(p.get("avg_price")) * fx_now
        unrealized += qty * price * fx_now - cost
    _, tax_all = tax_on(realized + unrealized)
    return {
        "year": year, "usdkrw": round(fx_now, 2), "rate": RATE, "deduction_krw": DEDUCTION_KRW,
        "realized_krw": round(realized), "realized_usd": round(realized_usd, 2), "sells": sells,
        "by_symbol": [{"symbol": s, "gain_krw": round(g)} for s, g in sorted(by_sym.items(), key=lambda x: -abs(x[1]))],
        "taxable_krw": round(taxable), "tax_krw": round(tax),
        "deduction_left_krw": round(max(0.0, DEDUCTION_KRW - max(realized, 0.0))),
        "unrealized_krw": round(unrealized), "tax_if_sold_krw": round(tax_all),
        "estimated_fx": guessed,
    }


def won(v: float) -> str:
    """1234567 -> '₩1,234,567'."""
    return f"₩{round(v):,}" if v >= 0 else f"-₩{round(-v):,}"


def from_paper(paper: Path, year: int | None = None) -> dict | None:
    """estimate() from paper/state.json and trades.jsonl. Never raises."""
    try:
        state = json.loads((paper / "state.json").read_text(encoding="utf-8-sig"))
        trades = []
        tp = paper / "trades.jsonl"
        if tp.exists():
            for line in tp.read_text(encoding="utf-8-sig").splitlines():
                try:
                    trades.append(json.loads(line))
                except ValueError:
                    continue
        return estimate(trades, state.get("positions") or [], year or datetime.now().astimezone().year,
                        (state.get("fx") or {}).get("usdkrw"))
    except Exception:  # noqa: BLE001 - an estimate must never break the caller
        return None
