"""Command effect rules: map a command + flags + operands to its effect.

This is the single source of truth for *what a command does* — its intent
(destructive / additive / read / unknown) and inherent danger weight. It
replaces the scattered intent tables and the flat ``COMMAND_WEIGHTS`` dict so
that flag-sensitive cases are handled in one place:

- ``find . -delete`` / ``find . -exec rm`` → destructive (not "read")
- ``sed -i`` (in-place) → destructive; plain ``sed`` → read
- ``git reset --hard`` / ``git clean -fdx`` / ``git push --force`` → destructive
- ``dd of=...`` → critical
- ``> file`` redirect clobber → overwrites

Pure functions, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Effect model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Effect:
    """The effect of a command.

    Example::

        >>> classify_effect("find", ["-delete"], ["."])
        Effect(intent='destructive', weight=0.7, recursive=False, ...)
    """

    intent: str  # "destructive" | "additive" | "read" | "unknown"
    weight: float  # inherent danger, 0.0 - 1.0
    recursive: bool = False
    in_place: bool = False
    note: str = ""
    safer_alternative: str | None = None


# ---------------------------------------------------------------------------
# Static tables
# ---------------------------------------------------------------------------

# Inherent danger weights for the common destructive/modifying commands.
COMMAND_WEIGHTS: dict[str, float] = {
    "mkfs": 1.0,
    "fdisk": 1.0,
    "dd": 0.9,
    "shred": 0.9,
    "rm": 0.9,
    "truncate": 0.8,
    "rmdir": 0.6,
    "mv": 0.5,
    "sed": 0.5,
    "chmod": 0.45,
    "chown": 0.45,
    "tee": 0.2,
    "cp": 0.15,
    "touch": 0.1,
    "mkdir": 0.1,
}

DEFAULT_WEIGHT: float = 0.3

_READ_COMMANDS: frozenset[str] = frozenset(
    {"cat", "head", "tail", "less", "more", "grep", "ls", "wc", "diff",
     "file", "stat", "echo", "pwd", "which", "env", "printenv", "du", "df"}
)

_ADDITIVE_COMMANDS: frozenset[str] = frozenset(
    {"touch", "mkdir", "cp", "tee", "install"}
)

_DESTRUCTIVE_COMMANDS: frozenset[str] = frozenset(
    {"rm", "rmdir", "truncate", "mkfs", "fdisk", "shred", "mv", "chmod", "chown"}
)

# PowerShell / cmd verbs and aliases → canonical POSIX-style command names.
# Applied by the parser before classification so the rest of the pipeline is
# shell-agnostic.
CANONICAL_COMMAND: dict[str, str] = {
    # remove
    "remove-item": "rm", "ri": "rm", "del": "rm", "erase": "rm", "rd": "rmdir",
    # move / rename
    "move-item": "mv", "move": "mv", "mi": "mv", "rename-item": "mv", "ren": "mv", "rni": "mv",
    # copy
    "copy-item": "cp", "copy": "cp", "cpi": "cp",
    # read
    "get-content": "cat", "gc": "cat", "type": "cat",
    "get-childitem": "ls", "gci": "ls", "dir": "ls",
    "select-string": "grep", "sls": "grep",
    # create / write
    "new-item": "touch", "ni": "touch",
    "set-content": "tee", "sc": "tee", "out-file": "tee", "add-content": "tee", "ac": "tee",
    "clear-content": "truncate", "clc": "truncate",
}

_RECURSIVE_LONG: frozenset[str] = frozenset({"--recursive", "-recurse", "-r"})
_FORCE_LONG: frozenset[str] = frozenset({"--force", "-force", "-f"})

# git subcommands that destroy or rewrite work, with their danger weight.
_GIT_DESTRUCTIVE_SAFER: dict[str, str] = {
    "reset": "snapshot first: `git stash` or `git branch backup` before a hard reset",
    "clean": "preview with `git clean -n` before `-f`",
    "checkout": "`git stash` to preserve local changes before discarding them",
    "restore": "`git stash` to preserve local changes before discarding them",
    "push": "use `--force-with-lease` instead of `--force`",
    "rebase": "`git branch backup` before rebasing shared history",
    "branch": "confirm the branch is merged before `-D`",
    "stash": "`git stash list` to confirm what you are dropping",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_effect(
    command: str,
    flags: list[str] | None = None,
    operands: list[str] | None = None,
    *,
    has_subshell: bool = False,
    clobber: bool = False,
) -> Effect:
    """Classify what a command does.

    Args:
        command: Canonical base command (already de-aliased), lowercased.
        flags: Parsed flags (``-rf``, ``--force``, ``-Recurse`` ...).
        operands: Positional, non-flag arguments (used for ``find -exec rm``,
            ``git`` subcommands, ``dd of=``).
        has_subshell: True if the command contains ``$(...)`` / backticks.
        clobber: True if a truncating ``>`` redirect targets a file.

    Returns:
        An :class:`Effect`.

    Example::

        >>> classify_effect("sed", ["-i"], ["s/a/b/", "f.py"]).intent
        'destructive'
        >>> classify_effect("sed", [], ["s/a/b/", "f.py"]).intent
        'read'
    """
    flags = flags or []
    operands = operands or []
    cmd = command.lower()

    if has_subshell:
        return Effect("unknown", DEFAULT_WEIGHT, note="contains command substitution; targets not statically resolvable")

    recursive = _is_recursive(flags)

    # --- command-specific rules (override the static tables) ---
    if cmd == "find":
        return _classify_find(flags, operands, recursive)
    if cmd == "sed":
        return _classify_sed(flags)
    if cmd == "dd":
        return _classify_dd(operands)
    if cmd == "git":
        return _classify_git(operands, flags)
    if cmd == "rsync":
        return _classify_rsync(flags)

    # --- static classification ---
    if cmd in _READ_COMMANDS:
        eff = Effect("read", 0.0)
    elif cmd in _DESTRUCTIVE_COMMANDS:
        eff = Effect(
            "destructive",
            _weight(cmd, recursive),
            recursive=recursive,
            safer_alternative=_safer(cmd),
        )
    elif cmd in _ADDITIVE_COMMANDS:
        eff = Effect("additive", _weight(cmd, recursive), recursive=recursive)
    else:
        eff = Effect("unknown", DEFAULT_WEIGHT)

    # A truncating redirect overwrites an existing file regardless of the base
    # command (e.g. ``echo x > important.conf``).
    if clobber and eff.intent in ("read", "unknown", "additive"):
        return Effect(
            "destructive",
            max(eff.weight, 0.5),
            note="output redirect (>) overwrites the target file",
            safer_alternative="use >> to append, or write to a new path",
        )
    return eff


def canonicalize(command: str) -> str:
    """Map a PowerShell/cmd verb or alias to its canonical command name.

    Example::

        >>> canonicalize("Remove-Item")
        'rm'
        >>> canonicalize("rm")
        'rm'
    """
    return CANONICAL_COMMAND.get(command.lower(), command)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_recursive(flags: list[str]) -> bool:
    for flag in flags:
        low = flag.lower()
        if low in _RECURSIVE_LONG:
            return True
        if flag.startswith("-") and not flag.startswith("--") and ("r" in flag[1:] or "R" in flag[1:]):
            return True
    return False


def _weight(cmd: str, recursive: bool) -> float:
    w = COMMAND_WEIGHTS.get(cmd, DEFAULT_WEIGHT)
    if cmd in ("chmod", "chown") and recursive:
        w = min(1.0, w * 1.6)
    return w


def _safer(cmd: str) -> str | None:
    if cmd in ("rm", "rmdir", "shred"):
        return "move to a trash dir, or `git rm --cached` for tracked files you want to keep"
    if cmd == "mv":
        return "verify the destination does not already exist (mv overwrites)"
    return None


def _classify_find(flags: list[str], operands: list[str], recursive: bool) -> Effect:
    has_delete = "-delete" in flags
    _DESTRUCTIVE_EXEC = ("rm", "rmdir", "shred", "truncate", "dd", "mv", "chmod")
    has_exec_rm = ("-exec" in flags or "-execdir" in flags) and any(
        o in _DESTRUCTIVE_EXEC for o in operands
    )
    if has_delete or has_exec_rm:
        return Effect(
            "destructive",
            0.8,
            recursive=True,
            note="find deletes/executes against every match",
            safer_alternative="run without -delete/-exec first to review the match list",
        )
    return Effect("read", 0.0)


def _classify_rsync(flags: list[str]) -> Effect:
    """rsync is additive-ish, but ``--delete`` removes destination files."""
    if any(f.startswith("--delete") for f in flags):
        return Effect(
            "destructive",
            0.6,
            note="rsync --delete removes destination files missing from the source",
            safer_alternative="preview with --dry-run --itemize-changes first",
        )
    return Effect("additive", 0.2, note="rsync overwrites matching destination files")


def _classify_sed(flags: list[str]) -> Effect:
    in_place = any(f == "--in-place" or f.lower().startswith("-i") for f in flags)
    if in_place:
        return Effect(
            "destructive",
            0.5,
            in_place=True,
            note="sed -i edits files in place",
            safer_alternative="omit -i to preview, or back up with -i.bak",
        )
    return Effect("read", 0.0)


def _classify_dd(operands: list[str]) -> Effect:
    writes = any(o.startswith("of=") for o in operands)
    return Effect(
        "destructive",
        1.0 if writes else 0.9,
        note="dd writes raw blocks; an output device/file is overwritten",
    )


def _classify_git(operands: list[str], flags: list[str]) -> Effect:
    sub = operands[0] if operands else ""
    rest = operands[1:]
    flagset = {f.lower() for f in flags}

    def destructive(weight: float, note: str) -> Effect:
        return Effect("destructive", weight, note=note, safer_alternative=_GIT_DESTRUCTIVE_SAFER.get(sub))

    if sub == "reset":
        if "--hard" in flagset:
            return destructive(0.8, "git reset --hard discards all uncommitted changes")
        return Effect("unknown", 0.3)
    if sub == "clean":
        if any(f.startswith("-") and "f" in f for f in flagset):
            return destructive(0.8, "git clean -f deletes untracked files (and -d directories)")
        return Effect("read", 0.0)
    if sub in ("checkout", "switch", "restore"):
        if "--" in operands or "--force" in flagset or "-f" in flagset or sub == "restore":
            return destructive(0.6, "discards local changes to the targeted paths")
        return Effect("unknown", 0.3)
    if sub == "push":
        if "--force" in flagset or "-f" in flagset:
            return destructive(0.8, "git push --force can overwrite remote history")
        return Effect("additive", 0.2)
    if sub == "branch" and ("-d" in flagset or "-D" in flagset):
        return destructive(0.5, "deletes a branch ref")
    if sub == "stash" and (rest[:1] in (["drop"], ["clear"]) if rest else False):
        return destructive(0.5, "drops stashed changes")
    if sub in ("rebase", "filter-branch", "filter-repo"):
        return destructive(0.6, "rewrites commit history")
    if sub in ("status", "log", "diff", "show", "fetch", "remote", "ls-files", "branch"):
        return Effect("read", 0.0)
    if sub in ("add", "commit", "stash", "tag", "init"):
        return Effect("additive", 0.1)
    return Effect("unknown", 0.3)
