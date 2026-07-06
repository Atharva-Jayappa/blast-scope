"""Resolve a shell command the way the shell would — before scoring it.

The shell is a two-stage machine: stage one rewrites the text (brace, tilde,
parameter, and glob expansion), stage two executes the result. Scoring the raw
string means scoring the *input* of stage one, while the damage is done by its
*output* — ``rm -rf $BUILD_DIR/`` with an unset ``BUILD_DIR`` executes as
``rm -rf /``. This module runs stage one safely: pure text transformation plus
read-only filesystem lookups (glob matching, symlink inspection). It never
executes any part of the command.

Blind spots are reported, not hidden. Expansions that cannot be resolved
statically (command substitution, complex parameter operators) and dangerous
residues (an unset variable collapsing a path toward ``/`` or ``$HOME``)
surface as :class:`Hazard` entries the scorer turns into floors — "couldn't
see inside" must never score lower than "saw inside and it was fine".

Expansion order mirrors bash: brace → tilde → parameter → word splitting →
pathname (glob). Command substitution spans (``$(...)``, backticks) are
preserved untouched; alias expansion is deliberately out of scope (aliases are
off in the non-interactive shells agents use).
"""

from __future__ import annotations

import glob as globlib
import itertools
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Note:
    """A record of one expansion the resolver performed (or declined).

    Example::

        Note(kind="glob", detail="glob 'src/*.py' matched 3 file(s)")
    """

    kind: str  # "brace" | "tilde" | "env" | "glob" | "glob_nomatch" |
    #            "complex_param" | "substitution" | "symlink" | "empty_expansion"
    detail: str


@dataclass(frozen=True)
class Hazard:
    """A dangerous residue of expansion that warrants a score floor.

    Example::

        Hazard(kind="unset_var_root", floor=0.85,
               detail="$BUILD_DIR is unset — '$BUILD_DIR/' resolves to '/'")
    """

    kind: str  # "unset_var_root" | "unset_var_prefix"
    detail: str
    floor: float


@dataclass(frozen=True)
class Resolution:
    """The resolved form of one chain segment, with an audit trail.

    ``resolved`` is the segment as the shell would execute it (best static
    effort); it equals ``original`` verbatim when nothing needed expanding.

    Example::

        >>> r = resolve_segment("rm -rf ~/build", Path("/proj"), {"HOME": "/home/u"})
        >>> r.resolved
        'rm -rf /home/u/build'
    """

    original: str
    resolved: str
    notes: tuple[Note, ...]
    hazards: tuple[Hazard, ...]

    @property
    def changed(self) -> bool:
        """True when resolution rewrote the segment text."""
        return self.resolved != self.original


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Glob matches written back into the resolved text. Each becomes a parse
# target, and targets cost git subprocess checks downstream — keep it small.
_MAX_GLOB_INLINE = 20
# Filesystem matches examined per glob (for the match count in the note).
_MAX_GLOB_SCAN = 1000
# Words one brace expansion may produce.
_MAX_BRACE_WORDS = 64
# Notes kept per segment (deduplicated first).
_MAX_NOTES = 8

_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Quote-context markers, one per scanned character.
_Q_NONE = None  # bare: expandable, globbable, splittable at expansion
_Q_SINGLE = "'"  # single-quoted: fully literal
_Q_DOUBLE = '"'  # double-quoted: $ expands; no glob, no word split
_Q_ESCAPED = "\\"  # backslash-escaped or produced as a literal glob match
_Q_PROTECTED = "$"  # inside $(...) / `...` — untouched until phase 3
_Q_EXPANDED = "E"  # produced by an unquoted expansion: globbable + splittable

_GLOBBABLE = (_Q_NONE, _Q_EXPANDED)
_GLOB_CHARS = frozenset("*?[")


