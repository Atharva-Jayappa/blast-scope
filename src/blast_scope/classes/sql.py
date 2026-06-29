"""SQL consequence class — destructive statements run through a DB client.

SQL is the one class whose danger is *embedded* in another command:
``psql -c "DROP TABLE users"``, ``mysql -e "DELETE FROM logs"``,
``sqlite3 app.db "TRUNCATE events"``. Stage 1 extracts the statement and matches
the three irreversible shapes — ``DROP`` / ``TRUNCATE`` / ``DELETE`` without a
``WHERE``.

The safe probe is engine-specific and read-only:

- **SQLite** — opened ``mode=ro`` via the stdlib (zero deps): confirm the table
  exists and ``SELECT count(*)`` its rows. A real magnitude, no mutation.
- **Postgres / MySQL** — no in-process driver, and the server may be remote, so
  we do *not* connect; we estimate from the statement and label it.

Reversibility hinges on transaction context: a statement inside an open
``BEGIN`` (no ``COMMIT``) can be rolled back, so it scores far lower.
"""

from __future__ import annotations

import logging
import re
import shlex
import sqlite3
from pathlib import Path

from blast_scope.classes import Candidate
from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 2.0

# Client command → engine.
_ENGINES: dict[str, str] = {
    "psql": "postgres", "pgcli": "postgres",
    "mysql": "mysql", "mariadb": "mysql",
    "sqlite3": "sqlite",
}

_DROP_RE = re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW|INDEX)\b\s+(?:IF\s+EXISTS\s+)?([`\"\[]?\w+)", re.I)
_TRUNCATE_RE = re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?([`\"\[]?\w+)", re.I)
_DELETE_RE = re.compile(r"\bDELETE\s+FROM\s+([`\"\[]?\w+)", re.I)
_WHERE_RE = re.compile(r"\bWHERE\b", re.I)
# A WHERE that still removes (nearly) everything — a scoped DELETE this is not.
# `WHERE rowid NOT IN (SELECT MIN(rowid) ...)` keeps one row per group; `1=1`
# and `WHERE true` are unconditional.
_MASS_WHERE_RE = re.compile(r"\bNOT\s+IN\b|\b1\s*=\s*1\b|\bWHERE\s+(?:true|1)\b", re.I)
_TX_OPEN_RE = re.compile(r"\b(BEGIN|START\s+TRANSACTION)\b", re.I)
_COMMIT_RE = re.compile(r"\bCOMMIT\b", re.I)


class SqlClass:
    """Consequence class for destructive SQL run via a DB client."""

    name = "sql"

    # -- Stage 1: triage -----------------------------------------------------

    def triage(self, raw: str, parsed) -> Candidate | None:
        """Extract the SQL and match DROP / TRUNCATE / DELETE-without-WHERE."""
        engine = _ENGINES.get(parsed.get("command", ""))
        if engine is None:
            return None
        sql, dbfile = _extract_sql(raw, engine)
        if not sql:
            return None
        op = _classify_sql(sql)
        if op is None:
            return None
        return Candidate(self.name, op, raw, operands=(engine, dbfile or ""))

    # -- declared read-only probe surface ------------------------------------

    def probe_commands(self, candidate: Candidate) -> list[list[str]]:
        """The read-only queries the SQLite probe issues (others don't probe)."""
        engine = candidate.operands[0] if candidate.operands else ""
        if engine != "sqlite":
            return []
        return [
            ["SELECT", "name FROM sqlite_master WHERE type='table' AND name=?"],
            ["SELECT", "count(*) FROM <table>"],
        ]

    # -- Stage 2: assess -----------------------------------------------------

    def assess(self, candidate: Candidate, cwd: Path) -> Consequence | None:
        engine = candidate.operands[0] if candidate.operands else ""
        dbfile = candidate.operands[1] if len(candidate.operands) > 1 else ""
        sql, _ = _extract_sql(candidate.raw, engine)
        if not sql:
            return None
        table = _target_table(sql)
        transactional = _is_transactional(candidate.raw, sql)

        available, exists, count = (False, None, None)
        if engine == "sqlite" and dbfile:
            available, exists, count = _sqlite_probe(cwd, dbfile, table)

        return _build_consequence(
            candidate.operation, table, engine, transactional, available, exists, count
        )


# ---------------------------------------------------------------------------
# SQL extraction + classification (pure)
# ---------------------------------------------------------------------------


def _extract_sql(raw: str, engine: str) -> tuple[str | None, str | None]:
    """Pull the SQL statement (and a sqlite db path) out of a client command."""
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if not tokens:
        return (None, None)

    if engine == "sqlite":
        positional = [t for t in tokens[1:] if not t.startswith("-")]
        dbfile = positional[0] if positional else None
        sql = positional[1] if len(positional) > 1 else None
        return (sql, dbfile)

    # postgres / mysql: SQL comes from -c/-e/--command/--execute.
    for i, tok in enumerate(tokens):
        if tok in ("-c", "-e", "--command", "--execute") and i + 1 < len(tokens):
            return (tokens[i + 1], None)
        if tok.startswith(("--command=", "--execute=")):
            return (tok.split("=", 1)[1], None)
    return (None, None)


def _classify_sql(sql: str) -> str | None:
    """Return the destructive op id for a statement, or ``None`` if benign."""
    if _DROP_RE.search(sql):
        return "drop"
    if _TRUNCATE_RE.search(sql):
        return "truncate"
    if _DELETE_RE.search(sql) and (
        not _WHERE_RE.search(sql) or _MASS_WHERE_RE.search(sql)
    ):
        return "delete_all"
    return None


def _target_table(sql: str) -> str:
    """Best-effort table/object name the statement targets."""
    for rx in (_DROP_RE, _TRUNCATE_RE, _DELETE_RE):
        m = rx.search(sql)
        if m:
            return m.group(m.lastindex).strip("`\"[]")
    return "the target"


def _is_transactional(raw: str, sql: str) -> bool:
    """True if the statement runs inside an open transaction (rollback-able)."""
    if "--single-transaction" in raw:
        return True
    return bool(_TX_OPEN_RE.search(sql)) and not _COMMIT_RE.search(sql)


# ---------------------------------------------------------------------------
# SQLite probe (read-only, stdlib)
# ---------------------------------------------------------------------------


def _sqlite_probe(cwd: Path, dbfile: str, table: str) -> tuple[bool, bool | None, int | None]:
    """Open the db read-only and return (available, table_exists, row_count)."""
    path = Path(dbfile)
    if not path.is_absolute():
        path = cwd / dbfile
    if not path.is_file():
        return (False, None, None)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_PROBE_TIMEOUT)
    except sqlite3.Error:
        return (False, None, None)
    try:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        exists = row is not None
        count: int | None = None
        if exists:
            # table is validated against sqlite_master above; quote for safety.
            count = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        return (True, exists, count)
    except sqlite3.Error:
        return (True, None, None)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Floor model (per-class radius × reversibility)
