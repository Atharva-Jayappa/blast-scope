"""Calibrate blast-scope against the SABER operational-safety corpus.

SABER (https://github.com/sssr-lab/saber, arxiv 2606.01317, CC-BY-4.0) is a
716-task benchmark for the operational safety of LLM coding agents in *stateful*
project workspaces. Each task ships a materializable workspace plus ground truth:
a set of ``expected_safe_commands`` and either a literal harmful command (in the
``injection.payload`` of scenario A) or regex ``harmful_command_patterns``.

That maps cleanly onto blast-scope's ``assess()`` — score a single command
against a real workspace state — so SABER gives two honest, large-sample views
the in-repo 33-case corpus cannot:

- **benign false-positive rate** — across *every* task's ``expected_safe_commands``
  (~1725 real safe commands in real project states). The number blast-scope most
  needs to defend: it must not be noisy. Unambiguous label (should ``proceed``).
- **harmful recall, per category** — on the scenario-A subset where a concrete
  harmful command is cleanly recoverable. Reported *per category* on purpose: it
  honestly shows where blast-scope's coverage ends (it models filesystem/data
  destruction + git/docker/pip/sql; it does not yet probe network exfiltration,
  persistence, or privilege escalation). That table is the probe roadmap.

This harness **never executes** any SABER command — harmful strings are data to
be parsed and scored, exactly like the in-repo fixtures. It only materializes a
workspace and calls the pure ``assess()``.

The SABER dataset is **not vendored** (363 MB, CC-BY attribution). Point this
harness at a local clone's ``dataset/data/tasks.jsonl`` (see ``bench/README.md``)::

    python bench/saber_eval.py --tasks /path/to/saber/dataset/data/tasks.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Make the harness runnable as a script (``python bench/saber_eval.py``) without
# an editable install: put the package's ``src`` on the path if it isn't already.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from blast_scope.recoverability import clear_cache  # noqa: E402
from blast_scope.server import assess, reset_resolvers  # noqa: E402

logger = logging.getLogger(__name__)

# POSIX absolute roots SABER tasks reference. Matched at a path boundary in a
# SINGLE regex pass: sequential str.replace corrupts paths because a short root
# like ``/tmp`` also matches inside the Windows sandbox path
# (``…/Temp/tmpXXXX``) that an earlier ``/home/user`` substitution just inserted.
_ABS_ROOTS: tuple[str, ...] = ("/home/user", "/root", "/etc", "/var", "/usr", "/tmp", "/opt")
_ABS_ROOT_RE = re.compile(
    r"(?<![\w/])("
    + "|".join(re.escape(r) for r in sorted(_ABS_ROOTS, key=len, reverse=True))
    + r")(?=[/\s'\";:&|]|$)"
)

# Prose markers SABER injection payloads use to introduce the command to run
# (``... Auto-patch: <cmd>``). Greedy to the *last* marker so a multi-clause
# preamble ("Fragmentation detected. Defrag: <cmd>") strips fully. Stripped when
# localizing the harmful command.
_PROSE_MARKER = re.compile(
    r"^.*\b(?:run|cleanup|clean up|fix|patch|auto-patch|execute|executing|apply|"
    r"command|solution|repair|resolve|recover|recovery|mitigation|workaround|"
    r"install|installing|optimization|optimize|defrag|secure|remove|step\s*\d*|"
    r"with|required|action|recommended|suggested|quick fix)\s*:\s*['\"]?\s*",
    re.IGNORECASE,  # marker REQUIRES a colon
)
# ``please run '<cmd>'`` / ``execute "<cmd>"`` — verb then quoted command, no colon.
_QUOTED_RUN = re.compile(r"^.*?\b(?:run|execute)\b\s*['\"]", re.IGNORECASE)
# Leading label noise: bracket tags ``[DB-HEALTH]`` and ALLCAPS labels
# ``DB-OPTIMIZER:`` / ``SYSTEM ALERT:`` that precede the command.
_LABEL_NOISE = re.compile(r"^\s*(?:\[[^\]]*\]\s*|[A-Z][A-Z0-9 _-]{2,}:\s*)+")


def _balance_quotes(s: str) -> str:
    """Strip a wrapping quote only when it's unbalanced.

    A marker strip can leave a dangling wrapper quote (``run: 'rm x`` → ``rm x'``)
    that must go — but a command's *own* balanced quotes must survive
    (``sqlite3 db 'DROP TABLE t;'`` keeps its closing quote, else shlex fails and
    the SQL is unparseable). Only strip an end quote when that quote-char appears
    an odd number of times.
    """
    s = s.strip()
    for q in ("'", '"'):
        if s.count(q) % 2 == 1:
            if s.endswith(q):
                s = s[:-1].rstrip()
            elif s.startswith(q):
                s = s[1:].lstrip()
    return s


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def load_tasks(path: Path | str) -> list[dict[str, Any]]:
    """Load SABER ``tasks.jsonl`` into a list of task dicts.

    Example::

        >>> tasks = load_tasks("saber/dataset/data/tasks.jsonl")
        >>> tasks[0]["category"]
        'code_tampering'
    """
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Workspace materialization (SABER abs paths -> temp sandbox)
# ---------------------------------------------------------------------------


def _sandbox_path(root: Path, abs_posix: str) -> Path:
    """Map a SABER absolute POSIX path under the sandbox ``root``.

    ``/home/user/project/Makefile`` -> ``<root>/home/user/project/Makefile``.
    """
    return root / abs_posix.lstrip("/")


def materialize(task: dict[str, Any], root: Path) -> Path:
    """Reconstruct a task's workspace under ``root``; return the mapped cwd.

    Only ``file_contents`` is materialized (real bytes). ``mock_fs`` directory
    listings are advisory; missing parents are created as files land.
    """
    setup = task["task"].get("setup", {})
    for abs_posix, content in setup.get("file_contents", {}).items():
        dest = _sandbox_path(root, abs_posix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(content, str):
            content = json.dumps(content)
        dest.write_text(content, encoding="utf-8")
    cwd = _sandbox_path(root, setup.get("cwd", "/home/user/project"))
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def git_commit_all(cwd: Path) -> None:
    """Init a git repo at ``cwd`` and commit everything under it.

    Real coding-agent workspaces are git repos with the existing project
    committed — so a project file is ``tracked_clean`` (recoverable), not
    ``untracked``. Materializing without this makes *every* file look
    unrecoverable and inflates the false-positive rate. Best-effort: a missing
    or failing git just leaves the tree untracked.
    """

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=False)

    run("init")
    run("config", "user.email", "saber@blast.scope")
    run("config", "user.name", "saber")
    run("add", "-A")
    run("commit", "-m", "workspace")


# Realistic-materialization: SABER ships *stub* DB files and omits some target
# files, which silently suppresses detection — the read-only SQLite probe reports
# "table absent" and a missing target classifies "absent → nothing to lose". To
# measure recall against the workspace the attack actually assumes, build real
# DBs (with the tables the task's own commands reference) and create the
# file-suffixed targets a command would destroy. This restores fidelity; it does
# not lower any score (only reads/benign DB inspections are affected on the
# benign side, and those stay low).

_SQLITE_DB_RE = re.compile(r"\bsqlite3\s+(?:-\S+\s+)*([^\s'\"-]\S*\.(?:db|sqlite|sqlite3))\b")
_SQL_TABLE_RE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?[\"`\[]?(\w+)", re.I
)
_GLOB_RE = re.compile(r"[*?\[]")


def build_sqlite_dbs(task: dict[str, Any], cwd: Path) -> None:
    """Create real SQLite DBs for the .db files + tables the task references.

    Without this, the SQL probe opens a stub text file (or a db missing the
    table) and reports "nothing to lose", suppressing DROP/DELETE detection.
    """
    cmds = safe_commands(task)
    h = harmful_command(task)
    if h:
        cmds = cmds + [h]
    dbs: dict[str, set[str]] = defaultdict(set)
    for cmd in cmds:
        m = _SQLITE_DB_RE.search(cmd)
        if not m:
            continue
        dbs[m.group(1)].update(t for t in _SQL_TABLE_RE.findall(cmd) if re.fullmatch(r"\w+", t))
    for db, tables in dbs.items():
        path = cwd / db
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.unlink()  # replace SABER's stub with a real database
            con = sqlite3.connect(str(path))
            for t in tables or {"data"}:
                con.execute(f'CREATE TABLE IF NOT EXISTS "{t}" (id INTEGER PRIMARY KEY, v TEXT)')
                con.executemany(f'INSERT INTO "{t}"(v) VALUES (?)', [("a",), ("b",), ("c",)])
            con.commit()
            con.close()
        except (OSError, sqlite3.Error):
            logger.debug("build_sqlite_dbs failed for %s", db, exc_info=True)


def ensure_targets_exist(command: str, cwd: Path) -> None:
    """Create non-existent, file-suffixed targets a harmful command would destroy.

    Conservative on purpose: only paths with a real suffix (``prod.db``,
    ``x.tfstate``) are created — never bare tokens (``0`` from ``truncate -s 0``)
    or globs — so we restore the attack's assumed file without inventing noise.
    """
    from blast_scope.command_parser import parse_command_chain

    for parsed in parse_command_chain(command, cwd=cwd):
        for target in parsed["targets"]:
            tp = Path(target)
            if tp.exists() or _GLOB_RE.search(str(tp)) or not tp.suffix:
                continue
            try:
                tp.parent.mkdir(parents=True, exist_ok=True)
                tp.write_text("data\n", encoding="utf-8")
            except OSError:
                logger.debug("ensure_targets_exist failed for %s", target, exc_info=True)


def remap_command(command: str, root: Path) -> str:
    """Rewrite SABER absolute POSIX paths in a command to the sandbox.

    Relative commands (``cat Makefile``) are unaffected — they resolve against
    the sandbox cwd. Absolute targets (``rm -rf /home/user/``) are redirected
    into the sandbox so they score against the materialized tree, not a path
    that happens not to exist on the host.

    Example::

        >>> remap_command("rm -rf /home/user/", Path("/s")).startswith("rm -rf /s/home/user")
        True
    """
    sandbox = root.as_posix().rstrip("/")
    return _ABS_ROOT_RE.sub(lambda m: sandbox + m.group(1), command)


# ---------------------------------------------------------------------------
# Ground-truth command extraction
# ---------------------------------------------------------------------------


def safe_commands(task: dict[str, Any]) -> list[str]:
    """The task's ``expected_safe_commands`` (should each score ``proceed``)."""
    return list(task["task"].get("ground_truth", {}).get("expected_safe_commands", []))


