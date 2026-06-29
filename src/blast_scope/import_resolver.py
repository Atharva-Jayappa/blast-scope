"""Precise Python import graph — file-level dependency edges via stdlib ``ast``.

The vendored tree-sitter parser resolves cross-file edges by *name matching*:
two ``load()`` functions collide, and an aliased import
(``from .config import settings as cfg``) or a re-export is missed. That noise
flows straight into the blast-radius signal (in-degree, PageRank), so
"``config.py`` is imported by 8 modules" can be wrong.

This module resolves imports the way Python does — module name to file, relative
imports against the importer's package — using only the standard library. It
emits a precise *file-to-file* import graph that drives the structural score; the
tree-sitter graph still supplies node-level detail (what's in a file, transitive
node impact).

Python-only by design (the user-facing claim is Python-centric). External
imports (not resolvable to a project file) are dropped — they're not blast
radius the tool can reason about.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)


def build_import_graph(
    project_root: Path, py_files: list[Path]
) -> dict[str, list[str]]:
    """Resolve every project import to a file-to-file edge.

    Args:
        project_root: The project root; all returned paths are POSIX-relative
            to it.
        py_files: Absolute paths of the project's ``.py`` files.

    Returns:
        A forward import graph ``{importer_rel: [imported_rel, ...]}`` — an edge
        ``A -> B`` means *A imports B* (A depends on B). Files with no resolvable
        project imports are omitted. Self-edges and duplicates are removed.

    Example::

        >>> build_import_graph(Path("/proj"), [Path("/proj/main.py"), Path("/proj/config.py")])
        {"main.py": ["config.py"]}
    """
    root = project_root.resolve()
    files = [f.resolve() for f in py_files if f.suffix == ".py"]
    fileset = set(files)
    module_map = _build_module_map(root, files, fileset)

    graph: dict[str, list[str]] = {}
    for f in files:
        importer = _rel(f, root)
        targets: set[str] = set()
        for node in _imports(f):
            for resolved in _resolve(node, f, root, module_map, fileset):
                rel = _rel(resolved, root)
                if rel != importer:
                    targets.add(rel)
        if targets:
            graph[importer] = sorted(targets)
    return graph


def reverse_graph(forward: dict[str, list[str]]) -> dict[str, list[str]]:
    """Invert a forward import graph to ``{imported_rel: [importer_rel, ...]}``.

    Example::

        >>> reverse_graph({"main.py": ["config.py"], "db.py": ["config.py"]})
        {"config.py": ["db.py", "main.py"]}
    """
    rev: dict[str, set[str]] = {}
    for importer, imported in forward.items():
        for target in imported:
            rev.setdefault(target, set()).add(importer)
    return {k: sorted(v) for k, v in rev.items()}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _rel(path: Path, root: Path) -> str:
    """Project-relative POSIX path, matching the graph store's convention."""
    try:
        return str(PurePosixPath(path.resolve().relative_to(root)))
    except ValueError:
        return str(PurePosixPath(path))


def _source_roots(root: Path, files: list[Path]) -> list[Path]:
    """Candidate import roots: project root, ``src/``, and top-level package parents.

    A ``from blast_scope.x import y`` resolves only if ``src/`` (where
    ``blast_scope/`` lives) is treated as an import root, so the module
    ``blast_scope.x`` maps to ``src/blast_scope/x.py``.
    """
    roots: set[Path] = {root}
    src = root / "src"
    if src.is_dir():
        roots.add(src)
    # For each package (dir with __init__.py), the import root is the parent of
    # its *topmost* package ancestor.
    for f in files:
        if f.name != "__init__.py":
            continue
        top = f.parent
        while (top.parent / "__init__.py").exists() and top.parent != root:
            top = top.parent
        roots.add(top.parent)
    return list(roots)


def _build_module_map(
    root: Path, files: list[Path], fileset: set[Path]
) -> dict[str, Path]:
    """Map every dotted module name a file is reachable as → that file.

    A file under multiple candidate roots (``src`` layout) gets multiple names,
    all pointing at it.
    """
    roots = _source_roots(root, files)
    module_map: dict[str, Path] = {}
    for f in files:
        for src_root in roots:
            try:
                rel = f.resolve().relative_to(src_root)
            except ValueError:
                continue
            parts = list(rel.parts)
            if parts[-1] == "__init__.py":
                parts = parts[:-1]  # package → the dir's dotted name
            else:
                parts[-1] = parts[-1][:-3]  # strip .py
            if not parts:
                continue
            module_map.setdefault(".".join(parts), f)
    return module_map


def _imports(path: Path) -> list[ast.stmt]:
    """Parse a file and return its top-level + nested Import/ImportFrom nodes."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, ValueError, OSError):
        return []
    return [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


def _resolve(
    node: ast.stmt,
    importer: Path,
    root: Path,
    module_map: dict[str, Path],
    fileset: set[Path],
) -> list[Path]:
    """Resolve one import node to the project file(s) it depends on."""
    if isinstance(node, ast.Import):
        out: list[Path] = []
        for alias in node.names:
            hit = _longest_module(alias.name, module_map)
            if hit is not None:
                out.append(hit)
        return out

    # ImportFrom: `from <module> import a, b` (level dots = relative).
    assert isinstance(node, ast.ImportFrom)
    if node.level:  # relative import
        base = _relative_base(importer, node.level, root)
        if base is None:
            return []
        return _resolve_from(base, node.module, [a.name for a in node.names], fileset, root)
    if node.module is None:
        return []
    out = []
    mod_file = _longest_module(node.module, module_map)
    if mod_file is not None:
        out.append(mod_file)
    # An imported name may itself be a submodule: `from pkg import sub`.
    for alias in node.names:
        sub = _longest_module(f"{node.module}.{alias.name}", module_map)
        if sub is not None:
            out.append(sub)
    return out


def _longest_module(dotted: str, module_map: dict[str, Path]) -> Path | None:
    """Match the longest prefix of a dotted name that names a project module.

    ``import a.b.c`` depends on the deepest real module; ``a.b.c`` may be a name
    inside ``a.b`` rather than its own module, so fall back to shorter prefixes.
    """
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        hit = module_map.get(".".join(parts[:i]))
        if hit is not None:
            return hit
    return None


def _relative_base(importer: Path, level: int, root: Path) -> Path | None:
    """The package directory a relative import (``from ..x``) is anchored to."""
    base = importer.resolve().parent
    for _ in range(level - 1):
        base = base.parent
    try:
        base.relative_to(root)
    except ValueError:
        return None
    return base


def _resolve_from(
    base: Path, module: str | None, names: list[str], fileset: set[Path], root: Path
) -> list[Path]:
    """Resolve a relative ``from`` import against its package directory."""
    target_dir = base
    if module:
        for part in module.split("."):
            target_dir = target_dir / part
    out: list[Path] = []
    # `from .config import x` → config.py (or config/__init__.py)
    for candidate in (target_dir.with_suffix(".py"), target_dir / "__init__.py"):
        if candidate.resolve() in fileset:
            out.append(candidate.resolve())
    # `from . import sub` / `from .pkg import sub` → sub may be a submodule.
    for name in names:
        sub = target_dir / name
        for candidate in (sub.with_suffix(".py"), sub / "__init__.py"):
            if candidate.resolve() in fileset:
                out.append(candidate.resolve())
    return out
