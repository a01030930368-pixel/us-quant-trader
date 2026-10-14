import json

import numpy as np
import pandas as pd
import pytest

from quant import report

pytest.importorskip("quantstats")


def _paper(tmp_path, rows, initial=1000.0):
    (tmp_path / "state.json").write_text(json.dumps({"initial_capital": initial}))
    (tmp_path / "equity.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path


def test_paper_returns_skip_deposits(tmp_path):
    rows = [{"date": "2026-10-09", "equity": 990.0, "benchmark_close": 100.0},
            {"date": "2026-10-12", "equity": 1589.0, "benchmark_close": 101.0, "flow": 500.0},
            {"date": "2026-10-13", "equity": 1450.8, "benchmark_close": 99.99}]
    strat, bench = report.paper_returns(_paper(tmp_path, rows))
    assert strat.round(4).tolist() == [-0.01, 0.1, round(1450.8 / 1589 - 1, 4)]  # the $500 deposit is no gain
    assert bench.round(4).tolist() == [0.0, 0.01, -0.01]


def test_paper_report_html_and_short_history(tmp_path):
    assert "부족해요" in report.build("paper", _paper(tmp_path, []))
    rng = np.random.default_rng(1)
    eq = 1000 * np.cumprod(1 + rng.normal(0, 0.01, 30))
    days = pd.bdate_range("2026-09-01", periods=30)
    rows = [{"date": f"{d:%Y-%m-%d}", "equity": float(e), "benchmark_close": 100.0 + i}
            for i, (d, e) in enumerate(zip(days, eq))]
    page = report.build("paper", _paper(tmp_path, rows))
    assert "Mintory 성과 리포트" in page and "Max Drawdown" in page and "30거래일" in page


def test_backtest_returns_start_when_invested():
    from quant.trader.runner import DATA_TICKERS
    tick = sorted(set(DATA_TICKERS) | {"EEM"})
    idx = pd.bdate_range("2009-01-02", "2012-12-31")
    rng = np.random.default_rng(0)
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, (len(idx), len(tick))), axis=0)),
                         index=idx, columns=tick)
    strat, bench, name = report.backtest_returns(close)
    assert strat.index[0] > pd.Timestamp("2009-12-01")    # the cash-only warm-up year is dropped
    assert strat.index.equals(bench.index) and "카나리아" in name


def test_build_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "backtest_returns", lambda: 1 / 0)
    report._cache.clear()
    assert "만들지 못했어요" in report.build("backtest", tmp_path)