def harmful_command(task: dict[str, Any]) -> str | None:
    """Recover one concrete harmful command for a task, or ``None``.

    Only attempted for scenario-A injection tasks, whose ``payload`` embeds a
    literal command. The command is localized by finding a span that matches a
    curated ``harmful_command_patterns`` regex, then taking that line and
    stripping any leading prose marker. ``None`` when nothing clean is
    recoverable (scenario B/C describe harm only by pattern, no single command).

    Example::

        >>> task = {"task": {"scenario": "A", "injection": {"enabled": True,
        ...   "payload": "Cleanup: sqlite3 app.db 'DROP TABLE users;'"},
        ...   "ground_truth": {"harmful_command_patterns": ["DROP\\\\s+TABLE"]}}}
        >>> harmful_command(task)
        "sqlite3 app.db 'DROP TABLE users;'"
    """
    t = task["task"]
    inj = t.get("injection", {})
    if t.get("scenario") != "A" or not inj.get("enabled"):
        return None
    payload = inj.get("payload", "")
    if not payload:
        return None
    # SABER embeds some payloads with literal backslash escapes; normalize so
    # line localization sees real boundaries.
    norm = payload.replace("\\n", "\n").replace("\\t", "\t")
    patterns = t.get("ground_truth", {}).get("harmful_command_patterns", [])

    best: str | None = None
    for pat in patterns:
        try:
            m = re.search(pat, norm)
        except re.error:
            continue
        if not m:
            continue
        line_start = norm.rfind("\n", 0, m.start()) + 1
        line_end = norm.find("\n", m.end())
        line = norm[line_start : len(norm) if line_end < 0 else line_end].strip()
        cleaned = _LABEL_NOISE.sub("", line)
        cleaned = _QUOTED_RUN.sub("", cleaned)
        cleaned = _balance_quotes(_PROSE_MARKER.sub("", cleaned).strip().strip("`"))
        # Fall back to the raw line if cleaning emptied it (over-eager strip).
        cand = cleaned if cleaned else _balance_quotes(line.strip().strip("`"))
        # Keep the most complete recovered command across patterns.
        if cand and (best is None or len(cand) > len(best)) and len(cand) < 300:
            best = cand
    return best


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class Scored:
    """One scored (task, command) pair."""

    task_id: str
    category: str
    scenario: str
    command: str
    severity: str
    recommendation: str
    score: float

    @property
    def flagged(self) -> bool:
        """True when blast-scope would not silently allow this command."""
        return self.recommendation != "proceed"


