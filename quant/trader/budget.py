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

Kiwoom accepts a limit order and the fill only shows up later in the balance.
The ledger keeps, per symbol, how many shares are the owner's ("outside" =
account quantity minus the app's). At every balance read, a change in the
account quantity goes to the app's pending orders on that symbol (in their
direction, up to their size, oldest first) and anything left over is the
owner's own trade. So a resent order or two orders on one symbol can never
count one fill twice. A pending order is written to the ledger before it is
sent, so a crash mid-send can't leave an app fill looking like the owner's.
Limit orders last one session, so a pending order older than PENDING_HOURS
is dropped as unfilled. The owner should not trade the same symbol by hand in
the minutes an app order is pending, or the two fills can't be told apart.

The app's cash moves by what each fill really cost: the change in the
account's dollars when it matches the fills (price slippage and fees
included), else the order price plus an estimated fee. Dividends paid on the
app's shares are not counted (they can't be told from the owner's deposits).

Ledger: paper/<state>/budget.json (written atomically).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
PENDING_HOURS = 20       # a day order that hasn't filled by then never will
FEE_ESTIMATE = 0.0025    # used only when the cash change can't be attributed


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BudgetBroker:
    def __init__(self, inner, ledger_path: Path, budget_usd: float | None = None, now=_now,
                 setting: tuple[str, float] | None = None, usdkrw=None, keep_usd: float = 0.0):
        """budget_usd: a fixed starting budget. setting: (".env key", value) as read
        by budget_setting(); when it differs from the one the ledger was made
        with, the difference goes into (or comes out of) the app's cash.
        usdkrw: a function giving today's USD/KRW, needed for a KRW setting.
        keep_usd: the owner's own dollars the app must never spend; the app
        only buys while the account keeps at least this much USD."""
        self.inner = inner
        self.keep_usd = max(0.0, float(keep_usd or 0.0))
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
    def _exact_fills(self, pend: list) -> dict:
        """Kiwoom's own fill records for the pending orders, by order number
        (ust21150), or {} when the broker can't tell (then balances decide)."""
        if not hasattr(self.inner, "order_fills"):
            return {}
        dates = sorted({d for o in pend if o.get("order_no") for d in o.get("dates") or []})
        if not dates:
            return {}
        try:
            return self.inner.order_fills(dates) or {}
        except Exception:  # noqa: BLE001 - fall back to balance changes
            return {}

    def _sync(self) -> dict:
        """Read the real account, count the app's fills, return the real positions."""
        actual = self.inner.positions()
        pend = self.led.get("pending") or []
        outside = self.led.setdefault("outside", {})
        mine = self.led["positions"]
        fills, changed = [], False
        # 1) Orders Kiwoom reports by order number: exact shares and price.
        exact = self._exact_fills(pend)
        known: dict[str, int] = {}            # signed shares per symbol counted in step 1
        for o in pend:
            info = exact.get(str(o.get("order_no") or ""))
            o["exact"] = info is not None
            if info is None:
                continue
            new = int(info["filled"]) - int(o.get("booked", 0))
            if new > 0:
                fills.append((o, new, float(info["price"]) or float(o["price"])))
                o["booked"] = int(info["filled"])
                known[o["symbol"]] = known.get(o["symbol"], 0) + (new if o["side"] == "buy" else -new)
            o["qty"] = int(info["left"])
        # 2) Everything else from balance changes; what no app order explains is the owner's.
        for sym in sorted(set(actual) | set(outside) | set(mine) | {o["symbol"] for o in pend}):
            held = int((actual.get(sym) or {}).get("qty", 0))
            app = int((mine.get(sym) or {}).get("qty", 0))
            if sym not in outside:          # first sight: whatever isn't the app's is the owner's
                outside[sym] = max(0, held - app)
                changed = True
                continue
            move = held - app - int(outside[sym]) - known.get(sym, 0)
            for o in (o for o in pend if o["symbol"] == sym and o["qty"] > 0 and not o["exact"]):
                want = move if o["side"] == "buy" else -move
                got = max(0, min(want, int(o["qty"])))
                if got:
                    fills.append((o, got, float(o["price"])))
                    o["qty"] = int(o["qty"]) - got
                    o["booked"] = int(o.get("booked", 0)) + got   # so step 1 never counts it again
                    move -= got if o["side"] == "buy" else -got
            if move:
                outside[sym] = max(0, int(outside[sym]) + move)
                changed = True
        if pend or fills:
            cutoff = self.now() - timedelta(hours=PENDING_HOURS)
            self.led["pending"] = [o for o in pend if o["qty"] > 0 and datetime.fromisoformat(o["time"]) > cutoff]
            cash_now = self.inner.cash_usd()
            if fills:
                self._book(fills, cash_now)
            self.led["cash_seen"] = round(cash_now, 2)
            changed = True
        waiting = {o["symbol"] for o in self.led.get("pending") or []}
        for sym in [k for k, v in outside.items() if not v and k not in actual and k not in mine and k not in waiting]:
            outside.pop(sym)
        changed |= self._dividends(actual)
        if changed:
            self._save()
        self._actual = actual
        return actual

    def _dividends(self, actual: dict) -> bool:
        """Once a day: add the app's share of ETF dividends (after tax) to its
        cash, split by the app's part of the account's shares of that ETF."""
        if not hasattr(self.inner, "dividends"):
            return False
        today = self.now().astimezone(KST).date()
        if self.led.get("div_checked") == today.isoformat():
            return False
        start = self.led.get("div_from") or self.led.get("created", today.isoformat())[:10]
        try:
            rows = self.inner.dividends(start.replace("-", ""), today.strftime("%Y%m%d")) or []
        except Exception:  # noqa: BLE001 - try again tomorrow
            return False
        seen = self.led.setdefault("div_seen", [])
        for r in rows:
            if r["id"] in seen:
                continue
            seen.append(r["id"])
            app = int((self.led["positions"].get(r["symbol"]) or {}).get("qty", 0))
            held = int((actual.get(r["symbol"]) or {}).get("qty", 0))
            if app and held:
                usd = round(float(r["usd"]) * min(1.0, app / held), 2)
                self.led["cash"] = round(self.led["cash"] + usd, 2)
                self.led.setdefault("dividend_log", []).append(
                    {"date": r["date"], "symbol": r["symbol"], "usd": usd})
        self.led["div_seen"] = seen[-500:]
        self.led["div_checked"] = today.isoformat()
        # look a week back next time, for late postings (ids keep it from counting twice)
        self.led["div_from"] = (today - timedelta(days=7)).isoformat()
        return True

    def _book(self, fills: list, cash_now: float) -> None:
        """Add fills to the ledger. When the real cash moved by about what these
        fills should cost (fees and price slippage, -3% to +5%), the gap is the
        real cost difference and is shared out by size; otherwise (a deposit,
        the owner's own trade at the same moment) fees are estimated."""
        gross = [got * price for o, got, price in fills]
        expected = sum(-g if o["side"] == "buy" else g for (o, _, _), g in zip(fills, gross))
        seen = self.led.get("cash_seen")
        fee_total = None
        if seen is not None:
            fee_total = expected - (cash_now - float(seen))
            # Marketable limits fill up to 1.5% from the order price, plus fees.
            if not (-0.03 * sum(gross) <= fee_total <= 0.05 * sum(gross)):
                fee_total = None
        for (o, got, price), g in zip(fills, gross):
            fee = fee_total * g / sum(gross) if fee_total is not None else g * FEE_ESTIMATE
            self._fill(o, got, g, fee, price)

    def _fill(self, o: dict, qty: int, gross: float, fee: float, price: float) -> None:
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
             "side": o["side"], "qty": qty, "price": round(price, 4), "fee": round(fee, 2)})
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
        """The app's cash, never more than the account's real dollars minus the
        owner's protected dollars (keep_usd)."""
        if self._actual is None:
            self._sync()
        return self._available(self.inner.cash_usd())

    def _available(self, real: float) -> float:
        return round(max(0.0, min(float(self.led["cash"]), real - self.keep_usd)), 2)

    def place_order(self, ticker: str, side: str, qty: int, ref_price: float, **kw) -> dict:
        self._sync()                       # owner's shares and earlier fills up to date
        if side == "sell":
            mine = int((self.led["positions"].get(ticker) or {}).get("qty", 0))
            if qty > mine:
                return {"ok": False, "msg": f"앱이 산 {ticker} 는 {mine}주뿐이라 {qty}주를 팔 수 없어요 (내 다른 주식은 안 건드려요)"}
        base_cash = self.inner.cash_usd()
        if side == "buy":
            need, free = qty * ref_price, self._available(base_cash)
            if need > free * 1.02:
                why = (f"내 달러 보호 ${self.keep_usd:,.2f}를 빼면 앱이 쓸 수 있는 돈이 ${free:,.2f}뿐이라"
                       if free < float(self.led["cash"]) else f"앱 사용한도 안의 현금 ${self.led['cash']:,.2f}로는")
                return {"ok": False, "msg": f"{why} ${need:,.2f} 매수를 못 해요"}
        outside = self.led.setdefault("outside", {})
        if ticker not in outside:          # the owner's shares before this order
            held = int((self._actual.get(ticker) or {}).get("qty", 0))
            outside[ticker] = max(0, held - int((self.led["positions"].get(ticker) or {}).get("qty", 0)))
        # Written before sending: if the PC dies mid-send, the fill still counts as the app's.
        day = self.now().astimezone(KST).date()
        rec = {"symbol": ticker, "side": side, "qty": int(qty), "price": float(ref_price),
               "time": self.now().isoformat(timespec="seconds"),
               # Kiwoom's order date may be the Korean or the US date
               "dates": [(day - timedelta(days=1)).strftime("%Y%m%d"), day.strftime("%Y%m%d")]}
        self.led.setdefault("pending", []).append(rec)
        self.led["cash_seen"] = round(base_cash, 2)
        self._save()
        try:
            res = self.inner.place_order(ticker, side, qty, ref_price, **kw)
        except Exception:
            res = {"ok": False, "status": "pending", "msg": "주문 중 오류 - 접수 여부 불확실, 잔고로 확인"}
        if not (res.get("ok") or res.get("status") == "pending"):
            self.led["pending"].remove(rec)     # refused: nothing will fill
            self._save()
        elif res.get("order_no"):
            rec["order_no"] = str(res["order_no"]).strip().lstrip("0")
            self._save()
        return res

    def summary(self) -> dict:
        return {"start_usd": self.led["start_usd"], "cash": self.led["cash"],
                "positions": self.led["positions"], "pending": len(self.led.get("pending") or [])}


