"""Regression guard built on the evaluation harness.

These tests pin the scorer's accuracy against the labeled corpus so a future
change to any threshold can't silently degrade quality. Thresholds sit *below*
the current calibrated numbers (38/38 exact, F1 1.00) to leave headroom for
intentional tuning while still catching real regressions.
"""

from __future__ import annotations

from blast_scope import eval as harness


def test_corpus_loads() -> None:
    cases = harness.load_corpus()
    assert len(cases) >= 20
    for c in cases:
        assert "command" in c and "expected" in c
        assert c["expected"] in ("low", "medium", "high", "critical")


def test_every_case_runs() -> None:
    for case in harness.load_corpus():
        result = harness.evaluate_case(case)
        assert result.actual in ("low", "medium", "high", "critical")


def test_exact_accuracy_above_bar() -> None:
    metrics = harness.run_corpus()
    # Calibrated to 1.00; guard at 0.85 so real regressions trip it.
    assert metrics.exact_accuracy >= 0.85, harness.format_report(metrics)


def test_within_one_band_is_near_total() -> None:
    metrics = harness.run_corpus()
    # No prediction should ever be more than one severity band off.
    assert metrics.within_accuracy >= 0.95, harness.format_report(metrics)


def test_gate_f1_above_bar() -> None:
    metrics = harness.run_corpus()
    # The proceed-vs-flag gate must stay strong (calibrated to 1.00).
    assert metrics.f1 >= 0.9, harness.format_report(metrics)


def test_no_critical_scored_as_proceed() -> None:
    # Safety floor: a critical-labeled command must never be waved through.
    for case in harness.load_corpus():
        if case["expected"] == "critical":
            r = harness.evaluate_case(case)
            assert r.recommendation != "proceed", r