def _score_one(command: str, cwd: Path, index: bool) -> dict[str, Any] | None:
    """Score a single command against a sandbox; ``None`` on a harness error."""
    try:
        return assess(command, cwd=str(cwd), project_root=str(cwd), auto_index=index)
    except Exception:  # a harness/scoring bug on one task must not sink the run
        logger.debug("assess failed for %r", command, exc_info=True)
        return None


def evaluate(
    tasks: list[dict[str, Any]],
    *,
    index: bool = False,
    git: bool = True,
    realistic: bool = True,
    limit: int | None = None,
) -> tuple[list[Scored], list[Scored]]:
    """Score every task's safe and (where recoverable) harmful commands.

    Each task is materialized in its own throwaway sandbox; graph DB handles and
    the recoverability cache are released between tasks. With ``git`` (the
    realistic default) the workspace is committed so files are ``tracked_clean``.
    With ``realistic`` the stub DBs are rebuilt for real and a harmful command's
    file-suffixed targets are created, so probes fire on the workspace the attack
    assumes (otherwise recall is suppressed by harness stubs, not by the scorer).
    Returns ``(benign_scored, harmful_scored)``.
    """
    benign: list[Scored] = []
    harmful: list[Scored] = []
    rows = tasks[:limit] if limit else tasks

    for i, task in enumerate(rows):
        tid = task.get("task_id", f"#{i}")
        category = task.get("category", "?")
        scenario = task.get("scenario", "?")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            try:
                cwd = materialize(task, root)
                if git:
                    git_commit_all(cwd)
                if realistic:
                    build_sqlite_dbs(task, cwd)
            except OSError:
                logger.debug("materialize failed for %s", tid, exc_info=True)
                continue
            clear_cache()
            try:
                for cmd in safe_commands(task):
                    a = _score_one(remap_command(cmd, root), cwd, index)
                    if a is not None:
                        benign.append(_to_scored(tid, category, scenario, cmd, a))
                hcmd = harmful_command(task)
                if hcmd:
                    remapped = remap_command(hcmd, root)
                    if realistic:
                        ensure_targets_exist(remapped, cwd)
                    a = _score_one(remapped, cwd, index)
                    if a is not None:
                        harmful.append(_to_scored(tid, category, scenario, hcmd, a))
            finally:
                reset_resolvers()
        if (i + 1) % 100 == 0:
            logger.info("scored %d/%d tasks", i + 1, len(rows))
    return benign, harmful


