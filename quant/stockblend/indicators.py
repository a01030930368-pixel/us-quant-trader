"""Technical indicators on wide frames (index=date, columns=tickers)."""
from __future__ import annotations

import pandas as pd


def sma(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.rolling(n, min_periods=n).mean()


def rsi(close: pd.DataFrame, n: int) -> pd.DataFrame:
    """Wilder's RSI. 0 = only falls lately, 100 = only rises."""
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = up / down
    out = 100 - 100 / (1 + rs)
    return out.where(down > 0, 100.0).where(up.notna())


def atr(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int) -> pd.DataFrame:
    """Average true range: the typical daily move in dollars."""
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()]).groupby(level=0).max()
    tr = tr.reindex(close.index)
    return tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()


def ema(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.ewm(span=n, min_periods=n, adjust=False).mean()


def adx(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14):
    """Wilder's ADX with +DI and -DI. ADX above ~25-30 = a strong trend either way."""
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = atr(high, low, close, 1)          # true range (EWM with alpha 1 = itself)
    def wilder(x):
        return x.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    atr_n = wilder(tr)
    plus_di = 100 * wilder(plus_dm) / atr_n
    minus_di = 100 * wilder(minus_dm) / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return wilder(dx), plus_di, minus_di