@dataclass
class _Word:
    """One shell word as (char, quote-context) pairs."""

    chars: list[tuple[str, str | None]] = field(default_factory=list)
    explicit: bool = False  # quotes appeared, so an empty word still exists

    def text(self) -> str:
        return "".join(c for c, _ in self.chars)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_segment(
    segment: str,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> Resolution:
    """Statically expand one chain segment the way the shell would.

    Args:
        segment: A single command (no ``&&`` / ``;`` / ``|`` — split first).
        cwd: Directory relative globs and symlink checks resolve against.
        env: Environment for ``$VAR`` expansion. ``None`` means the current
            process environment (the hook shares the agent's env); pass an
            explicit mapping — possibly empty — for deterministic callers.
        project_root: Optional project root, reserved for scoping symlink
            evidence.

    Returns:
        A :class:`Resolution`; ``resolved`` is the original string verbatim
        when no expansion applied.

    Example::

        >>> resolve_segment("rm -rf $TMP/cache", Path("/p"), {"TMP": "/tmp"}).resolved
        'rm -rf /tmp/cache'
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    notes: list[Note] = []
    hazards: list[Hazard] = []
    changed = False

    words = _scan(segment)
    # Peel wrappers/assignments (`sudo rm`, `env rm`, `FOO=1 rm`) so the
    # unresolved-substitution hazard on a genuine deletion still fires — the
    # verb behind a wrapper must not hide it.
    from blast_scope.command_parser import real_command_verb

    destructive_verb = real_command_verb(segment) in _DESTRUCTIVE_VERBS

    home = _home_dir(env_map)
    out_words: list[_Word] = []
    for word in words:
        for braced in _expand_braces(word, notes):
            tilded = _expand_tilde(braced, home, notes)
            expanded, events = _expand_params(tilded, env_map, notes)
            substituted = _resolve_substitutions(
                expanded, cwd, notes, hazards, destructive_verb
            )
            if substituted.text() != word.text():
                changed = True
            _detect_unset_hazards(word, expanded, events, home, hazards, notes)
            for piece in _word_split(substituted):
                globbed = _expand_glob(piece, cwd, notes)
                if len(globbed) != 1 or globbed[0].text() != piece.text():
                    changed = True
                out_words.extend(globbed)

    if any(q == _Q_PROTECTED for w in out_words for _, q in w.chars):
        notes.append(
            Note("substitution", "command substitution left unresolved (never executed)")
        )

    _note_symlinks(out_words, cwd, notes)

    resolved = segment
    if changed:
        rendered = " ".join(_render(w) for w in out_words if w.text() or w.explicit)
        resolved = rendered or segment

    return Resolution(
        original=segment,
        resolved=resolved,
        notes=tuple(_dedup(notes)[:_MAX_NOTES]),
        hazards=tuple(hazards),
    )


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _scan(segment: str) -> list[_Word]:
    """Split a segment into words, tagging every char with its quote context.

    Command substitution spans (``$(...)`` with nesting, backticks) are tagged
    ``_Q_PROTECTED`` so later passes leave them byte-for-byte intact.

    Example::

        >>> [w.text() for w in _scan("rm 'a b' c")]
        ['rm', 'a b', 'c']
    """
    words: list[_Word] = []
    cur = _Word()
    quote: str | None = None
    i, n = 0, len(segment)

    def flush() -> None:
        nonlocal cur
        if cur.chars or cur.explicit:
            words.append(cur)
        cur = _Word()

    while i < n:
        c = segment[i]

        if quote is None:
            if c in " \t":
                flush()
                i += 1
                continue
            if c == "\\" and i + 1 < n:
                cur.chars.append((segment[i + 1], _Q_ESCAPED))
                i += 2
                continue
            if c in ("'", '"'):
                quote = c
                cur.explicit = True
                i += 1
                continue
            if c == "`":
                i = _copy_protected(segment, i, cur, until="`")
                continue
            if c == "$" and i + 1 < n and segment[i + 1] == "(":
                i = _copy_protected(segment, i, cur, until=")")
                continue
            cur.chars.append((c, _Q_NONE))
            i += 1
            continue

        # Inside quotes
        if c == quote:
            quote = None
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n and segment[i + 1] in '"$`\\':
                cur.chars.append((segment[i + 1], _Q_ESCAPED))
                i += 2
                continue
            if c == "`":
                i = _copy_protected(segment, i, cur, until="`")
                continue
            if c == "$" and i + 1 < n and segment[i + 1] == "(":
                i = _copy_protected(segment, i, cur, until=")")
                continue
        cur.chars.append((c, quote))
        i += 1

    flush()
    return words


def _copy_protected(segment: str, start: int, word: _Word, until: str) -> int:
    """Copy a substitution span verbatim with the protected tag; return new index."""
    i = start
    n = len(segment)
    if until == "`":
        word.chars.append((segment[i], _Q_PROTECTED))
        i += 1
        while i < n:
            word.chars.append((segment[i], _Q_PROTECTED))
            if segment[i] == "`":
                return i + 1
            i += 1
        return i
    # $( ... ) with nesting
    depth = 0
    while i < n:
        c = segment[i]
        word.chars.append((c, _Q_PROTECTED))
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


# ---------------------------------------------------------------------------
# Expansion passes (bash order: brace → tilde → parameter → split → glob)
# ---------------------------------------------------------------------------


def _expand_braces(word: _Word, notes: list[Note]) -> list[_Word]:
    """Expand the first unquoted ``{a,b}`` / ``{1..5}`` group, recursively.

    Groups with no comma and no range stay literal, as in bash (``{x}`` is
    just ``{x}``). Output is capped at ``_MAX_BRACE_WORDS``.

    Example::

        >>> [w.text() for w in _expand_braces(_scan("a{b,c}")[0], [])]
        ['ab', 'ac']
    """
    span = _find_brace_group(word)
    if span is None:
        return [word]
    start, end = span
    inner = word.chars[start + 1 : end]
    alternatives = _brace_alternatives(inner)
    if alternatives is None:
        return [word]

    prefix, suffix = word.chars[:start], word.chars[end + 1 :]
    out: list[_Word] = []
    for alt in alternatives:
        candidate = _Word(chars=prefix + alt + list(suffix), explicit=word.explicit)
        out.extend(_expand_braces(candidate, notes))
        if len(out) >= _MAX_BRACE_WORDS:
            out = out[:_MAX_BRACE_WORDS]
            break
    if out and len(out) > 1 or (out and out[0].text() != word.text()):
        notes.append(Note("brace", f"brace expansion produced {len(out)} word(s)"))
    return out or [word]


def _find_brace_group(word: _Word) -> tuple[int, int] | None:
    """Locate the first complete unquoted ``{...}`` span; None when absent."""
    depth = 0
    start = -1
    for i, (c, q) in enumerate(word.chars):
        if q is not _Q_NONE:
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                return (start, i)
    return None


def _brace_alternatives(
    inner: list[tuple[str, str | None]],
) -> list[list[tuple[str, str | None]]] | None:
    """Split a brace body into alternatives; None when it isn't expandable."""
    text = "".join(c for c, _ in inner)
    range_match = re.fullmatch(r"(-?\d+)\.\.(-?\d+)", text)
    if range_match and all(q is _Q_NONE for _, q in inner):
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        step = 1 if hi >= lo else -1
        values = list(range(lo, hi + step, step))[:_MAX_BRACE_WORDS]
        return [[(ch, _Q_NONE) for ch in str(v)] for v in values]

    # Comma alternatives at top nesting level
    alternatives: list[list[tuple[str, str | None]]] = []
    current: list[tuple[str, str | None]] = []
    depth = 0
    found_comma = False
    for c, q in inner:
        if q is _Q_NONE:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == "," and depth == 0:
                found_comma = True
                alternatives.append(current)
                current = []
                continue
        current.append((c, q))
    alternatives.append(current)
    return alternatives if found_comma else None


def _expand_tilde(word: _Word, home: str, notes: list[Note]) -> _Word:
    """Expand a leading unquoted ``~`` (``~/...`` or bare ``~``).

    ``~user`` is left alone — resolving other users' homes is host-specific
    and rarely what an agent command means.

    Example::

        >>> _expand_tilde(_scan("~/x")[0], "/home/u", []).text()
        '/home/u/x'
    """
    if not word.chars or word.chars[0] != ("~", _Q_NONE):
        return word
    if len(word.chars) > 1 and word.chars[1][0] != "/":
        return word
    notes.append(Note("tilde", f"~ expanded to {home}"))
    # Tilde results are not subject to word splitting or globbing in bash;
    # the double-quote tag models that (a Windows home may contain spaces).
    replacement = [(ch, _Q_DOUBLE) for ch in home]
    return _Word(chars=replacement + word.chars[1:], explicit=word.explicit)


@dataclass(frozen=True)
class _ParamEvent:
    """One ``$VAR`` expansion inside a word."""

    name: str
    value: str | None  # None = unset
    leading: bool  # the expansion started the word
    unquoted: bool


def _expand_params(
    word: _Word, env: Mapping[str, str], notes: list[Note]
) -> tuple[_Word, list[_ParamEvent]]:
    """Expand ``$VAR`` / ``${VAR}`` in unquoted and double-quoted context.

    Unset variables expand to the empty string (bash default) — the caller
    inspects the returned events to decide whether that was dangerous.
    Complex operators (``${VAR:-x}`` etc.) are left literal and noted.

    Example::

        >>> w, ev = _expand_params(_scan("$A/b")[0], {"A": "/x"}, [])
        >>> w.text()
        '/x/b'
    """
    out: list[tuple[str, str | None]] = []
    events: list[_ParamEvent] = []
    chars = word.chars
    i = 0
    while i < len(chars):
        c, q = chars[i]
        if c != "$" or q not in (_Q_NONE, _Q_DOUBLE):
            out.append((c, q))
            i += 1
            continue

        rest = "".join(ch for ch, cq in chars[i + 1 :] if cq == q)
        # Only a contiguous same-context run is a candidate name
        run_len = 0
        for ch, cq in chars[i + 1 :]:
            if cq != q:
                break
            run_len += 1
        run = "".join(ch for ch, _ in chars[i + 1 : i + 1 + run_len])

        name: str | None = None
        consumed = 0
        if run.startswith("{"):
            close = run.find("}")
            if close != -1:
                body = run[1:close]
                if _VAR_NAME.fullmatch(body):
                    name = body
                    consumed = close + 1
                else:
                    notes.append(
                        Note("complex_param", f"${{{body}}} left unresolved (operator syntax)")
                    )
                    out.append((c, q))
                    i += 1
                    continue
        else:
            m = _VAR_NAME.match(run)
            if m:
                name = m.group(0)
                consumed = m.end()

        if name is None:
            out.append((c, q))
            i += 1
            continue

        value = env.get(name)
        events.append(
            _ParamEvent(name=name, value=value, leading=i == 0, unquoted=q is _Q_NONE)
        )
        mark = _Q_DOUBLE if q == _Q_DOUBLE else _Q_EXPANDED
        out.extend((ch, mark) for ch in (value or ""))
        if value is not None:
            notes.append(Note("env", f"${name} → {value!r}"))
        i += 1 + consumed

    return _Word(chars=out, explicit=word.explicit), events


def _word_split(word: _Word) -> list[_Word]:
    """Split a word at whitespace produced by unquoted expansion.

    Bash splits only *expanded* text — literal characters never split here.

    Example::

        >>> w, _ = _expand_params(_scan("$F")[0], {"F": "a b"}, [])
        >>> [p.text() for p in _word_split(w)]
        ['a', 'b']
    """
    if not any(q == _Q_EXPANDED and c.isspace() for c, q in word.chars):
        return [word]
    pieces: list[_Word] = []
    cur = _Word(explicit=word.explicit)
    for c, q in word.chars:
        if q == _Q_EXPANDED and c.isspace():
            if cur.chars:
                pieces.append(cur)
            cur = _Word()
            continue
        cur.chars.append((c, q))
    if cur.chars:
        pieces.append(cur)
    return pieces or [word]


def _expand_glob(word: _Word, cwd: Path, notes: list[Note]) -> list[_Word]:
    """Expand unquoted glob characters against the real filesystem.

    Bash semantics: matches replace the pattern; no match keeps the pattern
    literally (nullglob off) — noted so the scorer knows the target set was
    empty at analysis time. Matches are capped at ``_MAX_GLOB_INLINE`` words.

    Example::

        >>> _expand_glob(_scan("*.nomatch")[0], Path("."), [])[0].text()
        '*.nomatch'
    """
    if not any(q in _GLOBBABLE and c in _GLOB_CHARS for c, q in word.chars):
        return [word]

    pattern = "".join(
        c if q in _GLOBBABLE else globlib.escape(c) for c, q in word.chars
    )
    try:
        if PurePosixPath(pattern).is_absolute() or (len(pattern) > 1 and pattern[1] == ":"):
            it = globlib.iglob(pattern, recursive=True)
        else:
            it = globlib.iglob(pattern, root_dir=str(cwd), recursive=True)
        matches = sorted(itertools.islice(it, _MAX_GLOB_SCAN))
    except (OSError, ValueError):
        matches = []

    if not matches:
        notes.append(
            Note("glob_nomatch", f"glob '{word.text()}' matched nothing — passed literally")
        )
        return [word]

    shown = matches[:_MAX_GLOB_INLINE]
    detail = f"glob '{word.text()}' matched {len(matches)} file(s)"
    if len(matches) > len(shown):
        detail += f" (scoring first {len(shown)})"
    if len(matches) == _MAX_GLOB_SCAN:
        detail += " [scan capped]"
    notes.append(Note("glob", detail))
    return [
        _Word(chars=[(ch, _Q_ESCAPED) for ch in m], explicit=False) for m in shown
    ]


# ---------------------------------------------------------------------------
# Command substitution — resolve $(...) via strictly read-only execution
# ---------------------------------------------------------------------------
#
# `rm -rf $(find . -name '*.log')` names its targets through the output of an
# inner command. That target set is statically undecidable — but the inner
# command here is a pure read, and running *just it* (never the outer command)
# tells us exactly what would be deleted. This mirrors the project's existing
# read-only-probe philosophy (`git status`, `docker volume inspect`, SQLite
# `mode=ro`): observe state through side-effect-free reads, with a timeout,
# degrading to an uncertainty signal when the probe can't run.
#
# The allowlist is deny-by-default and checks the verb AND its flags; any
# shell metacharacter or nested substitution inside the span disqualifies it.

# Outer verbs whose substitution-driven target set is worth a hazard floor
# when it cannot be resolved.
_DESTRUCTIVE_VERBS = frozenset(
    {"rm", "shred", "rmdir", "unlink", "mv", "dd", "truncate"}
)

# Substitution verbs that never read file *content* — they print their string
# args, list names, or report the environment. File-content readers
# (`cat`/`head`/`tail`/`wc`) and path revealers (`realpath`/`readlink`) are
# DELIBERATELY excluded: the use case is "expand a target list" (via
# `ls`/`find`/`git ls-files`), never "read a file". Allowing `cat` turned this
# into a read-any-file exfiltration channel — `rm -rf $(cat ~/.aws/credentials)`
# would execute `cat` during *analysis* and surface the contents.
_SAFE_SUBSTITUTION_VERBS = frozenset(
    {"ls", "echo", "printf", "basename", "dirname", "date", "pwd", "which", "whoami"}
)
# Verbs that touch the filesystem by path — their path arguments must stay
# inside the working tree, so a substitution can't enumerate `/etc` or `~/.ssh`.
_FS_READING_VERBS = frozenset({"ls", "find", "git"})
_FORBIDDEN_FIND_FLAGS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprintf"}
)
_SAFE_GIT_SUBCOMMANDS = frozenset({"ls-files", "rev-parse", "describe"})
# Reject shell metacharacters AND control characters (a newline would let
# `$(ls\ncat /etc/passwd)` slip a second argv past this filter).
_INNER_METACHARS = re.compile(r"[|;&<>`\n\r]|\$\(")
_SUBSTITUTION_TIMEOUT_S = 2
_MAX_SUBSTITUTION_BYTES = 4096
_UNRESOLVED_SUBSTITUTION_FLOOR = 0.35


