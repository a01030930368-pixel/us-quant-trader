import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import build  # noqa: E402


def _payload(html: str) -> dict:
    m = re.search(r'<script id="dashboard-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "data block missing"
    return json.loads(m.group(1))


def test_build_sample(tmp_path):
    out = tmp_path / "d.html"
    assert build.main(["--sample", "--no-open", "--out", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    data = _payload(html)
    assert data["meta"]["source"] == "sample"
    assert data["state"]["schema_version"] == 1
    assert data["trades"] and data["equity"]


def test_falls_back_to_sample_when_no_records(tmp_path):
    out = tmp_path / "d.html"
    assert build.main(["--data", str(tmp_path / "missing"), "--no-open", "--out", str(out)]) == 0
    assert _payload(out.read_text(encoding="utf-8"))["meta"]["source"] == "sample"


def test_reads_paper_records_and_skips_torn_line(tmp_path):
    d = tmp_path / "paper"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"schema_version": 1, "mode": "kiwoom_mock",
                                              "initial_capital": 1000, "cash": 1000, "positions": []}),
                                  encoding="utf-8")
    (d / "equity.jsonl").write_text('{"date": "2026-10-09", "equity": 1000}\n{"date": "2026-10-1', encoding="utf-8")
    out = tmp_path / "d.html"
    assert build.main(["--data", str(d), "--no-open", "--out", str(out)]) == 0
    data = _payload(out.read_text(encoding="utf-8"))
    assert data["meta"]["source"] == "paper"
    assert len(data["equity"]) == 1 and data["trades"] == []


def test_script_breakout_is_escaped():
    html = build.render({"state": {"strategy": {"name": "</script><b>x"}}}, full_page=False)
    assert "</script><b>" not in html
