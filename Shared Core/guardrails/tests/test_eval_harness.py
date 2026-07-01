"""CI guard for the eval/ harness (golden set + precision/recall/F1 + latency SLOs).

Imports the runner directly (no subprocess) and enforces a floor on the binary flagged-task
metrics so a regression in the rules/pipeline is caught. Latency is asserted softly (a busy
CI box can be slow) — only that the numbers are produced and the input p50 is within a
generous multiple of the SLO, so the SLO accounting itself is exercised without flaking.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL = Path(__file__).resolve().parent.parent / "eval"
if str(_EVAL) not in sys.path:
    sys.path.insert(0, str(_EVAL))

import run_eval  # noqa: E402


def test_golden_set_loads_and_is_labeled() -> None:
    rows = run_eval.load_golden()
    assert len(rows) >= 20
    labels = {r["label"] for r in rows}
    assert {"benign", "toxic", "jailbreak", "injection", "pii"} <= labels
    for r in rows:
        assert r["expected_decision"] in ("allow", "warn", "redact", "block")


def test_eval_metrics_meet_floor() -> None:
    rows = run_eval.load_golden()
    summary = run_eval.summarize(run_eval.run(rows))
    o = summary["overall"]
    assert o["precision"] >= run_eval.MIN_PRECISION, summary
    assert o["recall"] >= run_eval.MIN_RECALL, summary
    assert o["f1"] >= run_eval.MIN_F1, summary
    # Benign must never be flagged (no false positives on clean input).
    assert summary["per_label"]["benign"]["precision"] == 1.0, summary
    # Jailbreak / injection / toxic must all be caught (recall 1.0 with the stub).
    for label in ("jailbreak", "injection", "toxic"):
        assert summary["per_label"][label]["recall"] == 1.0, (label, summary)


def test_eval_reports_latency_and_slo_flags() -> None:
    rows = run_eval.load_golden()
    summary = run_eval.summarize(run_eval.run(rows))
    lat = summary["latency_ms"]
    assert lat["input_p50"] >= 0.0 and lat["input_p99"] >= 0.0
    assert set(summary["slo"]) == {
        "input_p50_pass", "input_p99_pass", "output_p50_pass", "output_p99_pass",
    }
    # The pure regex/heuristic pipeline is single-digit-ms; input p50 should sit well under
    # a generous 10x the SLO even on a loaded CI box (exercises the SLO accounting).
    assert lat["input_p50"] <= run_eval.SLO_INPUT_P50_S * 1000 * 10
