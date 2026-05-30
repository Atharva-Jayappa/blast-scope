"""Tests for blast_scope.snapshot — capture/restore/list."""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_scope import snapshot


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
