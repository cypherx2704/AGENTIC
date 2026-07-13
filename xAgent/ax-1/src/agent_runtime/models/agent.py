"""Agent runtime-config model (Component 1).

``AgentRuntime`` is the in-process view of a ``xagent.agents`` row — the runtime
configuration the execution pipeline reads on the LOAD stage (LLM model, system
prompt, token budgets, guardrail policy). It is distinct from the Auth identity row.

``AgentRuntimeRegistration`` is the validated body of
``POST /v1/agents/{agent_id}/runtime``. tenant_id / agent_id are NOT taken from this
body — they come from the path + the JWT (Contract 13); the body carries config only.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryScope = Literal["none", "agent", "user", "tenant", "session"]
AgentStatus = Literal["active", "inactive", "pending_config"]

# ── Status-transition rules (Component 1 lifecycle, WP08) ──────────────────────────────
# The lifecycle graph the PUT endpoint enforces. ``pending_config`` is the INITIAL state
# of a freshly-registered (not-yet-finalised) runtime; once the config is completed the
# agent moves to ``active`` (runnable) or ``inactive`` (configured but parked). ``active``
# and ``inactive`` toggle freely. Regressing to ``pending_config`` is NOT allowed (a
# configured agent never becomes "unconfigured"). A self-transition (X -> X) is always
# permitted (an idempotent re-PUT that only edits config fields). The LOAD stage runs a
# task ONLY against an ``active`` runtime (load.py), so ``inactive`` parks an agent
# without deleting its config.
_ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_config": frozenset({"pending_config", "active", "inactive"}),
    "active": frozenset({"active", "inactive"}),
    "inactive": frozenset({"inactive", "active"}),
}


def is_valid_status_transition(current: str, target: str) -> bool:
    """Return True iff ``current -> target`` is a permitted status transition.

    An unknown ``current`` (should never happen — the column is CHECK-constrained) is
    treated as permitting only a self-transition, so a corrupt row can never be silently
    flipped to ``active``.
    """
    return target in _ALLOWED_STATUS_TRANSITIONS.get(current, frozenset({current}))


def bump_runtime_version(current: str) -> str:
    """Increment the PATCH component of a semver-ish ``runtime_version`` on each change.

    ``1.0.0 -> 1.0.1``. Best-effort: a non-semver value (operator-set free text) is left
    UNTOUCHED rather than mangled — the version is advisory provenance, not a gate. A
    bare ``MAJOR.MINOR`` (no patch) gets a ``.1`` appended.
    """
    parts = current.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return current  # non-numeric / free-text version — leave as-is
    if len(parts) < 3:
        parts += ["0"] * (3 - len(parts))
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts[:3] + parts[3:])


class AgentRuntime(BaseModel):
    """Resolved runtime config for one agent (one ``xagent.agents`` row)."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    tenant_id: str
    name: str
    runtime_version: str = "1.0.0"
    status: AgentStatus = "active"

    # LLM configuration.
    llm_model: str = "smart"
    system_prompt: str = ""
    max_tokens: int = 2048
    temperature: float = 0.7

    # Integration config (first-cycle reads only a subset; rest carried forward).
    memory_scope: MemoryScope = "agent"
    guardrail_policy_id: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    # Per-agent tool-loop toggle (migration 0007). True (default) => the TOOL_LOOP stage
    # runs the full bounded LLM<->tool loop ("multiple request"). False => the stage skips
    # even with allowed_tools, so the task makes a single LLM call ("per request" — for
    # rate-limited / free-tier models). Default true preserves prior behaviour for every
    # existing agent. See core/stages/tool_loop.py for the enforcing skip.
    tool_loop_enabled: bool = True
    allowed_skills: list[str] = Field(default_factory=list)
    allowed_kb_ids: list[str] = Field(default_factory=list)
    rag_top_k_per_kb: int = 5
    rag_min_score: float = 0.7
    token_budget_per_task: int = 10000

    # Orchestrator hierarchy (denormalised from auth.agents at runtime-registration time, migration
    # 0006). immutable_llm=true locks llm_model against a later PUT (enforced in api/agents.py).
    immutable_llm: bool = False

    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def effective_max_tokens(self) -> int:
        """First-cycle single-call budget cap: min(max_tokens, token_budget_per_task)."""
        return min(self.max_tokens, self.token_budget_per_task)


class AgentRuntimeRegistration(BaseModel):
    """Body of ``POST /v1/agents/{agent_id}/runtime`` (Component 1 step 2).

    Config only — identity (tenant_id, agent_id) comes from JWT + path, never the body.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    runtime_version: str = "1.0.0"
    status: AgentStatus = "active"

    llm_model: str = "smart"
    system_prompt: str = Field(..., min_length=1)
    max_tokens: int = Field(default=2048, ge=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    memory_scope: MemoryScope = "agent"
    guardrail_policy_id: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    # See AgentRuntime.tool_loop_enabled. Default true = current multi-call behaviour;
    # set false to force a single LLM call (skip the tool loop) for this agent.
    tool_loop_enabled: bool = True
    allowed_skills: list[str] = Field(default_factory=list)
    allowed_kb_ids: list[str] = Field(default_factory=list)
    rag_top_k_per_kb: int = Field(default=5, ge=1, le=20)
    rag_min_score: float = Field(default=0.7, ge=0.0, le=1.0)
    token_budget_per_task: int = Field(default=10000, ge=1)

    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
