"""The eval/ harness is wired + composite scoring measurably beats cosine on the golden set.

Imports the runner directly (no subprocess) and asserts the recall@k / helpfulness deltas,
so the improvement is regression-guarded by CI.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"


def _load_runner():  # type: ignore[no-untyped-def]
    name = "memory_eval_runner"
    spec = importlib.util.spec_from_file_location(name, _EVAL_DIR / "run_eval.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec so dataclasses defined in the module can resolve cls.__module__.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_composite_beats_cosine_on_golden_set() -> None:
    runner = _load_runner()
    golden = _EVAL_DIR / "golden_memories.json"
    assert golden.exists()
    cosine, composite = await runner.run(golden)

    # The golden set is engineered so pure cosine picks the stale distractor every time.
    assert cosine.recall_at_k < composite.recall_at_k
    assert composite.recall_at_k == 1.0
    assert composite.helpfulness >= cosine.helpfulness


@pytest.mark.asyncio
async def test_composite_never_regresses() -> None:
    runner = _load_runner()
    cosine, composite = await runner.run(_EVAL_DIR / "golden_memories.json")
    assert composite.recall_at_k >= cosine.recall_at_k
