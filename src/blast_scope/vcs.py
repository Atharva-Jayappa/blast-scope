"""Version-control consequences — what a git command would actually destroy.

``command_effects`` already knows that ``git reset --hard`` is destructive. This
module answers the harder, context-dependent question: *how much would it
actually destroy right now?* ``git reset --hard`` on a clean tree is harmless;
on a tree with 12 modified files it obliterates a day's work. The difference is
the current working-tree state, which we read (cached) via ``recoverability``.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

from blast_scope.consequences import Consequence
from blast_scope.command_parser import ParsedCommand
from blast_scope.recoverability import working_tree_state

logger = logging.getLogger(__name__)

# Cap the floor a count-based consequence can reach, so a huge dirty tree
# doesn't trivially saturate the score.
_MAX_COUNT_FLOOR = 0.9


def analyze_git(parsed: ParsedCommand, raw: str, cwd: Path) -> Consequence | None:
    """Return the consequence of a destructive git command, or ``None``.

    Args:
        parsed: Parsed command (used only to confirm ``command == "git"``).
        raw: Original command string — the parser discards the subcommand,
            so we re-tokenize to recover ``reset`` / ``clean`` / ``push`` etc.
        cwd: Working directory, used to locate the repository.

    Returns:
        A ``Consequence`` describing the real impact, or ``None`` if this is
        not a destructive git operation.

    Example::

        >>> analyze_git(parse_command("git reset --hard"), "git reset --hard", cwd)
        Consequence(domain='vcs', floor=0.7, evidence='git reset --hard would discard ...')
    """
    if parsed["command"] != "git":
        return None

    sub, flags = _subcommand(raw)
    if sub is None:
        return None

    counts = working_tree_state(cwd)
    modified, untracked = (counts[0], counts[1]) if counts else (0, 0)

    if sub == "reset" and _has(flags, "--hard"):
        return _from_count(
            modified,
            "git reset --hard would discard {n} file(s) with uncommitted changes",
            "git reset --hard, but the working tree is clean — nothing to lose",
        )
    if sub == "clean" and _has_force_clean(flags):
        return _from_count(
            untracked,
            "git clean would delete {n} untracked file(s) permanently",
            "git clean, but there are no untracked files to remove",
        )
    if sub in ("checkout", "restore", "switch") and (
        sub == "restore" or _has(flags, "--force", "-f") or _targets_paths(raw, sub)
    ):
        return _from_count(
            modified,
            "discarding local changes would lose {n} modified file(s)",
            "no local modifications to discard",
        )
    if sub == "stash" and _drops_stash(raw):
        return Consequence("vcs", 0.5, "dropping a stash permanently removes those changes")
    if sub == "push" and _has(flags, "--force", "-f"):
        return Consequence(
            "vcs", 0.7,
            "force-push can overwrite remote history other clones depend on "
            "(prefer --force-with-lease)",
        )
    if sub in ("rebase", "filter-branch", "filter-repo"):
        return Consequence("vcs", 0.6, f"git {sub} rewrites commit history")
    if sub == "branch" and _has(flags, "-D"):
        return Consequence("vcs", 0.4, "force-deleting a branch drops unmerged commits")
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _subcommand(raw: str) -> tuple[str | None, list[str]]:
    """Extract the git subcommand and flags from a raw command string."""
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if not tokens or tokens[0] != "git":
        # tolerate a leading "sudo"
        if len(tokens) >= 2 and tokens[0] == "sudo" and tokens[1] == "git":
            tokens = tokens[1:]
        else:
            return (None, [])
    rest = tokens[1:]
    sub: str | None = None
    flags: list[str] = []
    for tok in rest:
        if tok.startswith("-"):
            flags.append(tok)
        elif sub is None:
            sub = tok
        # remaining positionals are operands, not needed here
    return (sub, flags)


def _has(flags: list[str], *wanted: str) -> bool:
    return any(f in wanted for f in flags)


def _has_force_clean(flags: list[str]) -> bool:
    # `git clean -f`, `-fd`, `-fdx`, `--force` all qualify.
    return any(
        f == "--force" or (f.startswith("-") and not f.startswith("--") and "f" in f)
        for f in flags
    )


def _targets_paths(raw: str, sub: str) -> bool:
    # `git checkout -- path` or `git checkout .` discards working-tree changes.
    return " -- " in f" {raw} " or raw.rstrip().endswith((" .", f"{sub} ."))


def _drops_stash(raw: str) -> bool:
    return any(word in raw.split() for word in ("drop", "clear", "pop"))


def _from_count(count: int, busy_msg: str, clean_msg: str) -> Consequence:
    """Build a count-scaled consequence; floor is 0 when there is nothing to lose."""
    if count <= 0:
        return Consequence("vcs", 0.0, clean_msg)
    floor = min(_MAX_COUNT_FLOOR, 0.4 + 0.05 * count)
    return Consequence("vcs", floor, busy_msg.format(n=count))
