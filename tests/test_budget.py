"""App budget: the app trades only its own slice of a shared real account."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from quant.trader.budget import BudgetBroker, budget_setting
from quant.trader.brokers import SimBroker
from quant.trader.runner import Runner, TraderConfig


class LaterFillBroker:
    """Like Kiwoom: an order is only accepted; it shows up in the balance once fill() runs."""

    def __init__(self, cash, positions=None, fee=0.001):
        self.cash = cash
        self.pos = dict(positions or {})
        self.orders = []
        self.fee = fee

    def positions(self):
        return {k: {"qty": v, "avg_price": 100.0} for k, v in self.pos.items() if v}

    def cash_usd(self):
        return self.cash

    def place_order(self, ticker, side, qty, ref_price, **kw):
        self.orders.append((ticker, side, qty, ref_price))
        return {"ok": True, "status": "pending", "order_no": str(len(self.orders))}

    def fill(self, price_move=1.0):
        for t, side, qty, p in self.orders:
            value = qty * p * price_move
            if side == "buy":
                self.pos[t] = self.pos.get(t, 0) + qty
                self.cash -= value * (1 + self.fee)
            else:
                self.pos[t] = self.pos.get(t, 0) - qty
                self.cash += value * (1 - self.fee)
        self.orders = []


def clock(start=datetime(2026, 11, 9, 14, tzinfo=timezone.utc)):
    t = {"now": start}
    return t, (lambda: t["now"])


def test_sees_only_its_budget_not_the_owners_money_or_shares(tmp_path):
    inner = LaterFillBroker(cash=5_000.0, positions={"QQQ": 10, "AAPL": 3})
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=750.0)
    assert b.positions() == {} and b.cash_usd() == 750.0


def test_fills_move_app_cash_and_shares_and_owner_holdings_stay_out(tmp_path):
    inner = LaterFillBroker(cash=5_000.0, positions={"QQQ": 10})
    t, now = clock()
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0, now=now)
    b.place_order("QQQ", "buy", 1, 600.0)
    assert b.positions() == {}                      # accepted, not filled yet
    inner.fill()
    assert b.positions()["QQQ"]["qty"] == 1         # the owner's 10 are not the app's
    assert b.cash_usd() == pytest.approx(1_000 - 600 * 1.001, abs=0.01)   # real fee from the cash change
    # Selling more than the app bought is refused before reaching the broker.
    res = b.place_order("QQQ", "sell", 5, 600.0)
    assert not res["ok"] and inner.orders == []
    b.place_order("QQQ", "sell", 1, 700.0)
    inner.fill()
    assert b.positions() == {} and inner.pos["QQQ"] == 10
    assert b.cash_usd() == pytest.approx(1_000 - 600.6 + 700 * 0.999, abs=0.01)


def test_gains_and_losses_change_what_the_app_rebalances(tmp_path):
    inner = LaterFillBroker(cash=10_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("TQQQ", "buy", 10, 90.0)
    inner.fill()
    b.positions()
    b.place_order("TQQQ", "sell", 10, 108.0)       # +20%
    inner.fill()
    b.positions()
    assert b.cash_usd() == pytest.approx(1_000 - 900.9 + 1078.92, abs=0.01)


def test_buy_over_the_app_cash_is_refused(tmp_path):
    inner = LaterFillBroker(cash=10_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=500.0)
    assert not b.place_order("SPY", "buy", 1, 680.0)["ok"] and inner.orders == []


def test_cash_is_capped_by_the_real_dollars(tmp_path):
    inner = LaterFillBroker(cash=200.0)            # the won wasn't exchanged yet
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=750.0)
    assert b.cash_usd() == 200.0


def test_shares_are_capped_by_what_the_account_holds(tmp_path):
    inner = LaterFillBroker(cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("IEF", "buy", 5, 90.0)
    inner.fill()
    b.positions()
    inner.pos["IEF"] = 2                            # the owner sold some by hand
    assert b.positions()["IEF"]["qty"] == 2


def test_partial_and_unfilled_orders(tmp_path):
    inner = LaterFillBroker(cash=5_000.0)
    t, now = clock()
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0, now=now)
    b.place_order("IEF", "buy", 6, 90.0)
    inner.orders = []
    inner.pos["IEF"] = 2                            # only 2 of 6 filled
    inner.cash -= 180.18
    assert b.positions()["IEF"]["qty"] == 2
    assert b.led["pending"][0]["qty"] == 4
    t["now"] += timedelta(hours=21)                 # the day order expired
    b.positions()
    assert b.led["pending"] == []
    assert b.cash_usd() == pytest.approx(1_000 - 180.18, abs=0.01)


def test_cash_change_that_isnt_the_fill_uses_an_estimated_fee(tmp_path):
    inner = LaterFillBroker(cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("IEF", "buy", 5, 90.0)
    inner.fill()
    inner.cash += 3_000.0                           # the owner deposited at the same time
    b.positions()
    assert b.cash_usd() == pytest.approx(1_000 - 450 * 1.0025, abs=0.01)


def test_ledger_survives_restarts(tmp_path):
    inner = LaterFillBroker(cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("IEF", "buy", 5, 90.0)
    again = BudgetBroker(inner, tmp_path / "budget.json")     # budget not needed any more
    inner.fill()
    assert again.positions()["IEF"]["qty"] == 5


def test_won_setting_and_changing_it_later(tmp_path):
    inner = LaterFillBroker(cash=5_000.0)
    path = tmp_path / "budget.json"
    b = BudgetBroker(inner, path, setting=("APP_BUDGET_KRW", 1_000_000.0), usdkrw=lambda: 1_400.0)
    assert b.cash_usd() == pytest.approx(714.29, abs=0.01)
    same = BudgetBroker(inner, path, setting=("APP_BUDGET_KRW", 1_000_000.0), usdkrw=lambda: 1_300.0)
    assert same.changed == "" and same.cash_usd() == pytest.approx(714.29, abs=0.01)
    more = BudgetBroker(inner, path, setting=("APP_BUDGET_KRW", 3_000_000.0), usdkrw=lambda: 1_250.0)
    assert more.cash_usd() == pytest.approx(714.29 + 1_600.0, abs=0.01) and "+$1,600.00" in more.changed
    with pytest.raises(RuntimeError):
        BudgetBroker(inner, tmp_path / "other.json", setting=("APP_BUDGET_KRW", 1.0), usdkrw=lambda: None)


def test_budget_setting_reads_env():
    assert budget_setting({}) is None
    assert budget_setting({"APP_BUDGET_KRW": "1,000,000"}) == ("APP_BUDGET_KRW", 1_000_000.0)
    assert budget_setting({"APP_BUDGET_KRW": "1000000", "APP_BUDGET_USD": "$700"}) == ("APP_BUDGET_USD", 700.0)
    with pytest.raises(RuntimeError):
        budget_setting({"APP_BUDGET_KRW": "백만"})


def test_runner_rebalances_only_the_budget(tmp_path):
    import numpy as np
    import pandas as pd
    from quant.trader.runner import DATA_TICKERS
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2015-01-01", "2026-10-08")
    close = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, (len(idx), len(DATA_TICKERS))), axis=0),
                         index=idx, columns=DATA_TICKERS)
    inner = SimBroker(tmp_path / "acct.json", starting_cash=50_000.0)
    inner.acct["positions"]["AAPL"] = 100           # the owner's own stock
    cfg = TraderConfig(state_dir=tmp_path / "live", max_order_usd=5_000)
    b = BudgetBroker(inner, cfg.state_dir / "budget.json", budget_usd=5_000.0)
    s = Runner(b, cfg, mode="kiwoom_live").run(close=close, now=datetime(2026, 10, 8, 14, tzinfo=timezone.utc))
    spent = 50_000.0 - inner.cash_usd()
    assert 4_000 < spent <= 5_000 and inner.positions()["AAPL"]["qty"] == 100
    assert "AAPL" not in b.positions()
    assert s["cash"] + sum(p["qty"] * p.get("last_price", p["avg_price"]) for p in s["positions"]) == pytest.approx(5_000, rel=0.02)


def test_run_trader_app_budget(monkeypatch, tmp_path):
    from quant.trader import runner
    from scripts import run_trader
    inner = LaterFillBroker(cash=5_000.0)
    assert run_trader.app_budget(inner, tmp_path, env={}) is inner
    monkeypatch.setattr(runner, "fetch_usdkrw", lambda: 1_400.0)
    b = run_trader.app_budget(inner, tmp_path, env={"APP_BUDGET_KRW": "1000000"})
    assert isinstance(b, BudgetBroker) and b.cash_usd() == pytest.approx(714.29, abs=0.01)
    assert json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))["start_usd"] == pytest.approx(714.29)
    # Once a ledger exists it stays in charge even if the .env line is removed.
    assert isinstance(run_trader.app_budget(inner, tmp_path, env={}), BudgetBroker)
