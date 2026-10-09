"""Vectorised portfolio backtest driven by target weights.

Weights decided with data up to day t are traded at day t's close and earn
returns from t+1 onward, so a strategy never sees its own future.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class BacktestResult:
    equity: pd.Series      # portfolio value, starts at 1.0
    returns: pd.Series     # daily net returns
    weights: pd.Series | pd.DataFrame
    turnover: pd.Series    # sum of |weight change| per day
    n_trades: int          # days on which the portfolio was rebalanced


def run_backtest(prices: pd.DataFrame, weights: pd.DataFrame,
                 cost_bps: float = 10.0,
                 opens: pd.DataFrame | None = None) -> BacktestResult:
    """Simulate holding `weights` (rows sum to <=1, remainder is cash).

    cost_bps: one-way cost per unit of turnover in basis points
    (commission + slippage + FX spread).
    opens: if given, weights decided at day t's close are traded at day t+1's
    open (realistic for a bot that reads the close and orders next morning).
    Otherwise they are traded at day t's close.
    """
    weights = weights.reindex(prices.index).ffill().fillna(0.0)
    weights = weights.reindex(columns=prices.columns, fill_value=0.0)

    if opens is None:
        asset_ret = prices.pct_change().fillna(0.0)
        held = weights.shift(1).fillna(0.0)
        gross = (held * asset_ret).sum(axis=1)
    else:
        opens = opens.reindex(index=prices.index, columns=prices.columns)
        overnight = (opens / prices.shift(1) - 1).fillna(0.0)
        intraday = (prices / opens - 1).fillna(0.0)
        before = weights.shift(2).fillna(0.0)   # held until this morning's open
        after = weights.shift(1).fillna(0.0)    # bought at this morning's open
        gross = ((1 + (before * overnight).sum(axis=1))
                 * (1 + (after * intraday).sum(axis=1)) - 1)
        weights = weights.shift(1).fillna(0.0)  # trades happen a day later
    turnover = weights.diff().abs().sum(axis=1)
    turnover.iloc[0] = weights.iloc[0].abs().sum()
    net = gross - turnover * cost_bps / 10_000

    equity = (1 + net).cumprod()
    n_trades = int((turnover > 1e-9).sum())
    return BacktestResult(equity, net, weights, turnover, n_trades)
