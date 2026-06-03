"""Tests for blast_scope.snapshot — capture/restore/list + snapshot policy."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from blast_scope import snapshot
from blast_scope.recoverability import clear_cache


class TestCreate:
    def test_no_existing_targets_returns_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.txt"
        assert snapshot.create_snapshot([missing], root=tmp_path) is None

    def test_snapshot_records_existing_targets(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        f.write_text("key: val\n")
        manifest = snapshot.create_snapshot([f], root=tmp_path, reason="rm config.yaml")
        assert manifest is not None
        assert manifest["reason"] == "rm config.yaml"
        assert len(manifest["entries"]) == 1
        assert (snapshot.snapshots_dir(tmp_path) / manifest["id"] / "data.tar.gz").exists()

    def test_missing_targets_are_skipped(self, tmp_path: Path) -> None:
        present = tmp_path / "a.txt"
        present.write_text("a")
        absent = tmp_path / "b.txt"
        manifest = snapshot.create_snapshot([present, absent], root=tmp_path)
        assert manifest is not None
        assert len(manifest["entries"]) == 1


class TestPlan:
    """plan_snapshot — the recoverability + size-cap policy (Move 1)."""

    def test_archives_unrecoverable_file(self, tmp_path: Path) -> None:
        clear_cache()
        secret = tmp_path / ".env"  # secret, not in any repo → unrecoverable
        secret.write_text("API_KEY=xyz")
        plan = snapshot.plan_snapshot([secret])
        assert plan["archive"] == [str(secret)]
        assert plan["skipped_recoverable"] == []
        assert plan["skipped_oversize"] == []

    def test_skips_regenerable_dir(self, tmp_path: Path) -> None:
        clear_cache()
        nm = tmp_path / "node_modules"
        (nm / "pkg").mkdir(parents=True)
        (nm / "pkg" / "index.js").write_text("x")
        plan = snapshot.plan_snapshot([nm])
        assert plan["archive"] == []
        assert plan["skipped_recoverable"] == [str(nm)]

    def test_skips_absent_target_entirely(self, tmp_path: Path) -> None:
        clear_cache()
        plan = snapshot.plan_snapshot([tmp_path / "ghost.txt"])
        assert plan == {
            "archive": [],
            "skipped_recoverable": [],
            "skipped_oversize": [],
        }

    def test_skips_git_clean_tracked_file(self, tmp_path: Path) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not available")
        clear_cache()
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        tracked = tmp_path / "app.py"
        tracked.write_text("print('hi')\n")
        subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

        plan = snapshot.plan_snapshot([tracked])
        # git already has it — no need to spend a tarball on it.
        assert plan["skipped_recoverable"] == [str(tracked)]
        assert plan["archive"] == []

    def test_oversize_file_is_skipped_with_warning(self, tmp_path: Path) -> None:
        clear_cache()
        big = tmp_path / "data.bin"
        big.write_bytes(b"0" * 4096)
        plan = snapshot.plan_snapshot([big], max_bytes=1024)
        assert plan["archive"] == []
        assert plan["skipped_oversize"] == [str(big)]

    def test_oversize_dir_is_skipped(self, tmp_path: Path) -> None:
        clear_cache()
        d = tmp_path / "blob"
        d.mkdir()
        (d / "a").write_bytes(b"x" * 2048)
        (d / "b").write_bytes(b"y" * 2048)
        plan = snapshot.plan_snapshot([d], max_bytes=1024)
        assert plan["skipped_oversize"] == [str(d)]

    def test_within_cap_dir_is_archived(self, tmp_path: Path) -> None:
        clear_cache()
        d = tmp_path / "cfg"
        d.mkdir()
        (d / "a").write_bytes(b"x" * 10)
        plan = snapshot.plan_snapshot([d], max_bytes=1024)
        assert plan["archive"] == [str(d)]


class TestRestore:
    def test_restore_file_after_delete(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.env"
        f.write_text("API_KEY=xyz")
        manifest = snapshot.create_snapshot([f], root=tmp_path)
        assert manifest is not None

        f.unlink()  # simulate the destructive command
        assert not f.exists()

        restored = snapshot.restore_snapshot(manifest["id"], root=tmp_path)
        assert restored == [str(f.resolve())]
        assert f.read_text() == "API_KEY=xyz"

    def test_restore_directory_tree(self, tmp_path: Path) -> None:
        d = tmp_path / "config"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_text("a")
        (d / "sub" / "b.txt").write_text("b")
        manifest = snapshot.create_snapshot([d], root=tmp_path)
        assert manifest is not None

        import shutil

        shutil.rmtree(d)
        assert not d.exists()

        snapshot.restore_snapshot(manifest["id"], root=tmp_path)
        assert (d / "a.txt").read_text() == "a"
        assert (d / "sub" / "b.txt").read_text() == "b"

    def test_restore_overwrites_current_contents(self, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        f.write_text("original")
        manifest = snapshot.create_snapshot([f], root=tmp_path)
        assert manifest is not None

        f.write_text("corrupted")  # something clobbered it
        snapshot.restore_snapshot(manifest["id"], root=tmp_path)
        assert f.read_text() == "original"

    def test_restore_unknown_id_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            snapshot.restore_snapshot("does-not-exist", root=tmp_path)


class TestList:
    def test_empty_when_none(self, tmp_path: Path) -> None:
        assert snapshot.list_snapshots(tmp_path) == []

    def test_lists_newest_first(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        f.write_text("x")
        first = snapshot.create_snapshot([f], root=tmp_path, reason="one")
        second = snapshot.create_snapshot([f], root=tmp_path, reason="two")
        assert first is not None and second is not None

        listed = snapshot.list_snapshots(tmp_path)
        ids = [s["id"] for s in listed]
        assert set(ids) == {first["id"], second["id"]}
        assert ids == sorted(ids, reverse=True)
