"""Orchestrate the pilot as decoupled phases that meet at JSON.

    # 1. generate the shared scenario set (anywhere)
    python -m bench.rung3_pilot.run_pilot generate --n 500 --out scenarios.json

    # 2a. grounded arm (LINUX with the sandbox)
    python -m bench.rung3_pilot.run_pilot grounded --scenarios scenarios.json --out grounded.json

    # 2b. judge arm (anywhere, needs OPENROUTER_API_KEY)
    python -m bench.rung3_pilot.run_pilot judge --scenarios scenarios.json --out judge.json

    # 3. compare + verdict
    python -m bench.rung3_pilot.run_pilot compare \
        --scenarios scenarios.json --grounded grounded.json --judge judge.json

Add ``--mock`` to ``grounded`` / ``judge`` to fabricate labels offline (no
sandbox, no API) — for validating the harness end-to-end before real resources
are wired up.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from . import compare as cmp_mod
from . import scenarios as scen_mod


# ---------------------------------------------------------------------------
# (De)serialization
# ---------------------------------------------------------------------------


def _scenario_to_dict(sc: scen_mod.Scenario) -> dict:
    ws = sc.workspace
    return {
        "id": sc.id, "command": sc.command, "stratum": sc.stratum,
        "expected": sc.expected,
        "workspace": {
            "files": ws.files, "tracked": list(ws.tracked), "dirty": list(ws.dirty),
            "executable": list(ws.executable), "git": ws.git,
        },
    }


def _scenario_from_dict(d: dict) -> scen_mod.Scenario:
    w = d["workspace"]
    return scen_mod.Scenario(
        id=d["id"], command=d["command"], stratum=d["stratum"], expected=d["expected"],
        workspace=scen_mod.WorkspaceSpec(
            files=w["files"], tracked=tuple(w["tracked"]), dirty=tuple(w["dirty"]),
            executable=tuple(w["executable"]), git=w["git"],
        ),
    )


def _load_scenarios(path: Path) -> list[scen_mod.Scenario]:
    return [_scenario_from_dict(d) for d in json.loads(path.read_text())]


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def cmd_generate(args) -> None:
    scen = scen_mod.generate(n=args.n, seed=args.seed)
    Path(args.out).write_text(
        json.dumps([_scenario_to_dict(s) for s in scen], indent=2)
    )
    by_stratum: dict[str, int] = {}
    for s in scen:
        by_stratum[s.stratum] = by_stratum.get(s.stratum, 0) + 1
    print(f"generated {len(scen)} scenarios → {args.out}")
    for k in sorted(by_stratum):
        print(f"  {k:<18} {by_stratum[k]}")


def cmd_grounded(args) -> None:
    scen = _load_scenarios(Path(args.scenarios))
    if args.mock:
        results = [_mock_grounded(s) for s in scen]
    else:
        from . import grounded
        results = [r.__dict__ for r in grounded.run(scen)]
    out = {r["id"]: r for r in results}
    Path(args.out).write_text(json.dumps(out, indent=2))
    labels: dict[str, int] = {}
    for r in results:
        labels[r["label"]] = labels.get(r["label"], 0) + 1
    print(f"grounded: {len(results)} scenarios → {args.out}")
    for k in sorted(labels):
        print(f"  {k:<10} {labels[k]}")


def cmd_judge(args) -> None:
    scen = _load_scenarios(Path(args.scenarios))
    if args.mock:
        results = [_mock_judge(s) for s in scen]
    else:
        from . import judge
        results = [r.__dict__ for r in judge.run(scen, model=args.model)]
    out = {r["id"]: r for r in results}
    Path(args.out).write_text(json.dumps(out, indent=2))
    labels: dict[str, int] = {}
    for r in results:
        labels[r["label"]] = labels.get(r["label"], 0) + 1
    print(f"judge ({args.model if not args.mock else 'mock'}): "
          f"{len(results)} scenarios → {args.out}")
    for k in sorted(labels):
        print(f"  {k:<10} {labels[k]}")


def cmd_compare(args) -> None:
    scen = _load_scenarios(Path(args.scenarios))
    by_id = {
        s.id: {"command": s.command, "stratum": s.stratum, "expected": s.expected}
        for s in scen
    }
    grounded = json.loads(Path(args.grounded).read_text())
    judge = json.loads(Path(args.judge).read_text())
    result = cmp_mod.compare(grounded, judge, by_id)
    verdict, why = cmp_mod.recommendation(result)
    _print_report(result, verdict, why)
    if args.out:
        payload = {**result.__dict__, "verdict": verdict, "verdict_reason": why}
        Path(args.out).write_text(json.dumps(payload, indent=2))
        print(f"\nfull report + disagreements → {args.out}")


def _print_report(r: cmp_mod.Comparison, verdict: str, why: str) -> None:
    print("=" * 60)
    print("blast-scope Rung 3 — judge vs grounded disagreement pilot")
    print("=" * 60)
    print(f"scenarios: {r.n_total}   comparable (both binary): {r.n_compared}")
    print(f"excluded — grounded unknown: {r.excluded['grounded_unknown']}, "
          f"gated: {r.excluded['grounded_gated']}, judge error: {r.excluded['judge_error']}")
    print(f"\nCohen's kappa: {r.kappa:.3f}   disagreement rate: {100*r.disagreement_rate:.1f}%")
    print("\nconfusion (grounded × judge):")
    print(f"           judge=harm   judge=safe")
    print(f"  g=harm   {r.confusion['g_harm_j_harm']:>8}   {r.confusion['g_harm_j_safe']:>8}"
          f"   <- under-read (missed real harm): {r.under_read}")
    print(f"  g=safe   {r.confusion['g_safe_j_harm']:>8}   {r.confusion['g_safe_j_safe']:>8}"
          f"   <- over-read (false alarm): {r.over_read}")
    print("\nby stratum (compared / agree / disagree):")
    for k in sorted(r.by_stratum):
        s = r.by_stratum[k]
        print(f"  {k:<18} {s['compared']:>4} / {s['agree']:>4} / {s['disagree']:>4}")
    print(f"\nVERDICT: {verdict}")
    print(f"  {why}")
    if r.disagreements:
        print(f"\nsample disagreements (first 8 of {len(r.disagreements)}):")
        for d in r.disagreements[:8]:
            print(f"  [{d['stratum']}] {d['command']}")
            print(f"      grounded={d['grounded']} ({d['grounded_reason']})")
            print(f"      judge={d['judge']} ({d['judge_reason']})")


# ---------------------------------------------------------------------------
# Offline mocks (for validating the harness without a sandbox or API)
# ---------------------------------------------------------------------------


def _mock_grounded(sc: scen_mod.Scenario) -> dict:
    """Grounded label ≈ the scenario's construction, with a little unknown noise."""
    rng = random.Random(hash(("g", sc.id)) & 0xFFFF)
    if rng.random() < 0.04:
        return {"id": sc.id, "label": "unknown", "destroyed": [],
                "irreversible": [], "reason": "mock nondeterministic", "stable": False}
    return {"id": sc.id, "label": sc.expected, "destroyed": [], "irreversible": [],
            "reason": f"mock: constructed {sc.expected}", "stable": True}


