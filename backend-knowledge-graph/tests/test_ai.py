"""AI-proposer invariants: capped + tagged + cited, never overrides static facts,
gap-triggered, and deterministic-by-pinning (with a fail-closed sealed mode)."""

from __future__ import annotations

from typing import Any

import pytest

from bkg.ai import AiCache, AiProposal, HeuristicProvider, input_hash, propose_for_endpoints
from bkg.service import GraphService


def test_proposal_is_capped_tagged_and_cited(fastapi_sources: dict[str, str]) -> None:
    # fastapi_sources endpoints have no response_model -> they are gaps
    proposed = GraphService.from_sources(fastapi_sources).propose_gaps()
    with_ai = [e for e in proposed if "ai_proposals" in e]
    assert with_ai
    for endpoint in with_ai:
        for p in endpoint["ai_proposals"]:
            assert p["source"] == "ai"
            assert p["confidence"] == "ai-inferred"  # capped — a labeled opinion
            assert p["verification_status"] == "unverified"
            assert ":" in p["citation"]  # file:line anchor
            assert p["input_hash"]


def test_ai_never_overrides_static_facts(fastapi_sources: dict[str, str]) -> None:
    base = {e["id"]: e for e in GraphService.from_sources(fastapi_sources).list_endpoints()}
    enriched = {e["id"]: e for e in GraphService.from_sources(fastapi_sources).propose_gaps()}
    for eid, static in base.items():
        for key, value in static.items():
            assert enriched[eid][key] == value  # every static field is byte-for-byte unchanged
        assert set(enriched[eid]) - set(static) <= {"ai_proposals"}  # AI lives only in a separate field


def test_only_gaps_get_proposals(fastapi_dto_sources: dict[str, str]) -> None:
    # these endpoints resolve response_model=UserOut -> not gaps -> no proposals
    proposed = GraphService.from_sources(fastapi_dto_sources).propose_gaps()
    assert all("ai_proposals" not in e for e in proposed)


class _OnceProvider:
    """Raises if asked to analyze the same slice twice — proves the cache replays."""

    id = "once@1"

    def __init__(self) -> None:
        self.calls = 0

    def model(self) -> dict[str, Any]:
        return {"v": 1}

    def analyze(self, task: str, facts: dict[str, Any]) -> list[AiProposal]:
        self.calls += 1
        return [
            AiProposal(
                kind="x",
                subject=facts["id"],
                value="v",
                citation=f"{facts['handler_file']}:{facts['handler_line']}",
                input_hash=input_hash(self, task, facts),
            )
        ]


def test_pin_replay_avoids_re_invoking_the_model(fastapi_sources: dict[str, str]) -> None:
    endpoints = GraphService.from_sources(fastapi_sources).list_endpoints()
    provider = _OnceProvider()
    cache = AiCache()

    propose_for_endpoints(endpoints, provider, cache)
    calls_after_first = provider.calls
    assert calls_after_first > 0

    propose_for_endpoints(endpoints, provider, cache)  # same slices -> replay from cache
    assert provider.calls == calls_after_first  # the model was NOT re-invoked


def test_sealed_mode_fails_closed_on_a_miss() -> None:
    with pytest.raises(RuntimeError, match="sealed replay"):
        AiCache(sealed=True).get("no-such-hash")


def test_provider_confidence_is_capped_for_facts() -> None:
    # the reference provider only ever emits ai-inferred (never static-certain/etc.)
    provider = HeuristicProvider()
    facts = {"id": "e", "method": "GET", "handler": "list_users", "handler_file": "a.py", "handler_line": 1}
    proposals = provider.analyze("response_shape", facts)
    assert proposals and all(p.confidence == "ai-inferred" and p.source == "ai" for p in proposals)
