"""The B1/B4/B5/B6/B7 eval-harness extensions are wired + each measurably shows its effect.

Imports the runner directly (no subprocess) and asserts the deltas, so every feature's
offline demonstration is regression-guarded by CI alongside the composite guard.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"


def _load_runner():  # type: ignore[no-untyped-def]
    name = "memory_eval_runner_ext"
    spec = importlib.util.spec_from_file_location(name, _EVAL_DIR / "run_eval.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _golden(runner):  # type: ignore[no-untyped-def]
    return runner._read_golden(_EVAL_DIR / "golden_memories.json")


def test_b1_float16_recall_delta_is_zero() -> None:
    runner = _load_runner()
    r = runner.float16_recall_delta(_golden(runner))
    assert r["recall_f32"] == 1.0          # self-retrieval baseline is meaningful
    assert abs(r["delta"]) < 1e-9          # halfvec (float16) leaves recall unchanged


@pytest.mark.asyncio
async def test_b4_actr_frequency_breaks_the_tie() -> None:
    runner = _load_runner()
    r = await runner.run_frequency_actr(_golden(runner))
    assert r["cosine_recall"] == 0.0       # cosine/exponential tie -> the rare one-off wins
    assert r["exponential_recall"] == 0.0
    assert r["actr_recall"] == 1.0         # power_actr promotes the high-frequency memory


@pytest.mark.asyncio
async def test_b5_extraction_beats_the_averaged_blob() -> None:
    runner = _load_runner()
    r = await runner.run_extraction(_golden(runner))
    assert r["extracted_sim"] > r["blob_sim"]       # isolated fact less diluted than the blob
    assert r["extracted_top1_over_blob"] == 1.0     # focused fact outranks the blob


@pytest.mark.asyncio
async def test_b6_mmr_improves_coverage_and_diversity() -> None:
    runner = _load_runner()
    r = await runner.run_mmr(_golden(runner))
    assert r["mmr_coverage"] > r["cosine_coverage"]
    assert r["mmr_ilad"] > r["cosine_ilad"]


@pytest.mark.asyncio
async def test_b7_link_expansion_recovers_the_multi_hop_target() -> None:
    runner = _load_runner()
    r = await runner.run_linking(_golden(runner))
    assert r["vector_only_recall"] == 0.0
    assert r["expanded_recall"] == 1.0