def _resolve_substitutions(
    word: _Word,
    cwd: Path,
    notes: list[Note],
    hazards: list[Hazard],
    destructive_verb: bool,
) -> _Word:
    """Replace allowlisted ``$(...)`` spans with their read-only output.

    Non-allowlisted spans stay untouched; when the outer command is
    destructive, that blindness becomes an ``unresolved_substitution`` hazard
    — an invisible target list on a deletion must not score low by default.
    """
    if not any(q == _Q_PROTECTED for _, q in word.chars):
        return word

    out: list[tuple[str, str | None]] = []
    chars = word.chars
    i = 0
    while i < len(chars):
        c, q = chars[i]
        if q != _Q_PROTECTED:
            out.append((c, q))
            i += 1
            continue
        j = i
        run: list[str] = []
        while j < len(chars) and chars[j][1] == _Q_PROTECTED:
            run.append(chars[j][0])
            j += 1
        span = "".join(run)
        inner = span[2:-1] if span.startswith("$(") else span.strip("`")
        output = _execute_readonly(inner.strip(), cwd)
        if output is None:
            if destructive_verb:
                hazards.append(
                    Hazard(
                        kind="unresolved_substitution",
                        floor=_UNRESOLVED_SUBSTITUTION_FLOOR,
                        detail=(
                            f"$({inner.strip()}) not resolvable read-only — "
                            "a destructive command's target list is invisible"
                        ),
                    )
                )
            out.extend((ch, _Q_PROTECTED) for ch in span)
        else:
            notes.append(
                Note(
                    "substitution_resolved",
                    f"$({inner.strip()}) resolved via read-only run",
                )
            )
            out.extend((ch, _Q_EXPANDED) for ch in output)
        i = j
    return _Word(chars=out, explicit=word.explicit)


