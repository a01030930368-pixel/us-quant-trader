"""New research strategies: weights are valid and never use future prices."""
import numpy as np
import pandas as pd
import pytest

from quant import new_strategies as ns

TICKERS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "SHY", "GLD", "DBC", "VNQ", "SPY3X", "TLT3X", *ns.SECTORS]


@pytest.fixture(scope="module")
def data():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2004-01-01", periods=1500)
    rets = rng.normal(0.0003, 0.012, (len(idx), len(TICKERS)))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=TICKERS)
    high = close * (1 + rng.uniform(0, 0.01, close.shape))
    low = close * (1 - rng.uniform(0, 0.01, close.shape))
    vix = pd.Series(rng.uniform(12, 30, len(idx)), index=idx)
    vix3m = pd.Series(rng.uniform(14, 28, len(idx)), index=idx)
    return close, high, low, vix, vix3m


def builders(close, high, low, vix, vix3m):
    return {
        "rsi": lambda c: ns.rsi_dip(c),
        "ibs": lambda c: ns.ibs_dip(c, high.loc[c.index], low.loc[c.index]),
        "tom": lambda c: ns.turn_of_month(c),
        "trend": lambda c: ns.trend_parity(c),
        "vix": lambda c: ns.vix_term(c, vix.loc[c.index], vix3m.loc[c.index]),
        "static": lambda c: ns.static_mix(c),
        "lowvol": lambda c: ns.low_vol_sectors(c),
        "gtt": lambda c: ns.growth_trend(c, UNRATE),
        "credit": lambda c: ns.credit_trend(c, vix),
        "halloween": lambda c: ns.halloween(c),
        "parity": lambda c: ns.risk_parity(c),
        "hfea": lambda c: ns.leveraged_pair(c),
        "hfea_vol": lambda c: ns.leveraged_parity(c),
        "volman": lambda c: ns.vol_managed(c),
        "reversal": lambda c: ns.sector_reversal(c),
    }


UNRATE = pd.Series(np.random.default_rng(1).uniform(3, 9, 80),
                   index=pd.date_range("2003-01-01", periods=80, freq="MS"))


@pytest.mark.parametrize("name", ["rsi", "ibs", "tom", "trend", "vix", "static", "lowvol", "gtt", "credit", "halloween", "parity", "hfea", "hfea_vol", "volman", "reversal"])
def test_weights_valid_and_no_lookahead(data, name):
    close, high, low, vix, vix3m = data
    build = builders(close, high, low, vix, vix3m)[name]
    full = build(close)
    assert (full >= -1e-12).all().all()
    assert (full.sum(axis=1) <= 1 + 1e-9).all()
    cut = 1100
    # Weights decided up to day `cut` must not change when later prices change.
    # turn_of_month looks one session ahead on the calendar only (known in advance).
    part = build(close.iloc[:cut + 1])
    end = cut - 1 if name in ("tom", "halloween") else cut
    pd.testing.assert_frame_equal(full.iloc[:end], part.iloc[:end])


def test_macro_value_used_only_after_release():
    idx = pd.bdate_range("2020-01-01", "2020-04-30")
    s = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2020-01-01", "2020-02-01", "2020-03-01"]))
    at = ns.monthly_macro_at(idx, s)
    assert pd.isna(at.loc["2020-02-07"])     # January figure not published yet
    assert at.loc["2020-02-28"] == 1.0       # known by the February month end
    assert at.loc["2020-03-31"] == 2.0


def test_vol_managed_exposure():
    idx = pd.bdate_range("2020-01-01", periods=300)
    calm = pd.DataFrame({"SPY": 100 * np.exp(np.cumsum(np.full(300, 0.0005) + np.tile([0.001, -0.001], 150))),
                         "SPY3X": 100.0}, index=idx)
    w = ns.vol_managed(calm, target=0.15, max_lev=2.0)
    expo = w["SPY"] + 3 * w["SPY3X"]
    assert expo.iloc[-1] == pytest.approx(2.0)
    assert (w.sum(axis=1) <= 1 + 1e-9).all()
