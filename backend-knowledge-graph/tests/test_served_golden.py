"""Served-output regression net for the PartialGraph engine migration (Step 6).

The migration replaces the internal parser boundary (``Facts`` dict -> ``PartialGraph``)
and rewrites the pipeline's cross-file resolution, but the SERVED output — assembled
endpoints, schemas, config, trust summary, blast radius — must stay byte-for-byte
identical (it is the product contract). ``tests/golden_served.json`` was captured from
the pre-migration pipeline; this test asserts the current pipeline still reproduces it.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from bkg.service import GraphService
from parity_corpus import PROJECTS
from test_pipeline_oracle import DtoWorld, _edit

_GOLDEN = json.loads((Path(__file__).parent / "golden_served.json").read_text(encoding="utf-8"))


def _served(sources: dict[str, str]) -> dict[str, object]:
    svc = GraphService.from_sources(sources)
    return {
        "endpoints": svc.list_endpoints(),
        "schemas": svc.list_schemas(),
        "config": svc.list_config(),
        "trust": svc.trust_summary(),
    }


def _dto_sources(seed: int, steps: int = 8) -> dict[str, str]:
    rng = random.Random(seed)
    world = DtoWorld()
    for _ in range(steps):
        _edit(rng, world)
    return world.sources()


@pytest.mark.parametrize("name,sources", PROJECTS, ids=[n for n, _ in PROJECTS])
def test_served_output_matches_golden(name: str, sources: dict[str, str]) -> None:
    assert _served(sources) == _GOLDEN[name]


@pytest.mark.parametrize("seed", range(12))
def test_served_output_matches_golden_fuzz(seed: int) -> None:
    assert _served(_dto_sources(seed)) == _GOLDEN[f"dto_seed{seed}"]


def test_blast_radius_matches_golden() -> None:
    svc = GraphService.from_sources(dict(PROJECTS)["full_depth"])
    assert svc.blast_radius("app/schemas.py:UserBase") == _GOLDEN["_blast"]["full_depth/UserBase"]
    assert svc.blast_radius("app/schemas.py:UserCreate") == _GOLDEN["_blast"]["full_depth/UserCreate"]
