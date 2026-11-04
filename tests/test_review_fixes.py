"""Fixes from the pre-live review (2026-11-03): orders that expire unfilled,
limits that must never block sells, resends that could double an order,
share splits in the app budget ledger and the live ticker quote check."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from quant.trader.budget import BudgetBroker, split_ratio
from quant.trader.brokers import SimBroker
from quant.trader.runner import DATA_TICKERS, Runner, TraderConfig
from test_budget import LaterFillBroker


@pytest.fixture
def close():
    """Prices where QQQ leads in September and SPY in October, so November rebalances."""
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2015-01-01", "2026-11-13")
    df = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, (len(idx), len(DATA_TICKERS))), axis=0),
                      index=idx, columns=DATA_TICKERS)
    late = df.index >= "2026-10-01"
    df.loc[late, "QQQ"] *= np.linspace(1, 0.6, late.sum())
    df.loc[late, "SPY"] *= np.linspace(1, 1.6, late.sum())
    return df


def at(day: str) -> datetime:          # 23:50 KST = 09:50 New York in November (session open)
    return datetime.fromisoformat(day + "T14:50:00+00:00")


def trades(d):
    p = d / "trades.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def runner(broker, d, mode="kiwoom_mock", **cfg):
    r = Runner(broker, TraderConfig(state_dir=d, order_retries=0, max_daily_loss=None, **cfg), mode=mode)
    r.fx_source = lambda: None
    r.sleep = lambda s: None
    return r


def test_an_order_that_expires_unfilled_is_traded_again(tmp_path, close):
    """Before: an accepted order whose price ran past the limit counted as done,
    was never sent again and stayed "pending" forever (seen 4 days in a row)."""
    inner = LaterFillBroker(20_000.0)
    r = runner(inner, tmp_path)
    r.run(close=close.loc[:"2026-10-01"], now=at("2026-10-02"))
    inner.fill()
    r.run(close=close.loc[:"2026-11-04"], now=at("2026-11-05"))
    sent = list(inner.orders)
    assert any(o[1] == "sell" for o in sent)
    inner.orders = []                         # nothing filled: the session ended
    r.run(close=close.loc[:"2026-11-05"], now=at("2026-11-06"))
    again = list(inner.orders)
    assert {o[:2] for o in again} == {o[:2] for o in sent}       # same trades, sized from today's prices
    old = [t for t in trades(tmp_path) if t["date"] == "2026-11-04"]
    assert old and all(t["status"] == "cancelled" and "체결되지 않아" in t["message"] for t in old)


def test_a_partly_filled_buy_keeps_the_shares_it_got(tmp_path):
    d = tmp_path
    r = runner(SimBroker(d / "a.json"), d)
    (d / "trades.jsonl").write_text(json.dumps(
        {"time": "2026-11-04T23:50:00+09:00", "date": "2026-11-04", "symbol": "SPY", "side": "buy",
         "qty": 10, "price": 100.0, "status": "pending", "held_before": 2}) + "\n", encoding="utf-8")
    n = r._reconcile({"SPY": {"qty": 6, "avg_price": 101.0}}, datetime(2026, 11, 5, 23, 50, tzinfo=timezone(timedelta(hours=9))))
    t = trades(d)[0]
    assert n == 1 and t["status"] == "filled" and t["qty"] == 4 and t["price"] == 101.0


def test_limits_hold_back_buys_but_let_sells_go(tmp_path, close):
    """A crash day trips the daily-loss limit; the canary's sells must still go."""
    inner = SimBroker(tmp_path / "a.json", starting_cash=20_000.0, cost_bps=0)
    r = runner(inner, tmp_path / "p")
    r.run(close=close.loc[:"2026-10-01"], now=at("2026-10-02"))
    held = inner.positions()
    # yesterday's close was 30% higher: today trips the 10% daily loss limit
    eq = json.loads((tmp_path / "p" / "equity.jsonl").read_text().splitlines()[-1])["equity"]
    with open(tmp_path / "p" / "equity.jsonl", "a") as f:
        f.write(json.dumps({"date": "2026-11-03", "equity": eq * 1.3}) + "\n")
    r.cfg.max_daily_loss = 0.10
    s = r.run(close=close.loc[:"2026-11-04"], now=at("2026-11-05"))
    msg = " ".join(a["message"] for a in s["alerts"] if a["level"] == "error")
    sides = {t["side"] for t in trades(tmp_path / "p") if t["date"] == "2026-11-04"}
    assert "하루 손실 한도" in msg and "매도는 그대로" in msg and sides == {"sell"}
    assert inner.positions() != held
    meta = json.loads((tmp_path / "p" / "meta.json").read_text())
    assert meta.get("last_signal") != "2026-10-30"   # buys still owed: tomorrow tries again


def test_the_daily_loss_limit_ignores_an_old_last_row(tmp_path):
    r = runner(SimBroker(tmp_path / "a.json"), tmp_path)
    (tmp_path / "equity.jsonl").write_text(json.dumps({"date": "2026-10-01", "equity": 20_000}) + "\n")
    assert r._prev_equity("2026-11-04") is None
    assert r._prev_equity("2026-10-05") == 20_000


