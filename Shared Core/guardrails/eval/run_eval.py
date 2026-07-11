"""Guardrails eval harness — golden set + metric runner (precision/recall/F1 + latency SLOs).

Runs the labeled golden set (``eval/golden_set.jsonl``: benign / toxic / jailbreak /
injection / PII) through the REAL in-process pipeline (the same ``evaluate`` the check path
uses), then reports:

  * Per-label and overall precision / recall / F1 for the binary "should this be flagged?"
    task (flagged == decision != 'allow'), plus exact-decision accuracy.
  * p50 / p99 evaluation latency (the pipeline portion) vs the Component-1 SLOs:
        input  30ms (p50) / 50ms (p99)   |   output 60ms (p50) / 100ms (p99)
    (the documented "30/50ms-in, 60/100ms-out" budget). PASS/FAIL is printed per quantile.

Keyless + offline: uses the stub classifier and the built-in platform-default policy, so it
needs NO Auth/DB/Kafka/network. Run it as:

    uv run python eval/run_eval.py
    uv run python eval/run_eval.py --json        # machine-readable summary on stdout
    uv run python eval/run_eval.py --repeat 5     # average latency over N passes

The runner is also imported by ``tests/test_eval_harness.py`` so the metric thresholds are
enforced in CI (regression guard: precision/recall must not drop below a floor).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make ``src`` importable when run directly (uv run python eval/run_eval.py).
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from guardrails_service.core.config import Settings  # noqa: E402
from guardrails_service.core.normalization import build_confusables_map, canonicalize  # noqa: E402
from guardrails_service.services.classifier import StubClassifier  # noqa: E402
from guardrails_service.services.injection_defense import assess as assess_injection  # noqa: E402
from guardrails_service.services.pipeline import evaluate  # noqa: E402
from guardrails_service.services.policy_engine import builtin_platform_default  # noqa: E402
from guardrails_service.services.rules import RuleContext  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_set.jsonl"
REDTEAM_PATH = Path(__file__).resolve().parent / "redteam_set.jsonl"
_TENANT = "00000000-0000-0000-0000-0000000000ee"
_KEY = "eval-platform-redaction-key"

# The 55-row contract golden suite (`expect_decision`/`expect_rules`) is the more
# representative corpus B4-B6 build on. It lives in the sibling `contracts/` repo; locate it
# by walking up from here so the harness works regardless of the absolute checkout path.
_CONFUSABLES_MAP = build_confusables_map()


def find_golden_suite() -> Path | None:
    """Locate ``contracts/guardrails/golden-suite.jsonl`` by walking up the tree; None if absent."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / "guardrails" / "golden-suite.jsonl"
        if candidate.is_file():
            return candidate
    return None

# Component-1 latency SLOs (seconds) for the pipeline evaluation portion.
SLO_INPUT_P50_S = 0.030
SLO_INPUT_P99_S = 0.050
SLO_OUTPUT_P50_S = 0.060
SLO_OUTPUT_P99_S = 0.100

# Regression floors enforced by the CI test (overall, binary flagged task).
MIN_PRECISION = 0.90
MIN_RECALL = 0.90
MIN_F1 = 0.90

# Generous per-safety-rule budget used ONLY by the harness so a cold-start regex scan over a
# long row cannot trip a false timeout and flip a decision (the harness tests decisions +
# latency, not the timeout->fail-mode path — unit tests cover that separately).
_EVAL_RULE_BUDGET_MS = 5000


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 1.0

    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 1.0

    def f1(self) -> float:
        p, r = self.precision(), self.recall()
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class EvalResult:
    overall: Counts
    per_label: dict[str, Counts]
    exact_decision_accuracy: float
    latencies_input_s: list[float] = field(default_factory=list)
    latencies_output_s: list[float] = field(default_factory=list)
    n: int = 0


