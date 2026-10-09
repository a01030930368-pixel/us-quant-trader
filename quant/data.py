"""Daily adjusted close prices from Yahoo Finance, cached as CSV."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

CACHE_DIR = Path("data_cache")


def load_prices(tickers: list[str], start: str, end: str | None = None,
                use_cache: bool = True, field: str = "Close") -> pd.DataFrame:
    """Return adjusted prices (index=date, columns=tickers).

    field: "Close" or "Open" (both split/dividend adjusted).
    """
    CACHE_DIR.mkdir(exist_ok=True)
    key = f"{'_'.join(sorted(tickers))}_{start}_{end or 'latest'}_{field}.csv"
    path = CACHE_DIR / key
    if use_cache and path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True)

    import yfinance as yf

    def fetch(names: list[str]) -> pd.DataFrame:
        raw = yf.download(names, start=start, end=end, auto_adjust=True,
                          progress=False, group_by="column", threads=len(names) > 1)
        if raw.empty:
            return pd.DataFrame()
        out = raw[field]
        return out.to_frame(names[0]) if isinstance(out, pd.Series) else out

    prices = fetch(tickers).reindex(columns=tickers)
    # yfinance sometimes drops a ticker (e.g. "database is locked" in its
    # cache when downloading in parallel); fetch the missing ones again.
    for _ in range(2):
        missing = [t for t in tickers if prices[t].isna().all()]
        if not missing:
            break
        for t in missing:
            again = fetch([t])
            if t in again:
                prices = prices.combine_first(again[[t]])[tickers]
    missing = [t for t in tickers if prices[t].isna().all()]
    if missing:
        raise RuntimeError(f"가격 데이터를 받지 못했습니다: {', '.join(missing)}. "
                           "인터넷 연결과 티커를 확인하세요.")
    prices = prices.dropna(how="all").ffill()
    prices.to_csv(path)
    return prices


def add_leveraged(prices: pd.DataFrame, base: str, name: str, leverage: float = 3.0,
                  annual_cost: float = 0.03) -> pd.DataFrame:
    """Add a simulated daily-rebalanced leveraged ETF built from `base`.

    Real 3x ETFs (TQQQ, UPRO) only exist from 2010, so simulating them lets the
    backtest include 2008. annual_cost approximates the expense ratio plus the
    financing cost of the borrowed exposure.
    """
    daily = prices[base].pct_change().fillna(0.0) * leverage - annual_cost / 252
    out = prices.copy()
    out[name] = 100 * (1 + daily).cumprod()
    return out