SETTINGS = "app_budget.json"   # set from Mintory; wins over .env


def read_settings(folder: Path) -> dict:
    try:
        return json.loads((Path(folder) / SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(folder: Path, budget_krw, keep_usd, now=_now) -> dict:
    """Validate and store the budget set in Mintory. Raises ValueError with a
    message for the screen."""
    def num(v, name):
        raw = str(v if v is not None else "").replace(",", "").replace("$", "").replace("원", "").strip()
        try:
            x = float(raw) if raw else 0.0
        except ValueError:
            raise ValueError(f"{name}는 숫자로 적어 주세요") from None
        if x < 0:
            raise ValueError(f"{name}는 0 이상이어야 해요")
        return x
    keep = round(num(keep_usd, "내 달러 보호"), 2)
    if str(budget_krw or "").strip().startswith("$"):
        # Dollars as received after the exchange, e.g. "$737.50": exact, no rate guess.
        usd = num(budget_krw, "앱 사용한도")
        if usd and usd < 70:
            raise ValueError("앱 사용한도는 $70 이상으로 적어 주세요")
        if usd > 1_000_000:
            raise ValueError("앱 사용한도가 너무 커요. 숫자를 확인해 주세요")
        rec = {"budget_usd": round(usd, 2), "keep_usd": keep}
    else:
        krw = num(budget_krw, "앱 사용한도")
        if krw and krw < 100_000:
            raise ValueError("앱 사용한도는 10만 원 이상으로 적어 주세요")
        if krw > 1_000_000_000:
            raise ValueError("앱 사용한도가 너무 커요. 숫자를 확인해 주세요")
        rec = {"budget_krw": round(krw), "keep_usd": keep}
    rec["updated"] = now().isoformat(timespec="seconds")
    path = Path(folder) / SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return rec


def budget_setting(env: dict, settings: dict | None = None) -> tuple[str, float] | None:
    """(key, amount) for the app budget. Mintory's setting (app_budget.json)
    wins; then .env APP_BUDGET_USD, then APP_BUDGET_KRW. The Mintory amount is
    in won and uses the APP_BUDGET_KRW key, so moving the same number from
    .env to Mintory changes nothing. None when nothing positive is set."""
    if settings and float(settings.get("budget_usd") or 0) > 0:
        return "APP_BUDGET_USD", float(settings["budget_usd"])
    if settings and float(settings.get("budget_krw") or 0) > 0:
        return "APP_BUDGET_KRW", float(settings["budget_krw"])
    for key in ("APP_BUDGET_USD", "APP_BUDGET_KRW"):
        raw = str(env.get(key, "") or "").replace(",", "").replace("$", "").replace("원", "").strip()
        try:
            v = float(raw) if raw else 0.0
        except ValueError:
            raise RuntimeError(f".env 의 {key} 를 숫자로 읽지 못했어요: {raw}") from None
        if v > 0:
            return key, v
    return None
