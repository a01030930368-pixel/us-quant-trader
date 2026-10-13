"""New strategy families, unrelated to the momentum-ranking + canary strategy.

Each function returns target weights decided with data up to each row's close
(the backtest trades them at the next open). Money not assigned to a risky
asset goes to `idle` (a bond ETF) when given; strategies that trade often keep
it in cash by default, since switching into bonds and back doubles the costs.
These are research candidates only; nothing here is wired into the runner.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.strategies import _period_ends


def _frame(prices: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(0.0, index=prices.index, columns=prices.columns)


def _idle(w: pd.DataFrame, idle: str | None) -> pd.DataFrame:
    if idle:
        w[idle] = w[idle] + (1 - w.sum(axis=1)).clip(lower=0)
    return w


def _tranches(build, prices: pd.DataFrame, tranches: int, spacing: int) -> pd.DataFrame:
    """Average of the same monthly strategy rebalanced on different days."""
    return sum(build(k * spacing) for k in range(tranches)) / tranches


def _hold_monthly(prices: pd.DataFrame, sig: pd.DataFrame, offset: int) -> pd.DataFrame:
    ends = _period_ends(prices.index, "M", offset)
    w = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    w.loc[ends] = sig.loc[ends].reindex(columns=prices.columns).values
    return w.ffill().fillna(0.0)


# ---------- 1. short-term mean reversion ----------

def rsi(px: pd.Series, n: int = 2) -> pd.Series:
    d = px.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, 1e-12))


def rsi_dip(prices: pd.DataFrame, asset: str = "SPY", entry: float = 10, exit_: float = 70,
            trend: int = 200, idle: str | None = None) -> pd.DataFrame:
    """Connors RSI(2): buy an oversold close while above the long average,
    sell when RSI recovers or the trend breaks."""
    px = prices[asset]
    r, up = rsi(px).values, (px > px.rolling(trend).mean()).values
    pos, holding = np.zeros(len(px)), False
    for i in range(len(px)):
        if not holding and up[i] and r[i] < entry:
            holding = True
        elif holding and (r[i] > exit_ or not up[i]):
            holding = False
        pos[i] = holding
    w = _frame(prices)
    w[asset] = pos
    return _idle(w, idle)


def ibs_dip(prices: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, asset: str = "SPY",
            entry: float = 0.2, exit_: float = 0.7, idle: str | None = None) -> pd.DataFrame:
    """Internal bar strength (close near the day's low) mean reversion."""
    c, h, lo = prices[asset], high[asset].reindex(prices.index), low[asset].reindex(prices.index)
    ibs = ((c - lo) / (h - lo).replace(0, np.nan)).fillna(0.5).values
    pos, holding = np.zeros(len(c)), False
    for i in range(len(c)):
        if not holding and ibs[i] < entry:
            holding = True
        elif holding and ibs[i] > exit_:
            holding = False
        pos[i] = holding
    w = _frame(prices)
    w[asset] = pos
    return _idle(w, idle)


# ---------- 2. calendar ----------


def turn_of_month(prices: pd.DataFrame, asset: str = "SPY", before: int = 1, after: int = 3,
                  idle: str | None = None) -> pd.DataFrame:
    """Hold `asset` from `before` sessions before month end to the `after`-th
    session of the new month; the rest of the month sits in `idle`."""
    idx = prices.index
    month = idx.to_period("M")
    pos_in_month = pd.Series(1, index=idx).groupby(month).cumsum()
    left = pd.Series(1, index=idx)[::-1].groupby(month[::-1]).cumsum()[::-1]
    in_window = ((pos_in_month <= after) | (left <= before)).astype(float)
    w = _frame(prices)
    w[asset] = in_window.shift(-1).fillna(0.0)  # decided at today's close for tomorrow
    return _idle(w, idle)


# ---------- 3. cross-asset trend following with risk parity ----------

TREND_UNIVERSE = ["SPY", "EFA", "EEM", "IWM", "VNQ", "TLT", "IEF", "GLD", "DBC"]


def trend_parity(prices: pd.DataFrame, universe: list[str] | None = None, lookback: int = 252,
                 vol_window: int = 63, idle: str | None = "SHY", tranches: int = 4,
                 spacing: int = 5) -> pd.DataFrame:
    """Managed-futures style: every asset with a positive trailing return gets
    an inverse-volatility share; assets in a downtrend give their share to
    `idle`. Diversifies across stocks, bonds, gold, commodities, real estate."""
    uni = [a for a in (universe or TREND_UNIVERSE) if a in prices]
    px = prices[uni]
    up = px.pct_change(lookback) > 0
    inv = 1 / (px.pct_change().rolling(vol_window).std())
    inv = inv.where(px.pct_change(lookback).notna())
    share = inv.div(inv.sum(axis=1), axis=0)
    sig = (share * up).fillna(0.0)

    def build(offset):
        w = _hold_monthly(prices, sig, offset)
        return _idle(w, idle)
    return _tranches(build, prices, tranches, spacing)


# ---------- 4. volatility term structure ----------

def vix_term(prices: pd.DataFrame, vix: pd.Series, vix3m: pd.Series, asset: str = "SPY",
             threshold: float = 1.0, smooth: int = 5, safe: str | None = "IEF") -> pd.DataFrame:
    """Hold stocks while short-term implied vol is below 3-month implied vol
    (calm market, contango); move to bonds when the curve inverts (stress)."""
    ratio = (vix / vix3m).reindex(prices.index).ffill().rolling(smooth).mean()
    calm = (ratio < threshold) | ratio.isna()
    w = _frame(prices)
    w[asset] = calm.astype(float)
    return _idle(w, safe)


# ---------- 5. static all-weather mixes ----------

STATIC = {
    "all_weather": {"SPY": .30, "TLT": .40, "IEF": .15, "GLD": .075, "DBC": .075},
    "permanent": {"SPY": .25, "TLT": .25, "SHY": .25, "GLD": .25},
    "golden_butterfly": {"SPY": .20, "IWM": .20, "TLT": .20, "SHY": .20, "GLD": .20},
}


def static_mix(prices: pd.DataFrame, mix: str = "all_weather", leverage: float = 1.0,
               fallback: str = "IEF") -> pd.DataFrame:
    """Fixed weights rebalanced every day (no cost charged after day one).
    An asset without data yet hands its share to `fallback`."""
    w = _frame(prices)
    for a, s in STATIC[mix].items():
        live = prices[a].notna()
        w[a] += s * leverage * live
        w[fallback] += s * leverage * ~live
    return w


# ---------- 6. low-volatility sector rotation ----------

SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]


