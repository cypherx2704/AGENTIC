"""Microsoft Presidio PII analyzer (OPTIONAL dep + flag; default OFF).

When ``GUARDRAILS_PII_PRESIDIO`` is on, the Presidio analyzer runs BEFORE the existing
regex/HMAC redaction to lift PII RECALL (it locates names, locations, IBANs, SSNs, … the
regexes miss). Presidio ONLY locates spans — the deterministic ``[REDACTED:cat:hex8]`` HMAC
token rendering is unchanged (the pipeline renders both regex and Presidio spans identically).

GRACEFUL DEGRADATION: the ``presidio-analyzer`` package is an OPTIONAL extra (``pii`` extra).
When it is not installed, or its spaCy model is missing, :func:`build_presidio_analyzer`
returns ``None`` (logged) and the check path runs the current regex-only path — so keyless
dev, CI, and the default image are byte-identical to today. The flag defaulting OFF means
the analyzer is not even constructed unless an operator opts in.

Presidio entity types are mapped onto our redaction categories
(``PII_CATEGORIES`` in core.redaction). Unmapped entity types are surfaced under the generic
``pii`` category (still redacted, still a token) so a newly-recognised type is never dropped.
"""

from __future__ import annotations

import importlib.util

import structlog

from ..core.config import Settings

logger = structlog.get_logger(__name__)

# Presidio entity type -> our redaction category. Unmapped types fall back to 'pii'.
_ENTITY_CATEGORY_MAP: dict[str, str] = {
    "EMAIL_ADDRESS": "email",
    "PHONE_NUMBER": "phone",
    "CREDIT_CARD": "credit_card",
    "US_SSN": "ssn",
    "PERSON": "name",
    "US_PASSPORT": "passport",
}
_DEFAULT_CATEGORY = "pii"


def presidio_available() -> bool:
    """True if the optional ``presidio-analyzer`` dependency is importable."""
    return importlib.util.find_spec("presidio_analyzer") is not None


class PresidioPiiAnalyzer:
    """Thin wrapper over Presidio's ``AnalyzerEngine`` that returns ``(text, category)`` spans.

    The heavy ``AnalyzerEngine`` (and its spaCy NLP pipeline) is constructed ONCE at build
    time (lifespan) and reused; :meth:`analyze` is a pure read so it is safe to call on the
    hot path. Any analyze error degrades to an empty span list (never fails a check).
    """

    def __init__(self, engine: object, settings: Settings) -> None:
        self._engine = engine
        self._threshold = settings.presidio_score_threshold
        entities = settings.presidio_entities.strip()
        self._entities = [e.strip() for e in entities.split(",") if e.strip()] or None

    def analyze(self, text: str) -> list[tuple[str, str]]:
        """Return ``(matched_text, redaction_category)`` spans at/above the score threshold."""
        try:
            results = self._engine.analyze(  # type: ignore[attr-defined]
                text=text, entities=self._entities, language="en"
            )
        except Exception as exc:  # noqa: BLE001 — analysis is best-effort; never fail a check
            logger.warning("presidio_analyze_failed", error=str(exc))
            return []
        spans: list[tuple[str, str]] = []
        for r in results:
            score = getattr(r, "score", 0.0)
            if score < self._threshold:
                continue
            start = getattr(r, "start", None)
            end = getattr(r, "end", None)
            entity_type = getattr(r, "entity_type", "")
            if start is None or end is None:
                continue
            matched = text[start:end]
            if not matched:
                continue
            category = _ENTITY_CATEGORY_MAP.get(entity_type, _DEFAULT_CATEGORY)
            spans.append((matched, category))
        return spans


def build_presidio_analyzer(settings: Settings) -> PresidioPiiAnalyzer | None:
    """Build the Presidio analyzer when enabled + available; else ``None`` (regex-only path).

    Default OFF (``guardrails_pii_presidio=False``) -> ``None`` without importing anything.
    When ON but the dependency/model is unavailable -> ``None`` (logged), so the current
    regex/HMAC path runs unchanged.
    """
    if not settings.guardrails_pii_presidio:
        return None
    if not presidio_available():
        logger.warning("presidio_unavailable_falling_back_to_regex")
        return None
    try:
        from presidio_analyzer import AnalyzerEngine

        engine = AnalyzerEngine()
        logger.info("presidio_analyzer_built", threshold=settings.presidio_score_threshold)
        return PresidioPiiAnalyzer(engine, settings)
    except Exception as exc:  # noqa: BLE001 — model/build failure must not block startup
        logger.error("presidio_build_failed", error=str(exc))
        return None