def _execute_readonly(inner: str, cwd: Path) -> str | None:
    """Run an inner substitution command iff it is provably read-only.

    Returns its stdout (size-capped), or ``None`` when the command is not
    allowlisted, contains metacharacters, reaches outside the working tree, or
    fails/times out. The analyzed OUTER command is never executed here, and no
    command that reads file *content* is on the allowlist — so this can expand
    a target list but never disclose a file's bytes.
    """
    if not inner or _INNER_METACHARS.search(inner):
        return None
    try:
        tokens = shlex.split(inner)
    except ValueError:
        return None
    if not tokens:
        return None
    verb = Path(tokens[0]).name
    if verb == "find":
        if any(t in _FORBIDDEN_FIND_FLAGS for t in tokens):
            return None
    elif verb == "git":
        if len(tokens) < 2 or tokens[1] not in _SAFE_GIT_SUBCOMMANDS:
            return None
    elif verb not in _SAFE_SUBSTITUTION_VERBS:
        return None

    # Filesystem-reading verbs must stay inside the working tree — otherwise a
    # substitution enumerates arbitrary paths (`ls /etc`, `find / -name id_rsa`).
    if verb in _FS_READING_VERBS:
        try:
            cwd_res = cwd.resolve()
        except OSError:
            return None
        if any(_path_escapes_cwd(t, cwd_res) for t in tokens[1:]):
            return None

    try:
        proc = subprocess.run(
            tokens,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_SUBSTITUTION_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()[:_MAX_SUBSTITUTION_BYTES]


def _path_escapes_cwd(token: str, cwd_res: Path) -> bool:
    """True if a non-flag token resolves to a path outside the working tree.

    Flags (leading ``-``), git refs, and glob patterns that resolve within cwd
    are fine; an absolute path or ``../`` escape is not.
    """
    if token.startswith("-") or token in ("HEAD", "@"):
        return False
    p = Path(token)
    try:
        resolved = p.resolve() if (p.is_absolute() or token.startswith("/")) else (cwd_res / token).resolve()
    except OSError:
        return True
    try:
        resolved.relative_to(cwd_res)
        return False
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Hazards & evidence
# ---------------------------------------------------------------------------


def _detect_unset_hazards(
    original: _Word,
    expanded: _Word,
    events: list[_ParamEvent],
    home: str,
    hazards: list[Hazard],
    notes: list[Note],
) -> None:
    """Flag words where an unset/empty variable left a dangerous residue.

    The flagship case: ``rm -rf $BUILD_DIR/`` with ``BUILD_DIR`` unset
    executes as ``rm -rf /``. The residue *looks* harmless ("/" exists,
    nothing special) — the danger is that it isn't the path anyone intended.
    """
    empty_events = [e for e in events if not e.value and e.unquoted]
    if not empty_events:
        return

    orig_text = original.text()
    final = expanded.text()

    if final == "" and orig_text:
        notes.append(
            Note("empty_expansion", f"'{orig_text}' expanded to nothing (argument vanishes)")
        )
        return

    leading = any(e.leading for e in empty_events)
    names = ", ".join(f"${e.name}" for e in empty_events)
    norm_final = final.rstrip("/") or "/"
    norm_home = home.rstrip("/") or "/"

    if norm_final == "/" or norm_final == norm_home:
        what = "the filesystem root" if norm_final == "/" else "the home directory"
        hazards.append(
            Hazard(
                kind="unset_var_root",
                floor=0.85,
                detail=(
                    f"{names} is unset/empty — '{orig_text}' resolves to "
                    f"'{final}' ({what})"
                ),
            )
        )
    elif leading and final.startswith("/"):
        hazards.append(
            Hazard(
                kind="unset_var_prefix",
                floor=0.6,
                detail=(
                    f"{names} is unset/empty — '{orig_text}' silently re-roots to "
                    f"'{final}' (absolute path not anchored where intended)"
                ),
            )
        )


def _note_symlinks(words: list[_Word], cwd: Path, notes: list[Note]) -> None:
    """Record when a path-looking word is a symlink to somewhere else.

    The path the command names and the inode it touches are different things;
    downstream scoring already follows the link (``.resolve()``) — this makes
    the traversal visible in the evidence.
    """
    for word in words:
        text = word.text()
        if not text or text.startswith("-"):
            continue
        if "/" not in text and not text.startswith((".", "~")):
            continue
        try:
            p = Path(cwd, text) if not Path(text).is_absolute() else Path(text)
            if p.is_symlink():
                notes.append(
                    Note("symlink", f"'{text}' is a symlink → {p.resolve()}")
                )
        except OSError:
            continue


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(word: _Word) -> str:
    """Render a word back to shell text, quoting only when required."""
    text = word.text()
    if any(q == _Q_PROTECTED for _, q in word.chars):
        return text  # keep $(...)/backtick syntax detectable downstream
    if text == "":
        return "''"
    # Backslashes must be quoted too: the resolved text is re-tokenized in
    # POSIX mode, which would otherwise eat them out of Windows paths.
    if re.search(r"[\s'\"\\]", text):
        return shlex.quote(text)
    return text


def _home_dir(env: Mapping[str, str]) -> str:
    """Best-effort home directory from the given env, else the process's."""
    home = env.get("HOME") or env.get("USERPROFILE")
    if home:
        return home
    try:
        return str(Path.home())
    except (RuntimeError, OSError):
        return "~"


def _dedup(notes: list[Note]) -> list[Note]:
    """Drop duplicate notes, preserving order."""
    seen: set[tuple[str, str]] = set()
    out: list[Note] = []
    for note in notes:
        key = (note.kind, note.detail)
        if key not in seen:
            seen.add(key)
            out.append(note)
    return out


# ---------------------------------------------------------------------------
# Script transparency — resolve what a wrapper command actually runs
# ---------------------------------------------------------------------------
#
# `npm run clean` contains nothing to score; the danger lives in package.json.
# This pass rewrites indirection — shell -c payloads, npm/yarn/pnpm scripts,
# script files, Makefile targets — into the commands they execute, so the
# ordinary pipeline scores those. Wrappers that can't be opened statically
# (python -c, curl | sh) emit an `opaque_wrapper` hazard instead: not seeing
# inside must never score lower than seeing inside and finding it harmless.
#
# Nothing here executes anything. `make -n` is explicitly forbidden: GNU make
# runs `$(shell ...)` during Makefile *parsing*, dry-run or not.

_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_PACKAGE_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_OPAQUE_EVAL_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "perl": frozenset({"-e", "-E"}),
    "ruby": frozenset({"-e"}),
    "php": frozenset({"-r"}),
}
_OPAQUE_FLOOR = 0.35
# An opaque one-liner that names a destructive/exec token floors HIGH (advises)
# rather than medium (silent).
_INLINE_DANGER_FLOOR = 0.5
_PIPE_TO_SHELL_FLOOR = 0.5

