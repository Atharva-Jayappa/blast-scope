"""Parse shell commands into structured intent for blast radius scoring.

This module is pure functions with no side effects (except the optional
git-tracking check in _check_reversibility). It uses shlex for tokenization
and pathlib for all path handling.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from pathlib import Path
from typing import TypedDict

from blast_scope.command_effects import canonicalize, classify_effect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


class ParsedCommand(TypedDict):
    """Structured representation of a parsed shell command.

    Example::

        {
            "command": "rm",
            "targets": ["/home/user/project/config"],
            "flags": ["-rf"],
            "intent": "destructive",
            "recursive": True,
            "reversible": False,
        }
    """

    command: str
    targets: list[str]
    write_targets: list[str]  # subset of targets actually overwritten/destroyed
    flags: list[str]
    intent: str  # "destructive" | "additive" | "read" | "unknown"
    recursive: bool
    reversible: bool


# ---------------------------------------------------------------------------
# Intent classification tables
# ---------------------------------------------------------------------------

# Flags that indicate recursive operation
_RECURSIVE_LONG_FLAGS: frozenset[str] = frozenset({"--recursive"})
_RECURSIVE_SHORT_CHARS: frozenset[str] = frozenset({"r", "R"})

# Pattern to detect subshell / command substitution
_SUBSHELL_PATTERN: re.Pattern[str] = re.compile(r"\$\(|`")

# Redirect operators
_REDIRECT_PATTERN: re.Pattern[str] = re.compile(r"(\d*)(>>?)")

# Chain operators in priority order (longer matches first)
_CHAIN_OPERATORS: tuple[str, ...] = ("&&", "||", ";", "|")

# Hard cap on how many chain segments are parsed. Each path-bearing segment can
# fork a git subprocess (reversibility check), so an adversarial command with
# tens of thousands of segments would otherwise stall the advisory hook for
# minutes. Real commands chain a handful; 256 is far past any genuine use.
MAX_CHAIN_SEGMENTS: int = 256


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_command(
    raw: str, cwd: Path | None = None, shell: str = "auto"
) -> ParsedCommand:
    """Parse a raw shell command string into structured intent.

    Args:
        raw: The shell command string to parse.
        cwd: Working directory for resolving relative paths.
             Defaults to the current working directory.
        shell: ``"posix"`` (bash/sh), ``"powershell"`` (PowerShell/pwsh/cmd),
            or ``"auto"`` (default → POSIX). PowerShell mode preserves
            backslashes in Windows paths and de-aliases cmdlets
            (``Remove-Item`` → ``rm`` etc.).

    Returns:
        A ``ParsedCommand`` dict describing the command's structure and intent.

    Example::

        >>> parse_command("rm -rf ./config", cwd=Path("/project"))
        {
            "command": "rm",
            "targets": ["/project/config"],
            "flags": ["-rf"],
            "intent": "destructive",
            "recursive": True,
            "reversible": False,
        }
        >>> parse_command("Remove-Item -Recurse build", shell="powershell")["command"]
        'rm'
    """
    if cwd is None:
        cwd = Path.cwd()

    raw = raw.strip()
    if not raw:
        return _empty_result()

    posix = shell not in ("powershell", "pwsh", "cmd")

    # Detect subshell / command substitution — we can't statically resolve these
    has_subshell = bool(_SUBSHELL_PATTERN.search(raw))

    # Tokenize
    tokens = _tokenize(raw, posix=posix)
    if not tokens:
        return _empty_result()

    # Extract redirect targets before main parsing
    tokens, redirect_targets, clobber = _extract_redirects(tokens)
    if not tokens:
        return _empty_result()

    # Skip sudo prefix
    idx = 0
    if tokens[idx] == "sudo":
        idx += 1
        # Skip sudo flags. Some flags take an argument (-u USER, -g GROUP, -C fd).
        _SUDO_ARG_FLAGS = frozenset({"-u", "-g", "-C", "-D", "-R", "-T", "-h", "--user", "--group"})
        while idx < len(tokens) and tokens[idx].startswith("-"):
            flag = tokens[idx]
            idx += 1
            # If this flag takes an argument, skip the next token too
            if flag in _SUDO_ARG_FLAGS and idx < len(tokens):
                idx += 1
        if idx >= len(tokens):
            return _empty_result()

    # Canonicalize PowerShell/cmd verbs (Remove-Item → rm) so the rest of the
    # pipeline is shell-agnostic.
    base_command = canonicalize(tokens[idx])
    remaining = tokens[idx + 1 :]

    # Separate flags from positional arguments
    flags: list[str] = []
    positional: list[str] = []
    hit_double_dash = False

    for token in remaining:
        if hit_double_dash:
            positional.append(token)
        elif token == "--":
            positional.append(token)  # keep as a marker for `git checkout -- path`
            hit_double_dash = True
        elif token.startswith("-"):
            flags.append(token)
        else:
            positional.append(token)

    # Resolve positional args + redirect targets as paths
    targets = _resolve_targets(positional + redirect_targets, cwd)

    # Classify intent / effect (flag- and operand-sensitive)
    effect = classify_effect(
        base_command, flags, positional, has_subshell=has_subshell, clobber=clobber
    )
    intent = effect.intent

    # Targets actually destroyed/overwritten — drives the recoverability floor.
    # Normally every target; but a command made destructive ONLY by a truncating
    # redirect (`sqlite3 db.precious '.dump' > out.sql`) overwrites the *redirect*
    # target — its operands are read, and must not inherit a deletion's floor.
    write_targets = targets
    if clobber and intent == "destructive":
        base = classify_effect(
            base_command, flags, positional, has_subshell=has_subshell, clobber=False
        )
        if base.intent != "destructive":
            write_targets = _resolve_targets(redirect_targets, cwd)

    # Check for recursive flags
    recursive = _check_recursive(flags)

    # Check reversibility for each target
    reversible = all(_check_reversibility(Path(t)) for t in targets) if targets else False

    return ParsedCommand(
        command=base_command,
        targets=targets,
        write_targets=write_targets,
        flags=flags,
        intent=intent,
        recursive=recursive,
        reversible=reversible,
    )


def split_command_chain(raw: str) -> list[str]:
    """Split a chained shell command into individual command strings.

    Splits on ``&&``, ``||``, ``;``, and ``|``. Quoted regions and command
    substitution (``$(...)`` / backticks) are preserved — operators inside
    them do not split.

    Example::

        >>> split_command_chain("cd /tmp && rm -rf .")
        ["cd /tmp", "rm -rf ."]
        >>> split_command_chain("echo 'a; b'; ls")
        ["echo 'a; b'", "ls"]
    """
    raw = raw.strip()
    if not raw:
        return []

    parts: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(raw)

    # Quote / nesting state
    quote: str | None = None  # "'" or '"' or None
    paren_depth = 0           # tracks $( ... )
    in_backtick = False

    while i < n:
        c = raw[i]

        # Backslash escape — copy the next char verbatim
        if c == "\\" and i + 1 < n and quote != "'":
            buf.append(c)
            buf.append(raw[i + 1])
            i += 2
            continue

        # Quote handling
        if quote is None and not in_backtick:
            if c == "'" or c == '"':
                quote = c
                buf.append(c)
                i += 1
                continue
        elif quote == c:
            quote = None
            buf.append(c)
            i += 1
            continue

        if quote is not None:
            buf.append(c)
            i += 1
            continue

        # Backtick command substitution
        if c == "`":
            in_backtick = not in_backtick
            buf.append(c)
            i += 1
            continue

        if in_backtick:
            buf.append(c)
            i += 1
            continue

        # $( ... ) command substitution
        if c == "$" and i + 1 < n and raw[i + 1] == "(":
            paren_depth += 1
            buf.append(c)
            buf.append(raw[i + 1])
            i += 2
            continue
        if paren_depth > 0:
            if c == "(":
                paren_depth += 1
            elif c == ")":
                paren_depth -= 1
            buf.append(c)
            i += 1
            continue

        # Operator detection (only at top level)
        matched_op: str | None = None
        for op in _CHAIN_OPERATORS:
            if raw.startswith(op, i):
                matched_op = op
                break

        if matched_op is not None:
            segment = "".join(buf).strip()
            if segment:
                parts.append(segment)
            buf = []
            i += len(matched_op)
            continue

        buf.append(c)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)

    return parts


def parse_command_chain(
    raw: str, cwd: Path | None = None, shell: str = "auto"
) -> list[ParsedCommand]:
    """Parse a chained shell command into a list of ParsedCommand entries.

    Splits the input on shell chain operators and parses each segment.
    A leading ``cd <path>`` segment updates the working directory used to
    resolve subsequent commands in the chain — so ``cd /tmp && rm -rf .``
    correctly evaluates the ``rm`` against ``/tmp``.

    Args:
        raw: The shell command string (possibly containing ``&&``, ``||``,
             ``;``, or ``|``).
        cwd: Working directory for the first segment. Defaults to the
             current working directory.

    Returns:
        One ``ParsedCommand`` per segment, in chain order. Returns an
        empty list for empty input.

    Example::

        >>> parse_command_chain("cd /tmp && rm -rf .", cwd=Path("/home/user"))
        [{"command": "cd", ...}, {"command": "rm", "targets": ["/tmp"], ...}]
    """
    if cwd is None:
        cwd = Path.cwd()

    segments = split_command_chain(raw)[:MAX_CHAIN_SEGMENTS]
    if not segments:
        return []

    parsed_list: list[ParsedCommand] = []
    current_cwd = cwd

    for segment in segments:
        parsed = parse_command(segment, cwd=current_cwd, shell=shell)
        parsed_list.append(parsed)

        # Track cd to update cwd for following segments
        if parsed["command"] == "cd" and parsed["targets"]:
            new_dir = Path(parsed["targets"][0])
            if new_dir.is_dir():
                current_cwd = new_dir

    return parsed_list


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _empty_result() -> ParsedCommand:
    """Return a ParsedCommand for empty or unparseable input."""
    return ParsedCommand(
        command="",
        targets=[],
        write_targets=[],
        flags=[],
        intent="unknown",
        recursive=False,
        reversible=False,
    )


def _tokenize(raw: str, posix: bool = True) -> list[str]:
    """Tokenize a shell command string using shlex, with fallback.

    With ``posix=False`` (PowerShell/cmd) backslashes are preserved so Windows
    paths like ``.\\build`` survive tokenization; surrounding quotes are then
    stripped manually.

    Example::

        >>> _tokenize("rm -rf ./config")
        ["rm", "-rf", "./config"]
    """
    try:
        toks = shlex.split(raw, posix=posix)
    except ValueError:
        logger.debug("shlex.split failed for %r, falling back to whitespace split", raw)
        return raw.split()
    if not posix:
        # Non-POSIX shlex keeps surrounding quotes inside tokens; strip them.
        toks = [
            t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"') else t
            for t in toks
        ]
    return toks


def _extract_redirects(tokens: list[str]) -> tuple[list[str], list[str], bool]:
    """Extract redirect targets from token list.

    Returns the cleaned token list, a list of redirect target paths, and a
    ``clobber`` flag that is True when a truncating ``>`` redirect (not ``>>``)
    targets a file.

    Example::

        >>> _extract_redirects(["echo", "hello", ">", "output.txt"])
        (["echo", "hello"], ["output.txt"], True)
    """
    cleaned: list[str] = []
    redirect_targets: list[str] = []
    clobber = False
    skip_next = False

    for i, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue

        # Standalone redirect: > file or >> file
        if token in (">", ">>", "2>", "2>>"):
            if i + 1 < len(tokens):
                redirect_targets.append(tokens[i + 1])
                skip_next = True
                if token in (">", "2>"):
                    clobber = True
            continue

        # Redirect attached to target: >file or >>file
        if _REDIRECT_PATTERN.match(token) and len(token) > len(_REDIRECT_PATTERN.match(token).group(0)):  # type: ignore[union-attr]
            m = _REDIRECT_PATTERN.match(token)
            assert m is not None
            target = token[m.end() :]
            redirect_targets.append(target)
            if m.group(2) == ">":
                clobber = True
            continue

        cleaned.append(token)

    return cleaned, redirect_targets, clobber


def _resolve_targets(positional: list[str], cwd: Path) -> list[str]:
    """Resolve positional arguments as absolute paths.

    Filters out arguments that look like non-path values (e.g. regex
    patterns passed to grep). Uses a simple heuristic: if it contains
    path separator characters or starts with './' or '/', treat it as a path.

    Example::

        >>> _resolve_targets(["./config", "*.py"], Path("/project"))
        ["/project/config"]
    """
    targets: list[str] = []
    for arg in positional:
        if _looks_like_path(arg):
            resolved = (cwd / arg).resolve()
            targets.append(str(resolved))
    return targets


def _looks_like_path(arg: str) -> bool:
    """Heuristic: does this argument look like a filesystem path?

    Example::

        >>> _looks_like_path("./config")
        True
        >>> _looks_like_path("import")
        False
    """
    if not arg:
        return False
    # Obvious path indicators
    if arg.startswith(("/", "./", "../", "~")):
        return True
    # Contains path separators
    if "/" in arg or "\\" in arg:
        return True
    # Has a file extension
    if "." in arg and not arg.startswith("-"):
        return True
    # Bare names (could be files in cwd) — treat as paths
    # This is intentionally broad; the graph resolver will filter non-existent paths
    if not arg.startswith("-") and not any(c in arg for c in ("=", "(", ")", "{", "}", "|", "&", ";", "*", "?")):
        return True
    return False


def _classify_intent(command: str, has_subshell: bool) -> str:
    """Classify the bare intent of a command (no flags/operands).

    Thin wrapper over :func:`blast_scope.command_effects.classify_effect`,
    kept for callers that only have the command name.

    Example::

        >>> _classify_intent("rm", False)
        "destructive"
        >>> _classify_intent("cat", False)
        "read"
    """
    return classify_effect(canonicalize(command), [], [], has_subshell=has_subshell).intent


def _check_recursive(flags: list[str]) -> bool:
    """Check whether any flag indicates a recursive operation.

    Example::

        >>> _check_recursive(["-rf"])
        True
        >>> _check_recursive(["--force"])
        False
    """
    for flag in flags:
        if flag in _RECURSIVE_LONG_FLAGS:
            return True
        # Short flags: check each character after the leading dash
        if flag.startswith("-") and not flag.startswith("--"):
            chars = flag[1:]
            if _RECURSIVE_SHORT_CHARS & set(chars):
                return True
    return False


def _check_reversibility(path: Path) -> bool:
    """Check if a path is within a git repository and tracked.

    This is the only function in the module with side effects (subprocess call).
    It is factored out so tests can mock it.

    Example::

        >>> _check_reversibility(Path("/home/user/git-project/file.py"))
        True  # if the file is git-tracked
    """
    try:
        # Check if the path (or its parent) is inside a git repo
        check_dir = path if path.is_dir() else path.parent
        if not check_dir.exists():
            return False

        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(check_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False

        # Check if the specific file is tracked
        if path.is_file():
            track_result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(path.name)],
                cwd=str(path.parent),
                capture_output=True,
                text=True,
                timeout=5,
            )
            return track_result.returncode == 0

        # For directories, consider them reversible if they're inside a git repo
        return True

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
