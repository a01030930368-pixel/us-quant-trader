import pandas as pd

from quant import live


def bars():
    idx = pd.DatetimeIndex(["2026-10-08 15:50", "2026-10-08 15:55",
                            "2026-10-09 09:30", "2026-10-09 09:35", "2026-10-09 09:40"],
                           tz="America/New_York")
    return pd.DataFrame({"QQQ": [600.0, 610.0, 612.0, 615.0, 616.1],
                         "TQQQ": [None, None, 100.0, None, 101.0]}, index=idx)


def test_clean_symbols_keeps_valid_tickers_only():
    assert live.clean_symbols(" qqq,TQQQ,qqq,,brk.b,<script>,1ABC") == ["QQQ", "TQQQ", "BRK.B"]
    assert len(live.clean_symbols(",".join(f"A{i}" for i in range(30)))) == live.MAX_SYMBOLS
    assert live.clean_symbols("^gspc,^VIX,^^X,^1") == ["^GSPC", "^VIX"]


def test_summarize_uses_last_session_against_previous_close():
    q = live.summarize(bars())
    assert q["QQQ"]["price"] == 616.1 and q["QQQ"]["prev_close"] == 610.0
    assert round(q["QQQ"]["change"], 4) == 0.01
    assert q["QQQ"]["session"] == "2026-10-09"
    assert q["QQQ"]["spark"] == [612.0, 615.0, 616.1]
    assert q["TQQQ"]["prev_close"] is None and q["TQQQ"]["change"] is None


def test_quotes_are_cached_and_never_raise(monkeypatch):
    monkeypatch.setattr(live, "_cache", {})
    calls = []

    def fetch(symbols):
        calls.append(symbols)
        return bars()

    a = live.quotes(["QQQ", "TQQQ"], fetch)
    b = live.quotes(["TQQQ", "QQQ"], fetch)
    assert a is b and len(calls) == 1 and a["as_of"]
    assert live.quotes([]) == {"quotes": {}, "as_of": None}

    def boom(symbols):
        raise ConnectionError("yahoo down")

    bad = live.quotes(["SPY"], boom)
    assert bad["quotes"] == {} and "yahoo down" in bad["error"]


def test_history_points_cache_and_errors():
    import pandas as pd
    from quant import live
    live._hist.clear()
    calls = []

    def fetch(sym, period, interval):
        calls.append((sym, period, interval))
        if interval == "1d" and period == "5d":
            return pd.Series([10.0, 11.0], index=pd.to_datetime(["2026-10-09", "2026-10-12"]))
        idx = pd.date_range("2026-10-12 09:30", periods=3, freq="5min", tz="America/New_York")
        return pd.Series([11.5, None, 12.0], index=idx)

    d = live.history("qqq", "1D", fetch)
    assert d["symbol"] == "QQQ" and d["intraday"] and len(d["points"]) == 2
    assert d["points"][0][0].startswith("2026-10-12T09:30") and d["prev_close"] == 10.0
    assert live.history("QQQ", "1D", fetch) is d and len(calls) == 2       # cached

    daily = live.history("QQQ", "bogus", lambda *a: pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-10-09", "2026-10-12"])))
    assert daily["range"] == "1M" and daily["points"] == [["2026-10-09", 1.0], ["2026-10-12", 2.0]]
    assert live.history("<x>", "1M")["points"] == []
    bad = live.history("SPY", "1Y", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert bad["points"] == [] and "down" in bad["error"]
