"""Real LLM provider (vendor-neutral seam) + on-disk pin/replay cache."""

from __future__ import annotations

import os

import pytest

from bkg.ai import AiCache, AiProposal, LlmProvider, input_hash, propose_for_endpoints
from bkg.service import GraphService

_FACTS = {
    "id": "e",
    "method": "GET",
    "resolved_path": "/x",
    "handler": "list_things",
    "handler_file": "a.py",
    "handler_line": 3,
    "response": None,
}


def test_llm_proposal_is_capped_tagged_and_cited(fastapi_sources: dict[str, str]) -> None:
    provider = LlmProvider(complete=lambda prompt: "a paginated list of orders")
    endpoints = GraphService.from_sources(fastapi_sources).list_endpoints()  # gaps: no response_model
    proposals = propose_for_endpoints(endpoints, provider, AiCache())
    assert proposals
    for plist in proposals.values():
        for p in plist:
            assert p.source == "ai"
            assert p.confidence == "ai-inferred"  # capped
            assert p.value == "a paginated list of orders"
            assert ":" in p.citation


def test_llm_empty_completion_yields_no_proposal() -> None:
    provider = LlmProvider(complete=lambda prompt: "   ")
    assert provider.analyze("response_shape", _FACTS) == []


def test_llm_model_change_changes_the_content_address() -> None:
    p1 = LlmProvider(complete=lambda p: "x", model_id="claude-opus-4-8")
    p2 = LlmProvider(complete=lambda p: "x", model_id="claude-sonnet-5")
    assert input_hash(p1, "response_shape", _FACTS) != input_hash(p2, "response_shape", _FACTS)


def test_llm_output_is_pin_replayed_not_recalled(fastapi_sources: dict[str, str]) -> None:
    endpoints = GraphService.from_sources(fastapi_sources).list_endpoints()
    calls = {"n": 0}

    def complete(prompt: str) -> str:
        calls["n"] += 1
        return "the response body"

    provider = LlmProvider(complete=complete)
    cache = AiCache()
    propose_for_endpoints(endpoints, provider, cache)
    first = calls["n"]
    assert first > 0
    propose_for_endpoints(endpoints, provider, cache)  # same slices -> replay
    assert calls["n"] == first  # the model was NOT re-invoked


def test_disk_cache_persists_across_instances(tmp_path) -> None:
    path = str(tmp_path / "aicache")
    proposals = [AiProposal(kind="k", subject="s", value="v", citation="a.py:1", input_hash="h")]
    AiCache(path=path).put("deadbeef", proposals)

    reopened = AiCache(path=path)  # fresh in-memory tier, same disk
    got = reopened.get("deadbeef")
    assert got is not None
    assert got[0].value == "v"


def test_sealed_disk_cache_miss_fails_closed(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="sealed replay"):
        AiCache(sealed=True, path=str(tmp_path / "c")).get("beef")


def test_sealed_cache_serves_a_genuine_disk_hit(tmp_path) -> None:
    path = str(tmp_path / "c")
    key = "b" * 64
    AiCache(path=path).put(
        key, [AiProposal(kind="k", subject="s", value="v", citation="a.py:1", input_hash="h")]
    )
    got = AiCache(sealed=True, path=path).get(key)  # sealed, but the artifact is on disk
    assert got is not None
    assert got[0].value == "v"


def test_corrupt_disk_artifact_self_heals(tmp_path) -> None:
    path = str(tmp_path / "c")
    key = "a" * 64
    cache = AiCache(path=path)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, f"v1-{key}.json"), "w", encoding="utf-8") as f:
        f.write("{ not valid json")
    assert cache.get(key) is None  # corrupt -> treated as a miss, not a crash
    with pytest.raises(RuntimeError, match="sealed replay"):  # sealed still fails closed
        AiCache(sealed=True, path=path).get(key)


def test_invalid_cache_key_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        AiCache(path=str(tmp_path / "c")).put("../evil", [])


def test_generation_config_changes_the_content_address() -> None:
    small = LlmProvider(complete=lambda p: "x", config={"max_tokens": 64})
    big = LlmProvider(complete=lambda p: "x", config={"max_tokens": 1024})
    assert input_hash(small, "response_shape", _FACTS) != input_hash(big, "response_shape", _FACTS)
