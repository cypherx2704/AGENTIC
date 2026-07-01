"""Concrete pipeline stages (Component 3) + registry binding.

The foundation authors the pipeline ENGINE (``core/pipeline.py``: the ``Stage`` ABC, the
``PipelineContext`` carrier, the ``Pipeline`` runner, and the ordered ``STAGE_REGISTRY``
of stage *names*). This package supplies the concrete first-cycle (Phase 9A) stages and
binds them into their registry slots so ``Pipeline.from_registry(EventStage())`` produces
a runnable pipeline:

    LOAD -> PRE_GUARDRAIL -> PROMPT_BUILD -> LLM -> POST_GUARDRAIL  (then EVENT, always)

The enhancement slots (MEMORY_RETRIEVE, RAG_QUERY, SKILL_LOAD, TOOL_LOOP, MEMORY_WRITE)
stay disabled + unbound in the registry, so the runner skips them. ``EventStage`` is the
finally-equivalent terminal stage — it is NOT in the registry; the api layer passes an
instance to ``Pipeline.from_registry`` directly.

Binding happens at import time (idempotent) so importing this package is sufficient to
ready the registry. The downstream clients the stages call are wired separately via
``deps.set_clients(...)`` from the api-layer lifespan.
"""

from __future__ import annotations

from ..pipeline import bind_stage
from .event import EventStage
from .llm import LlmStage
from .load import LoadStage
from .memory_retrieve import MemoryRetrieveStage
from .memory_write import MemoryWriteStage
from .post_guardrail import PostGuardrailStage
from .pre_guardrail import PreGuardrailStage
from .prompt_build import PromptBuildStage
from .rag_query import RagQueryStage
from .skill_load import SkillLoadStage
from .tool_loop import ToolLoopStage

__all__ = [
    "EventStage",
    "LlmStage",
    "LoadStage",
    "MemoryRetrieveStage",
    "MemoryWriteStage",
    "PostGuardrailStage",
    "PreGuardrailStage",
    "PromptBuildStage",
    "RagQueryStage",
    "SkillLoadStage",
    "ToolLoopStage",
    "deps",
    "register_stages",
]


def register_stages() -> None:
    """Bind every concrete stage into its ``STAGE_REGISTRY`` slot.

    Idempotent: ``bind_stage`` simply (re)assigns the class on the matching slot, so
    importing this package more than once is safe. EVENT is bound separately (it is not
    a registry slot — the api layer supplies the instance to ``Pipeline.from_registry``).

    The WP12 enhancement stages (MEMORY_RETRIEVE / RAG_QUERY / TOOL_LOOP / MEMORY_WRITE)
    are BOUND here so they are runnable, but their registry slots stay ``enabled=False``
    (flipped only by ``STAGE_ENABLE_<NAME>`` env flags), so ``Pipeline.from_registry``
    SKIPS them by default — the basic LOAD->PRE_GUARDRAIL->PROMPT_BUILD->LLM->POST_GUARDRAIL
    ->EVENT pipeline is byte-unchanged and the existing suite stays green.
    """
    bind_stage("LOAD", LoadStage)
    bind_stage("PRE_GUARDRAIL", PreGuardrailStage)
    bind_stage("MEMORY_RETRIEVE", MemoryRetrieveStage)
    bind_stage("RAG_QUERY", RagQueryStage)
    bind_stage("SKILL_LOAD", SkillLoadStage)
    bind_stage("PROMPT_BUILD", PromptBuildStage)
    bind_stage("LLM", LlmStage)
    bind_stage("TOOL_LOOP", ToolLoopStage)
    bind_stage("POST_GUARDRAIL", PostGuardrailStage)
    bind_stage("MEMORY_WRITE", MemoryWriteStage)


# Bind at import time so `import ...core.stages` readies the registry.
register_stages()

from . import deps  # noqa: E402 — re-export after binding to avoid an import cycle
