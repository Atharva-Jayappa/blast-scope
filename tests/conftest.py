"""Shared test fixtures."""

from __future__ import annotations

import pytest

from blast_scope import indexing


@pytest.fixture(autouse=True)
def _no_background_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Background graph builds must never escape a test process.

    ``hook.run`` spawns a detached indexer for graphless projects; in the
    suite that would leave real subprocesses writing into tmp dirs mid-
    cleanup. Tests that exercise the spawn re-patch ``_spawn_detached``
    with their own recorder.
    """
    monkeypatch.setattr(indexing, "_spawn_detached", lambda cmd: None)
