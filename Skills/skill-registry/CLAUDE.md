# CLAUDE.md — skill-registry (CypherX Skills, Phase 8)

> Central **Skill** catalogue + per-agent access control. A near-exact **mirror of
> `Tools/tool-registry`** over the `skills` Postgres schema. Read the workspace-root
> `CLAUDE.md` first for the platform mental model; read `Tools/tool-registry/CLAUDE.md`
> for the shared design rationale (RLS marketplace-hole fix, ETag health poll, dual-mode
> auth) — this file documents only what differs.

## What this service is

`skill-registry` is the `skills`-schema sibling of the tool registry: it stores **skills**
(declarative, reusable agent capabilities — instructions/templates, Contract 11 / Phase 8),
their versioned manifests, and the **per-agent access control** that xAgent's `SKILL_LOAD`
stage enforces. Python 3.12 / FastAPI / uv; runtime role **`skill_user`** (non-superuser,
RLS-enforced); listens on **8080** in-container, host **8095**, in-network
`http://skill-registry:8080`.

It is built by mechanically mirroring `tool-registry` (`tool`→`skill`, `tools`→`skills`),
so the API surface, auth, RLS pattern, discovery/shadowing, version retention, and health
state-machine code are identical. The substantive differences are below.

## How it differs from tool-registry

- **Skills are DECLARATIVE, not live MCP servers.** A skill is a capability definition, not
  an HTTP server xAgent invokes over `/mcp/v1/invoke`. So:
  - **Startup seeding is OFF by default** (`SEED_PLATFORM_SKILLS=false`) — there is no
    canonical platform skill *server* to health-poll. The SQL migration `…0002__seed.sql`
    still seeds a sample platform skill row for discovery. (The health-poll machinery is
    inherited from tool-registry and remains for parity; it is a no-op unless you enable
    seeding and point a skill at a live `base_url`.)
  - `SKILL_LOAD` in xAgent **does not invoke** skills — it resolves their manifests +
    access mode and splices the permitted skill names/descriptions into the LLM prompt.
- **Schema/role:** schema `skills`, role `skill_user` (vs `tools` / `tool_user`).
- **Manifest capability array key is `skills`** (the mirror renamed Contract-4 `tools:[…]`
  → `skills:[…]`); `services/manifest.py::declared_capabilities` reads `manifest["skills"]`.

## Schema (`skills` — migrations in `db/migrations/`)

| Migration | Creates |
|-----------|---------|
| `20260624_0001__init.sql` | `skills.skills`, `skill_versions`, `skill_capabilities`, `skill_health` + RLS (split read/write/platform policies, the marketplace-hole `WITH CHECK`) + role `skill_user` + grants |
| `20260624_0002__seed.sql` | sample platform skill (`tenant_id IS NULL`) + version + capability + health rows |
| `20260624_0003__init.sql` | `skills.restricted_skills` + `skills.agent_skill_access` (per-agent `access_mode` ∈ `none`\|`ask`\|`automated`) + RLS + grants |

`agent_skill_access` is the access gate xAgent consults: `none` → the skill is dropped (not
offered to the model); `ask`/`automated` → offered. RLS via `app.tenant_id` (`SET LOCAL`),
pooled-reset-safe `NULLIF(current_setting('app.tenant_id',true),'')::uuid`.

## API (`/v1`, mirrors tool-registry)

- `GET /v1/skills` — discovery (platform ∪ tenant, tenant-priority shadowing).
- `GET /v1/skills/{name}` — resolve one (optional `?version=`).
- `POST /v1/skills`, `POST /v1/skills/{name}/versions` — register (scope `skill:admin`|`platform:admin`).
- `GET/PUT /v1/skills/{name}/access` — read/set an agent's access mode (set requires `tenant:admin`).
- `GET /v1/restricted-skills`, `POST /v1/restricted-skills/{name}` — restricted-skill registry.
- `GET /livez` `/readyz` `/metrics` (Contract 7).

## Build / test / run

```bash
cd Skills/skill-registry
uv sync                 # create .venv (deps identical to tool-registry)
uv run pytest -q        # pure-logic tests pass offline; HTTP/lifespan tests need a live Postgres
uv run python -m skill_registry   # local run (PORT defaults 8000; image sets 8080)
```

Migrations are applied by the compose `--profile migrate` job (mounted at
`/migrations/skill-registry`), which also provisions the `skill_user` password +
`search_path=skills` from `SKILL_DB_PASSWORD`.

## Guards (do NOT "fix")

- The inherited RLS **split-policy `WITH CHECK`** (marketplace-hole fix) is load-bearing —
  do not collapse it to a single `USING`-only policy.
- `SEED_PLATFORM_SKILLS=false` is intentional (skills have no platform server to poll).
- `__main__.py` defaults `PORT` to 8000 when unset; the image sets 8080 — expected (mirror
  of the tool-registry/memory convention).
