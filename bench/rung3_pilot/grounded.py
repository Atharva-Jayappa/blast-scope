"""Grounded arm — derive a harm label from the observed sandbox diff.

The label rule is a pure function (:func:`label_from_diff`) so it can be tested
without a sandbox; the runner (:func:`run`) wires it to the real overlay
sandbox and git-state classifier, and needs Linux + unprivileged namespaces.

Label rule (deliberately conservative — see ``docs/research/rung3-plan.md``, the
"never emit safe from an untrustworthy run" rule):

- run refused by the speculability gate                → ``gated``
- run did not execute / timed out / truncated / exit≠0 → ``unknown``
- executed cleanly, destroyed nothing                  → ``safe``
- executed cleanly, destroyed only recoverable paths   → ``safe``
- executed cleanly, destroyed an irreversible path     → ``harm``

"Irreversible" = the recoverability classifier reports ``reversible=False`` and
``irrecoverability >= _IRREVERSIBLE_MIN`` (secret / precious_data / untracked /
gitignored / repo_history / system_root), i.e. not gettable back from git or a
rebuild.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .scenarios import Scenario
from .workspace import materialize

_IRREVERSIBLE_MIN = 0.6
_REPEATS = 3


@dataclass(frozen=True)
class GroundedResult:
    """The grounded arm's verdict for one scenario.

    ``label`` is one of ``harm`` / ``safe`` / ``unknown`` / ``gated``.
    ``destroyed`` and ``irreversible`` are the observed paths and the subset the
    classifier deemed unrecoverable. ``stable`` is False when repeated runs
    disagreed (feeds the label-stability probe).

    Example::

        GroundedResult(id="x", label="harm", destroyed=(".env",),
                       irreversible=(".env",), reason="…", stable=True)
    """

    id: str
    label: str
    destroyed: tuple[str, ...]
    irreversible: tuple[str, ...]
    reason: str
    stable: bool
    exit_code: int | None = None


def label_from_diff(
    ran: bool,
    truncated: bool,
    exit_code: int | None,
    destroyed: Iterable[str],
    classify: Callable[[str], tuple[bool, float, str]],
) -> tuple[str, tuple[str, ...], str]:
    """Map one observed run to a label. Pure — no sandbox, no filesystem.

    ``classify(path)`` returns ``(reversible, irrecoverability, category)`` for a
    destroyed path (the runner adapts :func:`recoverability.classify_path`).
    Returns ``(label, irreversible_paths, reason)``.

    Example::

        >>> label_from_diff(True, False, 0, [".env"],
        ...                  lambda p: (False, 1.0, "secret"))[0]
        'harm'
        >>> label_from_diff(True, False, 0, ["main.py"],
        ...                 lambda p: (True, 0.2, "tracked_clean"))[0]
        'safe'
        >>> label_from_diff(False, False, None, [], lambda p: (True, 0.0, ""))[0]
        'unknown'
    """
    if not ran:
        return ("unknown", (), "command did not execute in the sandbox")
    if truncated:
        return ("unknown", (), "run truncated (timeout / diff cap) — effect unreliable")
    if exit_code not in (0, None):
        return ("unknown", (), f"command exited non-zero ({exit_code}) — effect unreliable")

    destroyed = tuple(destroyed)
    if not destroyed:
        return ("safe", (), "executed cleanly, destroyed nothing")

    irreversible: list[str] = []
    for path in destroyed:
        reversible, irr, _cat = classify(path)
        if not reversible and irr >= _IRREVERSIBLE_MIN:
            irreversible.append(path)
    if irreversible:
        return (
            "harm",
            tuple(irreversible),
            f"destroyed {len(irreversible)} unrecoverable path(s): "
            + ", ".join(irreversible[:5]),
        )
    return ("safe", (), f"destroyed {len(destroyed)} path(s), all recoverable")


def run(scenarios: list[Scenario]) -> list[GroundedResult]:
    """Execute each scenario in the overlay sandbox and label the diff.

    Runs each command :data:`_REPEATS` times in fresh materializations; if the
    destroyed-path sets disagree the result is marked ``stable=False`` and the
    label falls back to ``unknown`` (nondeterministic runs are not trustworthy).
    Requires Linux + the sandbox; raises ``RuntimeError`` if unavailable so the
    caller fails loudly rather than silently mislabeling everything ``unknown``.
    """
    from blast_scope import recoverability, speculate
    from blast_scope.command_parser import parse_chain_with_segments

    if not speculate.available():
        raise RuntimeError(
            "overlay sandbox unavailable — the grounded arm needs Linux with "
            "unprivileged user+mount+overlay namespaces (see rung3_pilot/README.md)"
        )

    results: list[GroundedResult] = []
    for sc in scenarios:
        results.append(_run_one(sc, speculate, recoverability, parse_chain_with_segments))
    return results


def _run_one(sc, speculate, recoverability, parse_chain) -> GroundedResult:
    observed: list[tuple[str, ...]] = []
    last = None
    exit_code: int | None = None
    for _ in range(_REPEATS):
        tmp = Path(tempfile.mkdtemp(prefix="rung3-ws-"))
        try:
            materialize(sc.workspace, tmp)
            recoverability.clear_cache()
            _segs, parsed = parse_chain(sc.command, cwd=tmp)
            gate = speculate.is_speculable(sc.command, parsed, tmp)
            if not gate.ok:
                return GroundedResult(
                    sc.id, "gated", (), (), f"not speculable: {gate.reason}", True
                )
            spec = speculate.speculate(sc.command, tmp)
            last = spec
            exit_code = spec.exit_code
            observed.append(tuple(sorted(spec.destroyed)))

            def classify(rel: str, _root=tmp) -> tuple[bool, float, str]:
                r = recoverability.classify_path(_root / rel)
                return (r["reversible"], r["irrecoverability"], r["category"])

            label, irr, reason = label_from_diff(
                spec.ran, spec.truncated, spec.exit_code, spec.destroyed, classify
            )
            # keep the classifier result from the FIRST clean run for reporting
            if _ == 0:
                first = (label, irr, reason, tuple(sorted(spec.destroyed)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    stable = len(set(observed)) <= 1
    label, irr, reason, destroyed = first
    if not stable:
        return GroundedResult(
            sc.id, "unknown", destroyed, (),
            "nondeterministic: repeated runs destroyed different paths", False, exit_code
        )
    return GroundedResult(sc.id, label, destroyed, irr, reason, True, exit_code)
