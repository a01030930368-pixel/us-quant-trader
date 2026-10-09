"""Performance metrics computed from daily returns."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def cagr(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    total = float((1 + returns).prod())
    years = len(returns) / TRADING_DAYS
    return total ** (1 / years) - 1 if total > 0 else -1.0


def volatility(returns: pd.Series) -> float:
    return float(returns.std() * np.sqrt(TRADING_DAYS))


def sharpe(returns: pd.Series, rf: float = 0.0) -> float:
    excess = returns - rf / TRADING_DAYS
    sd = excess.std()
    return float(excess.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0


def sortino(returns: pd.Series, rf: float = 0.0) -> float:
    excess = returns - rf / TRADING_DAYS
    downside = excess[excess < 0].std()
    return float(excess.mean() / downside * np.sqrt(TRADING_DAYS)) if downside > 0 else 0.0


def max_drawdown(returns: pd.Series) -> float:
    """Most negative peak-to-trough drop, e.g. -0.25 for -25%."""
    equity = (1 + returns).cumprod()
    return float((equity / equity.cummax() - 1).min()) if len(equity) else 0.0


def calmar(returns: pd.Series) -> float:
    mdd = abs(max_drawdown(returns))
    return cagr(returns) / mdd if mdd > 0 else 0.0


def monthly_returns(returns: pd.Series) -> pd.Series:
    return (1 + returns).groupby(returns.index.to_period("M")).prod() - 1


def summarize(returns: pd.Series, turnover: pd.Series | None = None,
              n_trades: int | None = None) -> dict:
    out = {
        "cagr": cagr(returns),
        "vol": volatility(returns),
        "sharpe": sharpe(returns),
        "sortino": sortino(returns),
        "mdd": max_drawdown(returns),
        "calmar": calmar(returns),
    }
    if isinstance(returns.index, pd.DatetimeIndex) and len(returns):
        m = monthly_returns(returns)
        out["month_avg"] = float(m.mean())
        out["month_win_rate"] = float((m > 0).mean())
        out["worst_month"] = float(m.min())
    if turnover is not None:
        years = max(len(turnover) / TRADING_DAYS, 1e-9)
        out["turnover_per_year"] = float(turnover.sum() / years)
    if n_trades is not None:
        out["n_trades"] = n_trades
    return out
