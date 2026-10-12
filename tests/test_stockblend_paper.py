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


class FakeResp:
    def __init__(self, code, content=b""):
        self.status_code, self.content = code, content


class FakeSession:
    def __init__(self, files):
        self.files, self.calls = files, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        name = url.rsplit("/", 1)[1]
        return FakeResp(200, self.files[name]) if name in self.files else FakeResp(404)


def test_sync_downloads_both_files_from_the_ledger_branch(tmp_path):
    from quant import stockblend_sync
    s = FakeSession({"state.json": b'{"equity": 1}', "equity.jsonl": b'{"date": "2026-10-09"}\n'})
    assert stockblend_sync.sync(tmp_path, "tok", session=s) is None
    assert (tmp_path / "state.json").read_bytes() == b'{"equity": 1}'
    assert all(p == {"ref": "paper-stockblend"} for _, p, _ in s.calls)
    assert s.calls[0][2]["Authorization"] == "Bearer tok"


def test_sync_keeps_old_files_when_one_is_missing(tmp_path):
    from quant import stockblend_sync
    (tmp_path / "state.json").write_text("old")
    err = stockblend_sync.sync(tmp_path, "tok", session=FakeSession({"state.json": b"new"}))
    assert err and (tmp_path / "state.json").read_text() == "old"
    assert stockblend_sync.sync(tmp_path, None) is not None


def test_dashboard_reads_the_ledger_when_present(tmp_path):
    import json

    from dashboard import build
    (tmp_path / "state.json").write_text(json.dumps({"schema_version": 1}))
    assert "stockblend" not in build.load(tmp_path)
    (tmp_path / "stockblend").mkdir()
    (tmp_path / "stockblend" / "state.json").write_text(json.dumps({"equity": 100000}))
    (tmp_path / "stockblend" / "equity.jsonl").write_text('{"date": "2026-10-09", "equity": 100000}\n')
    data = build.load(tmp_path)
    assert data["stockblend"]["state"]["equity"] == 100000 and len(data["stockblend"]["equity"]) == 1


def test_agent_fetches_the_ledger_every_two_hours():
    from datetime import datetime, timedelta

    from scripts.mintory_agent import KST, stockblend_due
    now = datetime(2026, 10, 13, 8, 0, tzinfo=KST)
    assert stockblend_due(now, None)
    assert not stockblend_due(now, (now - timedelta(minutes=90)).isoformat())
    assert stockblend_due(now, (now - timedelta(hours=2)).isoformat())
