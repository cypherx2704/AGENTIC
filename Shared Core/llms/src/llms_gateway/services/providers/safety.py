"""Safety classify providers — deterministic STUB default + local safety-model seam.

Honors the platform-wide ``CLASSIFIER_MODE`` default ``stub`` (keyword/deterministic),
mirroring how chat/embeddings select mock vs real:

* :class:`StubSafetyProvider` (DEFAULT, ``CLASSIFIER_MODE=stub``) — a permissive,
  deterministic keyword classifier. With nothing of concern it returns
  ``verdict=allow`` and an empty ``categories`` list — reproducing today's permissive
  behaviour exactly. A small fixed set of deterministic keyword rules can raise the
  verdict (warn/block) and attach per-category scores so the surface is exercisable
  offline / in CI. No model, no keys, no network.

* :class:`LocalSafetyProvider` (``CLASSIFIER_MODE=local``) — the seam for a small safety
  model (Llama Guard / ShieldGemma / Prompt Guard class). It is NOT wired into the
  default image (no heavy model deps are added there); until a model runtime is
  provisioned it raises a clear Contract-2 ``SERVICE_UNAVAILABLE``. Provisioning is a
  later, additive change behind the same flag — the stub default is never affected.

Category names follow the platform convention (``hate``, ``self_harm``, ``pii``,
``jailbreak``, ``prompt_injection``). The verdict maps to enforcement downstream:
``allow`` passes, ``warn`` flags, ``redact`` masks, ``block`` denies.
"""

from __future__ import annotations

import re

from ...core.config import Settings
from ...core.errors import ApiError, ErrorCode
from ...models.unified import (
    ClassifyCategory,
    ClassifyRequest,
    ClassifyResponse,
)
from .base import NonChatProvider, ProviderAdaptor

# Shared in-house provider key (same as the rerank surface).
SAFETY_PROVIDER = "cypherx"

# Deterministic keyword rules: (category, verdict, [compiled patterns]). Evaluated
# case-insensitively. These intentionally mirror the conservative first-cycle Guardrails
# stub posture — high-confidence, low-recall — so the stub stays PERMISSIVE by default
# (clean text -> allow, empty categories). The real recall lives behind CLASSIFIER_MODE=local.
_RULES: list[tuple[str, str, list[re.Pattern[str]]]] = [
    (
        "prompt_injection",
        "block",
        [
            re.compile(r"\bignore\s+(all\s+)?previous\s+instructions\b", re.IGNORECASE),
            re.compile(r"\bdisregard\s+(the\s+)?(instructions|prompt)\s+above\b", re.IGNORECASE),
            re.compile(r"\bforget\s+your\s+previous\s+instructions\b", re.IGNORECASE),
            re.compile(r"\breveal\s+your\s+system\s+prompt\b", re.IGNORECASE),
        ],
    ),
    (
        "jailbreak",
        "block",
        [
            re.compile(r"\bDAN\s+mode\b", re.IGNORECASE),
            re.compile(r"\bdeveloper\s+mode\b", re.IGNORECASE),
            re.compile(r"\bdo\s+anything\s+now\b", re.IGNORECASE),
        ],
    ),
    (
        "self_harm",
        "warn",
        [
            re.compile(r"\bhow\s+to\s+(kill|harm|hurt)\s+(myself|yourself)\b", re.IGNORECASE),
        ],
    ),
    (
        "hate",
        "warn",
        [
            re.compile(r"\bi\s+hate\s+(all\s+)?(of\s+)?(you|them|those)\b", re.IGNORECASE),
        ],
    ),
]

# PII rules surface a `redact` verdict (mask-before-pass) rather than a hard block.
_PII_RULES: list[re.Pattern[str]] = [
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # email
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN-shaped
]

# Verdict severity ordering — the response verdict is the most severe rule that fired.
_VERDICT_RANK: dict[str, int] = {"allow": 0, "warn": 1, "redact": 2, "block": 3}


def _max_verdict(a: str, b: str) -> str:
    return a if _VERDICT_RANK[a] >= _VERDICT_RANK[b] else b


class StubSafetyProvider(NonChatProvider):
    """Deterministic, keyless, permissive-by-default safety classifier (the default)."""

    provider = SAFETY_PROVIDER

    async def classify(self, req: ClassifyRequest, *, model_id: str) -> ClassifyResponse:
        text = req.input
        verdict = "allow"
        categories: list[ClassifyCategory] = []
        scores: dict[str, float] = {}

        for category, rule_verdict, patterns in _RULES:
            if any(p.search(text) for p in patterns):
                verdict = _max_verdict(verdict, rule_verdict)
                # Deterministic high confidence for a fired keyword rule.
                categories.append(ClassifyCategory(name=category, score=0.95))
                scores[category] = 0.95

        if any(p.search(text) for p in _PII_RULES):
            verdict = _max_verdict(verdict, "redact")
            categories.append(ClassifyCategory(name="pii", score=0.9))
            scores["pii"] = 0.9

        return ClassifyResponse(
            verdict=verdict,  # type: ignore[arg-type]
            categories=categories,
            scores=scores or None,
            model=model_id,
        )


class LocalSafetyProvider(NonChatProvider):
    """Seam for a small local safety model. NOT in the default image.

    Selected only by ``CLASSIFIER_MODE=local``. The model (Llama Guard / ShieldGemma /
    Prompt Guard class) pulls heavy deps kept OUT of the default build, so until a model
    runtime is provisioned this raises a clear Contract-2 503 rather than silently
    degrading. Wiring an actual model is a later, additive change behind this same flag —
    the stub default is never affected.
    """

    provider = SAFETY_PROVIDER

    def __init__(self, settings: Settings) -> None:
        self._model_id = settings.classifier_local_model

    async def classify(self, req: ClassifyRequest, *, model_id: str) -> ClassifyResponse:
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "The local safety classifier is not provisioned in this image "
            "(set CLASSIFIER_MODE=stub for the deterministic classifier).",
            status_code=503,
            details={
                "reason": "CLASSIFIER_LOCAL_UNAVAILABLE",
                "configured_model": self._model_id,
            },
        )


def get_safety_provider(settings: Settings) -> ProviderAdaptor:
    """Select the safety classifier per ``CLASSIFIER_MODE`` (default 'stub')."""
    if settings.classifier_mode == "local":
        return LocalSafetyProvider(settings)
    return StubSafetyProvider()