def low_vol_sectors(prices: pd.DataFrame, top_n: int = 3, vol_window: int = 126,
                    tranches: int = 4, spacing: int = 5) -> pd.DataFrame:
    """Hold the `top_n` calmest sectors (lowest trailing volatility), equal weight."""
    vol = prices[SECTORS].pct_change().rolling(vol_window).std()
    rank = vol.rank(axis=1, method="first")
    sig = (rank <= top_n).astype(float) / top_n
    sig = sig.where(vol.notna().all(axis=1), 0.0)

    def build(offset):
        return _hold_monthly(prices, sig, offset)
    return _tranches(build, prices, tranches, spacing)


# ---------- 7. economy + price trend (Growth-Trend Timing) ----------

def monthly_macro_at(index: pd.DatetimeIndex, series: pd.Series, release_day: int = 10) -> pd.Series:
    """Map a monthly macro series (dated on the 1st of its month) onto daily
    dates, using only values already published: month m's figure counts from
    day `release_day` of month m + 1 (US unemployment comes out on the first
    Friday, so the September figure is known by October 10th)."""
    s = series.copy()
    s.index = (s.index.to_period("M") + 1).to_timestamp() + pd.Timedelta(days=release_day - 1)
    return s.reindex(index.union(s.index)).ffill().reindex(index)


def growth_trend(prices: pd.DataFrame, unrate: pd.Series, asset: str = "SPY", sma_months: int = 10,
                 ur_months: int = 12, safe: str | None = "IEF", tranches: int = 1,
                 spacing: int = 5, hold: dict | None = None) -> pd.DataFrame:
    """Philosophical Economics' Growth-Trend Timing: stay invested unless BOTH
    the unemployment rate is above its 12-month average (economy weakening)
    AND the price is below its 10-month average. Checked monthly.
    hold: what to own while invested (default 100% `asset`), e.g.
    {"QQQ": .5, "QQQ3X": .5} for 2x Nasdaq; `asset` is still the trend signal."""
    ur = unrate.dropna()
    ur_up = (ur > ur.rolling(ur_months).mean()).astype(float)
    ur_up = monthly_macro_at(prices.index, ur_up).fillna(0.0) > 0
    px = prices[asset]
    ends = _period_ends(prices.index, "M")
    sma = px.loc[ends].rolling(sma_months).mean().reindex(prices.index).ffill()
    below = px < sma
    out = (ur_up & below).astype(float)
    sig = _frame(prices)
    for a, share in (hold or {asset: 1.0}).items():
        sig[a] = share * (1 - out)

    def build(offset):
        w = _hold_monthly(prices, sig, offset)
        return _idle(w, safe)
    return _tranches(build, prices, tranches, spacing)


