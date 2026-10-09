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


def _monthly(prices: pd.DataFrame, signal: pd.DataFrame) -> pd.DataFrame:
    """Keep month-end rows of `signal`, hold them until the next month end."""
    ends = _month_ends(prices.index)
    w = pd.DataFrame(index=prices.index, columns=prices.columns, dtype=float)
    w.loc[ends] = signal.loc[ends].reindex(columns=prices.columns).values
    return w.ffill().fillna(0.0)


def buy_and_hold(prices: pd.DataFrame, asset: str = "SPY") -> pd.DataFrame:
    w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    w[asset] = 1.0
    return w


def sma_trend(prices: pd.DataFrame, asset: str = "SPY", window: int = 200,
              safe: str | None = None) -> pd.DataFrame:
    """Hold `asset` while its close is above its moving average, else `safe` (or cash)."""
    sig = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    above = prices[asset] > prices[asset].rolling(window).mean()
    sig[asset] = above.astype(float)
    if safe:
        sig[safe] = (~above).astype(float) * prices[asset].rolling(window).mean().notna()
    return _monthly(prices, sig)


def momentum_rotation(prices: pd.DataFrame, assets: list[str], lookback: int = 126,
                      top_n: int = 1, abs_filter: bool = True,
                      safe: str | None = None) -> pd.DataFrame:
    """Each month hold the top_n assets by trailing return (equal weight).

    abs_filter: an asset only qualifies if its own trailing return is > 0
    (dual momentum); slots that fail go to `safe` or cash.
    """
    mom = prices[assets].pct_change(lookback)
    sig = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for dt, row in mom.dropna(how="all").iterrows():
        picks = row.dropna().nlargest(top_n)
        for name, value in picks.items():
            if abs_filter and value <= 0:
                if safe:
                    sig.at[dt, safe] += 1.0 / top_n
                continue
            sig.at[dt, name] += 1.0 / top_n
    return _monthly(prices, sig)


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


REGISTRY: dict[str, Strategy] = {
    "buy_and_hold": buy_and_hold,
    "sma_trend": sma_trend,
    "momentum_rotation": momentum_rotation,
    "rsi2_reversion": rsi2_reversion,
}
