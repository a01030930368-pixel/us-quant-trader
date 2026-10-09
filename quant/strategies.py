"""Strategies: functions that turn prices into target weights.

Each strategy returns a DataFrame of target weights (same index as prices)
using only information available at each row's close.
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

Strategy = Callable[..., pd.DataFrame]


def _month_ends(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby(index.to_period("M")).last().values)


def _period_ends(index: pd.DatetimeIndex, freq: str, offset: int = 0) -> pd.DatetimeIndex:
    """Last session of each month ("M"), week ("W") or every session ("D").

    offset: for "M", use the session `offset` trading days after each month
    end instead (e.g. 5 = about a week into the next month).
    """
    if freq == "D":
        return index
    if freq == "M":
        ends = _month_ends(index)
        if offset:
            pos = index.get_indexer(ends) + offset
            ends = index[pos[pos < len(index)]]
        return ends
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby(index.to_period("W-FRI")).last().values)


def _monthly(prices: pd.DataFrame, signal: pd.DataFrame, freq: str = "M",
             offset: int = 0) -> pd.DataFrame:
    """Keep period-end rows of `signal` (month end by default) and hold them
    until the next period end."""
    ends = _period_ends(prices.index, freq, offset)
    w = pd.DataFrame(index=prices.index, columns=prices.columns, dtype=float)
    w.loc[ends] = signal.loc[ends].reindex(columns=prices.columns).values
    return w.ffill().fillna(0.0)


def buy_and_hold(prices: pd.DataFrame, asset: str = "SPY") -> pd.DataFrame:
    w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    w[asset] = 1.0
    return w


def sma_trend(prices: pd.DataFrame, asset: str = "SPY", window: int = 200,
              safe: str | None = None, signal: str | None = None) -> pd.DataFrame:
    """Hold `asset` while `signal` (default: asset itself) is above its moving
    average, else `safe` (or cash). Use signal="QQQ" to time a leveraged QQQ ETF
    on the unleveraged index, which whipsaws less."""
    signal = signal or asset
    sig = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    ma = prices[signal].rolling(window).mean()
    above = prices[signal] > ma
    sig[asset] = above.astype(float)
    if safe:
        sig[safe] = (~above).astype(float) * ma.notna()
    return _monthly(prices, sig)


def momentum_rotation(prices: pd.DataFrame, assets: list[str], lookback: int = 126,
                      top_n: int = 1, abs_filter: bool = True,
                      safe: str | None = None, lookbacks: list[int] | None = None,
                      freq: str = "M", offset: int = 0) -> pd.DataFrame:
    """Each month hold the top_n assets by trailing return (equal weight).

    abs_filter: an asset only qualifies if its own trailing return is > 0
    (dual momentum); slots that fail go to `safe` or cash.
    lookbacks: if given, rank on the average of several trailing returns
    instead of one `lookback` (less sensitive to the exact window).
    freq: "M" monthly (default), "W" weekly or "D" daily re-ranking.
    offset: rank `offset` trading days after each month end (see _period_ends).
    """
    if lookbacks:
        mom = sum(prices[assets].pct_change(lb) for lb in lookbacks) / len(lookbacks)
    else:
        mom = prices[assets].pct_change(lookback)
    sig = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    # Only period-end rows are kept by _monthly, so only rank those.
    ends = _period_ends(prices.index, freq, offset)
    for dt, row in mom.loc[ends].dropna(how="all").iterrows():
        picks = row.dropna().nlargest(top_n)
        for name, value in picks.items():
            if abs_filter and value <= 0:
                if safe:
                    sig.at[dt, safe] += 1.0 / top_n
                continue
            sig.at[dt, name] += 1.0 / top_n
    return _monthly(prices, sig, freq, offset)


def rsi2_reversion(prices: pd.DataFrame, asset: str = "SPY", entry: float = 10,
                   exit_: float = 70, trend_window: int = 200) -> pd.DataFrame:
    """Daily short-term mean reversion: buy oversold dips while in an uptrend."""
    px = prices[asset]
    delta = px.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, 1e-12))
    uptrend = px > px.rolling(trend_window).mean()

    pos = pd.Series(0.0, index=px.index)
    holding = False
    for i, dt in enumerate(px.index):
        if not holding and uptrend.iat[i] and rsi.iat[i] < entry:
            holding = True
        elif holding and (rsi.iat[i] > exit_ or not uptrend.iat[i]):
            holding = False
        pos.iat[i] = 1.0 if holding else 0.0
    w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    w[asset] = pos
    return w


def blend(prices: pd.DataFrame, parts: list, name: str = "") -> pd.DataFrame:
    """Run several strategies side by side with fixed capital shares.

    parts: [(strategy_name, params, share), ...]; shares should sum to 1.
    Each sleeve keeps its own rules; the total is their weighted sum.
    """
    total = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for strat, params, share in parts:
        total = total.add(REGISTRY[strat](prices, **params) * share, fill_value=0.0)
    return total


STABLE = ["SPY", "QQQ", "IWM", "EFA", "GLD"]
AGGR = ["QQQ3X", "SPY3X", "GLD", "TLT"]
LEV_BASE = {"QQQ3X": "QQQ", "SPY3X": "SPY"}


def _sample(flag: pd.Series, ends: pd.DatetimeIndex) -> pd.Series:
    """Read a daily True/False flag only on `ends` and hold it in between."""
    f = flag.astype(float).where(flag.index.isin(ends))
    return f.ffill().fillna(0.0) > 0.5


def _move_to(w: pd.DataFrame, cut: pd.DataFrame, to: str | None) -> pd.DataFrame:
    """Remove `cut` (same shape as w) from w and add its row sum to `to`
    (or leave it as cash when `to` is None)."""
    w = w - cut
    if to:
        w[to] = w[to] + cut.sum(axis=1)
    return w


def _trend_gate(prices: pd.DataFrame, w: pd.DataFrame, window: int, safe: str | None,
                check: str) -> pd.DataFrame:
    """Drop a leveraged ETF while its unleveraged index is below its `window`-day
    average, checked every `check` period; the freed weight goes to `safe`."""
    ends = _period_ends(prices.index, check)
    cut = pd.DataFrame(0.0, index=w.index, columns=w.columns)
    for lev, base in LEV_BASE.items():
        if lev not in w.columns or base not in prices.columns:
            continue
        below = prices[base] < prices[base].rolling(window).mean()
        below = _sample(below, ends)
        cut[lev] = w[lev].where(below, 0.0)
    return _move_to(w, cut, safe)


def _vol_scale(prices: pd.DataFrame, w: pd.DataFrame, target: float, window: int,
               safe: str | None, check: str) -> pd.DataFrame:
    """Shrink the risky part of `w` so its recent volatility (current holdings,
    last `window` sessions, annualised) is at most `target`. Checked every
    `check` period; the freed weight goes to `safe`."""
    rets = prices.pct_change()
    risky = [c for c in w.columns if c != safe]
    scale = pd.Series(float("nan"), index=w.index)
    for dt in _period_ends(prices.index, check):
        i = prices.index.get_loc(dt)
        if i < window:
            continue
        hold = w.loc[dt, risky]
        hold = hold[hold > 0]
        if hold.empty:
            scale[dt] = 1.0
            continue
        r = rets.iloc[i - window + 1:i + 1][hold.index].fillna(0.0) @ hold
        vol = float(r.std() * 252 ** 0.5)
        scale[dt] = min(1.0, target / vol) if vol > 0 else 1.0
    scale = scale.ffill().fillna(1.0)
    cut = w[risky].mul(1 - scale, axis=0).reindex(columns=w.columns, fill_value=0.0)
    return _move_to(w, cut, safe)


def momentum_blend(prices: pd.DataFrame, aggr_share: float = 0.5, lookback: int = 63,
                   lookbacks: list[int] | None = None, rebalance: str = "M",
                   safe: str = "IEF", trend_window: int | None = None,
                   trend_check: str = "D", vol_target: float | None = None,
                   vol_window: int = 20, vol_check: str = "W",
                   safe_filter: bool = False, offset: int = 0,
                   stable_assets: list[str] | None = None,
                   aggr_assets: list[str] | None = None) -> pd.DataFrame:
    """The stable/3x momentum blend with optional ways to cut losses sooner.

    Defaults reproduce the original 50/50 blend (monthly, lookback 63):
      stable sleeve: best of STABLE by momentum, else `safe`;
      aggressive sleeve: best 2 of AGGR by momentum, else `safe`.
    Options (all off by default):
      rebalance: re-rank "W" weekly or "D" daily instead of monthly.
      lookbacks: rank on the average of several windows, e.g. [21, 63, 126, 252].
      trend_window: hold a 3x ETF only while its index (QQQ/SPY) is above this
        moving average, checked every `trend_check`.
      vol_target: cap the aggressive sleeve's recent volatility (e.g. 0.4 = 40%/yr),
        checked every `vol_check`.
      safe_filter: hold cash instead of `safe` while `safe` itself is falling
        (its own `lookback` return <= 0), e.g. bonds in 2022.
      offset: monthly rebalance `offset` trading days after month end.
      stable_assets / aggr_assets: replace the STABLE / AGGR candidate lists.
    Weights change on the day a check fires, so a runner that executes them
    must run on every check day, not only at month end.
    """
    kw = dict(lookback=lookback, lookbacks=lookbacks, safe=safe, freq=rebalance, offset=offset)
    stable = momentum_rotation(prices, stable_assets or STABLE, top_n=1, **kw)
    aggr = momentum_rotation(prices, aggr_assets or AGGR, top_n=2, **kw)
    if trend_window:
        aggr = _trend_gate(prices, aggr, trend_window, safe, trend_check)
    if vol_target:
        aggr = _vol_scale(prices, aggr, vol_target, vol_window, safe, vol_check)
    total = stable * (1 - aggr_share) + aggr * aggr_share
    if safe_filter:
        falling = prices[safe].pct_change(lookback) <= 0
        ends = _period_ends(prices.index, rebalance)
        falling = _sample(falling, ends)
        total[safe] = total[safe].where(~falling, 0.0)
    return total


def tranched(prices: pd.DataFrame, strategy: str = "momentum_blend", tranches: int = 4,
             spacing: int = 5, **params) -> pd.DataFrame:
    """Split the money into `tranches` equal parts that each run `strategy`
    monthly but rebalance on different days (month end, +spacing, +2*spacing
    trading days, ...). Removes the luck of one fixed rebalance day; the
    portfolio changes a little every `spacing` days instead of all at once."""
    parts = [REGISTRY[strategy](prices, offset=k * spacing, **params) for k in range(tranches)]
    return sum(parts) / tranches


def core_satellite(prices: pd.DataFrame, core: str = "QQQ", core_share: float = 0.5,
                   strategy: str = "momentum_blend", **params) -> pd.DataFrame:
    """Always hold `core_share` in `core`; run `strategy` with the rest."""
    w = REGISTRY[strategy](prices, **params) * (1 - core_share)
    w[core] = w[core] + core_share
    return w


def momentum_13612(prices: pd.DataFrame) -> pd.DataFrame:
    """Keller's 13612W momentum: weighted 1/3/6/12-month returns (fast)."""
    r = {m: prices.pct_change(21 * m) for m in (1, 3, 6, 12)}
    return (12 * r[1] + 4 * r[3] + 2 * r[6] + r[12]) / 4


