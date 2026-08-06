"""Tests for blast_scope.indexing — hook-driven graph freshness."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from blast_scope import hook, indexing
from blast_scope.server import assess, reset_resolvers

SAMPLE_PROJECT = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture(autouse=True)
def _release_handles() -> None:
    """Windows: close cached SQLite handles so tmp dirs can be deleted."""
    yield
    reset_resolvers()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    import shutil

    dest = tmp_path / "proj"
    shutil.copytree(SAMPLE_PROJECT, dest)
    return dest


def _graph_db(root: Path) -> Path:
    return root / ".blast-scope" / "graph.db"


class TestLock:
    def test_acquire_and_release(self, project: Path) -> None:
        assert indexing._try_lock(project) is True
        assert indexing._try_lock(project) is False  # held
        indexing._unlock(project)
        assert indexing._try_lock(project) is True
        indexing._unlock(project)

    def test_stale_lock_is_broken(self, project: Path) -> None:
        assert indexing._try_lock(project) is True
        lock = indexing._lock_path(project)
        old = time.time() - indexing._LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))
        # A fresh contender treats the abandoned lock as free.
        assert indexing._try_lock(project) is True
        indexing._unlock(project)


class TestRefreshGraph:
    def test_builds_missing_graph(self, project: Path) -> None:
        assert indexing.refresh_graph(project) == "refreshed"
        assert _graph_db(project).exists()
        assert not indexing._lock_path(project).exists()  # lock released

    def test_busy_when_lock_held(self, project: Path) -> None:
        assert indexing._try_lock(project) is True
        try:
            assert indexing.refresh_graph(project) == "busy"
            assert not _graph_db(project).exists()  # skipped, not waited
        finally:
            indexing._unlock(project)

    def test_refresh_picks_up_edits(self, project: Path) -> None:
        """The staleness fix itself: a post-build edit lands in the graph."""
        indexing.refresh_graph(project)
        (project / "extra.py").write_text("import config\n")
        assert indexing.refresh_graph(project) == "refreshed"

        from blast_scope.graph_resolver import GraphResolver

        r = GraphResolver(project)
        try:
            # config.py now has a third importer: main.py, db.py, extra.py.
            assert r.resolve_path(project / "config.py")["in_degree"] == 3
        finally:
            r.close()


class TestSpawnBackgroundBuild:
    def test_spawns_indexing_module(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned: list[list[str]] = []
        monkeypatch.setattr(indexing, "_spawn_detached", spawned.append)
        assert indexing.spawn_background_build(project) is True
        assert spawned[0][-3:] == ["-m", "blast_scope.indexing", str(project)]

    def test_skips_when_build_already_running(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned: list[list[str]] = []
        monkeypatch.setattr(indexing, "_spawn_detached", spawned.append)
        assert indexing._try_lock(project) is True
        try:
            assert indexing.spawn_background_build(project) is False
            assert spawned == []
        finally:
            indexing._unlock(project)


class TestEnsureFreshGraph:
    def test_missing_project_dir(self, tmp_path: Path) -> None:
        assert indexing.ensure_fresh_graph(tmp_path / "nope") == "absent"

    def test_missing_graph_spawns_background_build(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned: list[Path] = []
        monkeypatch.setattr(
            indexing, "spawn_background_build", lambda root: spawned.append(root)
        )
        assert indexing.ensure_fresh_graph(project) == "building"
        assert spawned == [project]

    def test_existing_graph_is_refreshed_inline(self, project: Path) -> None:
        indexing.refresh_graph(project)
        assert indexing.ensure_fresh_graph(project) == "refreshed"


class TestMain:
    def test_builds_graph(self, project: Path) -> None:
        assert indexing.main([str(project)]) == 0
        assert _graph_db(project).exists()

    def test_bad_usage(self) -> None:
        assert indexing.main([]) == 1


class TestGraphContextFlag:
    def test_absent_without_graph(self, project: Path) -> None:
        out = assess("rm -rf ./config.py", cwd=str(project),
                     project_root=str(project), auto_index=False)
        assert out["graph_context"] is False

    def test_present_with_graph(self, project: Path) -> None:
        indexing.refresh_graph(project)
        out = assess("rm -rf ./config.py", cwd=str(project),
                     project_root=str(project), auto_index=False)
        assert out["graph_context"] is True


class TestHookIntegration:
    def test_pretooluse_refreshes_before_scoring(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refreshed: list[Path] = []
        monkeypatch.setattr(
            indexing, "ensure_fresh_graph", lambda root: refreshed.append(root)
        )
        hook.run(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": str(project)}
        )
        assert refreshed == [project]

    def test_session_start_spawns_build(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned: list[Path] = []
        monkeypatch.setattr(
            indexing, "spawn_background_build", lambda root: spawned.append(root)
        )
        assert hook.run_session_start({"cwd": str(project)}) == {}
        assert spawned == [project]

    def test_main_dispatches_session_start(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import io
        import sys as _sys

        spawned: list[Path] = []
        monkeypatch.setattr(
            indexing, "spawn_background_build", lambda root: spawned.append(root)
        )
        payload = json.dumps(
            {"hook_event_name": "SessionStart", "cwd": str(project)}
        )
        monkeypatch.setattr(_sys, "stdin", io.StringIO(payload))
        with pytest.raises(SystemExit) as exc:
            hook.main()
        assert exc.value.code == 0
        assert spawned == [project]
        assert capsys.readouterr().out == ""  # nothing to tell the agent

    def test_graphless_advisory_carries_note(self, project: Path) -> None:
        """A critical verdict without graph context says so out loud."""
        (project / ".env").write_text("KEY=x\n")
        out = hook.run(
            {"tool_name": "Bash", "tool_input": {"command": "rm .env"},
             "cwd": str(project)}
        )
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "no dependency graph yet" in ctx