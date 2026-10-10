import zipfile

from quant import backup


def fill(paper, text="v1"):
    (paper / "logs").mkdir(parents=True, exist_ok=True)
    (paper / "archive").mkdir(exist_ok=True)
    (paper / "state.json").write_text(text)
    (paper / "trades.jsonl").write_text("t")
    (paper / "archive" / "old.jsonl").write_text("a")
    (paper / "logs" / "agent.log").write_text("big log")


def test_make_zips_records_without_logs(tmp_path):
    paper, dest = tmp_path / "paper", tmp_path / "backups"
    fill(paper)
    out = backup.make(paper, dest, "2026-10-12")
    assert out.name == "paper-2026-10-12.zip"
    assert sorted(zipfile.ZipFile(out).namelist()) == ["archive/old.jsonl", "state.json", "trades.jsonl"]
    assert backup.make(tmp_path / "missing", dest, "2026-10-13") is None


def test_keeps_only_recent(tmp_path):
    paper, dest = tmp_path / "paper", tmp_path / "backups"
    fill(paper)
    for d in range(1, 6):
        backup.make(paper, dest, f"2026-10-{d:02d}", keep=3)
    assert [p.name for p in backup.listing(dest)] == [
        "paper-2026-10-05.zip", "paper-2026-10-04.zip", "paper-2026-10-03.zip"]


def test_daily_copies_to_backup_dir(tmp_path):
    paper = tmp_path / "paper"
    fill(paper)
    saved = backup.daily(paper, tmp_path / "b", "2026-10-12", str(tmp_path / "cloud"))
    assert [p.parent.name for p in saved] == ["b", "cloud"] and saved[1].exists()
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    assert len(backup.daily(paper, tmp_path / "b", "2026-10-13", str(blocked))) == 1


def test_restore_saves_current_first(tmp_path):
    paper, dest = tmp_path / "paper", tmp_path / "backups"
    fill(paper, "old")
    zip_old = backup.make(paper, dest, "2026-10-12")
    (paper / "state.json").write_text("new")
    (paper / "extra.json").write_text("x")
    before = backup.restore(zip_old, paper, dest, "2026-10-14")
    assert (paper / "state.json").read_text() == "old" and not (paper / "extra.json").exists()
    assert "extra.json" in zipfile.ZipFile(before).namelist()
    assert (paper / "logs" / "agent.log").exists()           # logs untouched
    same_day = backup.restore(dest / "paper-2026-10-14.zip", paper, dest, "2026-10-14")
    assert same_day and (paper / "state.json").read_text() == "new"
