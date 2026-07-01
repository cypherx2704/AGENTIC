"""Unified (OpenAI-superset) chat-completion request/response models (Component 1).

This schema is a superset of every provider's request/response shape — agents write
to one schema and the gateway translates per-provider. Crucially it is *open from
day one*: tools[]/tool_choice/tool_calls and multimodal image_url content blocks are
modelled now even though SSRF-hardened multimodal fetching and other refinements are
deferred to a later pass.

JSON field names follow the OpenAI standard (tool_calls, tool_call_id, image_url, …)
via pydantic field aliases; ``populate_by_name=True`` lets internal code use the
Python attribute names too.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.errors import ApiError, ErrorCode

# Tool name regex shared by OpenAI + Anthropic.
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Identity / correlation keys that MUST NOT appear in client metadata (Component 1).
RESERVED_METADATA_KEYS = frozenset(
    {"agent_id", "tenant_id", "trace_id", "span_id", "request_id", "task_id", "user_id", "org_id"}
)


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# ── Content blocks ─────────────────────────────────────────────────────────────
class TextContent(_Base):
    type: Literal["text"]
    text: str


class ImageUrl(_Base):
    url: str
    detail: Literal["auto", "low", "high"] = "auto"


class ImageUrlContent(_Base):
    type: Literal["image_url"]
    image_url: ImageUrl


ContentPart = Annotated[TextContent | ImageUrlContent, Field(discriminator="type")]


# ── Tools / tool calls ─────────────────────────────────────────────────────────
class FunctionDefinition(_Base):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError("tool function name must match ^[a-zA-Z0-9_-]{1,64}$")
        return v


class Tool(_Base):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class FunctionCall(_Base):
    name: str
    arguments: str  # JSON-encoded string (OpenAI convention)


class ToolCall(_Base):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class NamedToolChoiceFunction(_Base):
    name: str


class NamedToolChoice(_Base):
    type: Literal["function"]
    function: NamedToolChoiceFunction


ToolChoice = Literal["auto", "none", "required"] | NamedToolChoice


# ── Messages ────────────────────────────────────────────────────────────────────
class Message(_Base):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    # assistant turns may carry tool_calls
    tool_calls: list[ToolCall] | None = Field(default=None, alias="tool_calls")
    # tool turns reference the call they answer
    tool_call_id: str | None = Field(default=None, alias="tool_call_id")
    name: str | None = None


# ── Streaming options ─────────────────────────────────────────────────────────
class StreamOptions(_Base):
    include_usage: bool = True
    # Reserved wire-format switch (Component 6); `false` rejected at runtime until Phase 12.
    aggregate_tool_calls: bool = True


# ── response_format ──────────────────────────────────────────────────────────────
class ResponseFormat(_Base):
    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: dict[str, Any] | None = None


# ── Request ──────────────────────────────────────────────────────────────────────
class ChatCompletionRequest(_Base):
    model: str
    messages: list[Message] = Field(min_length=1)
    tools: list[Tool] | None = None
    tool_choice: ToolChoice | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stop: str | list[str] | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    parallel_tool_calls: bool = True
    response_format: ResponseFormat | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("stop")
    @classmethod
    def _validate_stop(cls, v: str | list[str] | None) -> str | list[str] | None:
        if isinstance(v, list) and len(v) > 4:
            raise ValueError("stop accepts at most 4 sequences")
        return v

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        reserved = RESERVED_METADATA_KEYS.intersection(v.keys())
        if reserved:
            # Surface as a Contract 2 VALIDATION_ERROR (400) rather than a 422 body.
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "metadata must not contain reserved identity keys.",
                details={"reason": "reserved_metadata_key", "keys": sorted(reserved)},
            )
        return v

    @model_validator(mode="after")
    def _reject_aggregate_false(self) -> ChatCompletionRequest:
        if self.stream_options is not None and self.stream_options.aggregate_tool_calls is False:
            raise ApiError(
                ErrorCode.MODEL_UNSUPPORTED,
                "stream_options.aggregate_tool_calls=false is not yet implemented.",
                status_code=422,
                details={"reason": "FEATURE_NOT_YET_IMPLEMENTED"},
            )
        return self


# ── Response ──────────────────────────────────────────────────────────────────
class Usage(_Base):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_prompt_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0


class ResponseMessage(_Base):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = Field(default=None, alias="tool_calls")


FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "budget_exceeded"]


class Choice(_Base):
    index: int = 0
    message: ResponseMessage
    finish_reason: FinishReason | None = None


class ChatCompletionResponse(_Base):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage


# ── Embeddings (WP06 — POST /v1/embeddings) ─────────────────────────────────────
# OpenAI-shaped embeddings request/response. Embeddings have no completion tokens,
# so usage carries only prompt_tokens/total_tokens (+ gateway cost_usd, output 0 by
# convention). Caps on input count / payload size are enforced in the endpoint
# (config keys embeddings_max_input_items / embeddings_max_payload_bytes), not here.
class EmbeddingRequest(_Base):
    model: str
    # str OR list[str] (OpenAI also allows token-id arrays; the gateway accepts text
    # only — the RAG/Memory callers send strings — keeping the validation tractable).
    input: str | list[str] = Field(min_length=1)
    # Output vector length (text-embedding-3-* support Matryoshka truncation). When
    # omitted the provider returns its native dimension.
    dimensions: int | None = Field(default=None, ge=1)
    # Opaque end-user id forwarded to the provider for abuse monitoring (OpenAI).
    user: str | None = None
    encoding_format: Literal["float", "base64"] = "float"

    @field_validator("input")
    @classmethod
    def _validate_input(cls, v: str | list[str]) -> str | list[str]:
        if isinstance(v, list):
            if not v:
                raise ValueError("input must contain at least one string")
            if any(not isinstance(item, str) for item in v):
                raise ValueError("input list must contain only strings")
        return v


class EmbeddingUsage(_Base):
    # Embeddings bill on input tokens only; total_tokens == prompt_tokens.
    prompt_tokens: int
    total_tokens: int
    cost_usd: float = 0.0


class EmbeddingData(_Base):
    object: Literal["embedding"] = "embedding"
    embedding: list[float]
    index: int


class EmbeddingResponse(_Base):
    object: Literal["list"] = "list"
    data: list[EmbeddingData]
    model: str
    usage: EmbeddingUsage


# ── Rerank (POST /v1/rerank) ────────────────────────────────────────────────────
# Cross-encoder reranking surface (contracts/api/rerank.schema.json). Given a query
# and candidate documents, returns the candidates scored + ordered by relevance.
# Forward-compatible by contract: request/response objects tolerate unknown fields
# (extra="allow") so later phases can add optional fields (return_documents, etc.)
# without a breaking change. Rerank has no completion tokens; usage carries optional
# total_tokens + search_units (Contract-19 metering — UNITS, not a token cost rewrite).
class _OpenBase(BaseModel):
    """Open variant of ``_Base`` — populate_by_name + tolerate unknown fields.

    Contract objects (rerank/classify) set additionalProperties:true; mirror that here
    so the gateway never rejects a forward-compatible field a caller/contract adds.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class RerankDocument(_OpenBase):
    # Optional caller-supplied id, echoed back in the matching result when present.
    id: str | None = None
    text: str = Field(min_length=1)


