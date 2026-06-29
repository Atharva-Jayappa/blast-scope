"""Classify how recoverable a path is if a command destroys it.

This is the *reversibility axis* of the risk model — orthogonal to how much
code depends on a path. It answers: "if this is deleted/overwritten, can it be
gotten back, and how bad is losing it?"

Key categories and why they matter:

- ``regenerable`` — node_modules / dist / .venv / __pycache__: safe to nuke
  (fixes the old "untracked ⇒ risky" backwardness).
- ``secret`` — .env / *.pem / id_rsa: unrecoverable *and* sensitive; max risk
  even with zero importers (the old model scored these LOW).
- ``precious_data`` — *.tfstate / *.sqlite / *.db: irreplaceable state.
- git states — ``tracked_clean`` (recoverable), ``tracked_dirty`` (uncommitted
  changes would be lost), ``untracked`` / ``gitignored`` (not in history).

Git state is read once per repository and cached (``ls-files`` + ``status``),
so repeated classification is cheap.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path, PurePosixPath
from typing import TypedDict

logger = logging.getLogger(__name__)


class Recoverability(TypedDict):
    """How recoverable a path is.

    Example::

        {"category": "secret", "irrecoverability": 1.0,
         "reversible": False, "reason": "matches secret/credential pattern"}
    """

    category: str
    irrecoverability: float  # 0.0 (trivially recoverable) .. 1.0 (gone for good)
    reversible: bool
    reason: str


# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

_REGENERABLE_DIRS: frozenset[str] = frozenset({
    "node_modules", "dist", "build", "target", ".venv", "venv", "__pycache__",
    ".next", ".nuxt", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle",
    ".tox", ".turbo", ".parcel-cache", "coverage", ".cache", "obj", ".terraform",
})

_SECRET_SUFFIXES: frozenset[str] = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".kdbx", ".ppk", ".gpg"}
)
_SECRET_NAMES: frozenset[str] = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json",
    "credentials.yaml", "credentials.yml", ".npmrc", ".pypirc", ".netrc", ".htpasswd",
    "secrets.json", "secrets.yaml", "secrets.yml",
})
_PRECIOUS_SUFFIXES: frozenset[str] = frozenset(
    {".tfstate", ".sqlite", ".sqlite3", ".db", ".dump", ".mdb", ".rdb"}
)


# ---------------------------------------------------------------------------
# Per-repository state cache
# ---------------------------------------------------------------------------


class _RepoState:
    """Git tracking/ignore/dirty state for one repository, read once."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tracked: set[str] = set()
        self.modified: set[str] = set()
        self.untracked: set[str] = set()
        self.ignored: set[str] = set()
        self._load()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout if result.returncode == 0 else ""

    def _load(self) -> None:
        for line in self._git("ls-files").splitlines():
            if line:
                self.tracked.add(line.strip())
        for line in self._git("status", "--porcelain", "--ignored").splitlines():
            if len(line) < 4:
                continue
            status, rest = line[:2], line[3:]
            if " -> " in rest:  # rename
                rest = rest.split(" -> ", 1)[1]
            rest = rest.strip().strip('"').rstrip("/")
            if status == "??":
                self.untracked.add(rest)
            elif status == "!!":
                self.ignored.add(rest)
            else:
                self.modified.add(rest)


_repo_state_cache: dict[str, _RepoState | None] = {}
_root_cache: dict[str, str | None] = {}


def clear_cache() -> None:
    """Drop all cached git state (call after a refresh/index)."""
    _repo_state_cache.clear()
    _root_cache.clear()


def working_tree_state(path: Path) -> tuple[int, int, int] | None:
    """Return ``(modified, untracked, tracked)`` counts for ``path``'s repo.

    Reuses the per-repository git-state cache, so this is cheap to call
    repeatedly. Returns ``None`` when ``path`` is not inside a git repo or
    git state is unavailable.

    Example::

        >>> working_tree_state(Path("/proj/src"))
        (3, 1, 42)  # 3 modified, 1 untracked, 42 tracked
    """
    root = _repo_root(path)
    if root is None:
        return None
    state = _repo_state(root)
    if state is None:
        return None
    return (len(state.modified), len(state.untracked), len(state.tracked))


