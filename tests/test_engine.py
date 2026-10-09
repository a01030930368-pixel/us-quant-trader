import numpy as np
import pytest
import pandas as pd

from quant.backtest import run_backtest
from quant.metrics import cagr, max_drawdown, sharpe
from quant.ranking import Config, evaluate, expand_grid
from quant.strategies import buy_and_hold, momentum_rotation, rsi2_reversion, sma_trend


def test_buy_and_hold_matches_asset(prices):
    res = run_backtest(prices, buy_and_hold(prices, "SPY"), cost_bps=0)
    expected = prices["SPY"] / prices["SPY"].iloc[0]
    assert np.allclose(res.equity.values, expected.values)


def test_costs_reduce_returns(prices):
    w = momentum_rotation(prices, ["SPY", "QQQ", "GLD"], lookback=63)
    free = run_backtest(prices, w, cost_bps=0).equity.iloc[-1]
    costly = run_backtest(prices, w, cost_bps=50).equity.iloc[-1]
    assert costly < free


def test_no_lookahead(prices):
    """Changing a future price must not change past weights."""
    w1 = sma_trend(prices, "SPY", 200, safe="IEF")
    bumped = prices.copy()
    bumped.iloc[-10:, 0] *= 3
    w2 = sma_trend(bumped, "SPY", 200, safe="IEF")
    pd.testing.assert_frame_equal(w1.iloc[:-30], w2.iloc[:-30])


def test_weights_never_exceed_one(prices):
    for w in [sma_trend(prices, "SPY", 200, safe="IEF"),
              momentum_rotation(prices, ["SPY", "QQQ", "GLD"], top_n=2, safe="IEF"),
              rsi2_reversion(prices, "SPY")]:
        assert (w.sum(axis=1) <= 1 + 1e-9).all()


def test_metrics_known_values():
    r = pd.Series([0.01] * 252)
    assert abs(cagr(r) - (1.01 ** 252 - 1)) < 1e-9
    assert max_drawdown(pd.Series([0.1, -0.5, 0.2])) == -0.5
    assert sharpe(pd.Series([0.0] * 10)) == 0.0


def test_ranking_orders_by_score(prices):
    configs = [Config("buy_and_hold", {"asset": "SPY"})]
    configs += expand_grid("sma_trend", {"asset": "SPY", "safe": "IEF"},
                           {"window": [100, 150, 200]})
    table = evaluate(prices, configs, split="2018-01-01", min_trades=1, max_mdd=-1.0)
    assert len(table) == 4
    assert table["score"].is_monotonic_decreasing
    assert {"is_sharpe", "oos_sharpe", "robustness"} <= set(table.columns)


def test_leveraged_series_triples_daily_moves(prices):
    from quant.data import add_leveraged
    out = add_leveraged(prices, "SPY", "SPY3X", leverage=3.0, annual_cost=0.0)
    r = out.pct_change().dropna()
    assert np.allclose(r["SPY3X"], 3 * r["SPY"])


def test_monthly_metrics_present(prices):
    from quant.metrics import summarize
    res = run_backtest(prices, buy_and_hold(prices, "SPY"), cost_bps=0)
    m = summarize(res.returns)
    assert 0 <= m["month_win_rate"] <= 1
    assert m["worst_month"] <= m["month_avg"]


def test_sma_trend_uses_signal_asset(prices):
    w = sma_trend(prices, "QQQ", 200, safe="IEF", signal="SPY")
    above = (prices["SPY"] > prices["SPY"].rolling(200).mean()).astype(float)
    month_end = w.index[w.index.to_series().groupby(w.index.to_period("M")).transform("max") == w.index]
    assert (w.loc[month_end[-12:], "QQQ"].values == above.loc[month_end[-12:]].values).all()


def test_blend_is_weighted_sum(prices):
    from quant.strategies import blend
    a = buy_and_hold(prices, "SPY")
    b = buy_and_hold(prices, "GLD")
    w = blend(prices, [("buy_and_hold", {"asset": "SPY"}, 0.7),
                       ("buy_and_hold", {"asset": "GLD"}, 0.3)])
    pd.testing.assert_frame_equal(w, a * 0.7 + b * 0.3)


