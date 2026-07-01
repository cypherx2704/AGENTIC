"""Regression-gate the eval/ harness metrics in CI (network-free).

The eval harness (eval/run_eval.py) MEASURES the Phase KG accuracy wins against the golden
set. This test pins those metrics so a regression that lowers schema-rejection precision,
schema recall, the confidence-floor accuracy, or coreference accuracy fails CI."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_RUN_EVAL = Path(__file__).resolve().parent.parent / "eval" / "run_eval.py"


def _load_runner():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("cxa1_eval_run", _RUN_EVAL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_eval_harness_passes_all_targets() -> None:
    summary = _load_runner().run()
    assert summary["passed"], summary["metrics"]
    for key, target in summary["targets"].items():
        assert summary["metrics"][key] >= target, (key, summary["metrics"][key], target)


def test_eval_extraction_no_hallucination_leak() -> None:
    mod = _load_runner()
    ex = mod.eval_extraction()
    # Not a single off-schema / below-floor edge survived the schema+drop pass.
    assert ex["kept_false_positive"] == 0
    assert ex["schema_rejection_precision"] == 1.0


def test_eval_coref_no_misses() -> None:
    co = _load_runner().eval_coref()
    assert co["misses"] == []