class NoReplyOnce(SimBroker):
    """Takes the first order but never answers it; Kiwoom's history shows it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent, self.history = [], {}

    def place_order(self, ticker, side, qty, ref_price, **kw):
        self.sent.append((ticker, side, qty))
        if len(self.sent) == 1:
            self.history["777"] = {"symbol": ticker, "side": side, "filled": 0, "left": qty, "cancelled": 0}
            return {"ok": False, "status": "pending", "msg": "키움 응답 없음"}
        return super().place_order(ticker, side, qty, ref_price)

    def order_fills(self, dates):
        return self.history


def test_an_unanswered_order_found_in_the_history_is_not_sent_twice(tmp_path, close):
    b = NoReplyOnce(tmp_path / "a.json", starting_cash=20_000.0)
    r = Runner(b, TraderConfig(state_dir=tmp_path / "p", order_retries=2, retry_wait_s=0), mode="kiwoom_mock")
    r.fx_source, r.sleep = (lambda: None), (lambda s: None)
    r.run(close=close.loc[:"2026-10-01"], now=at("2026-10-02"))
    first = b.sent[0]
    assert b.sent.count(first) == 1
    rec = next(t for t in trades(tmp_path / "p") if t["symbol"] == first[0])
    assert rec["status"] == "pending" and rec["order_id"] == "777"


def test_live_never_resends_when_the_history_cant_be_read(tmp_path, close):
    class Silent(SimBroker):
        sent = 0

        def place_order(self, *a, **kw):
            Silent.sent += 1
            return {"ok": False, "status": "pending", "msg": "키움 응답 없음"}
    inner = Silent(tmp_path / "a.json", starting_cash=5_000.0)
    b = BudgetBroker(inner, tmp_path / "live" / "budget.json", budget_usd=5_000.0)
    r = Runner(b, TraderConfig(state_dir=tmp_path / "live", order_retries=2, retry_wait_s=0, max_order_usd=5_000,
                               pause_file=tmp_path / "STOP"), mode="kiwoom_live")
    r.fx_source, r.sleep = (lambda: None), (lambda s: None)
    s = r.run(close=close.loc[:"2026-10-01"], now=at("2026-10-02"))
    assert Silent.sent == len(s.get("planned_orders") or trades(tmp_path / "live"))


def test_pending_buys_reserve_the_app_cash(tmp_path):
    inner = LaterFillBroker(5_000.0)
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    assert b.place_order("SPY", "buy", 6, 100.0)["ok"]
    res = b.place_order("SPY", "buy", 6, 100.0)          # same order again before it fills
    assert not res["ok"] and "매수를 못 해요" in res["msg"]


def test_buys_wait_for_the_sells_to_show_up(tmp_path, close):
    """An app-budget account counts sale money once the shares are gone; the
    buys used to go one second after the sells and be refused."""
    class FillsOnSecondLook(LaterFillBroker):
        looks = 0

        def positions(self):
            if self.orders:
                self.looks += 1
                if self.looks >= 3:
                    self.fill()
            return super().positions()
    inner = FillsOnSecondLook(50_000.0)
    b = BudgetBroker(inner, tmp_path / "live" / "budget.json", budget_usd=10_000.0)
    r = runner(b, tmp_path / "live", mode="kiwoom_live", max_order_usd=20_000, pause_file=tmp_path / "STOP")
    r.run(close=close.loc[:"2026-10-01"], now=at("2026-10-02"))
    inner.fill()
    r.run(close=close.loc[:"2026-11-04"], now=at("2026-11-05"))
    nov = [t for t in trades(tmp_path / "live") if t["date"] == "2026-11-04"]
    assert any(t["side"] == "buy" and t["status"] != "rejected" for t in nov), nov


def test_split_ratio():
    assert split_ratio(100, 200, 80.0, 40.2) == 2
    assert split_ratio(100, 50, 20.0, 40.0) == 0.5
    assert split_ratio(100, 200, 80.0, 80.0) is None     # bought as many again: a trade
    assert split_ratio(100, 200, None, 40.0) is None


def test_a_share_split_stays_the_apps(tmp_path):
    inner = LaterFillBroker(5_000.0, {"TQQQ": 10})         # 10 are the owner's
    b = BudgetBroker(inner, tmp_path / "budget.json", budget_usd=1_000.0)
    b.place_order("TQQQ", "buy", 5, 100.0)
    inner.fill()
    assert b.positions()["TQQQ"]["qty"] == 5
    inner.pos["TQQQ"] = 30                                 # 2:1 split: 15 -> 30
    inner.positions = lambda: {"TQQQ": {"qty": 30, "avg_price": 50.0}}
    assert b.positions()["TQQQ"]["qty"] == 10 and b.led["outside"]["TQQQ"] == 20
    assert b.led["positions"]["TQQQ"]["avg_price"] == pytest.approx(50.05, abs=0.1)


def test_live_key_check_quotes_every_live_ticker(tmp_path):
    from quant import readiness
    from scripts.run_trader import live_tickers

    class B:
        def __init__(self, key, secret, mock=True, exchanges=None):
            self.exchanges = exchanges or {}

        def cash_usd(self):
            return 10.0

        def quote(self, t):
            if t == "VTWO":
                raise RuntimeError("1903 종목코드와 거래소 코드가 맞지 않아요")
            return {}
    env = {"KIWOOM_LIVE_APP_KEY": "k", "KIWOOM_LIVE_SECRET_KEY": "s"}
    rec = readiness.check_live_keys(env, tmp_path, B)
    assert {"SPYM", "QQQM", "GLDM", "VTWO", "TQQQ", "UPRO", "IEF"} <= set(live_tickers())
    assert rec["ok"] and rec["quotes_checked"] == len(live_tickers()) and rec["quote_fail"][0].startswith("VTWO")
    items = {i["id"]: i for i in readiness.checklist(tmp_path, {}, {}, datetime(2026, 11, 5).date())}
    assert items["api"]["status"] == "fail" and "VTWO" in items["api"]["detail"]
