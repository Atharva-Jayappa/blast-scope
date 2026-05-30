"""Tests for incremental indexing and PageRank importance in GraphResolver."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from blast_scope.graph_resolver import GraphResolver

SAMPLE_PROJECT = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A writable copy of the sample project so tests can mutate files."""
    dest = tmp_path / "proj"
    shutil.copytree(SAMPLE_PROJECT, dest)
    return dest


def _build(project: Path, tmp_path: Path) -> GraphResolver:
    r = GraphResolver(project, db_path=tmp_path / "inc.db")
    r.build_graph()
    return r


class TestIncrementalIndexing:
    def test_rebuild_is_idempotent(self, project: Path, tmp_path: Path) -> None:
        r = _build(project, tmp_path)
        before = r._get_store().get_stats().total_nodes
        # Re-running with no changes must not duplicate or drop nodes.
        r.build_graph()
        after = r._get_store().get_stats().total_nodes
        assert after == before

    def test_unchanged_files_are_skipped(self, project: Path, tmp_path: Path) -> None:
        r = _build(project, tmp_path)
        hashes_before = r._get_store().get_file_hashes()
        # Nothing changed → an incremental rebuild keeps the same hashes/files.
        r.build_graph()
        assert r._get_store().get_file_hashes() == hashes_before

    def test_deleted_file_is_pruned(self, project: Path, tmp_path: Path) -> None:
        r = _build(project, tmp_path)
        store = r._get_store()
        assert any(Path(f).name == "db.py" for f in store.get_all_files())

        (project / "db.py").unlink()
        r.build_graph()

        files = {Path(f).name for f in store.get_all_files()}
        assert "db.py" not in files
        assert "config.py" in files

    def test_changed_file_is_reparsed(self, project: Path, tmp_path: Path) -> None:
        r = _build(project, tmp_path)
        store = r._get_store()
        old_hash = store.get_file_hashes()[r._to_graph_path(project / "config.py")]

        (project / "config.py").write_text("# changed\nVALUE = 1\n")
        r.build_graph()

        new_hash = store.get_file_hashes()[r._to_graph_path(project / "config.py")]
        assert new_hash != old_hash

    def test_force_rebuild(self, project: Path, tmp_path: Path) -> None:
        r = _build(project, tmp_path)
        before = r._get_store().get_stats().total_nodes
        r.build_graph(force=True)
        after = r._get_store().get_stats().total_nodes
        assert after == before


class TestImportance:
    def test_importance_populated(self, project: Path, tmp_path: Path) -> None:
        r = _build(project, tmp_path)
        result = r.resolve_path(project / "config.py")
        assert 0.0 <= result["importance"] <= 1.0

    def test_central_file_outranks_leaf(self, project: Path, tmp_path: Path) -> None:
        r = _build(project, tmp_path)
        # config.py is imported by main.py and db.py; main.py is imported by nobody.
        config = r.resolve_path(project / "config.py")
        main = r.resolve_path(project / "main.py")
        assert config["importance"] > main["importance"]

    def test_importance_survives_reload(self, project: Path, tmp_path: Path) -> None:
        _build(project, tmp_path)
        # A fresh resolver over the same DB must read PageRank from metadata,
        # not recompute it.
        fresh = GraphResolver(project, db_path=tmp_path / "inc.db")
        result = fresh.resolve_path(project / "config.py")
        assert result["importance"] > 0.0
