"""Orchestration engine (migration 0008) — the PROMPT -> ORCHESTRATOR -> SUB-AGENTS workflow.

The orchestrator (one per tenant, ``agent_type = 'orchestrator'``) decomposes a goal into a
DAG and runs each node as one of its OWN sub-agents INTERNALLY — in-tenant, reusing the
single-agent pipeline, with confinement enforced downstream (LLM alias allowlist, tool
grants). No A2A: A2A stays reserved for the external/cross-vendor boundary.

**Routing is the planner's decision, never the backend's.** The orchestrator LLM is shown a capability
catalogue of the tenant's real sub-agents (name + routing ``description`` + actual tools) and names one
per step; this package only VALIDATES what comes back (acyclic, within the depth/fanout caps, every
named target a real roster entry) and hands an invalid plan back to the planner to fix. It never
chooses, invents, or substitutes an agent. There is no keyword router and no preset template — both
existed once and both were routing rules in disguise. See :mod:`.decompose`.

Modules:
  * :mod:`.dag`       — pure DAG parse + Kahn cycle validation + caps + topological layers.
  * :mod:`.authz`     — pure guards: orchestrator-only, owns-its-sub-agent (404-invisible).
  * :mod:`.repo`      — RLS-scoped persistence for ``xagent.workflows`` / ``workflow_tasks`` + the
    roster read (sub-agent name/description/tools) and the agent-hierarchy read used by the guards.
  * :mod:`.decompose` — goal -> plan -> validated DAG, with the single gated repair attempt.
  * :mod:`.llm`       — the planner prompt (capability catalogue) + the synthesis pass.
  * :mod:`.driver`    — layered fan-out/join, budget ceiling, cancel, HIL gating.
  * :mod:`.executor`  — one node -> one sub-agent task under its own minted JWT (summary-only return).
  * :mod:`.service`   — the coordinator that wires it all together.
"""

from __future__ import annotations