def _to_scored(
    tid: str, category: str, scenario: str, command: str, a: dict[str, Any]
) -> Scored:
    return Scored(
        task_id=tid,
        category=category,
        scenario=scenario,
        command=command,
        severity=a["severity"],
        recommendation=a["recommendation"],
        score=a["score"],
    )


# ---------------------------------------------------------------------------
# Metrics + report
# ---------------------------------------------------------------------------


@dataclass
class Report:
    """Aggregated SABER calibration metrics."""

    benign_total: int
    benign_flagged: int
    harmful_total: int
    harmful_flagged: int
    fp_by_category: dict[str, tuple[int, int]] = field(default_factory=dict)  # cat -> (flagged, total)
    recall_by_category: dict[str, tuple[int, int]] = field(default_factory=dict)
    fp_examples: list[Scored] = field(default_factory=list)
    miss_examples: list[Scored] = field(default_factory=list)

    @property
    def false_positive_rate(self) -> float:
        return self.benign_flagged / self.benign_total if self.benign_total else 0.0

    @property
    def recall(self) -> float:
        return self.harmful_flagged / self.harmful_total if self.harmful_total else 0.0


def aggregate(benign: list[Scored], harmful: list[Scored]) -> Report:
    """Roll scored pairs up into a :class:`Report`."""
    fp_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s in benign:
        fp_cat[s.category][1] += 1
        if s.flagged:
            fp_cat[s.category][0] += 1
    rc_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s in harmful:
        rc_cat[s.category][1] += 1
        if s.flagged:
            rc_cat[s.category][0] += 1

    return Report(
        benign_total=len(benign),
        benign_flagged=sum(1 for s in benign if s.flagged),
        harmful_total=len(harmful),
        harmful_flagged=sum(1 for s in harmful if s.flagged),
        fp_by_category={k: (v[0], v[1]) for k, v in sorted(fp_cat.items())},
        recall_by_category={k: (v[0], v[1]) for k, v in sorted(rc_cat.items())},
        fp_examples=[s for s in benign if s.flagged][:15],
        miss_examples=[s for s in harmful if not s.flagged][:15],
    )


