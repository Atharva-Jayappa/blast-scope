"""Git consequence class — the exemplar for the class/probe abstraction.

Git already had a context-aware analyzer (:mod:`blast_scope.vcs`) that reads the
working-tree state to scale a destructive op's floor. This class *reuses* that
calibrated base (rather than re-deriving it) and extends it with the richer,
strictly read-only probes the v0.2 model calls for:

- **reflog window** — committed history a ``reset --hard`` / ``branch -D`` drops
  is still recoverable from the reflog, so the finding says so.
- **upstream divergence** — a ``push --force`` is only catastrophic if the
  remote actually has commits it would orphan; we count them via
  ``rev-list HEAD..@{u}`` and escalate when the branch is also *protected*.
- **branch merge state** — deleting a fully-merged branch is low risk; an
  unmerged one carries commits (still reflog-recoverable for the gc window).

Every probe here is a read-only git plumbing read (``status`` / ``reflog`` /
``rev-parse`` / ``rev-list``) — see :meth:`GitClass.probe_commands`. When a probe
can't run (not a repo, git missing, timeout) the class degrades to the base
consequence and labels the refinement ``estimated``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from blast_scope import vcs
from blast_scope.classes import Candidate
from blast_scope.command_parser import ParsedCommand
from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)

# Generous per-probe timeout: the hook tolerates a short delay for a genuinely
# destructive command, and degrades to a heuristic rather than stalling.
_PROBE_TIMEOUT = 3.0

# Branch names (and prefixes) whose history other people depend on. Force-pushing
# over these is the difference between "redo my own work" and "broke the team".
_PROTECTED_BRANCHES: frozenset[str] = frozenset(
    {"main", "master", "develop", "trunk", "prod", "production", "release"}
)
_PROTECTED_PREFIXES: tuple[str, ...] = ("release/", "hotfix/", "support/")


class GitClass:
    """Consequence class for destructive git operations."""

    name = "git"

    # -- Stage 1: triage (pure, no subprocess) ------------------------------

    def triage(self, raw: str, parsed: ParsedCommand) -> Candidate | None:
        """Classify a git command as a destructive operation, cheaply.

        Mirrors the destructive conditions in :func:`blast_scope.vcs.analyze_git`
        using only string/flag inspection — no repo reads — so non-destructive
        git (and every non-git command) exits in microseconds.

        Example::

            >>> GitClass().triage("git reset --hard", p).operation
            'reset_hard'
            >>> GitClass().triage("git status", p) is None
            True
        """
        if parsed.get("command") != "git":
            return None
        sub, flags = vcs._subcommand(raw)
        if sub is None:
            return None

        op = _destructive_op(sub, flags, raw)
        if op is None:
            return None
        return Candidate(cls=self.name, operation=op, raw=raw)

    # -- declared read-only probe surface (for the no-mutation test) ---------

    def probe_commands(self, candidate: Candidate) -> list[list[str]]:
        """The read-only git reads ``assess`` may run for this candidate."""
        cmds: list[list[str]] = [
            ["git", "reflog", "--oneline", "-n", "1"],
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ]
        if candidate.operation == "push_force":
            cmds += [
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                ["git", "rev-list", "--count", "HEAD..@{u}"],
            ]
        elif candidate.operation == "branch_delete":
            cmds += [
                ["git", "rev-list", "--count", "<branch>",
                 "--not", "--exclude=<branch>", "--branches", "--remotes"]
            ]
        return cmds

    # -- Stage 2: assess (probe + refine) -----------------------------------

    def assess(self, candidate: Candidate, cwd: Path) -> Consequence | None:
        """Compute the git consequence: calibrated base, refined by probes.

        The base floor (working-tree-scaled) comes from the existing, eval-
        calibrated :func:`blast_scope.vcs.analyze_git`; refinements only ever
        *raise* it on confirmed remote/branch danger, or annotate it, so the
        established filesystem/git calibration is preserved.
        """
        base = vcs.analyze_git({"command": "git"}, candidate.raw, cwd)
        if base is None:
            return None

        if candidate.operation == "push_force":
            floor, evidence, estimated = _refine_push_force(base, cwd)
        elif candidate.operation == "branch_delete":
            floor, evidence, estimated = _refine_branch_delete(base, cwd, candidate)
        elif candidate.operation == "reset_hard":
            floor, evidence, estimated = _annotate_reflog(base, cwd)
        else:
            floor, evidence, estimated = base.floor, base.evidence, False

        return Consequence("vcs", floor, evidence, estimated=estimated)


# ---------------------------------------------------------------------------
# Triage helper (pure)
# ---------------------------------------------------------------------------


def _destructive_op(sub: str, flags: list[str], raw: str) -> str | None:
    """Map a git subcommand+flags to a destructive operation id, or ``None``.

    Pure — matches :func:`blast_scope.vcs.analyze_git`'s gating so triage and the
    base consequence always agree on what counts as destructive.
    """
    if sub == "reset" and vcs._has(flags, "--hard"):
        return "reset_hard"
    if sub == "clean" and vcs._has_force_clean(flags):
        return "clean_force"
    if sub == "push" and vcs._has(flags, "--force", "-f"):
        return "push_force"
    if sub in ("checkout", "restore", "switch") and (
        sub == "restore" or vcs._has(flags, "--force", "-f") or vcs._targets_paths(raw, sub)
    ):
        return "discard_paths"
    if sub == "stash" and vcs._drops_stash(raw):
        return "stash_drop"
    if sub in ("rebase", "filter-branch", "filter-repo"):
        return "history_rewrite"
    if sub == "branch" and vcs._has(flags, "-D"):
        return "branch_delete"
    return None


# ---------------------------------------------------------------------------
# Probe helpers (read-only)
# ---------------------------------------------------------------------------


def _git_read(cwd: Path, *args: str) -> str | None:
    """Run a read-only ``git`` command, returning stdout or ``None`` on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _has_reflog(cwd: Path) -> bool:
    return bool(_git_read(cwd, "reflog", "--oneline", "-n", "1"))


