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
    cfg = TraderConfig(state_dir=tmp_path / "live", max_order_usd=5_000, pause_file=tmp_path / "STOP")
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


# -- cheaper twins (cfg.trade_as) ----------------------------------------------
def _close():
    import numpy as np
    import pandas as pd
    from quant.trader.runner import DATA_TICKERS
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2015-01-01", "2026-10-08")
    return pd.DataFrame(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, (len(idx), len(DATA_TICKERS))), axis=0),
                        index=idx, columns=DATA_TICKERS)


TWINS = {"SPY": "SPYM", "QQQ": "QQQM", "GLD": "GLDM", "IWM": "VTWO"}


def test_traded_merges_weights_onto_twins():
    from quant.trader.runner import traded
    cfg = TraderConfig(trade_as=TWINS)
    assert traded({"QQQ": 0.5, "TQQQ": 0.25, "GLD": 0.1}, cfg) == {"QQQM": 0.5, "TQQQ": 0.25, "GLDM": 0.1}


def test_runner_orders_the_twins_with_their_own_prices(tmp_path):
    close = _close()
    cfg = TraderConfig(state_dir=tmp_path / "p", max_order_usd=50_000, trade_as=TWINS)
    r = Runner(SimBroker(tmp_path / "a.json", starting_cash=10_000.0), cfg)
    fetched = []
    r.trade_fetch = lambda names: (fetched.append(names), __import__("pandas").DataFrame(
        {t: [close[o].iloc[-1] / 4] for o, t in TWINS.items()}))[1]
    s = r.run(close=close, now=datetime(2026, 10, 8, 14, tzinfo=timezone.utc), execute=False)
    syms = {o["symbol"] for o in s["planned_orders"]}
    assert syms and not syms & set(TWINS) and fetched == [sorted(TWINS.values())]
    for o in s["planned_orders"]:
        if o["symbol"] in TWINS.values():
            orig = next(k for k, v in TWINS.items() if v == o["symbol"])
            assert o["ref_price"] == pytest.approx(close[orig].iloc[-1] / 4, abs=0.01)


def test_runner_skips_trading_without_twin_prices(tmp_path):
    import pandas as pd
    cfg = TraderConfig(state_dir=tmp_path / "p", max_order_usd=50_000, trade_as=TWINS)
    r = Runner(SimBroker(tmp_path / "a.json", starting_cash=10_000.0), cfg)
    r.trade_fetch = lambda names: pd.DataFrame()
    s = r.run(close=_close(), now=datetime(2026, 10, 8, 14, tzinfo=timezone.utc))
    assert "가격을 받지 못해" in " ".join(a["message"] for a in s["alerts"])
    assert r.broker.positions() == {}


# -- budget set in Mintory, protected personal dollars --------------------------
def test_protected_dollars_are_never_spent(tmp_path):
    inner = LaterFillBroker(cash=1_000.0)          # $300 of it is the owner's own
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=714.0, keep_usd=300.0)
    assert b.cash_usd() == 700.0
    res = b.place_order("IEF", "buy", 8, 90.0)     # $720 would dip into the owner's $300
    assert not res["ok"] and "내 달러 보호" in res["msg"] and inner.orders == []
    assert b.place_order("IEF", "buy", 7, 90.0)["ok"]


def test_settings_saved_from_mintory_win_over_env(tmp_path):
    from quant.trader.budget import read_settings, save_settings
    rec = save_settings(tmp_path, "1,000,000", "500")
    assert rec["budget_krw"] == 1_000_000 and rec["keep_usd"] == 500.0
    assert read_settings(tmp_path)["budget_krw"] == 1_000_000
    assert budget_setting({"APP_BUDGET_KRW": "3000000"}, read_settings(tmp_path)) == ("APP_BUDGET_KRW", 1_000_000.0)
    for bad in ("abc", "-5", "5000"):
        with pytest.raises(ValueError):
            save_settings(tmp_path, bad, 0)


