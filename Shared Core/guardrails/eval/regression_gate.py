"""Decision-flip / negative-flip regression gate (B4).

Aggregate P/R/F1 can stay >= 0.90 while individual HIGH-STAKES decisions silently regress
when a tenant edits a custom regex/threshold, someone tunes an escalation band, or
``CLASSIFIER_MODE`` is flipped — the negative-flip literature (Positive-Congruent Training,
MUSCLE, FlipGuard) shows overall error can improve while per-example predictions regress.
This module freezes the allow/warn/redact/block decision for every row of the 55-row
contract golden suite into a checked-in baseline (``eval/baseline_decisions.json``) and, on
every CI run, diffs the live decisions against it:

  * **Negative flip (safety-critical downgrade)** — ``block``/``redact`` -> a strictly less
    strict decision (e.g. ``block``->``allow``, ``redact``->``allow``). These MUST equal 0
    (hard fail): one loosened pattern re-opening a jailbreak class is exactly what the
    mean-F1 floor cannot see.
  * **Benign flip (false-positive upgrade)** — ``allow``/``warn`` -> a stricter decision.
    Allowed within an explicit BUDGET (a few over-blocks are tolerable; a flood is not).

CI/eval-only, zero hot-path cost — it reuses the deterministic in-process ``pipeline.evaluate``
via ``run_eval._run_one`` over the frozen suite.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import run_eval  # noqa: E402

from guardrails_service.core.config import Settings  # noqa: E402

BASELINE_PATH = Path(__file__).resolve().parent / "baseline_decisions.json"

# Decision strictness order (matches the pipeline precedence ALLOW < WARN < REDACT < BLOCK).
_STRICTNESS: dict[str, int] = {"allow": 0, "warn": 1, "redact": 2, "block": 3}

# Baselines that are safety-critical: a downgrade FROM one of these is a negative flip.
_SAFETY_BASELINES = frozenset({"block", "redact"})

# Default budget for benign allow/warn -> stricter false-positive flips (0 = none tolerated
# by default; callers/tests can raise it explicitly).
DEFAULT_BENIGN_FLIP_BUDGET = 0


@dataclass
class Flip:
    row_id: str
    baseline: str
    current: str

    @property
    def is_negative(self) -> bool:
        """A safety-critical downgrade (block/redact -> strictly less strict)."""
        return (
            self.baseline in _SAFETY_BASELINES
            and _STRICTNESS.get(self.current, 0) < _STRICTNESS.get(self.baseline, 0)
        )

    @property
    def is_benign_upgrade(self) -> bool:
        """A benign row (allow/warn baseline) that became STRICTER (a false-positive flip)."""
        return (
            self.baseline not in _SAFETY_BASELINES
            and _STRICTNESS.get(self.current, 0) > _STRICTNESS.get(self.baseline, 0)
        )


@dataclass
class FlipReport:
    negative_flips: list[Flip] = field(default_factory=list)
    benign_flips: list[Flip] = field(default_factory=list)
    other_flips: list[Flip] = field(default_factory=list)
    total_rows: int = 0

    @property
    def negative_flip_rate(self) -> float:
        return len(self.negative_flips) / self.total_rows if self.total_rows else 0.0

    def to_dict(self) -> dict[str, Any]:
        def _fmt(flips: list[Flip]) -> list[dict[str, str]]:
            return [{"id": f.row_id, "from": f.baseline, "to": f.current} for f in flips]

        return {
            "total_rows": self.total_rows,
            "negative_flip_rate": round(self.negative_flip_rate, 4),
            "negative_flips": _fmt(self.negative_flips),
            "benign_flips": _fmt(self.benign_flips),
            "other_flips": _fmt(self.other_flips),
        }


def compute_decisions(rows: list[dict[str, Any]], settings: Settings | None = None) -> dict[str, str]:
    """Return ``{row_id: decision}`` by running each row through the real pipeline."""
    settings = settings or Settings()
    out: dict[str, str] = {}
    for row in rows:
        decision, _ = run_eval._run_one(row, settings)
        out[str(row["id"])] = decision
    return out


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("decisions", data))


def save_baseline(decisions: dict[str, str], path: Path = BASELINE_PATH) -> None:
    payload = {
        "_comment": (
            "Frozen allow/warn/redact/block decision per contract golden-suite row (B4). "
            "Regenerate with `python eval/regression_gate.py --update` ONLY on an INTENDED "
            "decision change; a safety-critical downgrade must never be baselined silently."
        ),
        "decisions": dict(sorted(decisions.items())),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def diff_decisions(baseline: dict[str, str], current: dict[str, str]) -> FlipReport:
    """Classify every changed decision into negative / benign / other flips."""
    report = FlipReport(total_rows=len(current))
    for row_id, cur in current.items():
        base = baseline.get(row_id)
        if base is None or base == cur:
            continue
        flip = Flip(row_id=row_id, baseline=base, current=cur)
        if flip.is_negative:
            report.negative_flips.append(flip)
        elif flip.is_benign_upgrade:
            report.benign_flips.append(flip)
        else:
            report.other_flips.append(flip)
    return report


def run_gate(
    *,
    rows: list[dict[str, Any]] | None = None,
    baseline: dict[str, str] | None = None,
    benign_flip_budget: int = DEFAULT_BENIGN_FLIP_BUDGET,
) -> tuple[bool, FlipReport]:
    """Run the gate. Returns (passed, report).

    Fails when ANY negative (safety-critical downgrade) flip is present, or when benign
    false-positive flips exceed ``benign_flip_budget``. Rows default to the contract suite;
    baseline defaults to the checked-in frozen decisions.
    """
    rows = rows if rows is not None else run_eval.load_golden_suite()
    baseline = baseline if baseline is not None else load_baseline()
    current = compute_decisions(rows)
    report = diff_decisions(baseline, current)
    passed = not report.negative_flips and len(report.benign_flips) <= benign_flip_budget
    return passed, report


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Decision-flip regression gate (B4).")
    parser.add_argument(
        "--update", action="store_true", help="Regenerate the frozen baseline from current decisions."
    )
    parser.add_argument("--budget", type=int, default=DEFAULT_BENIGN_FLIP_BUDGET)
    args = parser.parse_args(argv)

    rows = run_eval.load_golden_suite()
    if not rows:
        print("golden suite not found; nothing to gate.")
        return 0
    current = compute_decisions(rows)
    if args.update:
        save_baseline(current)
        print(f"baseline written: {len(current)} rows -> {BASELINE_PATH}")
        return 0

    report = diff_decisions(load_baseline(), current)
    print(json.dumps(report.to_dict(), indent=2))
    passed = not report.negative_flips and len(report.benign_flips) <= args.budget
    if not passed:
        print("REGRESSION GATE: FAIL")
        return 1
    print("REGRESSION GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
