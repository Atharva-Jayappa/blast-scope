"""Tests for blast_scope.graph_resolver."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from blast_scope.graph_resolver import GraphResolver, GraphResolution

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PROJECT = FIXTURES_DIR / "sample_project"


@pytest.fixture()
def resolver(tmp_path: Path) -> GraphResolver:
    """Build a GraphResolver over the sample_project fixture."""
    db_path = tmp_path / "test_graph.db"
    r = GraphResolver(SAMPLE_PROJECT, db_path=db_path)
    r.build_graph()
    return r


class TestBuildGraph:
    def test_graph_contains_all_files(self, resolver: GraphResolver) -> None:
        """All 3 fixture files should be in the graph."""
        store = resolver._get_store()
        files = store.get_all_files()
        file_names = {Path(f).name for f in files}
        assert "main.py" in file_names
        assert "config.py" in file_names
        assert "db.py" in file_names

    def test_graph_has_nodes(self, resolver: GraphResolver) -> None:
        """Graph should have File + Function nodes."""
        store = resolver._get_store()
        stats = store.get_stats()
        assert stats.total_nodes >= 6  # 3 File + 3 Function nodes


class TestResolveConfigPy:
    """config.py is imported by both main.py and db.py.

    Ground truth from CLAUDE.md: touching config.py must affect main.py and db.py.
    """

    def test_in_degree_at_least_2(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(SAMPLE_PROJECT / "config.py")
        # Both main.py and db.py import config
        assert result["in_degree"] >= 2

    def test_affected_includes_main_and_db(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(SAMPLE_PROJECT / "config.py")
        affected_files = {
            Path(n["file_path"]).name
            for n in result["affected_nodes"]
        }
        assert "main.py" in affected_files
        assert "db.py" in affected_files

    def test_nodes_in_file(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(SAMPLE_PROJECT / "config.py")
        assert len(result["nodes_in_file"]) >= 1  # At least the File node


class TestResolveDbPy:
    """db.py is imported by main.py."""

    def test_in_degree_at_least_1(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(SAMPLE_PROJECT / "db.py")
        assert result["in_degree"] >= 1

    def test_affected_includes_main(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(SAMPLE_PROJECT / "db.py")
        affected_files = {
            Path(n["file_path"]).name
            for n in result["affected_nodes"]
        }
        assert "main.py" in affected_files


class TestResolveMainPy:
    """main.py imports things but nothing imports main.py."""

    def test_minimal_affected(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(SAMPLE_PROJECT / "main.py")
        # main.py has affected nodes because it *imports* config and db,
        # and the CTE traverses bidirectionally. But in_degree should be 0
        # since nothing imports main.py from outside.
        # Note: the CTE finds connections in both directions, so affected
        # nodes may still include config.py and db.py.
        # The key assertion: nothing else imports main.py.
        assert result["in_degree"] == 0


class TestEdgeCases:
    def test_nonexistent_path(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(SAMPLE_PROJECT / "nonexistent.py")
        assert result["nodes_in_file"] == []
        assert result["affected_nodes"] == []
        assert result["in_degree"] == 0

    def test_path_outside_project(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(Path("/some/random/path.py"))
        assert result["nodes_in_file"] == []
        assert result["affected_nodes"] == []
        assert result["total_affected"] == 0

    def test_resolve_paths_batch(self, resolver: GraphResolver) -> None:
        results = resolver.resolve_paths([
            SAMPLE_PROJECT / "config.py",
            SAMPLE_PROJECT / "db.py",
        ])
        assert len(results) == 2
        assert results[0]["in_degree"] >= 2  # config.py
        assert results[1]["in_degree"] >= 1  # db.py


class TestDirectoryResolution:
    def test_resolve_project_directory(self, resolver: GraphResolver) -> None:
        result = resolver.resolve_path(SAMPLE_PROJECT)
        # Should aggregate all files
        assert len(result["nodes_in_file"]) >= 3  # At least 3 File nodes
