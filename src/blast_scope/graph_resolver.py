"""Resolve filesystem paths to dependency graph nodes.

Given a filesystem path, find all code entities defined in that file and
determine which other parts of the codebase would be affected if that path
were modified or deleted. This is the bridge between the command parser's
target paths and the structural risk score.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path, PurePosixPath
from typing import TypedDict

from blast_scope.vendor.crg import CodeParser, GraphStore, NodeInfo, EdgeInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


class ResolvedNode(TypedDict):
    """A code entity affected by a path change.

    Example::

        {
            "qualified_name": "config.py::load",
            "kind": "Function",
            "file_path": "config.py",
            "depth": 1,
        }
    """

    qualified_name: str
    kind: str
    file_path: str
    depth: int


class GraphResolution(TypedDict):
    """Result of resolving a filesystem path against the dependency graph.

    Example::

        {
            "target_path": "/project/config.py",
            "nodes_in_file": ["config.py", "config.py::load"],
            "affected_nodes": [...],
            "in_degree": 2,
            "total_affected": 3,
        }
    """

    target_path: str
    nodes_in_file: list[str]
    affected_nodes: list[ResolvedNode]
    in_degree: int
    total_affected: int


# ---------------------------------------------------------------------------
# GraphResolver
# ---------------------------------------------------------------------------


class GraphResolver:
    """Resolve filesystem paths to dependency graph impact.

    Wraps the vendored code-review-graph parser and graph store to provide
    path-to-node resolution — given a filesystem path, find everything
    that depends on it.

    Example::

        >>> resolver = GraphResolver(Path("/project"))
        >>> resolver.build_graph()
        >>> result = resolver.resolve_path(Path("/project/config.py"))
        >>> result["in_degree"]
        2
    """

    def __init__(self, project_root: Path, db_path: Path | None = None) -> None:
        self._project_root = project_root.resolve()
        if db_path is None:
            db_path = self._project_root / ".blast-scope" / "graph.db"
        self._db_path = db_path
        self._parser = CodeParser()
        self._store: GraphStore | None = None

    def _get_store(self) -> GraphStore:
        """Lazily initialize the graph store."""
        if self._store is None:
            self._store = GraphStore(self._db_path)
        return self._store

    def build_graph(self) -> None:
        """Parse all source files in the project and populate the graph.

        Walks the project tree, parses each recognized source file with
        Tree-sitter, and inserts the resulting nodes and edges into the
        SQLite graph store.

        Example::

            >>> resolver = GraphResolver(Path("/project"))
            >>> resolver.build_graph()
        """
        store = self._get_store()

        for source_file in self._walk_sources():
            rel_path = self._to_graph_path(source_file)
            try:
                file_hash = hashlib.md5(
                    source_file.read_bytes(), usedforsecurity=False
                ).hexdigest()
                nodes, edges = self._parser.parse_file(source_file)
                # Normalize file paths in nodes and edges to use relative POSIX paths
                normalized_nodes = self._normalize_nodes(nodes, source_file)
                normalized_edges = self._normalize_edges(edges, source_file)
                store.store_file_nodes_edges(
                    rel_path, normalized_nodes, normalized_edges, fhash=file_hash
                )
            except Exception:
                logger.warning("Failed to parse %s", source_file, exc_info=True)

    def resolve_path(self, target: Path) -> GraphResolution:
        """Resolve a single filesystem path to its graph impact.

        Args:
            target: Absolute filesystem path to resolve.

        Returns:
            A ``GraphResolution`` describing what the graph says depends
            on this path.

        Example::

            >>> resolver.resolve_path(Path("/project/config.py"))
            {"target_path": "/project/config.py", "in_degree": 2, ...}
        """
        store = self._get_store()
        target = target.resolve()

        # Check if path is inside the project
        try:
            target.relative_to(self._project_root)
        except ValueError:
            return _empty_resolution(str(target))

        rel_path = self._to_graph_path(target)

        # If it's a directory, aggregate all files within it
        if target.is_dir():
            return self._resolve_directory(target)

        # Find nodes defined in this file
        file_nodes = store.get_nodes_by_file(rel_path)
        nodes_in_file = [n.qualified_name for n in file_nodes]

        if not nodes_in_file:
            return _empty_resolution(str(target))

        # Get impact radius — all nodes affected by changes to this file
        impact = store.get_impact_radius_sql([rel_path])

        # Build affected nodes list from impacted nodes (excluding the seed nodes)
        affected: list[ResolvedNode] = []
        seed_qns = {n.qualified_name for n in impact["changed_nodes"]}

        for node in impact["impacted_nodes"]:
            affected.append(
                ResolvedNode(
                    qualified_name=node.qualified_name,
                    kind=node.kind,
                    file_path=node.file_path,
                    depth=1,  # CTE doesn't expose per-node depth easily
                )
            )

        # in_degree = number of edges pointing TO nodes in this file
        in_degree = 0
        for qn in nodes_in_file:
            edges = store.get_edges_by_target(qn)
            # Only count edges from OTHER files
            in_degree += sum(
                1 for e in edges if e.file_path != rel_path
            )

        return GraphResolution(
            target_path=str(target),
            nodes_in_file=nodes_in_file,
            affected_nodes=affected,
            in_degree=in_degree,
            total_affected=len(affected),
        )

    def resolve_paths(self, targets: list[Path]) -> list[GraphResolution]:
        """Batch resolution for multiple paths.

        Example::

            >>> resolver.resolve_paths([Path("/project/config.py"), Path("/project/db.py")])
            [GraphResolution(...), GraphResolution(...)]
        """
        return [self.resolve_path(t) for t in targets]

    def close(self) -> None:
        """Close the graph store connection."""
        if self._store is not None:
            self._store.close()
            self._store = None

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _to_graph_path(self, abs_path: Path) -> str:
        """Convert an absolute path to a project-relative POSIX path for graph storage.

        Example::

            >>> resolver._to_graph_path(Path("/project/src/config.py"))
            "src/config.py"
        """
        try:
            rel = abs_path.resolve().relative_to(self._project_root)
        except ValueError:
            return str(abs_path)
        # Normalize to forward slashes for cross-platform consistency
        return str(PurePosixPath(rel))

    def _normalize_nodes(
        self, nodes: list[NodeInfo], source_file: Path
    ) -> list[NodeInfo]:
        """Normalize file_path fields in nodes to relative POSIX paths."""
        rel_path = self._to_graph_path(source_file)
        for node in nodes:
            if node.kind == "File":
                node.file_path = rel_path
                node.name = rel_path
            else:
                node.file_path = rel_path
        return nodes

    def _normalize_edges(
        self, edges: list[EdgeInfo], source_file: Path
    ) -> list[EdgeInfo]:
        """Normalize file paths in edges to relative POSIX paths."""
        rel_path = self._to_graph_path(source_file)
        for edge in edges:
            edge.file_path = rel_path
            # Normalize source and target paths that are absolute
            edge.source = self._normalize_qualified_name(edge.source)
            edge.target = self._normalize_qualified_name(edge.target)
        return edges

    def _normalize_qualified_name(self, qn: str) -> str:
        """Normalize a qualified name's path component to be project-relative.

        Qualified names have the format ``file_path::entity_name`` or are
        just a file path. This converts absolute paths to relative POSIX.

        Example::

            >>> resolver._normalize_qualified_name("/project/config.py::load")
            "config.py::load"
        """
        if "::" in qn:
            path_part, name_part = qn.split("::", 1)
        else:
            path_part = qn
            name_part = None

        # Try to make path relative
        try:
            abs_path = Path(path_part).resolve()
            rel = abs_path.relative_to(self._project_root)
            path_part = str(PurePosixPath(rel))
        except (ValueError, OSError):
            pass

        if name_part is not None:
            return f"{path_part}::{name_part}"
        return path_part

    def _resolve_directory(self, target: Path) -> GraphResolution:
        """Resolve a directory by aggregating all files within it."""
        all_nodes: list[str] = []
        all_affected: list[ResolvedNode] = []
        total_in_degree = 0

        for source_file in self._walk_sources(target):
            resolution = self.resolve_path(source_file)
            all_nodes.extend(resolution["nodes_in_file"])
            all_affected.extend(resolution["affected_nodes"])
            total_in_degree += resolution["in_degree"]

        # Deduplicate affected nodes
        seen: set[str] = set()
        unique_affected: list[ResolvedNode] = []
        for node in all_affected:
            if node["qualified_name"] not in seen:
                seen.add(node["qualified_name"])
                unique_affected.append(node)

        return GraphResolution(
            target_path=str(target),
            nodes_in_file=all_nodes,
            affected_nodes=unique_affected,
            in_degree=total_in_degree,
            total_affected=len(unique_affected),
        )

    def _walk_sources(self, root: Path | None = None) -> list[Path]:
        """Walk the project tree and return parseable source files.

        Skips hidden directories, __pycache__, node_modules, .git, etc.

        Example::

            >>> resolver._walk_sources()
            [Path("/project/config.py"), Path("/project/db.py"), ...]
        """
        if root is None:
            root = self._project_root

        skip_dirs = frozenset({
            ".git", ".hg", ".svn", "__pycache__", "node_modules",
            ".venv", "venv", ".tox", ".mypy_cache", ".ruff_cache",
            ".blast-scope",
        })

        # Recognized extensions from the vendored parser
        recognized = frozenset({
            ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
            ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb",
            ".kt", ".swift", ".php", ".scala", ".sol", ".dart",
            ".lua", ".m", ".sh", ".bash", ".ex", ".exs", ".vue",
            ".r", ".R", ".pl", ".pm",
        })

        sources: list[Path] = []
        for item in root.rglob("*"):
            # Skip items in excluded directories
            if any(part in skip_dirs for part in item.parts):
                continue
            if item.is_file() and item.suffix in recognized:
                sources.append(item)
        return sources


def _empty_resolution(target_path: str) -> GraphResolution:
    """Return an empty GraphResolution for paths outside the project or with no data."""
    return GraphResolution(
        target_path=target_path,
        nodes_in_file=[],
        affected_nodes=[],
        in_degree=0,
        total_affected=0,
    )