def test_run_trader_uses_mintory_settings(monkeypatch, tmp_path):
    from quant.trader import runner
    from quant.trader.budget import save_settings
    from scripts import run_trader
    monkeypatch.setattr(runner, "fetch_usdkrw", lambda: 1_400.0)
    save_settings(tmp_path, 1_000_000, 200)
    inner = LaterFillBroker(cash=900.0)
    b = run_trader.app_budget(inner, tmp_path / "rehearsal", env={}, settings_dir=tmp_path)
    assert b.cash_usd() == pytest.approx(700.0)     # min($714.29, $900 - $200)
    # Same amount moved from .env to Mintory: no change to the ledger.
    again = run_trader.app_budget(inner, tmp_path / "rehearsal", env={"APP_BUDGET_KRW": "1000000"}, settings_dir=tmp_path)
    assert again.changed == ""


# -- review 2026-11-02: edge cases of a shared real account -----------------------
def test_resent_order_is_not_counted_twice(tmp_path):
    inner = LaterFillBroker(cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("IEF", "buy", 5, 90.0)          # Kiwoom didn't answer...
    b.place_order("IEF", "buy", 5, 90.0)          # ...so the runner sent it again
    inner.orders = inner.orders[:1]               # but only the first one existed
    inner.fill()
    assert b.positions()["IEF"]["qty"] == 5
    assert b.cash_usd() == pytest.approx(1_000 - 450 * 1.001, abs=0.01)
    assert sum(o["qty"] for o in b.led["pending"]) == 5    # the resend waits, then expires


class ExactBroker(LaterFillBroker):
    """Also answers like Kiwoom's ust21150 (fills by order number) and ust21100 (dividends)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.done = {}
        self.divs = []
        self.no = 0

    def place_order(self, ticker, side, qty, ref_price, **kw):
        self.no += 1
        self.orders.append((ticker, side, qty, ref_price, f"{self.no:09d}"))
        return {"ok": True, "status": "pending", "order_no": f"{self.no:09d}"}

    def fill(self, price_move=1.0):
        for t, side, qty, p, no in self.orders:
            value = qty * p * price_move
            self.pos[t] = self.pos.get(t, 0) + (qty if side == "buy" else -qty)
            self.cash += -value * (1 + self.fee) if side == "buy" else value * (1 - self.fee)
            self.done[no.lstrip("0")] = {"symbol": t, "side": side, "filled": qty, "price": p * price_move,
                                         "left": 0, "cancelled": 0}
        self.orders = []

    def order_fills(self, dates):
        return dict(self.done)

    def dividends(self, start, end):
        return list(self.divs)


def test_owner_trading_the_same_etf_stays_the_owners(tmp_path):
    inner = ExactBroker(cash=5_000.0, positions={"QQQM": 4})
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("QQQM", "buy", 2, 280.0)
    inner.fill()
    assert b.positions()["QQQM"]["qty"] == 2
    inner.pos["QQQM"] += 3                        # the owner buys 3 more by hand
    inner.cash -= 840.0
    assert b.positions()["QQQM"]["qty"] == 2 and b.led["outside"]["QQQM"] == 7
    assert b.cash_usd() == pytest.approx(1_000 - 560 * 1.001, abs=0.01)
    # The owner buys 1 in the same minutes an app sell fills: Kiwoom's order
    # record says the app sold 2, so the owner's buy stays the owner's.
    b.place_order("QQQM", "sell", 2, 300.0)
    inner.fill()
    inner.pos["QQQM"] += 1
    inner.cash -= 300.0
    assert b.positions() == {} and inner.pos["QQQM"] == 8
    assert b.led["outside"]["QQQM"] == 8


def test_without_order_records_a_same_minute_owner_trade_is_netted(tmp_path):
    """Fallback (no order record): only the net change is visible, documented limit."""
    inner = LaterFillBroker(cash=5_000.0, positions={"QQQM": 4})
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("QQQM", "buy", 2, 280.0)
    inner.fill(); b.positions()
    b.place_order("QQQM", "sell", 2, 300.0)
    inner.fill()
    inner.pos["QQQM"] += 1
    assert b.positions()["QQQM"]["qty"] == 1      # 1 of the app's 2 looks unsold for now


def test_exact_fill_price_and_late_record(tmp_path):
    inner = ExactBroker(cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("TQQQ", "buy", 10, 70.0)
    rec = b.led["pending"][0]
    assert rec["order_no"] == "1" and len(rec["dates"]) == 2
    inner.fill(price_move=0.995)
    done = inner.done
    inner.done = {}                               # Kiwoom's record not there yet: balance decides
    assert b.positions()["TQQQ"]["qty"] == 10
    inner.done = done                             # the record shows up later: not counted twice
    assert b.positions()["TQQQ"]["qty"] == 10
    assert b.cash_usd() == pytest.approx(1_000 - 700 * 0.995 * 1.001, abs=0.01)


def test_dividends_on_app_shares_are_added(tmp_path):
    inner = ExactBroker(cash=5_000.0, positions={"IEF": 6})      # owner holds 6 IEF
    t, now = clock()
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0, now=now)
    b.place_order("IEF", "buy", 4, 90.0)
    inner.fill(); b.positions()
    cash = b.cash_usd()
    inner.divs = [{"id": "20261110-1-IEF", "date": "20261110", "symbol": "IEF", "usd": 3.0}]
    inner.cash += 3.0
    b.positions()                                 # already checked today
    assert b.cash_usd() == cash
    t["now"] += timedelta(days=1)
    b.positions()
    assert b.cash_usd() == pytest.approx(cash + 1.2, abs=0.01)  # 4 of 10 shares are the app's
    t["now"] += timedelta(days=1)
    b.positions()                                 # same dividend seen again: not added twice
    assert b.cash_usd() == pytest.approx(cash + 1.2, abs=0.01)


def test_owner_buys_more_while_app_buy_is_pending(tmp_path):
    inner = LaterFillBroker(cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("IEF", "buy", 3, 90.0)
    inner.fill()
    inner.pos["IEF"] += 10                        # owner's own 10 at the same moment
    inner.cash -= 900.0
    assert b.positions()["IEF"]["qty"] == 3 and b.led["outside"]["IEF"] == 10


def test_crash_during_send_still_counts_the_fill(tmp_path):
    class Flaky(LaterFillBroker):
        def place_order(self, *a, **kw):
            super().place_order(*a, **kw)
            raise RuntimeError("connection dropped after sending")
    inner = Flaky(cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    res = b.place_order("IEF", "buy", 4, 90.0)
    assert res["status"] == "pending"
    # A brand-new process (after a restart) reads the same ledger.
    inner.fill()
    again = BudgetBroker(inner, tmp_path / "budget.json")
    assert again.positions()["IEF"]["qty"] == 4


def test_refused_order_leaves_nothing_pending(tmp_path):
    class Refuse(LaterFillBroker):
        def place_order(self, *a, **kw):
            return {"ok": False, "msg": "거부"}
    b = BudgetBroker(Refuse(cash=5_000.0), tmp_path / "budget.json", budget_usd=1_000.0)
    assert not b.place_order("IEF", "buy", 4, 90.0)["ok"] and b.led["pending"] == []


def test_fill_at_a_better_price_uses_the_real_cost(tmp_path):
    inner = LaterFillBroker(cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("TQQQ", "buy", 10, 70.0)
    inner.fill(price_move=0.99)                   # filled 1% under the order price
    b.positions()
    assert b.cash_usd() == pytest.approx(1_000 - 700 * 0.99 * 1.001, abs=0.01)


def test_month_of_trading_keeps_app_and_owner_apart(tmp_path):
    """Buy, owner deposit and own trades, a gain, a loss, rebalance: the app's
    value is always its budget plus its own results, and the owner's are untouched."""
    inner = LaterFillBroker(cash=745.03 + 2_000.0, positions={"QQQM": 5}, fee=0.0025)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=745.03)
    px = {"QQQM": 280.0, "IEF": 92.0, "TQQQ": 62.0}
    b.place_order("QQQM", "buy", 1, px["QQQM"]); b.place_order("TQQQ", "buy", 4, px["TQQQ"]); b.place_order("IEF", "buy", 2, px["IEF"])
    inner.fill(); b.positions()
    spent = (280 + 248 + 184) * 1.0025
    assert b.cash_usd() == pytest.approx(745.03 - spent, abs=0.01)
    inner.cash += 5_000.0                         # owner deposits
    inner.pos["QQQM"] += 2; inner.cash -= 560.0   # owner buys QQQM
    assert b.cash_usd() == pytest.approx(745.03 - spent, abs=0.01)
    b.place_order("TQQQ", "sell", 4, 75.0)        # app sells TQQQ at a gain
    inner.fill(); b.positions()
    cash = 745.03 - spent + 300 * 0.9975
    assert b.cash_usd() == pytest.approx(cash, abs=0.01)
    b.place_order("IEF", "buy", 2, 91.0)
    inner.fill(); b.positions()
    cash -= 182 * 1.0025
    assert b.cash_usd() == pytest.approx(cash, abs=0.01)
    assert b.positions() == {"QQQM": {"qty": 1, "avg_price": pytest.approx(280 * 1.0025, abs=0.01)},
                             "IEF": {"qty": 4, "avg_price": pytest.approx((184 + 182) * 1.0025 / 4, abs=0.01)}}
    assert inner.pos["QQQM"] == 8 and b.led["outside"]["QQQM"] == 7