def canary_gate(prices: pd.DataFrame, canary: list[str] | None = None,
                safe: str | None = "IEF",
                strategy: str = "momentum_blend", **params) -> pd.DataFrame:
    """Defensive Asset Allocation style breadth gate on a whole strategy.

    Each month end look at the canaries (default emerging markets EEM and
    aggregate bonds AGG). If both have positive 13612W momentum, run the
    strategy in full; if one is negative, half of it moves to `safe`; if both
    are negative, everything goes to `safe` (None = cash). Unlike a trend filter on the held
    asset, this reads risk appetite in other markets, which tends to turn first.
    """
    canary = canary or ["EEM", "AGG"]
    w = REGISTRY[strategy](prices, **params)
    bad = (momentum_13612(prices[canary]) <= 0).sum(axis=1) / len(canary)
    bad = bad.where(momentum_13612(prices[canary]).notna().all(axis=1), 0.0)
    ends = _period_ends(prices.index, "M", params.get("offset", 0))
    risk_off = bad.where(bad.index.isin(ends)).ffill().fillna(0.0)
    cut = w.mul(risk_off, axis=0)
    return _move_to(w, cut, safe)


REGISTRY: dict[str, Strategy] = {
    "buy_and_hold": buy_and_hold,
    "sma_trend": sma_trend,
    "momentum_rotation": momentum_rotation,
    "rsi2_reversion": rsi2_reversion,
    "blend": blend,
    "momentum_blend": momentum_blend,
    "tranched": tranched,
    "core_satellite": core_satellite,
    "canary_gate": canary_gate,
}
