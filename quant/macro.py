"""Economic series from FRED (St. Louis Fed), cached as CSV."""
from __future__ import annotations

import io

import pandas as pd

from quant.data import CACHE_DIR

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"


def load_fred(series_id: str, use_cache: bool = True) -> pd.Series:
    """Return a FRED series (index=date). E.g. UNRATE (monthly unemployment
    rate), BAA10Y (daily Moody's Baa yield minus 10-year Treasury)."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"fred_{series_id}.csv"
    if use_cache and path.exists():
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    else:
        import requests
        r = requests.get(URL.format(series_id), timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text), index_col=0, parse_dates=True)
        df.to_csv(path)
    s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
    s.name = series_id
    return s