# State-changing capabilities inside interpreter one-liners. A `python -c`
# naming none of these is treated as a read (evidence note only).
_INLINE_DANGER = re.compile(
    r"rmtree|\bunlink\b|\bremove\b|\brmdir\b|os\.system|subprocess|popen"
    r"|child_process|execSync|fs\.rm|\bshutil\b|truncate|DROP\s|DELETE\s"
    r"|\bkill\b|chmod|chown|writeText|unlinkSync|rimraf"
    # DB writes and obfuscation markers: `.execute(` is write-capable, and
    # base64/eval/exec inside a one-liner is hiding something by definition.
    r"|\bexecute\s*\(|b64decode|base64|\beval\s*\(|\bexec\s*\(",
    re.IGNORECASE,
)
_MAX_INDIRECTION_DEPTH = 2
_MAX_SCRIPT_BYTES = 65536
_MAX_SCRIPT_STATEMENTS = 200

_PIPE_TO_SHELL = re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b(?!\s*-c)")


@dataclass(frozen=True)
class Indirection:
    """Result of rewriting wrapper commands into what they actually run.

    Example::

        >>> ind = expand_indirection("npm run clean", Path("/proj"))
        >>> ind.command   # doctest: +SKIP
        'rm -rf dist'
    """

    original: str
    command: str
    notes: tuple[Note, ...]
    hazards: tuple[Hazard, ...]

    @property
    def changed(self) -> bool:
        return self.command != self.original


