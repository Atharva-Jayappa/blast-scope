"""Config / data consequences — references the AST graph cannot see.

A ``config.yaml`` or ``seed.json`` is loaded at runtime by *path string*
(``open("config.yaml")``, ``load_dotenv(".env")``), so no import edge points
at it. The dependency graph therefore reports zero in-degree and the scorer
would treat deleting it as harmless — exactly backwards.

This module closes that gap with a bounded textual scan: when the target is a
config/data file, it greps the source tree for mentions of the file's name and
raises a floor that scales with how many places reference it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)


# Extensions that are loaded by path at runtime rather than imported.
_CONFIG_SUFFIXES: frozenset[str] = frozenset({
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".config",
    ".properties", ".xml", ".csv", ".tsv", ".sql", ".env", ".params",
})
# Exact names (extensionless or dotfiles) worth scanning for.
_CONFIG_NAMES: frozenset[str] = frozenset({
    ".env", ".flaskenv", "config", "settings", ".npmrc", ".babelrc",
})

# Source files worth scanning for references.
_SOURCE_SUFFIXES: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rb", ".java", ".rs",
    ".php", ".sh", ".yaml", ".yml", ".toml", ".json", ".cfg", ".ini",
})

# Directories never worth scanning (regenerable / vendored / VCS internals).
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".venv", "venv", "dist", "build", "target",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "coverage", ".next", ".nuxt", "vendor",
})

# Bounds so the scan stays cheap even on large trees.
_MAX_FILES_SCANNED = 2000
_MAX_BYTES_PER_FILE = 256 * 1024

_BASE_FLOOR = 0.45
_PER_REF = 0.05
_MAX_FLOOR = 0.85


def analyze_config_refs(
    target: Path, project_root: Path | None = None
) -> Consequence | None:
    """Return a config-reference ``Consequence`` for ``target``, or ``None``.

    Only config/data files are considered (others return ``None`` immediately).
    For those, the source tree under ``project_root`` is scanned for textual
    references to the file's name; the floor scales with the reference count.

    Args:
        target: The path the command would touch.
        project_root: Root of the source tree to scan. When ``None`` the scan
            is skipped (we have nowhere bounded to look) and ``None`` returned.

    Returns:
        A ``Consequence`` in the ``config`` domain, or ``None`` if the target
        is not a config/data file or has no detectable references.

    Example::

        >>> analyze_config_refs(Path("/proj/config.yaml"), Path("/proj")).domain
        'config'
        >>> analyze_config_refs(Path("/proj/main.py"), Path("/proj")) is None
        True
    """
    if not _is_config_file(target):
        return None
    if project_root is None:
        return None

    name = target.name
    try:
        refs = _count_references(name, project_root, exclude=target)
    except OSError:
        logger.debug("config-ref scan failed for %s", name, exc_info=True)
        return None

    if refs <= 0:
        return None

    floor = min(_MAX_FLOOR, _BASE_FLOOR + _PER_REF * refs)
    plural = "place" if refs == 1 else "places"
    return Consequence(
        "config",
        floor,
        f"{name} is referenced by name in {refs} source {plural} "
        f"(loaded by path, invisible to the import graph)",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_config_file(path: Path) -> bool:
    name = path.name.lower()
    if name in _CONFIG_NAMES or name.startswith(".env."):
        return True
    return path.suffix.lower() in _CONFIG_SUFFIXES


def _count_references(name: str, root: Path, exclude: Path) -> int:
    """Count source files under ``root`` that mention ``name``.

    Counts *files* (not raw occurrences) so a file that names the config many
    times still contributes 1 — the signal we want is "how much code reaches
    for this", not how chatty any single module is.
    """
    try:
        exclude_resolved = exclude.resolve()
    except OSError:
        exclude_resolved = exclude

    refs = 0
    scanned = 0
    for path in _iter_source_files(root):
        if scanned >= _MAX_FILES_SCANNED:
            break
        scanned += 1
        try:
            if path.resolve() == exclude_resolved:
                continue
        except OSError:
            pass
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:_MAX_BYTES_PER_FILE]
        except OSError:
            continue
        if name in text:
            refs += 1
    return refs


def _iter_source_files(root: Path):
    """Yield scannable source files under ``root``, skipping noise dirs."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _SOURCE_SUFFIXES:
            yield path
