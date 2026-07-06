"""Find consequence class — dry-run oracle for ``find -delete`` / ``-exec rm``.

``find . -name '*.log' -delete`` names its victims through a filesystem walk;
statically the target set is unknowable. But find has a perfect, built-in
side-effect-free preview: the same expression with the destructive terminal
replaced by ``-print``. This class performs that rewrite *faithfully* and runs
it, turning "find deletes an unknown set" into an exact target list.

Faithfulness rules (each one is a way a naive rewrite lies):

- ``-delete`` implies ``-depth``. The rewrite must add ``-depth`` explicitly,
  or expressions using ``-prune`` traverse differently and the preview shows a
  different (larger) set than the deletion would touch.
- The destructive terminal is replaced **in place**, never appended: find
  evaluates its expression left-to-right and ``-delete`` acts as a filter for
  anything to its right.
- Expressions where the rewrite is not provably faithful — multiple
  destructive terminals, ``-o``/``-or`` alternation — are left alone and the
  static classification (destructive, weight 0.8) stands.
- ``-exec rm -r {} ;`` prints subtree *roots*; the true blast radius is each
  whole subtree. The evidence says so, and downstream recoverability walks
  directories anyway.

The rewritten command runs with a hard timeout and is the ONLY thing executed
— never the original.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from blast_scope.classes import Candidate
from blast_scope.command_parser import ParsedCommand
from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 2.0
_MAX_TARGETS = 200

# Destructive -exec payload verbs — kept in sync with
# command_effects._classify_find's set.
_DESTRUCTIVE_EXEC = frozenset({"rm", "rmdir", "shred", "truncate", "dd", "mv", "chmod"})


class FindClass:
    """Consequence class that previews find's destructive match set."""

    name = "find"

    def triage(self, raw: str, parsed: ParsedCommand) -> Candidate | None:
        """Detect a destructive find, cheaply (string inspection only).

        Example::

            >>> FindClass().triage("find . -name '*.tmp' -delete", p).operation
            'delete'
        """
        if parsed.get("command") != "find":
            return None
        tokens = _tokens(raw)
        if "-delete" in tokens:
            return Candidate(cls=self.name, operation="delete", raw=raw)
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in tokens:
                idx = tokens.index(flag)
                if idx + 1 < len(tokens) and Path(tokens[idx + 1]).name in _DESTRUCTIVE_EXEC:
                    return Candidate(cls=self.name, operation="exec", raw=raw)
        return None

    def assess(self, candidate: Candidate, cwd: Path) -> Consequence | None:
        """Run the faithful -print rewrite; emit the exact match set.

        Returns ``None`` (static classification stands) when the rewrite is
        not provably faithful or the probe cannot run.
        """
        rewritten, subtree_roots = _rewrite(candidate.raw)
        if rewritten is None:
            return None
        # Bound the walk to the working tree: `find / -name id_rsa -delete`
        # must not trigger a whole-disk traversal (and path disclosure) during
        # analysis. An out-of-tree root ⇒ keep the static classification.
        if not _roots_within_cwd(rewritten, cwd):
            return None
        out = _run_find(rewritten, cwd)
        if out is None:
            return None

        matches = [l.strip() for l in out.splitlines() if l.strip()][:_MAX_TARGETS]
        if not matches:
            return Consequence(
                "fs", 0.0, "find dry-run (rewritten to -print): matches nothing right now"
            )
        shown = ", ".join(Path(m).name for m in matches[:6])
        if len(matches) > 6:
            shown += "…"
        note = (
            " — matched paths are subtree ROOTS (recursive rm): whole trees go"
            if subtree_roots
            else ""
        )
        return Consequence(
            "fs",
            0.0,  # scoring comes from the targets via recoverability/mass gates
            f"find would destroy {len(matches)} path(s) (dry-run via -print): {shown}{note}",
            targets=tuple(matches),
        )


# ---------------------------------------------------------------------------
# Rewrite + runner
# ---------------------------------------------------------------------------


def _tokens(raw: str) -> list[str]:
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _roots_within_cwd(argv: list[str], cwd: Path) -> bool:
    """True if every ``find`` root path resolves inside the working tree.

    Roots are the operands between ``find`` and the first predicate/option
    (a token starting with ``-``, ``(``, ``!``). Absolute or ``../`` roots that
    escape ``cwd`` disqualify the probe.
    """
    try:
        cwd_res = cwd.resolve()
    except OSError:
        return False
    roots: list[str] = []
    for tok in argv[1:]:
        if tok.startswith(("-", "(", "!")):
            break
        roots.append(tok)
    if not roots:
        return False  # no explicit root → find defaults to cwd, but be strict
    for r in roots:
        p = Path(r)
        try:
            resolved = p.resolve() if (p.is_absolute() or r.startswith("/")) else (cwd_res / r).resolve()
            resolved.relative_to(cwd_res)
        except (OSError, ValueError):
            return False
    return True


def _rewrite(raw: str) -> tuple[list[str] | None, bool]:
    """Build the faithful -print preview argv, or ``(None, False)`` if unsure.

    Example::

        >>> _rewrite("find . -name '*.log' -delete")[0]
        ['find', '.', '-name', '*.log', '-print', '-depth']
    """
    tokens = _tokens(raw)
    if not tokens or Path(tokens[0]).name != "find":
        return None, False

    # Alternation makes the terminal's position semantics non-trivial — punt.
    if any(t in ("-o", "-or") for t in tokens):
        return None, False

    delete_count = tokens.count("-delete")
    exec_positions = [i for i, t in enumerate(tokens) if t in ("-exec", "-execdir", "-ok", "-okdir")]
    if delete_count + len(exec_positions) != 1:
        return None, False  # zero or multiple destructive terminals

    out = list(tokens)
    subtree_roots = False

    if delete_count == 1:
        idx = out.index("-delete")
        out[idx] = "-print"
        # -delete implies -depth; without it -prune behaves differently and
        # the preview's traversal diverges from the deletion's.
        if "-depth" not in out and "-d" not in out:
            # Insert as a positional option right after the path arguments.
            insert_at = 1
            while insert_at < len(out) and not out[insert_at].startswith(("-", "(", "!")):
                insert_at += 1
            out.insert(insert_at, "-depth")
    else:
        start = exec_positions[0]
        payload_verb = Path(out[start + 1]).name if start + 1 < len(out) else ""
        if payload_verb not in _DESTRUCTIVE_EXEC:
            return None, False
        # Find the clause terminator: ';' (shlex already unescaped '\;') or '+'.
        end = None
        for j in range(start + 1, len(out)):
            if out[j] in (";", "+"):
                end = j
                break
        if end is None:
            return None, False
        clause = out[start : end + 1]
        subtree_roots = payload_verb == "rm" and any(
            t.startswith("-") and "r" in t.lstrip("-").lower() for t in clause
        )
        out[start : end + 1] = ["-print"]

    return out, subtree_roots


def _run_find(argv: list[str], cwd: Path) -> str | None:
    """Execute the rewritten (read-only) find; None on any failure.

    Strict ``returncode == 0``: on Windows the ``find`` on PATH is the
    unrelated string-search tool and fails immediately — degrading here is
    exactly right.
    """
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout
