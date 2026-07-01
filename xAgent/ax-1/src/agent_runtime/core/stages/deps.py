"""Stage dependency access (the downstream clients the stages call).

The foundation pipeline runner instantiates concrete stages with NO constructor
arguments (``Pipeline.from_registry`` calls ``spec.stage_cls()``), so the PRE/POST
guardrail and LLM stages cannot receive their clients via ``__init__``. They are not
on the :class:`PipelineContext` either (that carrier is the per-task state, not the
shared infra handles). The shared, connection-pooled clients live on ``app.state``
(built once in the lifespan): ``app.state.guardrails_client`` / ``app.state.llms_client``.

This module is the seam the api layer wires once at startup (``set_clients(...)`` from
the lifespan, passing the same instances it stored on ``app.state``). The stages then
resolve them lazily via ``get_guardrails_client()`` / ``get_llms_client()``. Keeping the
holder here (rather than reaching into ``app.state`` from a stage) keeps stages
no-arg-constructible per the foundation contract and trivially testable — a test binds
fakes via ``set_clients(...)`` (or constructs a stage and sets ``stage._guardrails``).

The clients are typed structurally (duck-typed) to avoid a hard import cycle and to let
tests inject fakes that only implement the methods a given stage calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ...services.guardrails_client import GuardrailsClient
    from ...services.llms_client import LlmsClient
    from ...services.mcp_client import McpClient
    from ...services.memory_client import MemoryClient
    from ...services.rag_client import RagClient
    from ...services.registry_client import RegistryClient
    from ...services.skill_registry_client import SkillRegistryClient

# Process-wide client handles, set once by the api layer from app.state (lifespan).
_guardrails_client: GuardrailsClient | None = None
_llms_client: LlmsClient | None = None
# WP12 enhancement-stage clients — set lazily/separately so the basic pipeline (no
# enhancement stages) never needs them. A stage that runs but finds its client unwired
# raises SERVICE_UNAVAILABLE (the stage decides if that is fail-soft for the agent).
_rag_client: RagClient | None = None
_memory_client: MemoryClient | None = None
_registry_client: RegistryClient | None = None
_mcp_client: McpClient | None = None
_skill_registry_client: SkillRegistryClient | None = None
# Human-in-the-loop client (Phase 6) — OPTIONAL. When unset, an ``ask``-mode tool/skill is DENIED
# (an ask action must never auto-run without an explicit approval path). Duck-typed (Any) to avoid
# an import cycle and to let tests inject a fake with a ``request_and_wait`` coroutine.
_hil_client: Any | None = None
# Shared Valkey handle for the LOAD-stage agent-config read-through cache. Bound from
# the lifespan (the SAME instance stored on ``app.state.valkey``); SOFT — when unset
# (tests / no Valkey) the cache module bypasses to a straight DB read. Typed ``Any``
# because the LOAD stage only needs the duck-typed ``.client()`` surface (and tests may
# inject the conftest network-free double, which lacks it).
_valkey: Any | None = None


def set_clients(
    *,
    guardrails_client: GuardrailsClient | None,
    llms_client: LlmsClient | None,
) -> None:
    """Bind the shared downstream clients (called once from the api-layer lifespan).

    Passing the SAME instances stored on ``app.state`` keeps a single connection pool
    per downstream service. Either argument may be ``None`` only in tests that do not
    exercise the corresponding stage.
    """
    global _guardrails_client, _llms_client
    _guardrails_client = guardrails_client
    _llms_client = llms_client


def set_enhancement_clients(
    *,
    rag_client: RagClient | None = None,
    memory_client: MemoryClient | None = None,
    registry_client: RegistryClient | None = None,
    mcp_client: McpClient | None = None,
    skill_registry_client: SkillRegistryClient | None = None,
) -> None:
    """Bind the WP12 enhancement-stage clients (called once from the api-layer lifespan).

    Separate from :func:`set_clients` so the first-cycle wiring is untouched and tests that
    never run an enhancement stage can ignore these entirely. Pass the SAME instances
    stored on ``app.state`` to keep one connection pool per downstream service. Any argument
    may be ``None`` when its stage is not exercised; a stage that runs without its client
    raises SERVICE_UNAVAILABLE.
    """
    global _rag_client, _memory_client, _registry_client, _mcp_client, _skill_registry_client
    _rag_client = rag_client
    _memory_client = memory_client
    _registry_client = registry_client
    _mcp_client = mcp_client
    _skill_registry_client = skill_registry_client


def set_valkey(valkey: Any | None) -> None:
    """Bind the shared Valkey handle for the LOAD-stage agent-config cache (lifespan).

    SOFT: ``None`` (or a double without a live client) makes the cache bypass to a DB
    read. Pass the SAME instance stored on ``app.state.valkey`` so the cache shares the
    one lazy connection.
    """
    global _valkey
    _valkey = valkey


def get_valkey() -> Any | None:
    """Return the bound Valkey handle (or ``None`` -> the cache bypasses to a DB read)."""
    return _valkey


def set_hil_client(hil_client: Any | None) -> None:
    """Bind the human-in-the-loop client (Phase 6; called once from the api-layer lifespan)."""
    global _hil_client
    _hil_client = hil_client


def get_hil_client_optional() -> Any | None:
    """Return the bound HIL client, or ``None`` when HIL is not configured (ask -> deny)."""
    return _hil_client


def get_guardrails_client() -> GuardrailsClient:
    """Return the bound guardrails client, or raise SERVICE_UNAVAILABLE if unwired."""
    if _guardrails_client is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Guardrails client is not configured.")
    return _guardrails_client


def get_llms_client() -> LlmsClient:
    """Return the bound LLMs client, or raise SERVICE_UNAVAILABLE if unwired."""
    if _llms_client is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "LLMs client is not configured.")
    return _llms_client


def get_rag_client() -> RagClient:
    """Return the bound RAG client, or raise SERVICE_UNAVAILABLE if unwired (WP12)."""
    if _rag_client is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "RAG client is not configured.")
    return _rag_client


def get_memory_client() -> MemoryClient:
    """Return the bound Memory client, or raise SERVICE_UNAVAILABLE if unwired (WP12)."""
    if _memory_client is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Memory client is not configured.")
    return _memory_client


def get_registry_client() -> RegistryClient:
    """Return the bound Tool-Registry client, or raise SERVICE_UNAVAILABLE if unwired."""
    if _registry_client is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Tool-registry client is not configured.")
    return _registry_client


def get_mcp_client() -> McpClient:
    """Return the bound MCP client, or raise SERVICE_UNAVAILABLE if unwired (WP12)."""
    if _mcp_client is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "MCP client is not configured.")
    return _mcp_client


def get_skill_registry_client() -> SkillRegistryClient:
    """Return the bound Skill-Registry client, or raise SERVICE_UNAVAILABLE if unwired.

    SKILL_LOAD catches this and skips (fail-soft) — skills are an enhancement, never fatal.
    """
    if _skill_registry_client is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Skill-registry client is not configured.")
    return _skill_registry_client