def _is_protected(branch: str) -> bool:
    b = branch.lower()
    return b in _PROTECTED_BRANCHES or b.startswith(_PROTECTED_PREFIXES)


def _refine_push_force(base: Consequence, cwd: Path) -> tuple[float, str, bool]:
    """Refine a force-push floor by how much remote history is actually at risk.

    Raises the floor when the upstream has commits the push would orphan
    (more so on a protected branch). When there is no tracking branch we cannot
    verify the remote, so we keep the base floor and mark it estimated.
    """
    upstream = _git_read(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream is None:
        return (
            base.floor,
            base.evidence + " — no tracking branch, remote impact unverified",
            True,
        )

    orphaned_raw = _git_read(cwd, "rev-list", "--count", "HEAD..@{u}")
    try:
        orphaned = int(orphaned_raw) if orphaned_raw is not None else 0
    except ValueError:
        orphaned = 0

    branch = _git_read(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
    protected = _is_protected(branch)

    if orphaned > 0:
        floor = 0.85 if protected else max(base.floor, 0.8)
        where = f"protected branch {branch}" if protected else upstream
        return (
            max(base.floor, floor),
            f"force-push would orphan {orphaned} commit(s) on {where} that other "
            f"clones depend on (prefer --force-with-lease)",
            False,
        )

    # Upstream exists but is not ahead — little to no remote history to lose.
    # Kept at the base floor (a force-push is still a footgun) but said plainly.
    return (
        base.floor,
        f"force-push to {upstream}, which is not ahead of HEAD — little remote "
        f"history at risk (still prefer --force-with-lease)",
        False,
    )


def _refine_branch_delete(
    base: Consequence, cwd: Path, candidate: Candidate
) -> tuple[float, str, bool]:
    """Refine ``branch -D`` by whether the branch is merged / reflog-recoverable."""
    target = _branch_operand(candidate.raw)
    if target is None:
        return base.floor, base.evidence, False

    # Commits reachable from <target> but from no *other* branch/remote — i.e.
    # unique to this branch. The target must be excluded from the negated set,
    # otherwise it cancels itself out to zero.
    unmerged_raw = _git_read(
        cwd, "rev-list", "--count", target,
        "--not", "--exclude=" + target, "--branches", "--remotes",
    )
    if unmerged_raw is None:
        # Branch missing or probe failed — can't tell, keep base, mark estimate.
        return base.floor, base.evidence + " (merge state unverified)", True
    try:
        unmerged = int(unmerged_raw)
    except ValueError:
        return base.floor, base.evidence, True

    reflog = _has_reflog(cwd)
    if unmerged == 0:
        # Fully merged: nothing unique is lost, and the ref is in the reflog.
        return (
            min(base.floor, 0.25),
            f"branch {target} is fully merged — deletion loses no unique commits"
            + (" (also recoverable from reflog)" if reflog else ""),
            False,
        )
    recover = " but recoverable from the reflog for the gc window" if reflog else ""
    return (
        max(base.floor, 0.45),
        f"deleting {target} drops {unmerged} unmerged commit(s){recover}",
        False,
    )


def _annotate_reflog(base: Consequence, cwd: Path) -> tuple[float, str, bool]:
    """Add a reflog note to a ``reset --hard`` finding (floor unchanged).

    The danger of ``reset --hard`` is the *uncommitted* work it discards (which
    the reflog cannot recover); committed history it moves off is still in the
    reflog. We surface that nuance without changing the calibrated floor.
    """
    if base.floor <= 0.0:
        return base.floor, base.evidence, False
    if _has_reflog(cwd):
        return (
            base.floor,
            base.evidence + " — committed history stays in the reflog, but "
            "uncommitted changes do not",
            False,
        )
    return base.floor, base.evidence, False


def _branch_operand(raw: str) -> str | None:
    """Return the first non-flag operand after ``branch`` (the branch name)."""
    import shlex

    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if "branch" not in tokens:
        return None
    after = tokens[tokens.index("branch") + 1 :]
    for tok in after:
        if not tok.startswith("-"):
            return tok
    return None