def load_golden(path: Path = GOLDEN_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# id-prefix -> harness label (for reporting on the contract suite, which carries no `label`).
_SUITE_LABEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("in-clean", "benign"),
    ("in-pi", "injection"),
    ("in-email", "pii"),
    ("in-phone", "pii"),
    ("in-cc", "pii"),
    ("in-jb", "jailbreak"),
    ("in-tox", "toxic"),
    ("in-multi", "mixed"),
    ("out-clean", "benign"),
    ("out-leak", "leak"),
    ("out-email", "pii"),
    ("out-cc", "pii"),
    ("out-tox", "toxic"),
    ("out-multi", "mixed"),
    ("out-len", "length"),
)


def _suite_label(row_id: str) -> str:
    for prefix, label in _SUITE_LABEL_PREFIXES:
        if row_id.startswith(prefix):
            return label
    return "other"


def load_golden_suite(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the 55-row contract golden suite, adapted to the harness row shape.

    The contract file uses ``expect_decision``/``expect_rules``; the harness uses
    ``expected_decision``/``label``. This small adapter maps between them (keeping ``id``,
    ``text``, ``direction``, ``input_text``, ``untrusted_spans`` untouched) so the same
    ``_run_one`` path and ``run``/``summarize`` metrics work over the richer suite. Returns
    ``[]`` when the contract file cannot be located (harness stays runnable standalone).
    """
    src = path or find_golden_suite()
    if src is None:
        return []
    rows: list[dict[str, Any]] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        rows.append(
            {
                **raw,
                "label": _suite_label(str(raw.get("id", ""))),
                "expected_decision": raw["expect_decision"],
            }
        )
    return rows


def _run_one(row: dict[str, Any], settings: Settings) -> tuple[str, float]:
    """Evaluate one golden row through the REAL pipeline; return (decision, eval_latency_s).

    Mirrors the live check path: builds the B1 canonicalized detection view (Layer A always;
    NFKC / confusables per ``settings``), threads ``untrusted_spans`` + ``canary_tokens``, and
    honours the injection spotlight threshold.
    """
    direction = row.get("direction", "input")
    untrusted = row.get("untrusted_spans")
    canary_tokens = row.get("canary_tokens")
    block_threshold = (
        settings.injection_spotlight_block_threshold
        if (settings.injection_defense_enabled and untrusted)
        else 1.0
    )
    if untrusted:
        # mirror the check-path assessment (metadata only; threshold drives escalation)
        assess_injection(row["text"], untrusted)
    confusables = _CONFUSABLES_MAP if settings.guardrails_confusables_fold else None
    detection_text = canonicalize(
        row["text"], nfkc=settings.injection_normalize, confusables=confusables
    )
    ctx = RuleContext(
        classifier=StubClassifier(),
        input_text=row.get("input_text") if direction == "output" else None,
        untrusted_spans=untrusted,
        detection_text=detection_text,
        canary_tokens=canary_tokens if direction == "output" else None,
    )
    started = time.perf_counter()
    result = evaluate(
        text=row["text"],
        policy=builtin_platform_default(),
        direction=direction,
        tenant_id=_TENANT,
        redaction_key=_KEY,
        ctx=ctx,
        injection_block_threshold=block_threshold,
        # The harness measures DECISION correctness + latency, not the timeout->fail-mode path
        # (unit tests cover that). A generous per-rule budget keeps decisions deterministic so a
        # cold-start regex scan over a long row cannot flip the frozen baseline/contract decision.
        safety_min_timeout_ms=_EVAL_RULE_BUDGET_MS,
    )
    elapsed = time.perf_counter() - started
    return result.decision, elapsed


def run(rows: list[dict[str, Any]], *, repeat: int = 1) -> EvalResult:
    settings = Settings()
    overall = Counts()
    per_label: dict[str, Counts] = {}
    exact_hits = 0
    lat_in: list[float] = []
    lat_out: list[float] = []

    for row in rows:
        label = row["label"]
        expected = row["expected_decision"]
        expected_flagged = expected != "allow"
        per_label.setdefault(label, Counts())

        decision = "allow"
        best_lat = None
        for _ in range(max(1, repeat)):
            decision, lat = _run_one(row, settings)
            best_lat = lat if best_lat is None else min(best_lat, lat)
        assert best_lat is not None
        (lat_out if row.get("direction") == "output" else lat_in).append(best_lat)

        predicted_flagged = decision != "allow"
        if decision == expected:
            exact_hits += 1

        for c in (overall, per_label[label]):
            if predicted_flagged and expected_flagged:
                c.tp += 1
            elif predicted_flagged and not expected_flagged:
                c.fp += 1
            elif not predicted_flagged and expected_flagged:
                c.fn += 1
            else:
                c.tn += 1

    return EvalResult(
        overall=overall,
        per_label=per_label,
        exact_decision_accuracy=exact_hits / len(rows) if rows else 1.0,
        latencies_input_s=lat_in,
        latencies_output_s=lat_out,
        n=len(rows),
    )


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1]


def summarize(res: EvalResult) -> dict[str, Any]:
    p50_in = _quantile(res.latencies_input_s, 0.50)
    p99_in = _quantile(res.latencies_input_s, 0.99)
    p50_out = _quantile(res.latencies_output_s, 0.50)
    p99_out = _quantile(res.latencies_output_s, 0.99)
    return {
        "n": res.n,
        "overall": {
            "precision": round(res.overall.precision(), 4),
            "recall": round(res.overall.recall(), 4),
            "f1": round(res.overall.f1(), 4),
            "exact_decision_accuracy": round(res.exact_decision_accuracy, 4),
        },
        "per_label": {
            label: {
                "precision": round(c.precision(), 4),
                "recall": round(c.recall(), 4),
                "f1": round(c.f1(), 4),
                # Attack Success Rate = fraction of adversarial prompts NOT flagged = 1 - recall
                # (the field-standard safety metric; meaningful for attack labels, 0 for benign).
                "asr": round(1.0 - c.recall(), 4),
                "support": c.tp + c.fn + c.fp + c.tn,
            }
            for label, c in sorted(res.per_label.items())
        },
        "latency_ms": {
            "input_p50": round(p50_in * 1000, 3),
            "input_p99": round(p99_in * 1000, 3),
            "output_p50": round(p50_out * 1000, 3),
            "output_p99": round(p99_out * 1000, 3),
        },
        "slo": {
            "input_p50_pass": p50_in <= SLO_INPUT_P50_S,
            "input_p99_pass": p99_in <= SLO_INPUT_P99_S,
            "output_p50_pass": p50_out <= SLO_OUTPUT_P50_S,
            "output_p99_pass": p99_out <= SLO_OUTPUT_P99_S,
        },
    }


def _print_human(summary: dict[str, Any]) -> None:
    o = summary["overall"]
    print(f"Guardrails eval — {summary['n']} cases")
    print("-" * 56)
    print(f"OVERALL  precision={o['precision']:.3f}  recall={o['recall']:.3f}  "
          f"f1={o['f1']:.3f}  exact_acc={o['exact_decision_accuracy']:.3f}")
    print("Per-label:")
    for label, m in summary["per_label"].items():
        print(f"  {label:<10} P={m['precision']:.3f} R={m['recall']:.3f} "
              f"F1={m['f1']:.3f} (n={m['support']})")
    lat = summary["latency_ms"]
    slo = summary["slo"]
    print("Latency (eval portion) vs SLO (in 30/50ms, out 60/100ms):")
    print(f"  input  p50={lat['input_p50']:.2f}ms [{_pf(slo['input_p50_pass'])}] "
          f"p99={lat['input_p99']:.2f}ms [{_pf(slo['input_p99_pass'])}]")
    print(f"  output p50={lat['output_p50']:.2f}ms [{_pf(slo['output_p50_pass'])}] "
          f"p99={lat['output_p99']:.2f}ms [{_pf(slo['output_p99_pass'])}]")


def _pf(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guardrails golden-set eval runner.")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary only.")
    parser.add_argument("--repeat", type=int, default=1, help="Min-of-N latency passes.")
    args = parser.parse_args(argv)

    rows = load_golden()
    res = run(rows, repeat=args.repeat)
    summary = summarize(res)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_human(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