class RerankRequest(_OpenBase):
    # Optional like ClassifyRequest's model: when omitted (or null) the endpoint falls
    # back to settings.rerank_default_model (alias 'rerank-default'). Required-with
    # min_length=1 here would 422 before that default could ever apply -> dead code.
    model: str | None = Field(default=None, min_length=1)
    query: str = Field(min_length=1)
    documents: list[RerankDocument] = Field(min_length=1)
    # Optional: return only the top N results by score (all when omitted).
    top_n: int | None = Field(default=None, ge=1)


class RerankResult(_OpenBase):
    # Zero-based position of the scored document in the request `documents` array.
    index: int = Field(ge=0)
    # Caller-supplied id, echoed when the request document carried one.
    id: str | None = None
    # Relevance score; higher is more relevant (absolute range is model-specific).
    score: float


class RerankUsage(_OpenBase):
    # Optional metering accounting (Contract-19): processed tokens + billable search units.
    total_tokens: int = Field(default=0, ge=0)
    search_units: int = Field(default=0, ge=0)


class RerankResponse(_OpenBase):
    results: list[RerankResult]
    model: str
    usage: RerankUsage


# ── Safety classify (POST /v1/classify) ──────────────────────────────────────────
# Safety/moderation classifier surface (contracts/api/classify.schema.json). Classifies
# a single text payload on the inbound (input) or outbound (output) direction into a
# moderation verdict + per-category scores. CLASSIFIER_MODE=stub (the default) returns
# verdict=allow with empty/low scores — today's permissive behaviour unchanged.
ClassifyVerdict = Literal["allow", "warn", "redact", "block"]
ClassifyDirection = Literal["input", "output"]


class ClassifyRequest(_OpenBase):
    input: str = Field(min_length=1)
    direction: ClassifyDirection
    # Optional, open-ended caller context (tenant policy id, locale, excerpt, …).
    context: dict[str, Any] | None = None


class ClassifyCategory(_OpenBase):
    name: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)


class ClassifyResponse(_OpenBase):
    verdict: ClassifyVerdict
    categories: list[ClassifyCategory]
    # Optional convenience flat map of category name -> score alongside `categories`.
    scores: dict[str, float] | None = None
    model: str