def credit_trend(prices: pd.DataFrame, spread: pd.Series, asset: str = "SPY", spread_window: int = 252,
                 sma_days: int = 200, safe: str | None = "IEF") -> pd.DataFrame:
    """Same idea with corporate credit: leave stocks only when the Baa-Treasury
    spread is above its 1-year average (credit stress building) AND the price
    is below its 200-day average. Checked daily (the spread is published daily,
    used one day late to be safe)."""
    sp = spread.reindex(prices.index).ffill().shift(1)
    stress = sp > sp.rolling(spread_window).mean()
    px = prices[asset]
    below = px < px.rolling(sma_days).mean()
    w = _frame(prices)
    w[asset] = 1 - (stress & below).astype(float)
    return _idle(w, safe)


# ---------- 8. seasonality: "sell in May" ----------

def halloween(prices: pd.DataFrame, asset: str = "SPY", in_month: int = 11, out_month: int = 5,
              safe: str | None = "IEF") -> pd.DataFrame:
    """Hold stocks from the start of `in_month` until the start of `out_month`
    (default November to April), bonds the rest of the year."""
    m = prices.index.month
    nxt = pd.Series(prices.index, index=prices.index).shift(-1)
    nxt_month = nxt.dt.month.fillna(m[-1] if len(m) else 1).astype(int).values
    if in_month < out_month:
        hold = (nxt_month >= in_month) & (nxt_month < out_month)
    else:
        hold = (nxt_month >= in_month) | (nxt_month < out_month)
    w = _frame(prices)
    w[asset] = hold.astype(float)  # decided at today's close for tomorrow's month
    return _idle(w, safe)


# ---------- 9. two-asset risk parity ----------

def risk_parity(prices: pd.DataFrame, assets: tuple[str, ...] = ("SPY", "TLT"), vol_window: int = 63,
                tranches: int = 4, spacing: int = 5) -> pd.DataFrame:
    """Stocks and long bonds weighted so each contributes the same volatility
    (inverse-volatility weights), rebalanced monthly in 4 tranches."""
    a = list(assets)
    inv = 1 / prices[a].pct_change().rolling(vol_window).std()
    sig = inv.div(inv.sum(axis=1), axis=0).fillna(0.0)

    def build(offset):
        return _hold_monthly(prices, sig, offset)
    return _tranches(build, prices, tranches, spacing)


# ---------- 10. leveraged stocks + leveraged bonds (HFEA) ----------

def leveraged_pair(prices: pd.DataFrame, stock: str = "SPY3X", bond: str = "TLT3X",
                   stock_share: float = 0.55) -> pd.DataFrame:
    """'Hedgefundie's Excellent Adventure': 3x S&P 500 (UPRO) and 3x long
    Treasuries (TMF) at fixed weights. Long bonds usually rise when stocks
    crash, so the pair is far smoother than either alone, but it fails when
    stocks and bonds fall together (2022). Weights are held at target."""
    w = _frame(prices)
    w[stock] = stock_share
    w[bond] = 1 - stock_share
    return w


# ---------- 11. volatility-managed stocks (Moreira & Muir 2017) ----------

def vol_managed(prices: pd.DataFrame, asset: str = "SPY", lev_asset: str = "SPY3X", target: float = 0.15,
                window: int = 21, max_lev: float = 2.0) -> pd.DataFrame:
    """Scale stock exposure by target / recent realized volatility, between 0
    and `max_lev` times. Exposure above 1x comes from the 3x ETF:
    x in `lev_asset` and 1 - x in `asset` gives 1 + 2x exposure. Rebalanced
    at month ends only, using the volatility of the last `window` sessions."""
    vol = prices[asset].pct_change().rolling(window).std() * np.sqrt(252)
    lev = (target / vol).clip(upper=max_lev).fillna(1.0)
    ends = _period_ends(prices.index, "M")
    lev = lev.where(lev.index.isin(ends)).ffill().fillna(1.0)
    w = _frame(prices)
    over = ((lev - 1) / 2).clip(lower=0)
    w[lev_asset] = over
    w[asset] = lev.clip(upper=1) - over
    return w


def leveraged_parity(prices: pd.DataFrame, assets: tuple[str, ...] = ("SPY3X", "TLT3X"), vol_window: int = 63,
                     tranches: int = 4, spacing: int = 5) -> pd.DataFrame:
    """HFEA with volatility-based weights instead of fixed 55/45."""
    return risk_parity(prices, assets, vol_window, tranches, spacing)


# ---------- 12. sector short-term reversal ----------

def sector_reversal(prices: pd.DataFrame, top_n: int = 3, lookback: int = 21,
                    tranches: int = 4, spacing: int = 5) -> pd.DataFrame:
    """Buy the `top_n` sectors that fell the most over the last month
    (short-term overreaction tends to partly reverse), equal weight."""
    ret = prices[SECTORS].pct_change(lookback)
    rank = ret.rank(axis=1, method="first")
    sig = (rank <= top_n).astype(float) / top_n
    sig = sig.where(ret.notna().all(axis=1), 0.0)

    def build(offset):
        return _hold_monthly(prices, sig, offset)
    return _tranches(build, prices, tranches, spacing)
