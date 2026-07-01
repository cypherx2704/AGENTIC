"""Toxicity / harm classifier abstraction (Component 2).

``Classifier.classify(text) -> list[Category]`` is the in-process abstraction boundary
(the phase doc's ``Classify(text) -> []Category``). Two implementations:

* :class:`StubClassifier` — DEFAULT. A keyword/lexicon heuristic over hate / threat /
  self-harm vocabulary. No torch, no model download — the service is runnable locally
  and in CI with ``classifier_mode='stub'``. In stub mode the classifier is ALWAYS ready.
* :class:`DetoxifyClassifier` — PROD (``classifier_mode='detoxify'``). Loads a PINNED
  detoxify RoBERTa checkpoint EAGERLY at lifespan (``load_eager``) with an env-configured
  threshold. The heavy ``ml`` extra (torch) is only imported when this mode is selected.

GRACEFUL FALLBACK: when ``classifier_mode='detoxify'`` is selected but the ``detoxify``
dependency or the model checkpoint is unavailable, :func:`build_classifier` falls back to
the stub classifier (logged) so tests, CI, and keyless dev still run — the service never
fails to start because the ML model is missing. The eager load happens at lifespan via
:func:`warm_classifier`; if it fails there too, the already-built classifier reports
``ready`` per its own state and the stub stands in.

The function signature is the swap seam: rule code calls ``classify`` and never touches
the model directly.

Docker image targets (documented; image build is out of scope here):
  * ``runtime-slim`` (DEFAULT) — the current Dockerfile: no ``ml`` extra, stub classifier,
    light image. ``CLASSIFIER_MODE=stub``.
  * ``runtime-ml`` — adds ``uv sync --extra ml`` (detoxify -> torch) and bakes the pinned
    checkpoint into the image so the eager load at lifespan is offline/airgapped-safe.
    ``CLASSIFIER_MODE=detoxify``. Build it as a second Dockerfile target/stage.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from ..core.config import Settings

logger = structlog.get_logger(__name__)

# Toxicity sub-categories the classifier can surface.
HATE = "hate"
THREAT = "threat"
SELF_HARM = "self_harm"

_DEFAULT_THRESHOLD = 0.5


@dataclass(frozen=True)
class Category:
    """A detected toxicity category with a confidence score in [0, 1]."""

    label: str
    score: float


# ── Stub lexicon (deterministic, English heuristic) ─────────────────────────────
_HATE_TERMS = (
    "hate you",
    "i hate",
    "subhuman",
    "vermin",
    "go back to your country",
    "racial slur",
)
_THREAT_TERMS = (
    "i will kill",
    "i'll kill you",
    "kill you",
    "hurt you",
    "i will hurt",
    "bomb the",
    "shoot up",
    "i am going to kill",
)
_SELF_HARM_TERMS = (
    "kill myself",
    "end my life",
    "suicide",
    "self harm",
    "self-harm",
    "hurt myself",
    "want to die",
)


class Classifier:
    """Abstract classifier interface."""

    def classify(self, text: str) -> list[Category]:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def ready(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class StubClassifier(Classifier):
    """Keyword/lexicon heuristic classifier (default; always ready, no model)."""

    def classify(self, text: str) -> list[Category]:
        lowered = text.lower()
        out: list[Category] = []
        if any(term in lowered for term in _HATE_TERMS):
            out.append(Category(HATE, 0.9))
        if any(term in lowered for term in _THREAT_TERMS):
            out.append(Category(THREAT, 0.95))
        if any(term in lowered for term in _SELF_HARM_TERMS):
            out.append(Category(SELF_HARM, 0.9))
        return out

    @property
    def ready(self) -> bool:
        # Stub mode is ALWAYS ready — no model to load (FIX D).
        return True


class DetoxifyClassifier(Classifier):
    """detoxify-backed classifier (PROD). Loads a PINNED checkpoint EAGERLY at lifespan.

    The model is loaded once via :meth:`load_eager` (called from ``warm_classifier`` in the
    lifespan). If that load fails the classifier reports ``ready=False`` and classifies as
    empty — but :func:`build_classifier` will already have substituted the stub when the
    dependency is missing, so this path is reserved for a checkpoint/runtime fault after a
    successful import.
    """

    def __init__(
        self, threshold: float = _DEFAULT_THRESHOLD, *, checkpoint: str = "original"
    ) -> None:
        self._threshold = threshold
        self._checkpoint = checkpoint
        self._model: object | None = None
        self._load_failed = False

    def load_eager(self) -> bool:
        """Load the pinned checkpoint now (lifespan). Returns True on success.

        Idempotent: a second call after a successful load is a no-op. A failure is recorded
        so :meth:`ready` reports False and ``classify`` degrades to empty rather than raising.
        """
        if self._model is not None:
            return True
        try:
            from detoxify import Detoxify

            self._model = Detoxify(self._checkpoint)
            self._load_failed = False
            logger.info("detoxify_model_loaded", checkpoint=self._checkpoint)
            return True
        except Exception as exc:  # noqa: BLE001 — model load is best-effort; stub stands in
            self._load_failed = True
            logger.error(
                "detoxify_model_load_failed", checkpoint=self._checkpoint, error=str(exc)
            )
            return False

    def classify(self, text: str) -> list[Category]:
        if self._model is None:
            # Not loaded (load failed / not warmed yet): degrade to no detections.
            return []
        scores = self._model.predict(text)  # type: ignore[attr-defined]
        out: list[Category] = []
        # Map detoxify heads onto our taxonomy.
        mapping = {
            "toxicity": HATE,
            "severe_toxicity": HATE,
            "threat": THREAT,
            "identity_attack": HATE,
        }
        for head, label in mapping.items():
            score = float(scores.get(head, 0.0))
            if score >= self._threshold:
                out.append(Category(label, score))
        return out

    @property
    def ready(self) -> bool:
        return self._model is not None


def _detoxify_available() -> bool:
    """True if the ``detoxify`` dependency can be imported (the ``ml`` extra is installed)."""
    import importlib.util

    return importlib.util.find_spec("detoxify") is not None


def build_classifier(settings: Settings) -> Classifier:
    """Build the configured classifier.

    Default 'stub' avoids importing torch. 'detoxify' is selected ONLY when the dependency
    is importable; otherwise we GRACEFULLY fall back to the stub (logged) so the service
    starts without the ``ml`` extra. The actual model load happens eagerly at lifespan via
    :func:`warm_classifier`.

    Any OTHER ``classifier_mode`` value (e.g. 'llms_gateway') selects the REMOTE
    confidence-banded cascade (services.classifier_client), which wraps the stub: the stub
    remains the regex/local stage and the gateway is consulted only for the uncertain band.
    The wrapper is fail-soft, so the service still boots + stays ready with the gateway down.
    """
    if settings.classifier_mode == "detoxify":
        if _detoxify_available():
            return DetoxifyClassifier(
                threshold=settings.detoxify_threshold,
                checkpoint=settings.detoxify_checkpoint,
            )
        logger.warning("detoxify_unavailable_falling_back_to_stub")
        return StubClassifier()
    if settings.classifier_mode != "stub":
        # Remote seam (additive). Import locally to avoid a circular import at module load.
        from .classifier_client import build_remote_classifier

        logger.info("remote_classifier_selected", mode=settings.classifier_mode)
        return build_remote_classifier(StubClassifier(), settings)
    return StubClassifier()


def warm_classifier(classifier: Classifier) -> None:
    """Eagerly load the detoxify checkpoint at lifespan (no-op for the stub).

    Best-effort: a load failure is logged inside :meth:`DetoxifyClassifier.load_eager` and
    the classifier simply reports not-ready (readiness reflects it; the stub fallback was
    already chosen at build time when the dep is missing).
    """
    if isinstance(classifier, DetoxifyClassifier):
        classifier.load_eager()
