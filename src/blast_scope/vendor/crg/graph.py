"""SQLite-backed knowledge graph storage and query engine.

Stores code structure as nodes (File, Class, Function, Type, Test) and
edges (CALLS, IMPORTS_FROM, INHERITS, IMPLEMENTS, CONTAINS, TESTED_BY, DEPENDS_ON, REFERENCES).
Supports reverse-impact queries (who depends on a file).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .constants import MAX_IMPACT_DEPTH, MAX_IMPACT_NODES
from .parser import EdgeInfo, NodeInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- File, Class, Function, Type, Test
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,           -- CALLS, IMPORTS_FROM, INHERITS, REFERENCES, etc.
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_qualified ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file_path);
"""


@dataclass
class GraphNode:
    id: int
    kind: str
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    language: str
    parent_name: Optional[str]
    params: Optional[str]
    return_type: Optional[str]
    is_test: bool
    file_hash: Optional[str]
    extra: dict


@dataclass
class GraphEdge:
    id: int
    kind: str
    source_qualified: str
    target_qualified: str
    file_path: str
    line: int
    extra: dict


@dataclass
class GraphStats:
    total_nodes: int
    total_edges: int
    nodes_by_kind: dict[str, int]
    edges_by_kind: dict[str, int]
    languages: list[str]
    files_count: int
    last_updated: Optional[str]


# ---------------------------------------------------------------------------
# GraphStore
# ---------------------------------------------------------------------------


