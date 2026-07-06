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
``rev-parse`` / ``rev-list``), routed through :func:`_git_read`. When a probe
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

        Delegates to the canonical :func:`blast_scope.vcs.destructive_op` (the
        same classifier the consequence floor uses) — only string/flag
        inspection, no repo reads — so non-destructive git (and every non-git
        command) exits in microseconds.

        Example::

            >>> GitClass().triage("git reset --hard", p).operation
            'reset_hard'
            >>> GitClass().triage("git status", p) is None
            True
        """
        if parsed.get("command") != "git":
            return None
        sub, flags = vcs._subcommand(raw)
        op = vcs.destructive_op(sub, flags, raw)
        if op is None:
            return None
        return Candidate(cls=self.name, operation=op, raw=raw)

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

        targets: tuple[str, ...] = ()
        if candidate.operation == "push_force":
            floor, evidence, estimated = _refine_push_force(base, cwd)
        elif candidate.operation == "branch_delete":
            floor, evidence, estimated = _refine_branch_delete(base, cwd, candidate)
        elif candidate.operation == "reset_hard":
            floor, evidence, estimated = _refine_reset(base, cwd, candidate)
        elif candidate.operation == "clean_force":
            floor, evidence, estimated, targets = _refine_clean(base, cwd, candidate)
        elif candidate.operation == "discard_paths":
            floor, evidence, estimated, targets = _refine_discard(base, cwd, candidate)
        else:
            floor, evidence, estimated = base.floor, base.evidence, False

        return Consequence("vcs", floor, evidence, estimated=estimated, targets=targets)


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


# Floor for a reset that orphans committed history. Deliberately medium, not
# high: the reflog keeps orphaned commits recoverable for the gc window
# (~30-90 days by default) — the truly unrecoverable loss is the dirty
# working tree, which the count-scaled base floor already covers.
_RESET_DIVERGENCE_FLOOR = 0.45


def _refine_reset(
    base: Consequence, cwd: Path, candidate: Candidate
) -> tuple[float, str, bool]:
    """Refine ``reset --hard`` with the divergence to an explicit target ref.

    ``git reset --hard`` (implicit HEAD) only discards uncommitted work — the
    base floor covers it. ``git reset --hard origin/main`` *additionally*
    orphans every commit in ``<ref>..HEAD``; we count them with a read-only
    ``rev-list`` and floor at medium (reflog-recoverable, but not silently).
    """
    ref = _reset_operand(candidate.raw)
    if ref is None:
        return _annotate_reflog(base, cwd)

    if _git_read(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}") is None:
        return base.floor, base.evidence + f" (target ref {ref} unverified)", True

    orphaned_raw = _git_read(cwd, "rev-list", "--count", f"{ref}..HEAD")
    try:
        orphaned = int(orphaned_raw) if orphaned_raw is not None else 0
    except ValueError:
        orphaned = 0

    if orphaned <= 0:
        return (
            base.floor,
            base.evidence + f" — {ref} already contains HEAD, no commits orphaned",
            False,
        )

    reflog = _has_reflog(cwd)
    recover = (
        "recoverable via `git reflog` for the gc window (~30-90 days)"
        if reflog
        else "and this repo has NO reflog — they would be unreferenced"
    )
    floor = max(base.floor, _RESET_DIVERGENCE_FLOOR if reflog else 0.6)
    return (
        floor,
        f"reset --hard to {ref} orphans {orphaned} commit(s) — {recover}. "
        + base.evidence,
        False,
    )


def _refine_clean(
    base: Consequence, cwd: Path, candidate: Candidate
) -> tuple[float, str, bool, tuple[str, ...]]:
    """Replace the untracked-count estimate with ``git clean -n``'s exact list.

    The dry-run must mirror the real command's *selection* flags (``-d``,
    ``-x``/``-X``, ``-e`` excludes, pathspec) and swap only the execute switch
    (``-f`` → ``-n``) — dropping ``-x`` or ``-d`` would understate the blast
    radius. ``git clean -n`` is a pure enumeration: no deletion, no index
    write, no hooks.
    """
    probe = ["clean", "-n"]
    selection, pathspec = _clean_selection(candidate.raw)
    probe += selection + pathspec

    out = _git_read(cwd, *probe)
    if out is None:
        return base.floor, base.evidence + " (dry-run unavailable)", True, ()

    removed: list[str] = []
    skipped_repos: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Would remove "):
            removed.append(line[len("Would remove ") :].rstrip("/"))
        elif line.startswith("Would skip repository "):
            skipped_repos.append(line[len("Would skip repository ") :].rstrip("/"))

    if not removed and not skipped_repos:
        return 0.0, "git clean dry-run: nothing to remove", False, ()

    floor = min(0.9, 0.4 + 0.05 * len(removed)) if removed else base.floor
    shown = ", ".join(removed[:6]) + ("…" if len(removed) > 6 else "")
    evidence = (
        f"git clean would permanently delete {len(removed)} path(s) "
        f"(dry-run verified): {shown}"
    )
    if skipped_repos:
        evidence += (
            f" — plus {len(skipped_repos)} nested repo(s) skipped in the "
            f"preview that `-ff` WOULD remove: {', '.join(skipped_repos[:3])}"
        )
        floor = max(floor, 0.7)  # a nested repo is its own recovery net
    return floor, evidence, False, tuple(removed)


def _refine_discard(
    base: Consequence, cwd: Path, candidate: Candidate
) -> tuple[float, str, bool, tuple[str, ...]]:
    """Preview exactly which files a checkout/restore would clobber.

    ``git diff --name-only`` is a pure read that lists the files whose local
    content differs from what the checkout would write over them.
    """
    raw = candidate.raw
    if "--theirs" in raw or "--ours" in raw:
        out = _git_read(cwd, "diff", "--name-only", "--diff-filter=U")
        if out is None:
            return base.floor, base.evidence, True, ()
        files = [l.strip() for l in out.splitlines() if l.strip()]
        if not files:
            return base.floor, base.evidence + " (no unmerged paths right now)", False, ()
        side = "--theirs" if "--theirs" in raw else "--ours"
        floor = max(base.floor, min(0.9, 0.4 + 0.05 * len(files)))
        return (
            floor,
            f"checkout {side} overwrites the local side of {len(files)} "
            f"conflicted file(s): {', '.join(files[:6])}",
            False,
            tuple(files),
        )

    # The unrecoverable loss is UNCOMMITTED work under the pathspec — committed
    # differences vs the target ref just switch content and stay in history.
    # `diff HEAD` (worktree+index vs last commit) enumerates exactly that loss.
    _ref, pathspec = _checkout_operands(raw)
    args = ["diff", "--name-only", "HEAD"]
    if pathspec:
        args.append("--")
        args += pathspec
    out = _git_read(cwd, *args)
    if out is None:
        return base.floor, base.evidence, True, ()
    files = [l.strip() for l in out.splitlines() if l.strip()]
    if not files:
        return 0.0, "checkout/restore preview: no local changes would be clobbered", False, ()
    floor = min(0.9, 0.4 + 0.05 * len(files))
    return (
        floor,
        f"would discard uncommitted changes in {len(files)} file(s) "
        f"(diff-verified): {', '.join(files[:6])}",
        False,
        tuple(files),
    )


def _branch_operand(raw: str) -> str | None:
    """Return the first non-flag operand after ``branch`` (the branch name)."""
    after = _tokens_after(raw, "branch")
    for tok in after:
        if not tok.startswith("-"):
            return tok
    return None


def _reset_operand(raw: str) -> str | None:
    """Return the explicit target ref of a ``git reset``, or None for HEAD.

    ``git reset --hard`` → None (implicit HEAD); ``git reset --hard
    origin/main`` → ``origin/main``. A pathspec after ``--`` is not a ref.
    """
    after = _tokens_after(raw, "reset")
    for tok in after:
        if tok == "--":
            break
        if not tok.startswith("-"):
            return tok
    return None


def _clean_selection(raw: str) -> tuple[list[str], list[str]]:
    """Extract ``git clean``'s selection flags + pathspec, dropping force/quiet.

    Returns ``(selection_flags, pathspec)`` where selection is rebuilt from
    the original clusters: ``-fdx`` → ``['-d', '-x']``; ``-e PAT`` and
    ``--exclude=PAT`` are carried verbatim.
    """
    after = _tokens_after(raw, "clean")
    selection: list[str] = []
    pathspec: list[str] = []
    expect_exclude = False
    seen_double_dash = False
    for tok in after:
        if expect_exclude:
            selection += ["-e", tok]
            expect_exclude = False
            continue
        if seen_double_dash:
            pathspec.append(tok)
            continue
        if tok == "--":
            seen_double_dash = True
            continue
        if tok == "-e" or tok == "--exclude":
            expect_exclude = True
            continue
        if tok.startswith("--exclude="):
            selection += ["-e", tok[len("--exclude=") :]]
            continue
        if tok.startswith("--"):
            continue  # --force / --quiet / --dry-run — never mirrored
        if tok.startswith("-"):
            cluster = set(tok[1:])
            if "d" in cluster:
                selection.append("-d")
            if "x" in cluster:
                selection.append("-x")
            if "X" in cluster:
                selection.append("-X")
            continue
        pathspec.append(tok)
    return selection, pathspec


def _checkout_operands(raw: str) -> tuple[str | None, list[str]]:
    """Split checkout/restore operands into ``(ref, pathspec)``.

    ``git checkout main -- src/`` → ("main", ["src/"]);
    ``git checkout -- a.py`` → (None, ["a.py"]); ``git checkout .`` → (None, ["."]).
    """
    matched_sub = None
    after: list[str] = []
    for sub in ("checkout", "restore", "switch"):
        after = _tokens_after(raw, sub)
        if after:
            matched_sub = sub
            break
    if matched_sub is None:
        return None, []
    ref: str | None = None
    pathspec: list[str] = []
    seen_double_dash = False
    for tok in after:
        if tok == "--":
            seen_double_dash = True
            continue
        if tok.startswith("--source="):
            ref = tok[len("--source=") :]  # git restore names its ref this way
            continue
        if tok.startswith("-") and not seen_double_dash:
            continue
        # `git restore <paths>`: positionals are always paths, never refs.
        if (
            matched_sub == "restore"
            or seen_double_dash
            or tok == "."
            or "/" in tok
            or "*" in tok
        ):
            pathspec.append(tok)
        elif ref is None:
            ref = tok
        else:
            pathspec.append(tok)
    return ref, pathspec


def _tokens_after(raw: str, word: str) -> list[str]:
    """Tokens following the first occurrence of ``word`` in ``raw``."""
    import shlex

    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if word not in tokens:
        return []
    return tokens[tokens.index(word) + 1 :]
