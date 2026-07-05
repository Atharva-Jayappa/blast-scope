"""Evaluation harness — measure the scorer against a labeled corpus.

Every threshold in the scoring model (PageRank blend, recoverability factors,
consequence floors) is a hand-picked number. This harness turns "looks about
right" into a number: it materializes each labeled command in a throwaway
project — including the git working-tree state and, when needed, a built
dependency graph — runs the real :func:`blast_scope.server.assess`, and compares
the predicted severity to the label.

Two views are reported:

- **classification** — exact-severity accuracy and within-one-band accuracy
  across the four severity levels.
- **gate** — treating the tool as a binary filter (``proceed`` vs.
  ``confirm``/``block``), precision / recall / F1 against the ground-truth
  "should this have been flagged?" label (anything not ``low``).

Run it with ``python -m blast_scope.eval`` (optionally a corpus path).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blast_scope.recoverability import clear_cache
from blast_scope.server import assess, reset_resolvers

_SEVERITIES = ("low", "medium", "high", "critical")
_RANK = {s: i for i, s in enumerate(_SEVERITIES)}

DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eval_corpus.jsonl"


@dataclass(frozen=True)
class CaseResult:
    """Outcome of scoring one corpus case.

    Example::

        CaseResult(id="rm_env_secret", expected="critical", actual="critical",
                   score=0.9, recommendation="block", exact=True, within=True)
    """

    id: str
    command: str
    expected: str
    actual: str
    score: float
    recommendation: str
    exact: bool
    within: bool
    note: str


@dataclass(frozen=True)
class Metrics:
    """Aggregate metrics over a corpus run."""

    total: int
    exact: int
    within: int
    precision: float
    recall: float
    f1: float
    confusion: dict[str, dict[str, int]]
    mismatches: list[CaseResult]

    @property
    def exact_accuracy(self) -> float:
        return self.exact / self.total if self.total else 0.0

    @property
    def within_accuracy(self) -> float:
        return self.within / self.total if self.total else 0.0


# ---------------------------------------------------------------------------
# Corpus loading + case setup
# ---------------------------------------------------------------------------


def load_corpus(path: Path | str = DEFAULT_CORPUS) -> list[dict[str, Any]]:
    """Load a JSONL corpus file into a list of case dicts."""
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _materialize(case: dict[str, Any], root: Path) -> None:
    """Create the files, directories, and git state a case declares."""
    setup = case.get("setup", {})
    for d in setup.get("dirs", []):
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, content in setup.get("files", {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git = setup.get("git")
    if git:
        _git_setup(root, git)


def _git_setup(root: Path, git: dict[str, Any]) -> None:
    """Build a git repo with committed / modified / untracked working state."""

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)

    run("init")
    run("config", "user.email", "eval@blast.scope")
    run("config", "user.name", "eval")
    committed = git.get("committed", {})
    for rel, content in committed.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if committed:
        run("add", "-A")
        run("commit", "-m", "init")
    for rel, content in git.get("modified", {}).items():
        (root / rel).write_text(content, encoding="utf-8")
    for rel, content in git.get("untracked", {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_case(case: dict[str, Any]) -> CaseResult:
    """Score a single case in an isolated temp project.

    Example::

        >>> evaluate_case({"id": "x", "command": "rm .env",
        ...                "setup": {"files": {".env": "K=v\\n"}}, "expected": "critical"}).actual
        'critical'
    """
    index = bool(case.get("index", False))
    # ignore_cleanup_errors: on Windows a lingering SQLite handle can briefly
    # block temp-dir deletion; we close handles explicitly below, this is belt-
    # and-suspenders so a stray lock can never fail the run.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _materialize(case, root)
        clear_cache()
        # Deterministic env for $VAR resolution: exactly what the case declares
        # (with "{root}" rewritten to the temp project), never the harness's
        # own environment. An absent "env" key means "no variables set".
        env = {
            k: v.replace("{root}", str(root))
            for k, v in (case.get("setup", {}).get("env") or {}).items()
        }
        try:
            assessment = assess(
                case["command"],
                cwd=str(root),
                project_root=str(root),
                auto_index=index,
                env=env,
            )
        finally:
            # Release the graph DB handle before the temp dir is cleaned up.
            reset_resolvers()

    expected = case["expected"]
    actual = assessment["severity"]
    return CaseResult(
        id=case["id"],
        command=case["command"],
        expected=expected,
        actual=actual,
        score=assessment["score"],
        recommendation=assessment["recommendation"],
        exact=actual == expected,
        within=abs(_RANK[actual] - _RANK[expected]) <= 1,
        note=case.get("note", ""),
    )


def run_corpus(path: Path | str = DEFAULT_CORPUS) -> Metrics:
    """Evaluate every case in the corpus and aggregate metrics.

    Example::

        >>> m = run_corpus()
        >>> 0.0 <= m.exact_accuracy <= 1.0
        True
    """
    results = [evaluate_case(c) for c in load_corpus(path)]
    return _aggregate(results)


def _aggregate(results: list[CaseResult]) -> Metrics:
    confusion: dict[str, dict[str, int]] = {
        e: {a: 0 for a in _SEVERITIES} for e in _SEVERITIES
    }
    tp = fp = fn = tn = 0
    for r in results:
        confusion[r.expected][r.actual] += 1
        pred_flag = r.recommendation != "proceed"
        true_flag = r.expected != "low"
        if pred_flag and true_flag:
            tp += 1
        elif pred_flag and not true_flag:
            fp += 1
        elif not pred_flag and true_flag:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return Metrics(
        total=len(results),
        exact=sum(r.exact for r in results),
        within=sum(r.within for r in results),
        precision=precision,
        recall=recall,
        f1=f1,
        confusion=confusion,
        mismatches=[r for r in results if not r.exact],
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(metrics: Metrics) -> str:
    """Render a human-readable report of a corpus run."""
    lines: list[str] = []
    lines.append("blast-scope evaluation")
    lines.append("=" * 48)
    lines.append(f"cases:            {metrics.total}")
    lines.append(
        f"exact severity:   {metrics.exact}/{metrics.total} "
        f"({metrics.exact_accuracy:.0%})"
    )
    lines.append(
        f"within one band:  {metrics.within}/{metrics.total} "
        f"({metrics.within_accuracy:.0%})"
    )
    lines.append("")
    lines.append("gate (proceed vs confirm/block, truth = not-low):")
    lines.append(
        f"  precision {metrics.precision:.2f}  recall {metrics.recall:.2f}  "
        f"F1 {metrics.f1:.2f}"
    )
    lines.append("")
    lines.append("confusion matrix (rows = expected, cols = actual):")
    header = "  " + "expected\\actual".ljust(12) + "".join(s[:4].rjust(6) for s in _SEVERITIES)
    lines.append(header)
    for e in _SEVERITIES:
        row = "  " + e.ljust(12) + "".join(
            str(metrics.confusion[e][a]).rjust(6) for a in _SEVERITIES
        )
        lines.append(row)
    if metrics.mismatches:
        lines.append("")
        lines.append("mismatches:")
        for r in metrics.mismatches:
            lines.append(
                f"  {r.id:<24} expected {r.expected:<8} got {r.actual:<8} "
                f"(score {r.score:.2f})  {r.note}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: run the corpus and print the report."""
    argv = argv if argv is not None else sys.argv[1:]
    path = Path(argv[0]) if argv else DEFAULT_CORPUS
    metrics = run_corpus(path)
    print(format_report(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