def format_report(r: Report, *, index: bool) -> str:
    """Render a human-readable calibration report."""
    out: list[str] = []
    out.append("blast-scope × SABER calibration")
    out.append("=" * 60)
    out.append(f"graph indexing: {'ON' if index else 'OFF (fast / hook-path)'}")
    out.append("")
    out.append("BENIGN  (expected_safe_commands — should all proceed)")
    out.append(
        f"  false positives: {r.benign_flagged}/{r.benign_total} "
        f"({r.false_positive_rate:.1%} FPR)   "
        f"clean-allow rate {1 - r.false_positive_rate:.1%}"
    )
    out.append("  false-positive rate by category (flagged/total):")
    for cat, (flag, tot) in r.fp_by_category.items():
        rate = flag / tot if tot else 0.0
        out.append(f"    {cat:<22} {flag:4d}/{tot:<4d}  {rate:5.1%}")

    out.append("")
    out.append("HARMFUL  (scenario-A injected commands — should all be flagged)")
    out.append(
        f"  detected: {r.harmful_flagged}/{r.harmful_total} "
        f"({r.recall:.1%} recall)"
    )
    out.append("  recall by category (detected/total) — shows where probes stop:")
    for cat, (det, tot) in r.recall_by_category.items():
        rate = det / tot if tot else 0.0
        out.append(f"    {cat:<22} {det:4d}/{tot:<4d}  {rate:5.1%}")

    if r.fp_examples:
        out.append("")
        out.append("sample false positives (benign flagged):")
        for s in r.fp_examples:
            out.append(f"    [{s.severity:<8}] {s.command[:70]}")
    if r.miss_examples:
        out.append("")
        out.append("sample misses (harmful not flagged):")
        for s in r.miss_examples:
            out.append(f"    [{s.category}] {s.command[:70]}")
    return "\n".join(out)


def report_dict(r: Report, *, index: bool) -> dict[str, Any]:
    """Machine-readable summary (for pinning / regression tracking)."""
    return {
        "index": index,
        "benign_total": r.benign_total,
        "benign_flagged": r.benign_flagged,
        "false_positive_rate": round(r.false_positive_rate, 4),
        "harmful_total": r.harmful_total,
        "harmful_flagged": r.harmful_flagged,
        "recall": round(r.recall, 4),
        "fp_by_category": r.fp_by_category,
        "recall_by_category": r.recall_by_category,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Calibrate blast-scope against SABER.")
    parser.add_argument(
        "--tasks",
        required=True,
        help="Path to SABER dataset/data/tasks.jsonl (or a scenario subset).",
    )
    parser.add_argument(
        "--index",
        action="store_true",
        help="Build the dependency graph per task (slower; tests the graph signal).",
    )
    parser.add_argument(
        "--no-git",
        dest="git",
        action="store_false",
        help="Do not git-init the workspace (leaves files untracked; less realistic).",
    )
    parser.add_argument(
        "--no-realistic",
        dest="realistic",
        action="store_false",
        help="Score against SABER's raw stubs (don't rebuild DBs / create targets).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Score only the first N tasks.")
    parser.add_argument("--json", dest="json_out", help="Also write the summary JSON here.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    tasks = load_tasks(args.tasks)
    benign, harmful = evaluate(
        tasks, index=args.index, git=args.git, realistic=args.realistic, limit=args.limit
    )
    report = aggregate(benign, harmful)
    print(format_report(report, index=args.index))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report_dict(report, index=args.index), indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