def _mock_judge(sc: scen_mod.Scenario) -> dict:
    """A judge that can't trace scripts (under-reads) and spooks at scary-looking
    deletes (over-reads) — to exercise the comparison, not to predict reality."""
    rng = random.Random(hash(("j", sc.id)) & 0xFFFF)
    if sc.stratum == "opaque_script":
        label = "safe" if rng.random() < 0.8 else "harm"  # under-reads
    elif sc.stratum == "regenerable":
        label = "harm" if rng.random() < 0.6 else "safe"  # over-reads
    elif sc.stratum == "tracked_recover":
        label = "harm" if rng.random() < 0.5 else "safe"  # over-reads (no git view)
    elif sc.stratum == "safe_read":
        label = "safe"
    else:  # clear_destroy, clobber
        label = "harm" if rng.random() < 0.85 else "safe"
    return {"id": sc.id, "label": label, "reason": f"mock judge ({sc.stratum})", "raw": ""}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Rung 3 go/no-go pilot")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate the shared scenario set")
    g.add_argument("--n", type=int, default=500)
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--out", default="scenarios.json")
    g.set_defaults(func=cmd_generate)

    gr = sub.add_parser("grounded", help="grounded arm (Linux sandbox)")
    gr.add_argument("--scenarios", required=True)
    gr.add_argument("--out", default="grounded.json")
    gr.add_argument("--mock", action="store_true")
    gr.set_defaults(func=cmd_grounded)

    j = sub.add_parser("judge", help="judge arm (OpenRouter)")
    j.add_argument("--scenarios", required=True)
    j.add_argument("--out", default="judge.json")
    j.add_argument("--model", default="deepseek/deepseek-chat")
    j.add_argument("--mock", action="store_true")
    j.set_defaults(func=cmd_judge)

    c = sub.add_parser("compare", help="compare arms + verdict")
    c.add_argument("--scenarios", required=True)
    c.add_argument("--grounded", required=True)
    c.add_argument("--judge", required=True)
    c.add_argument("--out", default="")
    c.set_defaults(func=cmd_compare)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
