import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from quant.trader.brokers import KiwoomBroker, SimBroker
from quant.trader.runner import DATA_TICKERS, Runner, TraderConfig, plan_orders, signal_date


@pytest.fixture
def close():
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2015-01-01", "2026-10-08")
    data = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, (len(idx), len(DATA_TICKERS))), axis=0)
    return pd.DataFrame(data, index=idx, columns=DATA_TICKERS)


def make_runner(tmp_path, cash=10_000.0):
    cfg = TraderConfig(state_dir=tmp_path / "paper")
    return Runner(SimBroker(cfg.state_dir / "sim_account.json", starting_cash=cash), cfg)


def trades(r):
    p = r.dir / "trades.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def errors(state):
    return " ".join(a["message"] for a in state["alerts"] if a["level"] == "error")


def test_signal_date_is_last_session_of_previous_month(close):
    assert signal_date(close.index, pd.Timestamp("2026-10-08")) == pd.Timestamp("2026-09-30")


def test_first_run_rebalances_then_waits(tmp_path, close):
    r = make_runner(tmp_path)
    now = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
    first = r.run(close=close, now=now)
    done = trades(r)
    assert done and all(t["status"] == "filled" for t in done)
    held = sum(p["qty"] * p["last_price"] for p in first["positions"])
    assert first["cash"] + held == pytest.approx(10_000, rel=0.01)
    assert first["cash"] < 10_000 * 0.15  # whole shares only, prices up to ~$1,100
    assert first["last_rebalance"] == "2026-10-08"
    r.run(close=close, now=now)
    assert len(trades(r)) == len(done)  # already executed this month


def test_check_mode_places_nothing(tmp_path, close):
    r = make_runner(tmp_path)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc), execute=False)
    assert any("점검 모드" in a["message"] for a in s["alerts"])
    assert trades(r) == [] and r.broker.positions() == {}


def test_stop_file_halts(tmp_path, close):
    r = make_runner(tmp_path)
    (r.dir / "STOP").touch()
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert "STOP" in errors(s) and r.broker.positions() == {}


def test_order_cap_blocks(tmp_path, close):
    cfg = TraderConfig(state_dir=tmp_path / "paper", max_order_usd=1_000)
    r = Runner(SimBroker(cfg.state_dir / "sim_account.json", starting_cash=100_000), cfg)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert "한도" in errors(s) and r.broker.positions() == {}


def test_default_cap_allows_large_single_position(tmp_path, close):
    r = make_runner(tmp_path, cash=100_000)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert not errors(s) and r.broker.positions()


def test_plan_sells_before_buys():
    orders = plan_orders({"QQQ": 1.0}, {"QQQ": 100.0, "SPY": 50.0},
                         {"SPY": {"qty": 100, "avg_price": 40.0}}, 0.0, TraderConfig())
    assert [(o["symbol"], o["side"]) for o in orders] == [("SPY", "sell"), ("QQQ", "buy")]


def test_dashboard_files_written(tmp_path, close):
    r = make_runner(tmp_path)
    r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    state = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == 1 and state["mode"] == "simulate"
    assert state["positions"] and state["targets"] and state["signal"]["scores"]
    assert sum(t["weight"] for t in state["targets"]) == pytest.approx(1.0)
    eq = (r.dir / "equity.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(eq) == 1 and json.loads(eq[0])["date"] == "2026-10-08"


def test_stale_prices_halt(tmp_path, close):
    r = make_runner(tmp_path)
    s = r.run(close=close, now=datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert "데이터" in errors(s) and r.broker.positions() == {}


class FakeKiwoom(KiwoomBroker):
    def __init__(self, responses):
        super().__init__("k", "s", mock=True, exchanges={"SPY": "NY"})
        self.responses, self.sent = responses, []

    def call(self, path, api_id, body, retries=2):
        self.sent.append((path, api_id, body))
        res = self.responses[api_id]
        if isinstance(res, Exception):
            raise res
        return res


def test_kiwoom_order_bodies():
    k = FakeKiwoom({"ust20000": {"return_code": 0, "ord_no": "000000123"},
                    "ust20001": {"return_code": 0, "ord_no": "000000124"}})
    buy = k.place_order("SPY", "buy", 3, 500.0)
    k.place_order("QQQ", "sell", 2, 400.0)
    assert buy == {"ok": True, "status": "pending", "order_no": "000000123",
                   "fill_price": 500.0, "fee": 0.0, "msg": ""}
    assert k.sent[0] == ("/api/us/ordr", "ust20000",
                         {"stex_tp": "NY", "stk_cd": "SPY", "ord_qty": "3",
                          "ord_uv": "", "trde_tp": "03"})
    assert k.sent[1][1] == "ust20001" and k.sent[1][2]["stex_tp"] == "ND"


def test_kiwoom_order_failure_is_reported():
    k = FakeKiwoom({"ust20000": RuntimeError("ust20000 실패 (HTTP 200): 장운영시간이 아닙니다")})
    res = k.place_order("SPY", "buy", 1, 500.0)
    assert not res["ok"] and "장운영" in res["msg"]


def test_kiwoom_positions_parsing():
    k = FakeKiwoom({"ust21070": {"return_code": 0, "result_list": [
        {"stk_cd": "SPY ", "poss_qty": "000000000005", "frgn_stk_book_uv": "+512.3400"},
        {"stk_cd": "GLD", "poss_qty": "0", "frgn_stk_book_uv": ""}]}})
    assert k.positions() == {"SPY": {"qty": 5, "avg_price": 512.34}}


def test_kiwoom_cash_uses_settled_projection():
    k = FakeKiwoom({"ust21160": {"return_code": 0, "d0_usd_fx_entr": "10000.00",
                                 "d1_usd_fx_entr": "8500.25", "d2_usd_fx_entr": "",
                                 "won_entr": "+00000000500000"}})
    assert k.cash_usd() == 8500.25
