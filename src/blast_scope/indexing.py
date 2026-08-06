"""Keep the dependency graph fresh outside the MCP server.

Historically the graph only existed if something called the MCP server's
``index_project`` — a session that never made an MCP call scored graphless
forever, silently losing the structural blast-radius signal. Two hook-driven
paths close that gap:

- **SessionStart** spawns a detached background build (the expensive cold
  index happens off the command path).
- **PreToolUse** calls :func:`ensure_fresh_graph` before scoring: an existing
  graph is refreshed incrementally (a stat sweep when nothing changed), a
  missing one triggers the same detached background build.

All builders share one lockfile so concurrent sessions never index the same
project twice. Everything here is advisory-adjacent: it never raises into the
hook path and never blocks — a contended lock means "score with what exists."
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from blast_scope.graph_resolver import GraphResolver

logger = logging.getLogger(__name__)

_LOCK_NAME = "graph.lock"
# A lock older than this is presumed abandoned (crashed indexer) and broken.
# Generous: a legitimate cold index of a huge repo should finish well within it.
_LOCK_STALE_SECONDS = 15 * 60


def _lock_path(project_root: Path) -> Path:
    """Lockfile location for a project's graph builds.

    Example::

        >>> _lock_path(Path("/proj")).name
        'graph.lock'
    """
    return project_root / ".blast-scope" / _LOCK_NAME


def _try_lock(project_root: Path) -> bool:
    """Acquire the build lock without blocking; break a stale one.

    Returns True if the lock was acquired (caller must :func:`_unlock`),
    False if another live builder holds it.

    Example::

        >>> _try_lock(Path("/proj")) and _unlock(Path("/proj")) is None
        True
    """
    lock = _lock_path(project_root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):  # second pass only after breaking a stale lock
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # holder released between open and stat — retry
            if age < _LOCK_STALE_SECONDS:
                return False
            try:
                lock.unlink()
            except OSError:
                return False  # racing another breaker — let it win
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def _unlock(project_root: Path) -> None:
    """Release the build lock (best-effort).

    Example::

        >>> _unlock(Path("/proj"))
    """
    try:
        _lock_path(project_root).unlink()
    except OSError:
        logger.debug("failed to remove graph lock", exc_info=True)


def refresh_graph(project_root: Path, force: bool = False) -> str:
    """Build or incrementally refresh the graph under the lock.

    Returns ``"refreshed"`` on success (whether or not anything changed),
    ``"busy"`` if another builder holds the lock, ``"error"`` on failure.
    Never raises.

    Example::

        >>> refresh_graph(Path("/proj"))
        'refreshed'
    """
    if not _try_lock(project_root):
        return "busy"
    try:
        resolver = GraphResolver(project_root)
        try:
            resolver.build_graph(force=force)
        finally:
            resolver.close()
        return "refreshed"
    except Exception:
        logger.exception("graph refresh failed for %s", project_root)
        return "error"
    finally:
        _unlock(project_root)


def _spawn_detached(cmd: list[str]) -> None:
    """Launch ``cmd`` fully detached: no console, no inherited stdio.

    Example::

        >>> _spawn_detached([sys.executable, "-c", "pass"])
    """
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def spawn_background_build(project_root: Path) -> bool:
    """Start a detached process that cold-builds the graph, and return.

    Skips spawning when a builder already holds the lock. The child runs
    ``python -m blast_scope.indexing <root>`` so a hook can exit
    immediately. Never raises.

    Example::

        >>> spawn_background_build(Path("/proj"))
        True
    """
    lock = _lock_path(project_root)
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime < _LOCK_STALE_SECONDS:
            return False  # a build is already running
    except OSError:
        pass
    try:
        _spawn_detached([sys.executable, "-m", "blast_scope.indexing", str(project_root)])
        return True
    except Exception:
        logger.exception("failed to spawn background index for %s", project_root)
        return False


def ensure_fresh_graph(project_root: Path) -> str:
    """Make the graph as current as possible without blocking the caller.

    The PreToolUse hook calls this before scoring: an existing graph is
    refreshed inline (incremental — a stat sweep when nothing changed), a
    missing one gets a detached background build so *later* commands score
    with structure even though this one can't. Never raises.

    Returns ``"refreshed"``, ``"busy"``, ``"building"`` (cold build spawned
    or already running), ``"absent"`` (no project dir), or ``"error"``.

    Example::

        >>> ensure_fresh_graph(Path("/proj"))
        'refreshed'
    """
    try:
        if not project_root.is_dir():
            return "absent"
        if not (project_root / ".blast-scope" / "graph.db").exists():
            spawn_background_build(project_root)
            return "building"
        return refresh_graph(project_root)
    except Exception:
        logger.exception("ensure_fresh_graph failed for %s", project_root)
        return "error"


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m blast_scope.indexing <project_root>``.

    Builds (or refreshes) the graph for one project under the lock. Exit
    code 0 on success or busy (another builder is doing the work), 1 on
    bad usage or build error.

    Example::

        >>> main(["/proj"])
        0
    """
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        logger.error("usage: python -m blast_scope.indexing <project_root>")
        return 1
    status = refresh_graph(Path(args[0]).resolve())
    return 0 if status in ("refreshed", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
