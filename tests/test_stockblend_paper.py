from datetime import datetime, timezone

import pandas as pd

from scripts.stockblend_paper import extend, last_complete_session


def series(values, start="2026-10-12"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)))


def sleeves(ret):
    return pd.DataFrame({"a": ret, "b": ret})


def test_first_run_writes_base_and_chains_returns():
    ret = pd.concat([series([0.5], "2026-10-08"), series([0.01, -0.02])])
    rows = extend([], ret, sleeves(ret), pd.Timestamp("2026-10-13"))
    assert rows[0] == {"date": "2026-10-09", "equity": 100000.0, "ret": 0.0, "sleeves": {}}
    assert [r["date"] for r in rows[1:]] == ["2026-10-12", "2026-10-13"]
    assert rows[2]["equity"] == round(100000 * 1.01 * 0.98, 2)
    assert rows[1]["sleeves"] == {"a": 0.01, "b": 0.01}


def test_written_days_are_never_changed():
    old = [{"date": "2026-10-09", "equity": 100000.0, "ret": 0.0, "sleeves": {}},
           {"date": "2026-10-12", "equity": 101000.0, "ret": 0.01, "sleeves": {}}]
    # A recomputation that disagrees about 10-12 must not rewrite it.
    ret = series([0.05, 0.02])
    rows = extend(old, ret, sleeves(ret), pd.Timestamp("2026-10-13"))
    assert len(rows) == 1 and rows[0]["date"] == "2026-10-13"
    assert rows[0]["equity"] == round(101000 * 1.02, 2)


def test_unfinished_session_is_not_recorded():
    ret = series([0.01, 0.02])
    rows = extend([], ret, sleeves(ret), pd.Timestamp("2026-10-12"))
    assert [r["date"] for r in rows] == ["2026-10-09", "2026-10-12"]


def test_last_complete_session_waits_for_the_close():
    # 15:00 ET on Monday: Monday is not closed yet, so the last final day is before it.
    assert last_complete_session(datetime(2026, 10, 12, 19, 0, tzinfo=timezone.utc)) == pd.Timestamp("2026-10-11")
    assert last_complete_session(datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc)) == pd.Timestamp("2026-10-12")
