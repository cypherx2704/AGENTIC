"""Orchestration engine (migration 0008) — the PROMPT -> ORCHESTRATOR -> SUB-AGENTS workflow.

The orchestrator (one per tenant, ``agent_type = 'orchestrator'``) decomposes a goal into a
DAG and runs each node as one of its OWN sub-agents INTERNALLY — in-tenant, reusing the
single-agent pipeline, with confinement enforced downstream (LLM alias allowlist, tool
grants). No A2A: A2A stays reserved for the external/cross-vendor boundary.

Foundations delivered here (phase B0):
  * :mod:`.dag`   — pure DAG parse + Kahn cycle validation + caps + topological layers.
  * :mod:`.authz` — pure guards: orchestrator-only, owns-its-sub-agent (404-invisible).
  * :mod:`.repo`  — RLS-scoped persistence for ``xagent.workflows`` / ``workflow_tasks`` /
    ``agent_presets`` + the agent-hierarchy read used by the guards.

The DAG *driver* (fan-out/join, budget ceiling, HIL gating) and the sub-agent *executor*
land in later phases (B1-B5); see ``SUBAGENT_WORKFLOW_PLAN.md``.
"""

from __future__ import annotations
