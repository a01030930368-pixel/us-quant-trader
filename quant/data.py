"""Daily adjusted close prices from Yahoo Finance, cached as CSV."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

CACHE_DIR = Path("data_cache")


def load_prices(tickers: list[str], start: str, end: str | None = None,
                use_cache: bool = True) -> pd.DataFrame:
    """Return a DataFrame of adjusted closes (index=date, columns=tickers)."""
    CACHE_DIR.mkdir(exist_ok=True)
    key = f"{'_'.join(sorted(tickers))}_{start}_{end or 'latest'}.csv"
    path = CACHE_DIR / key
    if use_cache and path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True)

    import yfinance as yf

    raw = yf.download(tickers, start=start, end=end, auto_adjust=True,
                      progress=False, group_by="column")
    if raw.empty:
        raise RuntimeError("가격 데이터를 받지 못했습니다. 인터넷 연결과 티커를 확인하세요.")
    prices = raw["Close"]
    if isinstance(prices, pd.Series):
        prices = prices.to_frame(tickers[0])
    prices = prices[tickers].dropna(how="all").ffill()
    prices.to_csv(path)
    return prices
