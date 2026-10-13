"""장중 시세: 보유 종목의 현재가, 전일 종가, 오늘 5분봉 흐름을 야후 파이낸스에서 받는다.

Mintory 화면 표시용이다. 주문 판단에는 쓰지 않는다. 같은 종목 묶음은 15초 동안
다시 받지 않아서, 화면을 여러 개 열어도 야후에 요청이 몰리지 않는다.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime

import pandas as pd

TTL = 15            # seconds a fetched quote set is reused
MAX_SYMBOLS = 12
SYMBOL = re.compile(r"^\^?[A-Z][A-Z0-9.\-]{0,9}(=X)?$")  # ^GSPC: Yahoo index, KRW=X: won per dollar

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}


def clean_symbols(raw: str) -> list[str]:
    out = []
    for s in raw.upper().split(","):
        s = s.strip()
        if SYMBOL.match(s) and s not in out:
            out.append(s)
    return out[:MAX_SYMBOLS]


def summarize(close: pd.DataFrame) -> dict:
    """5-minute closes for a few days (index tz-aware, columns = symbols) ->
    {symbol: {price, prev_close, change, time, session, spark}} for the last session."""
    out = {}
    for sym in close.columns:
        s = close[sym].dropna()
        if s.empty:
            continue
        days = s.index.normalize()
        last_day = days[-1]
        today = s[days == last_day]
        before = s[days < last_day]
        price = float(today.iloc[-1])
        prev = float(before.iloc[-1]) if len(before) else None
        out[sym] = {
            "price": round(price, 4),
            "prev_close": round(prev, 4) if prev else None,
            "change": round(price / prev - 1, 6) if prev else None,
            "time": today.index[-1].isoformat(),
            "session": str(last_day.date()),
            "spark": [round(float(v), 4) for v in today.tolist()],
        }
    return out


def _download(symbols: list[str]) -> pd.DataFrame:
    import yfinance as yf
    raw = yf.download(symbols, period="5d", interval="5m", auto_adjust=False, prepost=False,
                      progress=False, group_by="column", threads=False)
    if raw is None or raw.empty:
        return pd.DataFrame()
    close = raw["Close"]
    return close.to_frame(symbols[0]) if isinstance(close, pd.Series) else close


def quotes(symbols: list[str], fetch=None) -> dict:
    """Latest quotes for `symbols`, cached for TTL seconds. Never raises."""
    key = tuple(sorted(symbols))
    if not key:
        return {"quotes": {}, "as_of": None}
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
        try:
            data = {"quotes": summarize((fetch or _download)(list(key))),
                    "as_of": datetime.now().astimezone().isoformat(timespec="seconds")}
        except Exception as e:  # noqa: BLE001 - the page just keeps its last prices
            data = {"quotes": {}, "as_of": None, "error": str(e)[:200]}
        _cache[key] = (time.monotonic(), data)
        return data


# -- one symbol's price history for the chart window ---------------------------
# range -> (Yahoo period, bar interval, seconds a fetch is reused)
RANGES = {
    "1D": ("1d", "5m", 30), "1W": ("5d", "30m", 120), "1M": ("1mo", "1d", 900),
    "3M": ("3mo", "1d", 900), "1Y": ("1y", "1d", 1800), "5Y": ("5y", "1wk", 3600),
}
_hist: dict[tuple, tuple[float, dict]] = {}


def _download_history(symbol: str, period: str, interval: str) -> pd.Series:
    import yfinance as yf
    raw = yf.download(symbol, period=period, interval=interval, auto_adjust=False, prepost=False,
                      progress=False, threads=False)
    if raw is None or raw.empty:
        return pd.Series(dtype=float)
    close = raw["Close"]
    return close.iloc[:, 0] if isinstance(close, pd.DataFrame) else close


def history(symbol: str, rng: str, fetch=None) -> dict:
    """{symbol, range, intraday, points: [[time, close]], prev_close} for the chart.
    Cached per (symbol, range). Never raises."""
    syms = clean_symbols(symbol or "")
    rng = rng if rng in RANGES else "1M"
    if not syms:
        return {"error": "종목 이름이 올바르지 않아요.", "points": []}
    sym = syms[0]
    period, interval, ttl = RANGES[rng]
    key = (sym, rng)
    with _lock:
        hit = _hist.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    try:
        s = (fetch or _download_history)(sym, period, interval).dropna()
        intraday = interval.endswith("m")
        fmt = (lambda t: t.isoformat()) if intraday else (lambda t: str(t.date()))
        data = {"symbol": sym, "range": rng, "intraday": intraday,
                "points": [[fmt(t), round(float(v), 4)] for t, v in s.items()]}
        if rng == "1D" and data["points"]:
            day = (fetch or _download_history)(sym, "5d", "1d").dropna()
            day = day[[str(t.date()) < data["points"][-1][0][:10] for t in day.index]]
            data["prev_close"] = round(float(day.iloc[-1]), 4) if len(day) else None
    except Exception as e:  # noqa: BLE001 - the window just says it couldn't load
        return {"symbol": sym, "range": rng, "points": [], "error": str(e)[:200]}
    with _lock:
        _hist[key] = (time.monotonic(), data)
    return data