def test_kiwoom_order_fill_and_dividend_parsing():
    from quant.trader.brokers import KiwoomBroker, ReadOnlyBroker
    k = KiwoomBroker("k", "s", mock=False)
    sent = []

    def call(path, api_id, body, **kw):
        sent.append((api_id, body))
        if api_id == "ust21150":
            return {"result_lsit": [{"ord_no": "000000252", "stk_cd": "QQQM", "slby_tp_nm": "매수",
                                     "ord_qty": "000000000002", "cntr_qty": "000000000002",
                                     "cntr_uv": "283.1147", "ord_remnq": "000000000000",
                                     "cncl_qty": "000000000000"}]}
        return {"result_list": [{"deal_dt": "20261110", "deal_no": "000000007", "stk_cd": "IEF",
                                 "fc_exct_amt": "2.55", "fc_deal_amt": "3.00", "frgn_pay_txam": "0.45"}]}
    k.call = call
    assert k.order_fills(["20261109"]) == {"252": {"symbol": "QQQM", "side": "buy", "filled": 2,
                                                    "price": 283.1147, "left": 0, "cancelled": 0}}
    assert sent[0][1]["ord_dt"] == "20261109"
    assert ReadOnlyBroker(k).dividends("20261101", "20261110") == [
        {"id": "20261110-7-IEF", "date": "20261110", "symbol": "IEF", "usd": 2.55}]
    assert sent[1] == ("ust21100", {"strt_dt": "20261101", "end_dt": "20261110", "tp": "8", "stex_tp": "",
                                    "stk_cd": "", "krw_repl_skip_yn": "Y"})


def test_budget_in_dollars_after_the_exchange(tmp_path):
    from quant.trader.budget import read_settings, save_settings
    save_settings(tmp_path, "$737.50", "")
    assert budget_setting({}, read_settings(tmp_path)) == ("APP_BUDGET_USD", 737.5)
    inner = LaterFillBroker(cash=900.0)
    led = tmp_path / "budget.json"
    BudgetBroker(inner, led, setting=("APP_BUDGET_KRW", 1_000_000.0), usdkrw=lambda: 1_342.23)
    b = BudgetBroker(inner, led, setting=("APP_BUDGET_USD", 737.5))   # switched to the dollars received
    assert b.led["cash"] == pytest.approx(737.5, abs=0.01) and b.led["start_usd"] == pytest.approx(737.5, abs=0.01)
    with pytest.raises(ValueError):
        save_settings(tmp_path, "$5", "")