class GraphStore:
    """SQLite-backed code knowledge graph."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False,
            isolation_level=None,  # Disable implicit transactions (#135)
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- Write operations ---

    def upsert_node(self, node: NodeInfo, file_hash: str = "") -> int:
        """Insert or update a node. Returns the node ID."""
        now = time.time()
        qualified = self._make_qualified(node)
        extra = json.dumps(node.extra) if node.extra else "{}"

        self._conn.execute(
            """INSERT INTO nodes
               (kind, name, qualified_name, file_path, line_start, line_end,
                language, parent_name, params, return_type, modifiers, is_test,
                file_hash, extra, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(qualified_name) DO UPDATE SET
                 kind=excluded.kind, name=excluded.name,
                 file_path=excluded.file_path, line_start=excluded.line_start,
                 line_end=excluded.line_end, language=excluded.language,
                 parent_name=excluded.parent_name, params=excluded.params,
                 return_type=excluded.return_type, modifiers=excluded.modifiers,
                 is_test=excluded.is_test, file_hash=excluded.file_hash,
                 extra=excluded.extra, updated_at=excluded.updated_at
            """,
            (
                node.kind, node.name, qualified, node.file_path,
                node.line_start, node.line_end, node.language,
                node.parent_name, node.params, node.return_type,
                node.modifiers, int(node.is_test), file_hash,
                extra, now,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM nodes WHERE qualified_name = ?", (qualified,)
        ).fetchone()
        return row["id"]

    def upsert_edge(self, edge: EdgeInfo) -> int:
        """Insert or update an edge."""
        now = time.time()
        extra = json.dumps(edge.extra) if edge.extra else "{}"

        # Check for existing edge (include line so multiple call sites are preserved)
        existing = self._conn.execute(
            """SELECT id FROM edges
               WHERE kind=? AND source_qualified=? AND target_qualified=?
                     AND file_path=? AND line=?""",
            (edge.kind, edge.source, edge.target, edge.file_path, edge.line),
        ).fetchone()

        if existing:
            self._conn.execute(
                "UPDATE edges SET line=?, extra=?, updated_at=? WHERE id=?",
                (edge.line, extra, now, existing["id"]),
            )
            return existing["id"]

        self._conn.execute(
            """INSERT INTO edges
               (kind, source_qualified, target_qualified, file_path, line, extra, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (edge.kind, edge.source, edge.target, edge.file_path, edge.line, extra, now),
        )
        return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def remove_file_data(self, file_path: str) -> None:
        """Remove all nodes and edges associated with a file."""
        self._conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))
        self._conn.execute("DELETE FROM edges WHERE file_path = ?", (file_path,))
        # cache invalidation removed (networkx stripped)

    def store_file_nodes_edges(
        self, file_path: str, nodes: list[NodeInfo], edges: list[EdgeInfo], fhash: str = ""
    ) -> None:
        """Atomically replace all data for a file."""
        # Defense-in-depth: flush any pending transaction before BEGIN
        # IMMEDIATE.  The root cause (implicit transactions from legacy
        # isolation_level="") is fixed by setting isolation_level=None in
        # __init__, but external code accessing _conn directly (e.g.
        # _compute_summaries, flows.py, communities.py) could still leave
        # a transaction open.
        # See: https://github.com/tirth8205/code-review-graph/issues/135
        if self._conn.in_transaction:
            logger.warning("Flushing unexpected open transaction before BEGIN IMMEDIATE")
            self._conn.commit()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self.remove_file_data(file_path)
            for node in nodes:
                self.upsert_node(node, file_hash=fhash)
            for edge in edges:
                self.upsert_edge(edge)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        # cache invalidation removed (networkx stripped)

    def set_metadata(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def get_metadata(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # --- Read operations ---

    def get_nodes_by_file(self, file_path: str) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE file_path = ?", (file_path,)
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_edges_by_target(self, qualified_name: str) -> list[GraphEdge]:
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE target_qualified = ?", (qualified_name,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_all_files(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE kind = 'File'"
        ).fetchall()
        return [r["file_path"] for r in rows]

    def get_file_hashes(self) -> dict[str, str]:
        """Return ``{file_path: file_hash}`` for every indexed file.

        Used by incremental indexing to skip files whose content hash is
        unchanged. Keyed on the ``File`` node, which carries the file's hash.
        """
        rows = self._conn.execute(
            "SELECT file_path, file_hash FROM nodes WHERE kind = 'File'"
        ).fetchall()
        return {r["file_path"]: (r["file_hash"] or "") for r in rows}

    # --- Impact / Graph traversal ---

    def get_reverse_impact_sql(
        self,
        changed_files: list[str],
        max_depth: int = MAX_IMPACT_DEPTH,
        max_nodes: int = MAX_IMPACT_NODES,
    ) -> dict[str, Any]:
        """Find nodes that *depend on* the changed files (reverse dependencies).

        Unlike :meth:`get_impact_radius_sql` (which traverses edges in both
        directions), this follows edges only from ``target`` back to
        ``source`` — i.e. "who would break if this were deleted." Depth is
        tracked per node (1 = direct dependent).

        Returns dict with:
          - ``changed_nodes``: seed nodes in the changed files
          - ``dependents``: list of ``(GraphNode, depth)`` ordered by depth
        """
        empty = {"changed_nodes": [], "dependents": []}
        if not changed_files:
            return empty

        seeds: set[str] = set()
        for f in changed_files:
            for n in self.get_nodes_by_file(f):
                seeds.add(n.qualified_name)
        if not seeds:
            return empty

        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _rimpact_seeds (qn TEXT PRIMARY KEY)"
        )
        self._conn.execute("DELETE FROM _rimpact_seeds")
        seed_list = list(seeds)
        for i in range(0, len(seed_list), 450):
            batch = seed_list[i:i + 450]
            placeholders = ",".join("(?)" for _ in batch)
            self._conn.execute(  # nosec B608
                f"INSERT OR IGNORE INTO _rimpact_seeds (qn) VALUES {placeholders}", batch
            )

        cte_sql = """
        WITH RECURSIVE dependents(node_qn, depth) AS (
            SELECT qn, 0 FROM _rimpact_seeds
            UNION
            SELECT e.source_qualified, d.depth + 1
            FROM dependents d
            JOIN edges e ON e.target_qualified = d.node_qn
            WHERE d.depth < ?
        )
        SELECT node_qn, MIN(depth) AS depth
        FROM dependents
        GROUP BY node_qn
        ORDER BY depth
        LIMIT ?
        """
        rows = self._conn.execute(cte_sql, (max_depth, max_nodes + len(seeds))).fetchall()

        depth_by_qn = {r[0]: r[1] for r in rows if r[0] not in seeds}
        changed_nodes = self._batch_get_nodes(seeds)
        dep_nodes = self._batch_get_nodes(set(depth_by_qn))
        dependents = sorted(
            ((n, depth_by_qn[n.qualified_name]) for n in dep_nodes),
            key=lambda pair: pair[1],
        )
        return {"changed_nodes": changed_nodes, "dependents": dependents[:max_nodes]}

    def get_stats(self) -> GraphStats:
        """Return aggregate statistics about the graph."""
        total_nodes = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

        nodes_by_kind: dict[str, int] = {}
        for row in self._conn.execute("SELECT kind, COUNT(*) as cnt FROM nodes GROUP BY kind"):
            nodes_by_kind[row["kind"]] = row["cnt"]

        edges_by_kind: dict[str, int] = {}
        for row in self._conn.execute("SELECT kind, COUNT(*) as cnt FROM edges GROUP BY kind"):
            edges_by_kind[row["kind"]] = row["cnt"]

        languages = [
            r["language"] for r in self._conn.execute(
                "SELECT DISTINCT language FROM nodes WHERE language IS NOT NULL AND language != ''"
            )
        ]

        files_count = self._conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'File'"
        ).fetchone()[0]

        last_updated = self.get_metadata("last_updated")

        return GraphStats(
            total_nodes=total_nodes,
            total_edges=total_edges,
            nodes_by_kind=nodes_by_kind,
            edges_by_kind=edges_by_kind,
            languages=languages,
            files_count=files_count,
            last_updated=last_updated,
        )

    # --- Public edge access ---

    def get_all_edges(self) -> list[GraphEdge]:
        """Return all edges in the graph."""
        rows = self._conn.execute("SELECT * FROM edges").fetchall()
        return [self._row_to_edge(r) for r in rows]

    def _batch_get_nodes(self, qualified_names: set[str]) -> list[GraphNode]:
        """Batch-fetch nodes by qualified name, staying under SQLite variable limits."""
        if not qualified_names:
            return []
        qns = list(qualified_names)
        results: list[GraphNode] = []
        batch_size = 450
        for i in range(0, len(qns), batch_size):
            batch = qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT * FROM nodes WHERE qualified_name IN ({placeholders})",
                batch,
            ).fetchall()
            results.extend(self._row_to_node(r) for r in rows)
        return results

    # --- Internal helpers ---

    def _make_qualified(self, node: NodeInfo) -> str:
        if node.kind == "File":
            return node.file_path
        if node.parent_name:
            return f"{node.file_path}::{node.parent_name}.{node.name}"
        return f"{node.file_path}::{node.name}"

    def _row_to_node(self, row: sqlite3.Row) -> GraphNode:
        return GraphNode(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            file_path=row["file_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            language=row["language"] or "",
            parent_name=row["parent_name"],
            params=row["params"],
            return_type=row["return_type"],
            is_test=bool(row["is_test"]),
            file_hash=row["file_hash"],
            extra=json.loads(row["extra"]) if row["extra"] else {},
        )

    def _row_to_edge(self, row: sqlite3.Row) -> GraphEdge:
        return GraphEdge(
            id=row["id"],
            kind=row["kind"],
            source_qualified=row["source_qualified"],
            target_qualified=row["target_qualified"],
            file_path=row["file_path"],
            line=row["line"],
            extra=json.loads(row["extra"]) if row["extra"] else {},
        )
