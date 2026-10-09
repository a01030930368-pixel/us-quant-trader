import io
import zipfile

from scripts.update import apply, is_kept, unpack


def zipball(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("owner-repo-abc1234/", "")
        for rel, content in files.items():
            z.writestr(f"owner-repo-abc1234/{rel}", content)
    return buf.getvalue()


def test_is_kept():
    assert is_kept(".env") and is_kept("paper/state.json") and is_kept("reports\\dashboard.html")
    assert not is_kept("scripts/update.py") and not is_kept(".env.example")


def test_unpack_strips_top_folder_and_skips_user_data():
    files = unpack(zipball({"a.txt": "1", "scripts/b.py": "2", ".env": "x", "paper/s.json": "y"}))
    assert files == {"a.txt": b"1", "scripts/b.py": b"2"}


def test_apply_writes_changes_keeps_user_data_and_removes_dropped_files(tmp_path):
    (tmp_path / ".env").write_text("KEY=secret")
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / "state.json").write_text("{}")
    (tmp_path / "same.txt").write_bytes(b"same")
    (tmp_path / "old.py").write_text("gone")
    (tmp_path / "mine.txt").write_text("user file")
    changed, removed = apply({"same.txt": b"same", "new/x.py": b"x"},
                             ["same.txt", "old.py", "paper/state.json"], root=tmp_path)
    assert changed == ["new/x.py"]
    assert removed == ["old.py"]
    assert (tmp_path / "new" / "x.py").read_bytes() == b"x"
    assert (tmp_path / ".env").read_text() == "KEY=secret"
    assert (tmp_path / "paper" / "state.json").exists()
    assert (tmp_path / "mine.txt").exists()