def _repo_root(path: Path) -> Path | None:
    check_dir = path if path.is_dir() else path.parent
    key = str(check_dir)
    if key not in _root_cache:
        try:
            result = subprocess.run(
                ["git", "-C", str(check_dir), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10,
            )
            _root_cache[key] = result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            _root_cache[key] = None
    root = _root_cache[key]
    return Path(root) if root else None


def _repo_state(root: Path) -> _RepoState | None:
    key = str(root)
    if key not in _repo_state_cache:
        try:
            _repo_state_cache[key] = _RepoState(root)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            _repo_state_cache[key] = None
    return _repo_state_cache[key]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_path(path: Path) -> Recoverability:
    """Classify how recoverable ``path`` is if destroyed.

    Example::

        >>> classify_path(Path("/proj/.env"))["category"]
        'secret'
        >>> classify_path(Path("/proj/node_modules"))["category"]
        'regenerable'
    """
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        pass
    name = path.name.lower()
    exists = path.exists()

    # Deleting something that isn't there has no blast radius.
    if not exists:
        return _r("absent", 0.0, True, "path does not exist — nothing to lose")

    # Regenerable artifacts are cheap to rebuild — explicitly low risk.
    if _is_regenerable(path):
        return _r("regenerable", 0.05, True, "regenerable build/dependency artifact")

    # Deleting the .git directory — or a directory that contains it (the repo
    # root) — destroys the history that makes *everything inside* recoverable.
    # Without this, `rm -rf project/` reads as `tracked_clean` (recoverable) when
    # it actually takes the repo's own recovery net down with it.
    if path.is_dir() and (path.name == ".git" or (path / ".git").exists()):
        return _r("repo_history", 0.9, False,
                  "removes the .git history that makes everything inside recoverable")

    cat, irr, rev, reason = _git_classify(path)

    # Secrets / irreplaceable data raise the floor regardless of git state.
    if _is_secret(name):
        return _r("secret", max(irr, 0.9), False,
                  "matches secret/credential pattern — sensitive and not safely recoverable")
    if _is_precious_data(name):
        return _r("precious_data", max(irr, 0.85), False,
                  "irreplaceable data/state file")
    return _r(cat, irr, rev, reason)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _r(category: str, irr: float, reversible: bool, reason: str) -> Recoverability:
    return Recoverability(
        category=category, irrecoverability=irr, reversible=reversible, reason=reason
    )


def _is_regenerable(path: Path) -> bool:
    return any(part in _REGENERABLE_DIRS for part in path.parts)


def _is_secret(name: str) -> bool:
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _SECRET_NAMES:
        return True
    return any(name.endswith(suf) for suf in _SECRET_SUFFIXES)


def _is_precious_data(name: str) -> bool:
    return any(name.endswith(suf) for suf in _PRECIOUS_SUFFIXES)


def _git_classify(path: Path) -> tuple[str, float, bool, str]:
    root = _repo_root(path)
    if root is None:
        return ("untracked", 0.7, False, "not inside any git repository")
    state = _repo_state(root)
    if state is None:
        return ("untracked", 0.7, False, "git state unavailable")

    try:
        rel = str(PurePosixPath(path.relative_to(root)))
    except ValueError:
        return ("untracked", 0.7, False, "outside the repository root")

    if path.is_dir():
        prefix = rel + "/"
        if any(t == rel or t.startswith(prefix) for t in state.tracked):
            return ("tracked_clean", 0.3, True,
                    "directory of git-tracked files — recoverable from history")
        if rel in state.ignored or any(rel.startswith(i.rstrip("/") + "/") for i in state.ignored):
            return ("gitignored", 0.85, False, "git-ignored directory — not in history")
        return ("untracked", 0.65, False, "untracked directory — not in git history")

    if rel in state.ignored:
        return ("gitignored", 0.85, False, "git-ignored — not recoverable from history")
    if rel in state.tracked:
        if rel in state.modified:
            return ("tracked_dirty", 0.55, True,
                    "git-tracked but has uncommitted changes that would be lost")
        return ("tracked_clean", 0.2, True, "git-tracked and committed — recoverable")
    return ("untracked", 0.7, False, "untracked — not in git history")