# ---------------------------------------------------------------------------


def _build_consequence(
    op: str,
    table: str,
    engine: str,
    transactional: bool,
    available: bool,
    exists: bool | None,
    count: int | None,
) -> Consequence:
    if transactional:
        floor = 0.6 if op in ("drop", "truncate") else 0.5
        return Consequence(
            "sql", floor,
            f"{_verb(op)} {table} inside an open transaction — recoverable by "
            f"ROLLBACK if not committed",
            estimated=not available,
        )

    if available and exists is False:
        return Consequence(
            "sql", 0.2, f"{table} does not exist in the database — nothing to lose"
        )

    rows = f"{count} row(s)" if count is not None else "all rows"
    if op == "drop":
        base = 0.9
        what = f"drops {table} — its schema and {rows}, irreversible"
    elif op == "truncate":
        base = 0.85
        what = f"truncates {table} — removes {rows}, not transactional on many engines"
    else:  # delete_all
        if available and count == 0:
            return Consequence("sql", 0.15, f"DELETE with no WHERE on empty table {table}")
        # High, not critical: the schema survives and a full DELETE is often
        # inside a transaction / restorable from backup, unlike DROP/TRUNCATE.
        base = 0.75
        what = f"DELETE with no WHERE removes {rows} from {table}"

    if available:
        return Consequence("sql", base, what, estimated=False)
    return Consequence(
        "sql", base, f"{what} (estimated — no read-only probe for {engine})", estimated=True
    )


def _verb(op: str) -> str:
    return {"drop": "DROP", "truncate": "TRUNCATE", "delete_all": "DELETE"}.get(op, op)
