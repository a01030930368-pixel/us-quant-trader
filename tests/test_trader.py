import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from quant.trader.brokers import SimBroker
from quant.trader.runner import DATA_TICKERS, Runner, TraderConfig, plan_orders, signal_date


@pytest.fixture
def close():
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2015-01-01", "2026-10-08")
    data = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, (len(idx), len(DATA_TICKERS))), axis=0)
    return pd.DataFrame(data, index=idx, columns=DATA_TICKERS)


def make_runner(tmp_path, cash=10_000.0):
    cfg = TraderConfig(state_dir=tmp_path / "state")
    return Runner(SimBroker(cfg.state_dir / "acct.json", starting_cash=cash), cfg)


def test_signal_date_is_last_session_of_previous_month(close):
    assert signal_date(close.index, pd.Timestamp("2026-10-08")) == pd.Timestamp("2026-09-30")


def test_first_run_rebalances_then_waits(tmp_path, close):
    r = make_runner(tmp_path)
    now = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
    first = r.run(close=close, now=now)
    assert first["order_results"] and all(x["ok"] for x in first["order_results"])
    assert first["equity_usd"] == pytest.approx(10_000, rel=0.01)
    assert first["cash_usd"] < 10_000 * 0.15  # whole shares only, prices up to ~$1,100
    second = r.run(close=close, now=now)
    assert second["planned_orders"] == []
    assert "대기" in second["note"]


def test_check_mode_places_nothing(tmp_path, close):
    r = make_runner(tmp_path)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc), execute=False)
    assert s["planned_orders"] and not s["order_results"]
    assert r.broker.positions() == {}


def test_stop_file_halts(tmp_path, close):
    r = make_runner(tmp_path)
    (r.dir / "STOP").touch()
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert s["halted"] and r.broker.positions() == {}


def test_order_cap_blocks(tmp_path, close):
    r = make_runner(tmp_path, cash=1_000_000)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert s["halted"] and "한도" in s["note"]


def test_plan_sells_before_buys():
    orders = plan_orders({"QQQ": 1.0}, {"QQQ": 100.0, "SPY": 50.0}, {"SPY": 100}, 0.0,
                         TraderConfig())
    assert [o["side"] for o in orders] == ["sell", "buy"]


def test_status_file_written(tmp_path, close):
    r = make_runner(tmp_path)
    r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    status = json.loads((r.dir / "status.json").read_text(encoding="utf-8"))
    assert status["schema"] == 1 and status["holdings"]
    assert (r.dir / "equity.csv").exists() and (r.dir / "trades.jsonl").exists()


def test_stale_prices_halt(tmp_path, close):
    r = make_runner(tmp_path)
    s = r.run(close=close, now=datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert s["halted"] and "데이터" in s["note"] and r.broker.positions() == {}
