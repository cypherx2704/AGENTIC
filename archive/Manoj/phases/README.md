# CypherX AI — Implementation Phases
> Master index for all platform build phases

## Amendment Log (2026-06 — pre-build reconciliation)

- **First-cycle "definition of done" re-pinned to the amended Contract-15 gating.** Contract 15 defines 15 cases, not 10: cases **1–10** gate the spine (Phases 0–4 + 9A); cases **11–15** gate the enterprise wave (WP12/WP14); **all 15** are required for full first-cycle sign-off. The old "all 10 cases" wording is superseded.

---

## Legend

| Symbol | Meaning |
|--------|---------|
| ⚡ | Required for First Cycle — implement this first to verify end-to-end flow |
| 📋 | Full Enterprise — design now, implement after first cycle is verified working |
| 🏗️ | Service architecture must be planned separately before implementation |
| ✅ | Phase complete |
| 🔄 | Phase in progress |
| ⏳ | Phase pending |

---

## First Cycle Path

The **First Cycle** is the minimum implementation that proves the entire platform works end-to-end:

> Agent registered → JWT issued → Task submitted → Guardrails checked (in + out) → LLM called → Response returned, observable end-to-end.

**No tools, no memory, no skills, no RAG, no A2A, no orchestration in the first cycle.** Those are intentionally deferred — the goal of first cycle is to prove the spine works.

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 9A (simplified)
(Contracts) (Infra)    (Auth)      (LLMs)     (Guardrails) (xAgent single-agent, no tools/memory/skills)
```

**Definition of done (amended — see Amendment Log):** cases **1–10** of [Phase 0 Contract 15 — First-Cycle Smoke Test](./phase-00-contracts.md#contract-15--first-cycle-smoke-test-) pass twice on a cold-deployed dev environment — they gate the spine (Phases 0–4 + 9A). Cases **11–15** gate the enterprise wave (WP12/WP14); **all 15** cases are required for full first-cycle sign-off.

Only after this path is verified and stable do the remaining phases begin.

---

## All Phases

| Phase | Name | First Cycle | Status | Depends On |
|-------|------|-------------|--------|------------|
| [Phase 0](./phase-00-contracts.md) | Contracts & Standards | ⚡ Full | ⏳ | — |
| [Phase 1](./phase-01-infrastructure.md) | Infrastructure Foundation | ⚡ Partial | ⏳ | Phase 0 |
| [Phase 2](./phase-02-auth.md) | SharedCore / Auth | ⚡ Partial | ⏳ | Phase 1 |
| [Phase 3](./phase-03-llms.md) | SharedCore / LLMs | ⚡ Partial | ⏳ | Phase 1, 2 |
| [Phase 4](./phase-04-guardrails.md) | SharedCore / Guardrails | ⚡ Partial | ⏳ | Phase 1, 2 |
| [Phase 5](./phase-05-rag.md) | SharedCore / RAG | 📋 | ⏳ | Phase 1, 2, 3 |
| [Phase 6](./phase-06-memory.md) | SharedCore / Memory | 📋 | ⏳ | Phase 1, 2, 3 |
| [Phase 7](./phase-07-tools.md) | Tools (MCP Servers) | 📋 | ⏳ | Phase 1, 2 |
| [Phase 8](./phase-08-skills.md) | Skills System | 📋 | ⏳ | Phase 5, 7 |
| [Phase 9](./phase-09-xagent.md) | xAgent Core | ⚡ Partial | ⏳ | Phase 2, 3, 4 |
| [Phase 10](./phase-10-a2a-orchestration.md) | A2A & Orchestration | 📋 | ⏳ | Phase 9 |
| [Phase 11](./phase-11-platform-management.md) | Platform Management | 📋 | ⏳ | Phase 1–9 |
| [Phase 12](./phase-12-frontend.md) | Frontend | 📋 | ⏳ | Phase 2–9 |
| [Phase 13](./phase-13-hardening.md) | Hardening & External Readiness | 📋 | ⏳ | All phases |
| [Phase 14](./phase-14-sdks.md) | SDKs | 📋 | ⏳ | Phase 13 |

---

## Build Order (Full View)

```
FIRST CYCLE (verify end-to-end flow)
─────────────────────────────────────────────────
Phase 0   Contracts & Standards
Phase 1   Infrastructure Foundation       (first cycle subset)
Phase 2   SharedCore / Auth               (first cycle subset)
Phase 3   SharedCore / LLMs              (first cycle subset)
Phase 4   SharedCore / Guardrails        (first cycle subset)
Phase 9   xAgent Core                    (single-agent, first cycle subset)
─────────────────────────────────────────────────
↓ VERIFY FIRST CYCLE WORKS END-TO-END ↓
─────────────────────────────────────────────────

FULL ENTERPRISE BUILD (after first cycle verified)
─────────────────────────────────────────────────
Phase 5   SharedCore / RAG
Phase 6   SharedCore / Memory
Phase 7   Tools (MCP Servers)
Phase 8   Skills System
Phase 9   xAgent Core (enhance with Memory, RAG, Skills, Tools)
Phase 10  A2A & Orchestration
Phase 11  Platform Management
Phase 12  Frontend
Phase 13  Hardening & External Readiness
Phase 14  SDKs
─────────────────────────────────────────────────
```

---

## How to Use These Phase Files

Each phase file contains:
1. **Phase Overview** — what this phase delivers to the platform
2. **High Level Design (HLD)** — system context, component diagram, key architectural decisions
3. **Low Level Design (LLD)** — detailed components, data models, API contracts, flows, security, scalability

> **Rule:** Every LLD section must be fully designed (even 📋 items) before implementation of ⚡ items begins. Design everything, implement ⚡ first.

4. **Service Architecture Note** — reminder that each service needs its own architecture plan before code is written
5. **First Cycle Checklist** — what to build first
6. **Full Enterprise Checklist** — what to build after first cycle is verified

---

*Reference documents:*
- *[CYPHERX_AI_PLATFORM_PLAN.md](../CYPHERX_AI_PLATFORM_PLAN.md) — Master platform plan*
- *[CYPHERX_AI_ENTERPRISE_FLOW.md](../CYPHERX_AI_ENTERPRISE_FLOW.md) — Enterprise architecture flow*
