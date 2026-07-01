"""Real classifier seam: confidence-banded remote cascade (services.classifier_client).

The default CLASSIFIER_MODE=stub keeps everything keyless and in-process; the remote
gateway is consulted ONLY for the uncertain band, and any remote error falls back to the
stub. These tests exercise the band logic + mapping + fail-soft behaviour WITHOUT a real
gateway (the httpx call is monkeypatched / the band short-circuits before any call).
"""

from __future__ import annotations

import pytest

from guardrails_service.core.config import Settings
from guardrails_service.services.classifier import Category, StubClassifier
from guardrails_service.services.classifier_client import (
    RemoteSafetyClassifier,
    build_remote_classifier,
    is_remote_mode,
)


def _settings(**over: object) -> Settings:
    return Settings(**over)  # type: ignore[arg-type]


def test_is_remote_mode_only_for_non_stub_non_detoxify() -> None:
    assert is_remote_mode(_settings(classifier_mode="llms_gateway"))
    assert not is_remote_mode(_settings(classifier_mode="stub"))
    assert not is_remote_mode(_settings(classifier_mode="detoxify"))


def test_sync_classify_is_stub_only() -> None:
    clf = build_remote_classifier(StubClassifier(), _settings(classifier_mode="llms_gateway"))
    # The synchronous interface NEVER hits the network — stub categories only.
    assert clf.classify("you are subhuman vermin")  # stub fires hate
    assert clf.classify("what is 2 + 2?") == []


@pytest.mark.asyncio
async def test_confident_toxic_short_circuits_no_remote() -> None:
    # The stub scores hate at 0.9 (>= escalate_high 0.85) -> confidently toxic, no remote.
    clf = build_remote_classifier(StubClassifier(), _settings(classifier_mode="llms_gateway"))
    out = await clf.classify_remote("i hate you, subhuman", "input")
    assert out.used_remote is False
    assert out.fell_back is False
    assert any(c.label == "hate" for c in out.categories)


@pytest.mark.asyncio
async def test_confident_benign_short_circuits_no_remote() -> None:
    clf = build_remote_classifier(StubClassifier(), _settings(classifier_mode="llms_gateway"))
    out = await clf.classify_remote("what is the capital of France?", "input")
    assert out.used_remote is False
    assert out.categories == []


@pytest.mark.asyncio
async def test_uncertain_band_escalates_and_merges_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force an uncertain stub band by stubbing the inner classifier to a mid score.
    settings = _settings(classifier_mode="llms_gateway", classifier_remote_threshold=0.5)

    class _MidStub(StubClassifier):
        def classify(self, text: str) -> list[Category]:  # type: ignore[override]
            return [Category("hate", 0.5)]  # 0.5 is inside [0.30, 0.85)

    clf = RemoteSafetyClassifier(_MidStub(), settings)

    async def _fake_gateway(self: RemoteSafetyClassifier, text: str, direction: str):
        return [Category("threat", 0.95)], "block"

    monkeypatch.setattr(RemoteSafetyClassifier, "_call_gateway", _fake_gateway)
    out = await clf.classify_remote("borderline content", "input")
    assert out.used_remote is True
    assert out.fell_back is False
    labels = {c.label for c in out.categories}
    assert "hate" in labels and "threat" in labels  # stub + remote unioned
    assert out.verdict == "block"


@pytest.mark.asyncio
async def test_remote_error_falls_back_to_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(classifier_mode="llms_gateway")

    class _MidStub(StubClassifier):
        def classify(self, text: str) -> list[Category]:  # type: ignore[override]
            return [Category("hate", 0.5)]

    clf = RemoteSafetyClassifier(_MidStub(), settings)

    async def _boom(self: RemoteSafetyClassifier, text: str, direction: str):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(RemoteSafetyClassifier, "_call_gateway", _boom)
    out = await clf.classify_remote("borderline content", "input")
    assert out.used_remote is True
    assert out.fell_back is True
    # Fail-soft: the stub categories survive (remote can only ADD risk, never suppress).
    assert any(c.label == "hate" for c in out.categories)


def test_map_response_threshold_and_aliases() -> None:
    clf = RemoteSafetyClassifier(StubClassifier(), _settings(classifier_remote_threshold=0.5))
    cats = clf._map_response(
        {
            "verdict": "block",
            "categories": [
                {"name": "identity_attack", "score": 0.9},  # -> hate
                {"name": "toxicity", "score": 0.2},          # below threshold -> dropped
            ],
            "scores": {"violence": 0.8},                      # -> threat
            "extra_unknown_field": 123,                       # additive tolerance
        }
    )
    labels = {c.label for c in cats}
    assert "hate" in labels
    assert "threat" in labels
    assert "toxicity" not in labels
