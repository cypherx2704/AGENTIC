"""The AI-proposer layer — a cross-cutting, capped, deterministic-by-pinning
proposal layer.

Load-bearing invariants (the whole reason AI is safe here):
- **AI is never the source of truth.** Proposals enter tagged ``source:ai`` at
  ``ai-inferred`` (a labeled opinion) / ``ai-proposed`` (a corroboratable fact),
  and are surfaced ALONGSIDE the static facts — never merged into or overriding
  them. A static field keeps its static value regardless of any AI proposal.
- **Never on the query path.** Proposals are produced at enrichment time, not
  when an agent reads a fact.
- **Deterministic by pinning.** Every proposal is content-addressed
  (``input_hash`` over the slice ⊕ provider ⊕ model); the same input replays a
  pinned artifact rather than re-invoking a model. In *sealed* mode (a
  deterministic context — the rebuild oracle, CI) a cache miss is a fail-closed
  error, never a live call.
- **Every assembled AI fact carries a ``file:line`` citation** (Golden Rule 2).

This module ships the interface, the pin/replay cache, and a DETERMINISTIC
reference provider. A real LLM provider is a drop-in that implements ``analyze``;
the cache makes its (otherwise non-deterministic) output replayable.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .protocol.canonical import canonical_bytes, hexdigest

# bump when the on-disk artifact shape changes so old files are ignored, not
# mis-deserialized into a TypeError
_CACHE_VERSION = "v1"
_HEX_KEY = re.compile(r"[0-9a-f]+")


@dataclass(frozen=True)
class AiProposal:
    kind: str
    subject: str  # the fact this is about (e.g. an endpoint id)
    value: Any
    citation: str  # file:line anchor
    input_hash: str
    source: str = "ai"
    confidence: str = "ai-inferred"  # capped: never > ai-proposed for a fact
    verification_status: str = "unverified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "value": self.value,
            "citation": self.citation,
            "input_hash": self.input_hash,
            "source": self.source,
            "confidence": self.confidence,
            "verification_status": self.verification_status,
        }


class AiAnalysisProvider(Protocol):
    id: str

    def model(self) -> dict[str, Any]: ...

    def analyze(self, task: str, facts: dict[str, Any]) -> list[AiProposal]: ...


def input_hash(provider: AiAnalysisProvider, task: str, facts: dict[str, Any]) -> str:
    """Content address for a proposal: folding provider+model in means a model
    change forces a visible re-analysis (never a silent different answer)."""
    payload = {"provider": provider.id, "model": provider.model(), "task": task, "facts": facts}
    return hexdigest(canonical_bytes(payload))


class AiCache:
    """Content-addressed pin/replay store. Optionally persists artifacts to
    ``<path>/<hash>.json`` so a model call made once (an expensive, non-deterministic
    LLM call) replays deterministically across processes and CI. In *sealed* mode a
    miss fails closed — the guarantee that a deterministic context makes no live call."""

    def __init__(self, sealed: bool = False, path: str | None = None) -> None:
        self._store: dict[str, list[dict[str, Any]]] = {}
        self.sealed = sealed
        self._path = path
        if path is not None:
            os.makedirs(path, exist_ok=True)

    def _file(self, key: str) -> str:
        # keys are content-address digests (hex); reject anything else so a key
        # can never traverse out of the cache directory
        if not _HEX_KEY.fullmatch(key):
            raise ValueError(f"invalid cache key {key!r} (expected a hex digest)")
        return os.path.join(self._path or "", f"{_CACHE_VERSION}-{key}.json")

    def get(self, key: str) -> list[AiProposal] | None:
        data = self._store.get(key)
        if data is None and self._path is not None:
            path = self._file(key)
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as handle:
                        loaded = json.load(handle)
                    proposals = [AiProposal(**p) for p in loaded]  # validate the schema
                except (json.JSONDecodeError, TypeError, ValueError, OSError):
                    data = None  # corrupt / truncated / schema-drift -> treat as a miss (self-heals)
                else:
                    self._store[key] = loaded  # warm the in-memory tier
                    return proposals
        if data is not None:
            return [AiProposal(**p) for p in data]
        if self.sealed:
            raise RuntimeError(f"sealed replay: AI cache miss for {key!r} (no live model calls allowed)")
        return None

    def put(self, key: str, proposals: list[AiProposal]) -> None:
        data = [p.to_dict() for p in proposals]
        self._store[key] = data
        if self._path is not None:
            path = self._file(key)
            tmp = f"{path}.tmp"  # atomic replace so a crash/concurrent writer can't truncate
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
            os.replace(tmp, path)


class HeuristicProvider:
    """A DETERMINISTIC reference provider (no LLM). It proposes a response-shape
    HINT from the handler name / method for endpoints that lack a resolved
    response — standing in for a real model so the proposal/merge/pin-replay flow
    is testable and deterministic. Confidence is capped at ``ai-inferred``."""

    id = "heuristic@1"

    def model(self) -> dict[str, Any]:
        return {"provider": "heuristic", "model": "rules", "version": 1}

    def analyze(self, task: str, facts: dict[str, Any]) -> list[AiProposal]:
        if task != "response_shape":
            return []
        return [
            AiProposal(
                kind="response_shape",
                subject=facts["id"],
                value=self._hint(facts.get("handler", ""), facts.get("method", "")),
                citation=f"{facts['handler_file']}:{facts['handler_line']}",
                input_hash=input_hash(self, task, facts),
            )
        ]

    @staticmethod
    def _hint(handler: str, method: str) -> str:
        name = handler.lower()
        if name.startswith(("list", "get_all", "search")) or (
            method == "GET" and name.endswith("s")
        ):
            return "a collection of resources"
        if name.startswith(("get", "read", "fetch", "retrieve")):
            return "a single resource"
        if name.startswith(("create", "add")) or method == "POST":
            return "the created resource"
        if name.startswith(("update", "edit", "put", "patch")):
            return "the updated resource"
        if name.startswith("delete") or method == "DELETE":
            return "no content / a deletion result"
        return "an unknown response shape"


def _response_shape_prompt(facts: dict[str, Any]) -> str:
    """A deterministic prompt (no timestamps/randomness) describing one endpoint
    whose response shape static analysis could not resolve."""
    return (
        "You are labeling a backend HTTP endpoint whose response DTO could not be "
        "resolved by static analysis. In ONE short sentence, describe the most "
        "likely response shape. Be concrete but brief; do not add preamble.\n\n"
        f"method: {facts.get('method')}\n"
        f"path: {facts.get('resolved_path')}\n"
        f"handler: {facts.get('handler')}\n"
    )


class LlmProvider:
    """A real LLM-backed proposer — a drop-in on the ``AiAnalysisProvider`` seam.

    Vendor-neutral: it takes a ``complete(prompt) -> str`` callable, so it is fully
    testable with a fake and works with any model. Its (non-deterministic) output
    becomes deterministic via the pin/replay ``AiCache``. Confidence is capped at
    ``ai-inferred`` and every proposal is cited — AI never becomes a static fact.
    Use :func:`anthropic_complete` for a Claude-backed ``complete``."""

    id = "llm@1"

    def __init__(
        self,
        complete: Callable[[str], str],
        model_id: str = "claude-opus-4-8",
        vendor: str = "anthropic",
        config: dict[str, Any] | None = None,
    ) -> None:
        self._complete = complete
        self._model_id = model_id
        self._vendor = vendor
        # generation identity that the opaque `complete` closure carries (max_tokens,
        # temperature, system prompt, deployment id, …). The caller MUST pass the
        # params baked into `complete` so a config change re-analyses rather than
        # replaying a stale answer computed under different settings.
        self._config = dict(config or {})

    def model(self) -> dict[str, Any]:
        # folded into the content address: a vendor/model/config change forces a
        # visible re-analysis rather than a silently different cached answer
        return {"vendor": self._vendor, "model": self._model_id, "config": self._config}

    def analyze(self, task: str, facts: dict[str, Any]) -> list[AiProposal]:
        if task != "response_shape":
            return []
        text = self._complete(_response_shape_prompt(facts)).strip()
        if not text:
            return []
        return [
            AiProposal(
                kind="response_shape",
                subject=facts["id"],
                value=text,
                citation=f"{facts['handler_file']}:{facts['handler_line']}",
                input_hash=input_hash(self, task, facts),
            )
        ]


def anthropic_complete(model_id: str = "claude-opus-4-8", max_tokens: int = 256) -> Callable[[str], str]:
    """Build a Claude-backed ``complete(prompt) -> str`` using the Anthropic SDK.

    Lazy-imports ``anthropic`` (an optional dependency) so the core never requires
    it. Credentials resolve from the environment (``ANTHROPIC_API_KEY`` or an
    ``ant auth login`` profile). The response is short, so a plain non-streaming
    call is used; the ``AiCache`` makes repeated calls free and replayable."""
    import anthropic  # optional extra: pip install 'bkg[llm]'

    client = anthropic.Anthropic()

    def complete(prompt: str) -> str:
        message = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")

    return complete


_GAP_TASK = "response_shape"
_SLICE_KEYS = (
    "id", "method", "resolved_path", "handler", "handler_file", "handler_line", "response", "partial",
)


def _is_gap(endpoint: dict[str, Any]) -> bool:
    # a deterministic HOLE the proposer may fill: no resolved response, or a
    # DTO reference that couldn't be resolved (partial)
    return endpoint.get("response") is None or endpoint.get("partial", False)


def propose_for_endpoints(
    endpoints: list[dict[str, Any]],
    provider: AiAnalysisProvider,
    cache: AiCache,
) -> dict[str, list[AiProposal]]:
    """Gap-triggered: propose only for endpoints with a hole, replaying from the
    content-addressed cache when the slice is unchanged."""
    out: dict[str, list[AiProposal]] = {}
    for endpoint in endpoints:
        if not _is_gap(endpoint):
            continue
        facts = {k: endpoint[k] for k in _SLICE_KEYS if k in endpoint}
        key = input_hash(provider, _GAP_TASK, facts)
        proposals = cache.get(key)
        if proposals is None:
            proposals = provider.analyze(_GAP_TASK, facts)
            cache.put(key, proposals)
        if proposals:
            out[endpoint["id"]] = proposals
    return out
