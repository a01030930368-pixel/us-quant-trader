"""Daily OHLC prices and volume from Yahoo Finance (split/dividend adjusted), cached as CSV.

Adapted from us-quant-trader's quant/data.py: swing trading needs open, high
and low as well as close, because entries fill at the next open and stops are
checked against the day's low.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

CACHE_DIR = Path("data_cache")
FIELDS = ("Open", "High", "Low", "Close", "Volume")


def load_ohlc(tickers: list[str], start: str, end: str | None = None,
              use_cache: bool = True) -> dict[str, pd.DataFrame]:
    """Return {"Open"|"High"|"Low"|"Close"|"Volume": frame(index=date, columns=tickers)}.

    Tickers that return no data are dropped (with a printed warning) instead of
    failing the whole run, since a 100-stock universe always has a straggler.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.sha1(",".join(sorted(tickers)).encode()).hexdigest()[:10]
    paths = {f: CACHE_DIR / f"ohlc_{key}_{start}_{end or 'latest'}_{f}.csv" for f in FIELDS}
    if use_cache and all(p.exists() for p in paths.values()):
        return {f: pd.read_csv(p, index_col=0, parse_dates=True) for f, p in paths.items()}

    import yfinance as yf

    raw = yf.download(tickers, start=start, end=end, auto_adjust=True,
                      progress=False, group_by="column", threads=True)
    out = {f: raw[f].reindex(columns=tickers) for f in FIELDS}
    # yfinance sometimes drops tickers when downloading in parallel; retry them one by one.
    missing = [t for t in tickers if out["Close"][t].isna().all()]
    for t in missing:
        one = yf.download(t, start=start, end=end, auto_adjust=True, progress=False)
        if one.empty:
            continue
        if isinstance(one.columns, pd.MultiIndex):
            one.columns = one.columns.get_level_values(0)
        for f in FIELDS:
            out[f][t] = one[f].reindex(out[f].index)
    dead = [t for t in tickers if out["Close"][t].isna().all()]
    if dead:
        print(f"경고: 가격을 받지 못해 제외한 종목: {', '.join(dead)}")
    keep = [t for t in tickers if t not in dead]
    if not keep:
        raise RuntimeError("가격 데이터를 하나도 받지 못했습니다. 인터넷 연결을 확인하세요.")
    index = out["Close"][keep].dropna(how="all").index
    for f in FIELDS:
        out[f] = out[f].loc[index, keep]
        out[f].to_csv(paths[f])
    return out