def expand_indirection(command: str, cwd: Path) -> Indirection:
    """Rewrite script indirection into the commands it would execute.

    Applies up to ``_MAX_INDIRECTION_DEPTH`` rewrite rounds (a script may
    call another script). Purely static: reads package.json / Makefile /
    script files, never runs anything.

    Example::

        >>> expand_indirection("sh -c 'rm -rf /'", Path(".")).command
        'rm -rf /'
    """
    from blast_scope.command_parser import split_command_chain

    notes: list[Note] = []
    hazards: list[Hazard] = []

    if _PIPE_TO_SHELL.search(command):
        hazards.append(
            Hazard(
                kind="pipe_to_shell",
                floor=_PIPE_TO_SHELL_FLOOR,
                detail="pipes content into a shell — executes whatever the upstream produces",
            )
        )

    current = command
    seen: set[str] = set()
    for _ in range(_MAX_INDIRECTION_DEPTH):
        if current in seen:
            break
        seen.add(current)
        segments = split_command_chain(current)
        rewritten: list[str] = []
        changed = False
        for seg in segments:
            replacement = _expand_one_wrapper(seg, cwd, notes, hazards)
            if replacement is not None and replacement.strip():
                rewritten.append(replacement)
                changed = True
            else:
                rewritten.append(seg)
        if not changed:
            break
        current = " ; ".join(rewritten)

    return Indirection(
        original=command,
        command=current,
        notes=tuple(_dedup(notes)[:_MAX_NOTES]),
        hazards=tuple(hazards),
    )


