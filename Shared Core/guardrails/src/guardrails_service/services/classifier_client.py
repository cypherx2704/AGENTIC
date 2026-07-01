"""Remote classifier client + confidence-banded cascade (ADDITIVE, flagged).

The guardrails cascade for the toxicity rules is:

    cache -> regex -> RAG-sim -> small-LLM -> large-LLM -> HITL

The in-process :class:`StubClassifier` (keyword/lexicon heuristic) is the keyless
default and stays the regex/local stage. This module adds the SMALL/LARGE-LLM stage as a
REMOTE call to the llms-gateway ``POST /v1/classify`` surface, consulted ONLY when:

  * ``CLASSIFIER_MODE`` selects a remote transport (``classifier_mode != 'stub'`` and
    ``!= 'detoxify'`` — i.e. ``'llms_gateway'``), AND
  * the local stub signal is in the UNCERTAIN confidence band (latency guard): a clearly
    benign max-score (< ``classifier_escalate_low``) short-circuits as benign, and a
    clearly toxic score (>= ``classifier_escalate_high``) short-circuits as toxic — both
    WITHOUT a remote round-trip.

Only the ``[low, high)`` band escalates, so escalation is rare and the SLOs hold. The
remote call has its OWN short timeout; on ANY error/timeout the cascade FALLS BACK to the
stub categories (fail-closed safety is preserved — the stub still surfaces what it
detected). With the default ``CLASSIFIER_MODE=stub`` nothing escalates and the classifier
is byte-identical to today (no network, no new dependency surface on the hot path).

The wire DTO mirrors the gateway's ``ClassifyRequest`` / ``ClassifyResponse``
(``contracts/api/classify.schema.json``): request ``{input, direction, context?}``,
response ``{verdict, categories:[{name,score}], scores?, model}``. The client is tolerant
of ADDITIONAL response fields (additive contract discipline) — it reads only what it needs.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

from ..core import trace
from ..core.config import Settings
from .classifier import HATE, SELF_HARM, THREAT, Category, Classifier

logger = structlog.get_logger(__name__)

# Transport name selected by CLASSIFIER_MODE for the remote seam.
REMOTE_MODE_LLMS_GATEWAY = "llms_gateway"

# Map the gateway's safety category names onto our in-process taxonomy. Unknown names are
# kept as-is (additive tolerance) so a new gateway category still surfaces as a hit.
_REMOTE_CATEGORY_MAP: dict[str, str] = {
    "hate": HATE,
    "hate_speech": HATE,
    "harassment": HATE,
    "identity_attack": HATE,
    "toxicity": HATE,
    "severe_toxicity": HATE,
    "violence": THREAT,
    "threat": THREAT,
    "self_harm": SELF_HARM,
    "self-harm": SELF_HARM,
    "suicide": SELF_HARM,
}


@dataclass(frozen=True)
class RemoteClassifyOutcome:
    """Result of a remote classify attempt (used by the cascade + metrics/tests)."""

    categories: list[Category]
    used_remote: bool          # did we actually call the gateway?
    fell_back: bool            # did a remote error force a stub fallback?
    verdict: str | None = None  # the gateway verdict, when available


def _band(score: float, settings: Settings) -> str:
    """Classify a stub max-score into 'benign' | 'uncertain' | 'toxic' bands."""
    if score < settings.classifier_escalate_low:
        return "benign"
    if score >= settings.classifier_escalate_high:
        return "toxic"
    return "uncertain"


class RemoteSafetyClassifier(Classifier):
    """Confidence-banded classifier: stub-first, remote gateway only for the uncertain band.

    Implements the same :class:`Classifier` interface as the stub so it drops into the
    existing :class:`RuleContext` seam with NO pipeline change. ``classify`` is synchronous
    to match the interface; the (async) remote call is bridged by the caller through
    :meth:`classify_with_context` when an event loop is available. The plain synchronous
    :meth:`classify` ALWAYS returns the stub result (so any non-async caller is safe and
    keyless); the async path is what the check handler uses on the hot path.
    """

    def __init__(self, stub: Classifier, settings: Settings) -> None:
        self._stub = stub
        self._settings = settings

    @property
    def ready(self) -> bool:
        # The stub is always ready; the remote is best-effort (fail-soft), so readiness
        # reflects the always-ready local stage — the service never hard-fails on the
        # gateway being unreachable (mirrors the detoxify graceful-fallback posture).
        return self._stub.ready

    def classify(self, text: str) -> list[Category]:
        """Synchronous interface fallback: stub-only (no network on a sync path)."""
        return self._stub.classify(text)

    def _stub_max_score(self, cats: list[Category]) -> float:
        return max((c.score for c in cats), default=0.0)

    async def classify_remote(self, text: str, direction: str) -> RemoteClassifyOutcome:
        """Run the stub, then escalate to the gateway ONLY for the uncertain band.

        Returns the categories to apply plus provenance flags. Fail-soft: any remote
        error/timeout returns the stub categories with ``fell_back=True``.
        """
        stub_cats = self._stub.classify(text)
        max_score = self._stub_max_score(stub_cats)
        band = _band(max_score, self._settings)

        # Latency guard: confidently benign or confidently toxic -> no remote round-trip.
        if band != "uncertain":
            return RemoteClassifyOutcome(categories=stub_cats, used_remote=False, fell_back=False)

        try:
            remote_cats, verdict = await self._call_gateway(text, direction)
        except Exception as exc:  # noqa: BLE001 — remote is best-effort; fall back to stub
            logger.warning("remote_classify_failed", error=str(exc), direction=direction)
            return RemoteClassifyOutcome(categories=stub_cats, used_remote=True, fell_back=True)

        # Union the stub + remote categories (de-dup by label, keep the higher score) so the
        # remote NEVER suppresses a stub detection (fail-closed: remote can only ADD risk).
        merged: dict[str, Category] = {c.label: c for c in stub_cats}
        for c in remote_cats:
            existing = merged.get(c.label)
            if existing is None or c.score > existing.score:
                merged[c.label] = c
        return RemoteClassifyOutcome(
            categories=list(merged.values()), used_remote=True, fell_back=False, verdict=verdict
        )

    async def _call_gateway(self, text: str, direction: str) -> tuple[list[Category], str]:
        """Call llms-gateway ``POST /v1/classify`` and map the response onto categories."""
        settings = self._settings
        url = settings.llms_gateway_url.rstrip("/") + "/v1/classify"
        wire_direction = "output" if direction == "output" else "input"
        payload = {
            "input": text,
            "direction": wire_direction,
            "model": settings.classifier_remote_model,
            "context": {"source": "guardrails"},
        }
        # Forward correlation so the gateway's usage rows tie back to this check.
        headers = {
            "X-Request-ID": trace.request_id_var.get() or "",
            "Content-Type": "application/json",
        }
        timeout = settings.classifier_remote_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers={k: v for k, v in headers.items() if v})
            resp.raise_for_status()
            data = resp.json()
        return self._map_response(data), str(data.get("verdict", ""))

    def _map_response(self, data: dict) -> list[Category]:
        """Map a ClassifyResponse dict onto our Category list (threshold-filtered).

        Tolerant of additional fields (additive contract). Categories at/above
        ``classifier_remote_threshold`` are surfaced; the gateway's flat ``scores`` map is
        also honoured when present.
        """
        threshold = self._settings.classifier_remote_threshold
        out: dict[str, Category] = {}
        cats = data.get("categories")
        if isinstance(cats, list):
            for entry in cats:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                score = entry.get("score")
                if not isinstance(name, str) or not isinstance(score, (int, float)):
                    continue
                if float(score) >= threshold:
                    label = _REMOTE_CATEGORY_MAP.get(name.lower(), name.lower())
                    out[label] = Category(label, float(score))
        scores = data.get("scores")
        if isinstance(scores, dict):
            for name, score in scores.items():
                if isinstance(name, str) and isinstance(score, (int, float)) and float(score) >= threshold:
                    label = _REMOTE_CATEGORY_MAP.get(name.lower(), name.lower())
                    existing = out.get(label)
                    if existing is None or float(score) > existing.score:
                        out[label] = Category(label, float(score))
        return list(out.values())


def is_remote_mode(settings: Settings) -> bool:
    """True if CLASSIFIER_MODE selects the remote gateway transport."""
    return settings.classifier_mode not in ("stub", "detoxify")


def build_remote_classifier(stub: Classifier, settings: Settings) -> RemoteSafetyClassifier:
    """Wrap a (stub) classifier with the confidence-banded remote cascade."""
    return RemoteSafetyClassifier(stub, settings)
