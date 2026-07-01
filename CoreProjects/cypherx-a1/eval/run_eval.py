"""Knowledge-graph accuracy eval runner (pure, network-free, keyless).

Scores the reusable ``cypherx_a1.kg`` lib against the golden set in ``eval/golden/`` and
prints precision / recall / accuracy for the Phase KG accuracy wins:

  * schema-guided extraction — are off-schema (hallucinated) relations rejected while
    in-schema relations are kept?
  * confidence floor — are below-floor edges flagged/dropped correctly?
  * type-aware coreference — do mention pairs resolve as labeled?

Usage:
    python eval/run_eval.py            # human-readable report + JSON summary
    python eval/run_eval.py --json     # JSON only

Exit code is 0 when every metric clears its target threshold, else 1 (so it can gate CI).
The thresholds are intentionally conservative; ``tests/test_eval_harness.py`` pins them too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the src package importable when run as a plain script (mirrors pyproject pythonpath).
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cypherx_a1.kg import DEFAULT_SCHEMA, are_coreferent, parse_extracted_edges  # noqa: E402

_GOLDEN = Path(__file__).resolve().parent / "golden"

# Metric targets the harness gates on (a measured regression fails CI).
TARGETS = {
    "schema_rejection_precision": 1.0,  # every kept edge must be in-schema (no hallucination leaks)
    "schema_recall": 1.0,               # every in-schema edge must be kept (no over-rejection)
    "coref_accuracy": 1.0,              # the labeled coreference decisions must all match
    "floor_accuracy": 1.0,              # below-floor labeling must match
}


def _load(name: str) -> dict:
    return json.loads((_GOLDEN / name).read_text(encoding="utf-8"))


def eval_extraction() -> dict:
    """Schema-guided extraction + confidence-floor metrics.

    For each golden case we run the schema-enforced parser (reject mode) and compare the
    KEPT edges against the labeled in-schema edges. We also run a no-schema flag-mode pass to
    score the below-floor labeling.
    """
    data = _load("extraction_cases.json")
    floor = float(data.get("floor", 0.6))

    kept_tp = kept_fp = expected = 0   # for schema precision/recall
    floor_correct = floor_total = 0

    for case in data["cases"]:
        labels = {(label_lbl["rel"], label_lbl["target_key"]): label_lbl for label_lbl in case["labels"]}
        in_schema_keys = {k for k, v in labels.items() if v["in_schema"] and not v["below_floor"]}
        expected += len(in_schema_keys)

        # Schema-enforced, drop below-floor: the production "tight" config.
        parsed = parse_extracted_edges(
            case["content"], floor=floor, mode="drop", schema=DEFAULT_SCHEMA, schema_mode="reject"
        )
        kept = {(e.rel, e.target_key) for e in parsed.edges}
        for key in kept:
            if key in in_schema_keys:
                kept_tp += 1
            else:
                kept_fp += 1

        # Below-floor labeling: no-schema, FLAG mode keeps everything so we can read .flagged.
        flagged = parse_extracted_edges(case["content"], floor=floor, mode="flag")
        flag_by_key = {(e.rel, e.target_key): e.flagged for e in flagged.edges}
        for key, label_lbl in labels.items():
            if key in flag_by_key:
                floor_total += 1
                if flag_by_key[key] == label_lbl["below_floor"]:
                    floor_correct += 1

    precision = kept_tp / (kept_tp + kept_fp) if (kept_tp + kept_fp) else 1.0
    recall = kept_tp / expected if expected else 1.0
    floor_acc = floor_correct / floor_total if floor_total else 1.0
    return {
        "cases": len(data["cases"]),
        "schema_rejection_precision": round(precision, 4),
        "schema_recall": round(recall, 4),
        "floor_accuracy": round(floor_acc, 4),
        "kept_true_positive": kept_tp,
        "kept_false_positive": kept_fp,
        "expected_in_schema": expected,
    }


def eval_coref() -> dict:
    data = _load("coref_cases.json")
    correct = 0
    cases = data["cases"]
    misses: list[dict] = []
    for case in cases:
        got = are_coreferent(case["a"], case["b"], kind=case["kind"])
        if got == case["coreferent"]:
            correct += 1
        else:
            misses.append({"a": case["a"], "b": case["b"], "kind": case["kind"],
                           "expected": case["coreferent"], "got": got})
    return {
        "cases": len(cases),
        "coref_accuracy": round(correct / len(cases), 4) if cases else 1.0,
        "correct": correct,
        "misses": misses,
    }


def run() -> dict:
    extraction = eval_extraction()
    coref = eval_coref()
    metrics = {
        "schema_rejection_precision": extraction["schema_rejection_precision"],
        "schema_recall": extraction["schema_recall"],
        "floor_accuracy": extraction["floor_accuracy"],
        "coref_accuracy": coref["coref_accuracy"],
    }
    passed = all(metrics[k] >= TARGETS[k] for k in TARGETS)
    return {"metrics": metrics, "targets": TARGETS, "passed": passed,
            "extraction": extraction, "coref": coref}


def _report(summary: dict) -> str:
    lines = ["cypherx-a1 KG accuracy eval", "=" * 32]
    for k, target in summary["targets"].items():
        got = summary["metrics"][k]
        mark = "PASS" if got >= target else "FAIL"
        lines.append(f"  {k:<28} {got:>6.3f}  (target >= {target:.2f})  [{mark}]")
    ex = summary["extraction"]
    co = summary["coref"]
    lines.append(
        f"\n  extraction: {ex['cases']} cases, kept TP={ex['kept_true_positive']} "
        f"FP={ex['kept_false_positive']} / expected {ex['expected_in_schema']}"
    )
    lines.append(f"  coref:      {co['cases']} cases, {co['correct']} correct")
    if co["misses"]:
        lines.append(f"  coref misses: {co['misses']}")
    lines.append(f"\n  OVERALL: {'PASS' if summary['passed'] else 'FAIL'}")
    return "\n".join(lines)


def main() -> int:
    summary = run()
    if "--json" in sys.argv:
        print(json.dumps(summary, indent=2))
    else:
        print(_report(summary))
        print("\n" + json.dumps(summary["metrics"]))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