def test_next_open_execution_lags_one_day(prices):
    """If each day opened at its own close, trading at the next open would be the
    same as a one-day delay; if it opened at the previous close, the same as
    trading at the signal day's close."""
    same_close = run_backtest(prices, momentum_rotation(prices, ["SPY", "QQQ", "GLD"], lookback=63),
                              cost_bps=0, opens=prices.shift(1).bfill())
    base = run_backtest(prices, momentum_rotation(prices, ["SPY", "QQQ", "GLD"], lookback=63), cost_bps=0)
    assert np.allclose(same_close.returns.values[1:], base.returns.values[1:])
    opens = prices
    w = momentum_rotation(prices, ["SPY", "QQQ", "GLD"], lookback=63)
    nxt = run_backtest(prices, w, cost_bps=0, opens=opens)
    delayed = run_backtest(prices, w.shift(1).fillna(0.0), cost_bps=0)
    assert np.allclose(nxt.returns.values, delayed.returns.values)


@pytest.fixture
def blend_prices():
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2010-01-01", periods=252 * 6)
    cols = ["SPY", "QQQ", "IWM", "EFA", "GLD", "TLT", "IEF"]
    rets = rng.normal(0.0003, 0.01, size=(len(idx), len(cols)))
    px = pd.DataFrame(100 * np.cumprod(1 + rets, axis=0), index=idx, columns=cols)
    px["QQQ3X"] = 100 * (1 + 3 * px["QQQ"].pct_change().fillna(0)).cumprod()
    px["SPY3X"] = 100 * (1 + 3 * px["SPY"].pct_change().fillna(0)).cumprod()
    return px


def test_momentum_blend_defaults_match_original_blend(blend_prices):
    from quant.strategies import AGGR, STABLE, blend, momentum_blend
    old = blend(blend_prices, [
        ("momentum_rotation", {"assets": STABLE, "safe": "IEF", "lookback": 63, "top_n": 1}, 0.5),
        ("momentum_rotation", {"assets": AGGR, "safe": "IEF", "lookback": 63, "top_n": 2}, 0.5),
    ])
    pd.testing.assert_frame_equal(momentum_blend(blend_prices), old)


@pytest.mark.parametrize("opts", [
    {"rebalance": "W"},
    {"lookbacks": [21, 63, 126, 252]},
    {"trend_window": 200},
    {"vol_target": 0.3},
    {"trend_window": 200, "vol_target": 0.3, "safe_filter": True, "rebalance": "W"},
])
def test_momentum_blend_options(blend_prices, opts):
    from quant.strategies import momentum_blend
    w = momentum_blend(blend_prices, **opts)
    assert (w.sum(axis=1) <= 1 + 1e-9).all()
    assert (w >= -1e-12).all().all()
    bumped = blend_prices.copy()
    bumped.iloc[-10:] *= 2
    pd.testing.assert_frame_equal(w.iloc[:-30], momentum_blend(bumped, **opts).iloc[:-30])


def test_vol_target_lowers_leverage(blend_prices):
    from quant.strategies import momentum_blend
    plain = momentum_blend(blend_prices)[["QQQ3X", "SPY3X"]].sum(axis=1)
    capped = momentum_blend(blend_prices, vol_target=0.2)[["QQQ3X", "SPY3X"]].sum(axis=1)
    assert (capped <= plain + 1e-9).all() and capped.mean() < plain.mean()


@pytest.mark.parametrize("name,opts", [
    ("tranched", {"lookbacks": [21, 63, 126, 252]}),
    ("core_satellite", {"core_share": 0.3}),
    ("canary_gate", {"canary": ["EFA", "TLT"]}),
])
def test_research_wrappers_no_lookahead(blend_prices, name, opts):
    from quant.strategies import REGISTRY
    w = REGISTRY[name](blend_prices, **opts)
    assert (w.sum(axis=1) <= 1 + 1e-9).all()
    assert (w >= -1e-12).all().all()
    bumped = blend_prices.copy()
    bumped.iloc[-10:] *= 2
    pd.testing.assert_frame_equal(w.iloc[:-30], REGISTRY[name](bumped, **opts).iloc[:-30])


def test_one_tranche_is_plain_blend(blend_prices):
    from quant.strategies import momentum_blend, tranched
    pd.testing.assert_frame_equal(tranched(blend_prices, tranches=1), momentum_blend(blend_prices))


def test_tranche_offset_shifts_rebalance_day(blend_prices):
    from quant.strategies import momentum_blend
    w0 = momentum_blend(blend_prices).diff().abs().sum(axis=1)
    w5 = momentum_blend(blend_prices, offset=5).diff().abs().sum(axis=1)
    i0 = blend_prices.index.get_indexer(w0[w0 > 0].index[1:5])
    i5 = blend_prices.index.get_indexer(w5[w5 > 0].index[1:5])
    assert set(i5) - set(i0)  # trades land on different days
