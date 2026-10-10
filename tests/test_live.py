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
