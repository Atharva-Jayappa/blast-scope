"""Resolve filesystem paths to dependency graph nodes.

Given a filesystem path, find all code entities defined in that file and
determine which other parts of the codebase would be affected if that path
were modified or deleted. This is the bridge between the command parser's
target paths and the structural risk score.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path, PurePosixPath
from typing import TypedDict

from blast_scope import centrality
from blast_scope.import_resolver import build_import_graph, reverse_graph
from blast_scope.vendor.crg import CodeParser, GraphStore, NodeInfo, EdgeInfo

logger = logging.getLogger(__name__)

# Metadata key under which the file-level PageRank map is cached in the graph DB.
_PAGERANK_META_KEY = "pagerank_by_file"
# Precise stdlib-``ast`` file→file import graph (forward: importer → imported).
# When present it is the authoritative blast-radius signal (in-degree + PageRank),
# replacing the tree-sitter parser's name-matched edges.
_IMPORT_GRAPH_META_KEY = "import_graph"


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
            "importance": 1.0,
        }

    ``importance`` is the target file's normalized PageRank centrality in the
    dependency graph (0.0 = peripheral, 1.0 = the most depended-upon file).
    """

    target_path: str
    nodes_in_file: list[str]
    affected_nodes: list[ResolvedNode]
    in_degree: int
    total_affected: int
    importance: float


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
        self._pagerank: dict[str, float] | None = None
        self._import_reverse: dict[str, list[str]] | None = None

    def _get_store(self) -> GraphStore:
        """Lazily initialize the graph store."""
        if self._store is None:
            self._store = GraphStore(self._db_path)
        return self._store

    def build_graph(self, force: bool = False) -> None:
        """Parse the project's source files and populate the graph.

        Incremental by default: files whose content hash is unchanged since
        the last build are skipped, and files that have disappeared are
        pruned. Pass ``force=True`` to re-parse everything. After (re)building,
        weighted PageRank centrality is recomputed and cached.

        Args:
            force: Re-parse every file regardless of cached hashes.

        Example::

            >>> resolver = GraphResolver(Path("/project"))
            >>> resolver.build_graph()          # first run: parses everything
            >>> resolver.build_graph()          # later: only changed files
        """
        store = self._get_store()
        existing_hashes = store.get_file_hashes()
        seen: set[str] = set()

        for source_file in self._walk_sources():
            rel_path = self._to_graph_path(source_file)
            seen.add(rel_path)
            try:
                file_hash = hashlib.md5(
                    source_file.read_bytes(), usedforsecurity=False
                ).hexdigest()
                # Skip unchanged files unless a full rebuild was requested.
                if not force and existing_hashes.get(rel_path) == file_hash:
                    continue
                nodes, edges = self._parser.parse_file(source_file)
                # Normalize file paths in nodes and edges to use relative POSIX paths
                normalized_nodes = self._normalize_nodes(nodes, source_file)
                normalized_edges = self._normalize_edges(edges, source_file)
                store.store_file_nodes_edges(
                    rel_path, normalized_nodes, normalized_edges, fhash=file_hash
                )
            except Exception:
                logger.warning("Failed to parse %s", source_file, exc_info=True)

        # Prune files that no longer exist on disk.
        for stale in existing_hashes.keys() - seen:
            store.remove_file_data(stale)

        self._recompute_centrality(store)

    def _recompute_centrality(self, store: GraphStore) -> None:
        """Recompute the precise import graph and file-level PageRank.

        The precise stdlib-``ast`` import graph is the authoritative blast-radius
        signal. When the project has resolvable Python imports it drives both
        in-degree and PageRank (file→file edges, so centrality is computed
        directly at file granularity). A project with no resolvable Python
        imports (non-Python, or scripts) falls back to the tree-sitter graph so
        the signal never silently vanishes. Both are cached in the DB metadata.
        """
        py_files = [
            self._project_root / Path(rel)
            for rel in (self._to_graph_path(p) for p in self._walk_sources())
            if rel.endswith(".py")
        ]
        forward = build_import_graph(self._project_root, py_files)

        if forward:
            store.set_metadata(_IMPORT_GRAPH_META_KEY, json.dumps(forward))
            self._import_reverse = reverse_graph(forward)
            triples = [
                (importer, imported, "IMPORTS_FROM")
                for importer, imported_list in forward.items()
                for imported in imported_list
            ]
            # Nodes are file paths already — PageRank is file-level directly.
            file_scores = centrality.pagerank(triples)
            peak = max(file_scores.values(), default=0.0)
            if peak > 0.0:
                file_scores = {f: s / peak for f, s in file_scores.items()}
            store.set_metadata(_PAGERANK_META_KEY, json.dumps(file_scores))
            self._pagerank = file_scores
        else:
            store.set_metadata(_IMPORT_GRAPH_META_KEY, json.dumps({}))
            self._import_reverse = {}
            self._tree_sitter_pagerank(store)

    def _tree_sitter_pagerank(self, store: GraphStore) -> None:
        """Fallback PageRank over the tree-sitter node graph (non-Python projects).

        Node-level centrality is aggregated to the file level (summed, then
        normalized so the most central file is 1.0).
        """
        triples = [
            (e.source_qualified, e.target_qualified, e.kind)
            for e in store.get_all_edges()
        ]
        node_scores = centrality.pagerank(triples)

        file_scores: dict[str, float] = {}
        for qn, score in node_scores.items():
            file_path = qn.split("::", 1)[0]
            file_scores[file_path] = file_scores.get(file_path, 0.0) + score

        peak = max(file_scores.values(), default=0.0)
        if peak > 0.0:
            file_scores = {f: s / peak for f, s in file_scores.items()}

        store.set_metadata(_PAGERANK_META_KEY, json.dumps(file_scores))
        self._pagerank = file_scores

    def _pagerank_by_file(self) -> dict[str, float]:
        """Lazily load the cached file-level PageRank map."""
        if self._pagerank is None:
            raw = self._get_store().get_metadata(_PAGERANK_META_KEY)
            self._pagerank = json.loads(raw) if raw else {}
        return self._pagerank

    def _import_reverse_map(self) -> dict[str, list[str]]:
        """Lazily load the reverse precise import graph (imported → importers).

        Empty when the graph predates Track C or the project has no resolvable
        Python imports — callers then fall back to the tree-sitter in-degree.
        """
        if self._import_reverse is None:
            raw = self._get_store().get_metadata(_IMPORT_GRAPH_META_KEY)
            forward = json.loads(raw) if raw else {}
            self._import_reverse = reverse_graph(forward)
        return self._import_reverse

    def _in_degree(self, rel_path: str, nodes_in_file: list[str], store: GraphStore) -> int:
        """Importers of a file: precise import graph if present, else tree-sitter.

        Precise = the number of *distinct files* that import this one — exactly
        the "N modules import config.py" claim. The tree-sitter fallback counts
        cross-file edges into the file's nodes (name-matched, noisier).
        """
        rev = self._import_reverse_map()
        if rev:
            return len(rev.get(rel_path, []))
        return sum(
            1
            for qn in nodes_in_file
            for e in store.get_edges_by_target(qn)
            if e.file_path != rel_path
        )

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

        # Reverse-dependency impact — nodes that would break if this file were
        # deleted, with true per-node depth (1 = direct dependent).
        impact = store.get_reverse_impact_sql([rel_path])

        affected: list[ResolvedNode] = []
        for node, depth in impact["dependents"]:
            affected.append(
                ResolvedNode(
                    qualified_name=node.qualified_name,
                    kind=node.kind,
                    file_path=node.file_path,
                    depth=depth,
                )
            )

        # in_degree = how many other files depend on this one (precise import
        # graph when available, tree-sitter fallback otherwise).
        in_degree = self._in_degree(rel_path, nodes_in_file, store)

        return GraphResolution(
            target_path=str(target),
            nodes_in_file=nodes_in_file,
            affected_nodes=affected,
            in_degree=in_degree,
            total_affected=len(affected),
            importance=self._pagerank_by_file().get(rel_path, 0.0),
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
        max_importance = 0.0

        for source_file in self._walk_sources(target):
            resolution = self.resolve_path(source_file)
            all_nodes.extend(resolution["nodes_in_file"])
            all_affected.extend(resolution["affected_nodes"])
            total_in_degree += resolution["in_degree"]
            max_importance = max(max_importance, resolution["importance"])

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
            importance=max_importance,
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
        importance=0.0,
    )
