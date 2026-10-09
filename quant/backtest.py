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
                 cost_bps: float = 10.0) -> BacktestResult:
    """Simulate holding `weights` (rows sum to <=1, remainder is cash).

    cost_bps: one-way cost per unit of turnover in basis points
    (commission + slippage + FX spread).
    """
    weights = weights.reindex(prices.index).ffill().fillna(0.0)
    weights = weights.reindex(columns=prices.columns, fill_value=0.0)
    asset_ret = prices.pct_change().fillna(0.0)

    held = weights.shift(1).fillna(0.0)
    gross = (held * asset_ret).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    turnover.iloc[0] = weights.iloc[0].abs().sum()
    net = gross - turnover * cost_bps / 10_000

    equity = (1 + net).cumprod()
    n_trades = int((turnover > 1e-9).sum())
    return BacktestResult(equity, net, weights, turnover, n_trades)