def _expand_one_wrapper(
    segment: str, cwd: Path, notes: list[Note], hazards: list[Hazard]
) -> str | None:
    """Expand one segment's wrapper, or return None when it isn't one."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    # Skip leading VAR=value assignments and sudo
    i = 0
    while i < len(tokens) and ("=" in tokens[i] and not tokens[i].startswith("-")):
        i += 1
    if i < len(tokens) and tokens[i] == "sudo":
        i += 1
    if i >= len(tokens):
        return None
    verb = tokens[i]
    args = tokens[i + 1 :]
    base = Path(verb).name  # ./scripts/x.sh → x.sh ; /usr/bin/bash → bash

    if base in _SHELL_INTERPRETERS:
        return _expand_shell_wrapper(verb, args, cwd, notes, hazards)
    if verb in (".", "source") and args:
        return _read_script(Path(cwd, args[0]), args[0], notes, hazards)
    if base in _PACKAGE_RUNNERS:
        return _expand_package_script(base, args, cwd, notes)
    if base == "make":
        return _expand_make_target(args, cwd, notes, hazards)
    if base in _OPAQUE_EVAL_FLAGS and any(a in _OPAQUE_EVAL_FLAGS[base] for a in args):
        # Agents run benign one-liners (`python -c "import x"`) constantly —
        # an unconditional floor was the dominant FP source on SABER. Triage
        # the payload: floor only when it names a state-changing capability.
        payload = " ".join(a for a in args if not a.startswith("-"))
        if _INLINE_DANGER.search(payload):
            # An interpreter one-liner that explicitly names a destructive/exec
            # capability floors at HIGH so the hook actually surfaces it — a
            # medium floor stays silent, letting `python -c "shutil.rmtree(...)"`
            # slip past unremarked. (Obfuscation that hides the token from this
            # regex is unanalyzable statically — that is what speculation is for.)
            hazards.append(
                Hazard(
                    kind="opaque_wrapper",
                    floor=_INLINE_DANGER_FLOOR,
                    detail=(
                        f"{base} inline code names destructive/exec capability "
                        "— effects not statically visible"
                    ),
                )
            )
        else:
            notes.append(
                Note("wrapper", f"{base} inline code — no destructive tokens found")
            )
        return None
    # Direct script invocation: ./cleanup.sh or path/to/x.sh
    if ("/" in verb or verb.startswith(".")) and verb.endswith(".sh"):
        return _read_script(Path(cwd, verb), verb, notes, hazards)
    return None


def _expand_shell_wrapper(
    verb: str, args: list[str], cwd: Path, notes: list[Note], hazards: list[Hazard]
) -> str | None:
    """Expand ``sh -c '...'`` payloads and ``bash foo.sh`` script files."""
    payload_next = False
    for j, arg in enumerate(args):
        if payload_next or not arg.startswith("-"):
            if payload_next:
                notes.append(Note("wrapper", f"{Path(verb).name} -c payload expanded"))
                return arg
            # First positional: a script file
            return _read_script(Path(cwd, arg), arg, notes, hazards)
        if arg == "--":
            payload_next = False
            continue
        if "c" in arg.lstrip("-"):
            payload_next = True
    return None


def _read_script(
    path: Path, shown: str, notes: list[Note], hazards: list[Hazard]
) -> str | None:
    """Read a shell script and flatten its statements into one chain."""
    if not path.exists():
        # A missing script means the wrapper fails at runtime — nothing runs.
        # Flooring this was a SABER false-positive source (benign commands
        # referencing scripts the workspace doesn't ship).
        notes.append(
            Note("wrapper", f"script '{shown}' not found — command would fail, not execute")
        )
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_SCRIPT_BYTES]
    except OSError:
        hazards.append(
            Hazard(
                kind="opaque_wrapper",
                floor=_OPAQUE_FLOOR,
                detail=f"script '{shown}' exists but could not be read — effects unknown",
            )
        )
        return None
    statements: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        statements.append(stripped)
        if len(statements) >= _MAX_SCRIPT_STATEMENTS:
            break
    if not statements:
        return None
    notes.append(Note("wrapper", f"script '{shown}' expanded ({len(statements)} statement(s))"))
    return " ; ".join(statements)


def _expand_package_script(
    runner: str, args: list[str], cwd: Path, notes: list[Note]
) -> str | None:
    """Expand ``npm run X`` (and yarn/pnpm/bun) via package.json scripts.

    npm runs ``preX``, then ``X``, then ``postX`` — all three are chained so
    a destructive pre-hook can't hide behind an innocent script name.
    """
    import json

    rest = list(args)
    if rest and rest[0] in ("run", "run-script"):
        rest = rest[1:]
    elif runner == "npm":
        return None  # npm install / npm ci etc. — not script indirection
    name = next((a for a in rest if not a.startswith("-")), None)
    if not name or name == "--":
        return None
    try:
        pkg = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scripts = pkg.get("scripts") or {}
    if name not in scripts:
        return None
    parts = [
        scripts[key]
        for key in (f"pre{name}", name, f"post{name}")
        if isinstance(scripts.get(key), str) and scripts.get(key, "").strip()
    ]
    if not parts:
        return None
    notes.append(
        Note("wrapper", f"{runner} run {name} → {' ; '.join(parts)!r} (package.json)")
    )
    return " ; ".join(parts)


def _expand_make_target(
    args: list[str], cwd: Path, notes: list[Note], hazards: list[Hazard]
) -> str | None:
    """Statically expand a Makefile target's recipe (plus direct prereqs).

    Never invokes make: ``make -n`` executes ``$(shell ...)`` during parse.
    Recipes using ``$(shell ...)`` or recursive ``$(MAKE)`` are flagged opaque.
    """
    target = next((a for a in args if not a.startswith("-") and "=" not in a), None)
    makefile = next(
        (cwd / n for n in ("Makefile", "makefile", "GNUmakefile") if (cwd / n).is_file()),
        None,
    )
    if makefile is None:
        return None
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")[:_MAX_SCRIPT_BYTES]
    except OSError:
        return None

    recipes: dict[str, list[str]] = {}
    prereqs: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("\t"):
            if current is not None:
                recipes[current].append(line.strip())
            continue
        rule = re.match(r"^([A-Za-z0-9_./-]+)\s*:(?!=)\s*(.*)$", line)
        if rule:
            current = rule.group(1)
            recipes.setdefault(current, [])
            prereqs[current] = [p for p in rule.group(2).split() if p]
            order.append(current)
        elif line.strip():
            current = None

    if target is None:
        target = next((t for t in order if not t.startswith(".")), None)
    if target is None or target not in recipes:
        return None

    lines: list[str] = []
    for t in [*prereqs.get(target, []), target]:
        lines.extend(recipes.get(t, []))
    if not lines:
        return None

    cleaned: list[str] = []
    for line in lines[:_MAX_SCRIPT_STATEMENTS]:
        if "$(shell" in line or "${shell" in line or "$(MAKE)" in line:
            hazards.append(
                Hazard(
                    kind="opaque_wrapper",
                    floor=_OPAQUE_FLOOR,
                    detail=f"make {target}: recipe uses $(shell)/$(MAKE) — not statically visible",
                )
            )
            continue
        cleaned.append(line.lstrip("@-").strip())
    if not cleaned:
        return None
    notes.append(Note("wrapper", f"make {target} expanded ({len(cleaned)} recipe line(s))"))
    return " ; ".join(cleaned)
