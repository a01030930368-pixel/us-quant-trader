"""App budget: the app trades only its own slice of a shared brokerage account.

The owner keeps personal investments in the same Kiwoom account. BudgetBroker
wraps the real broker and shows the runner a virtual account made of only:

- the app's cash: the starting budget, plus what the app's sales brought in,
  minus what its buys cost, and
- the app's shares: only what the app itself bought, never the owner's.

So with a 1,000,000 KRW budget the app rebalances 1,000,000 KRW worth; if its
trades grow that to 1,200,000 it uses 1,200,000, and after a loss to 800,000
it uses 800,000. The owner's other holdings and cash are never counted,
bought or sold, even when the owner holds the same ETF.

Kiwoom accepts a limit order and the fill only shows up later in the balance,
so each order the app sends is kept as "pending" together with the account's
quantity of that symbol at the moment the order was sent. When the balance
moves in the order's direction, that move (up to the order's size) is counted
as the app's fill. Limit orders last one session, so a pending order older
than PENDING_HOURS is dropped as unfilled. The owner should not trade the same
symbol by hand while an app order is pending (the app's order nights), or the
two fills can't be told apart.

Ledger: paper/<state>/budget.json (written atomically).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

PENDING_HOURS = 20       # a day order that hasn't filled by then never will
FEE_ESTIMATE = 0.0025    # used only when the cash change can't be attributed


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BudgetBroker:
    def __init__(self, inner, ledger_path: Path, budget_usd: float | None = None, now=_now,
                 setting: tuple[str, float] | None = None, usdkrw=None):
        """budget_usd: a fixed starting budget. setting: (".env key", value) as read
        by budget_setting(); when it differs from the one the ledger was made
        with, the difference goes into (or comes out of) the app's cash.
        usdkrw: a function giving today's USD/KRW, needed for a KRW setting."""
        self.inner = inner
        self.path = Path(ledger_path)
        self.now = now
        self.changed = ""
        self.led = self._load()
        if setting and (not self.led or self.led.get("setting") != list(setting)):
            key, value = setting
            prev = self.led.get("setting") if self.led else None
            rate = None
            if key == "APP_BUDGET_KRW":
                rate = usdkrw() if usdkrw else None
                if not rate:
                    raise RuntimeError("환율을 못 받아 원화 사용한도를 달러로 바꾸지 못했어요. 잠시 뒤 다시 해 주세요")
            if not self.led:
                budget_usd = value if rate is None else round(value / rate, 2)
            else:
                # Same unit: only the change is converted (today's rate for won).
                # Unit switched: the new amount replaces the old one in dollars.
                if prev and prev[0] == key:
                    diff = value - prev[1] if rate is None else (value - prev[1]) / rate
                else:
                    diff = (value if rate is None else value / rate) - self.led.get("setting_usd", self.led["start_usd"])
                diff = round(diff, 2)
                self.led["cash"] = round(self.led["cash"] + diff, 2)
                self.led["start_usd"] = round(self.led["start_usd"] + diff, 2)
                self.led["setting"] = list(setting)
                self.led["setting_usd"] = round(self.led.get("setting_usd", 0.0) + diff, 2)
                self.led.setdefault("changes", []).append(
                    {"time": self.now().isoformat(timespec="seconds"), "setting": list(setting), "usd": diff})
                self.changed = f"앱 사용한도 변경: {'+' if diff >= 0 else '-'}${abs(diff):,.2f} (앱 현금 ${self.led['cash']:,.2f})"
                self._save()
        if not self.led:
            if not budget_usd or budget_usd <= 0:
                raise RuntimeError("앱 사용한도(APP_BUDGET_KRW 또는 APP_BUDGET_USD)가 .env 에 없어요")
            self.led = {"start_usd": round(float(budget_usd), 2), "cash": round(float(budget_usd), 2),
                        "positions": {}, "pending": [],
                        "created": self.now().isoformat(timespec="seconds")}
            if setting:
                self.led["setting"], self.led["setting_usd"] = list(setting), round(float(budget_usd), 2)
            self.changed = f"앱 사용한도 ${budget_usd:,.2f} 로 장부를 새로 만들었어요"
            self._save()
        self._actual: dict | None = None

    # -- ledger file -----------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.led, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- syncing fills ---------------------------------------------------
    def _sync(self) -> dict:
        """Read the real account, count the app's fills, return the real positions."""
        actual = self.inner.positions()
        pend = self.led.get("pending") or []
        if pend:
            fills, keep = [], []
            cutoff = self.now() - timedelta(hours=PENDING_HOURS)
            for o in pend:
                held = int((actual.get(o["symbol"]) or {}).get("qty", 0))
                move = held - int(o["base_qty"])
                got = max(0, min(move if o["side"] == "buy" else -move, int(o["qty"])))
                if got:
                    fills.append((o, got))
                    o["qty"] = int(o["qty"]) - got
                    o["base_qty"] = int(o["base_qty"]) + (got if o["side"] == "buy" else -got)
                if o["qty"] > 0 and datetime.fromisoformat(o["time"]) > cutoff:
                    keep.append(o)
            cash_now = self.inner.cash_usd()
            if fills:
                self._book(fills, cash_now)
            self.led["pending"] = keep
            self.led["cash_seen"] = round(cash_now, 2)
            self._save()
        self._actual = actual
        return actual

    def _book(self, fills: list, cash_now: float) -> None:
        """Add fills to the ledger. When the real cash moved by about what these
        fills should cost (within 5%), the gap is the real fee and is shared out
        by size; otherwise (a deposit, the owner's own trade) fees are estimated."""
        gross = [got * float(o["price"]) for o, got in fills]
        expected = sum(-g if o["side"] == "buy" else g for (o, _), g in zip(fills, gross))
        seen = self.led.get("cash_seen")
        fee_total = None
        if seen is not None:
            fee_total = expected - (cash_now - float(seen))
            if not (-0.001 * sum(gross) <= fee_total <= 0.05 * sum(gross)):
                fee_total = None
        for (o, got), g in zip(fills, gross):
            fee = fee_total * g / sum(gross) if fee_total is not None else g * FEE_ESTIMATE
            self._fill(o, got, g, fee)

    def _fill(self, o: dict, qty: int, gross: float, fee: float) -> None:
        pos = self.led["positions"]
        mine = pos.get(o["symbol"]) or {"qty": 0, "avg_price": 0.0}
        if o["side"] == "buy":
            cost = gross + fee
            new_qty = mine["qty"] + qty
            mine["avg_price"] = round((mine["qty"] * mine["avg_price"] + cost) / new_qty, 4)
            mine["qty"] = new_qty
            self.led["cash"] = round(self.led["cash"] - cost, 2)
        else:
            mine["qty"] = max(0, mine["qty"] - qty)
            self.led["cash"] = round(self.led["cash"] + gross - fee, 2)
        if mine["qty"]:
            pos[o["symbol"]] = mine
        else:
            pos.pop(o["symbol"], None)
        self.led.setdefault("fills", []).append(
            {"time": self.now().isoformat(timespec="seconds"), "symbol": o["symbol"],
             "side": o["side"], "qty": qty, "price": float(o["price"]), "fee": round(fee, 2)})
        self.led["fills"] = self.led["fills"][-200:]

    # -- broker surface --------------------------------------------------
    def positions(self) -> dict[str, dict]:
        """The app's shares only, never more than the account really holds."""
        actual = self._sync()
        out = {}
        for sym, p in self.led["positions"].items():
            qty = min(int(p["qty"]), int((actual.get(sym) or {}).get("qty", 0)))
            if qty > 0:
                out[sym] = {"qty": qty, "avg_price": float(p["avg_price"])}
        return out

    def cash_usd(self) -> float:
        """The app's cash, never more than the account's real dollars."""
        if self._actual is None:
            self._sync()
        real = self.inner.cash_usd()
        return round(max(0.0, min(float(self.led["cash"]), real)), 2)

    def place_order(self, ticker: str, side: str, qty: int, ref_price: float, **kw) -> dict:
        if side == "sell":
            mine = int((self.led["positions"].get(ticker) or {}).get("qty", 0))
            if qty > mine:
                return {"ok": False, "msg": f"앱이 산 {ticker} 는 {mine}주뿐이라 {qty}주를 팔 수 없어요 (내 다른 주식은 안 건드려요)"}
        else:
            need = qty * ref_price
            if need > float(self.led["cash"]) * 1.02:
                return {"ok": False, "msg": f"앱 사용한도 안의 현금 ${self.led['cash']:,.2f}로는 ${need:,.2f} 매수를 못 해요"}
        base = int((self.inner.positions().get(ticker) or {}).get("qty", 0))
        base_cash = self.inner.cash_usd()
        res = self.inner.place_order(ticker, side, qty, ref_price, **kw)
        if res.get("ok") or res.get("status") == "pending":
            self.led.setdefault("pending", []).append(
                {"symbol": ticker, "side": side, "qty": int(qty), "price": float(ref_price),
                 "base_qty": base, "base_cash": round(base_cash, 2),
                 "time": self.now().isoformat(timespec="seconds")})
            self.led["cash_seen"] = round(base_cash, 2)
            self._save()
        return res

    def summary(self) -> dict:
        return {"start_usd": self.led["start_usd"], "cash": self.led["cash"],
                "positions": self.led["positions"], "pending": len(self.led.get("pending") or [])}


def budget_setting(env: dict) -> tuple[str, float] | None:
    """(".env key", amount) for the app budget: APP_BUDGET_USD wins over
    APP_BUDGET_KRW. None when neither holds a positive number."""
    for key in ("APP_BUDGET_USD", "APP_BUDGET_KRW"):
        raw = str(env.get(key, "") or "").replace(",", "").replace("$", "").replace("원", "").strip()
        try:
            v = float(raw) if raw else 0.0
        except ValueError:
            raise RuntimeError(f".env 의 {key} 를 숫자로 읽지 못했어요: {raw}") from None
        if v > 0:
            return key, v
    return None
