-- =====================================================================================
-- llms-gateway — data-driven multi-provider BYOK: per-connection base_url + kind, and
-- runtime-addable providers (OpenRouter / OpenAI / self-hosted / future) with NO code or
-- manual seed. PostgreSQL 16. Additive + idempotent.
--
-- WHY: the user's design is "connect ANY provider from the UI/API, keyed per-tenant in the
-- DB, no env keys, no platform fallback, no code change per provider". To support an
-- OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, OpenAI itself, future ones) the
-- gateway needs (a) a per-connection base_url, and (b) a 'kind' telling the router which
-- wire protocol to speak ('openai_compatible' or 'anthropic'). The /v1/keys endpoint
-- auto-creates the providers row, so adding a brand-new provider is just an API/UI call.
-- =====================================================================================

-- Free-form provider names: drop the FK to llms.providers so a brand-new provider can be
-- connected from the UI/API with NO seed/code change. The `kind` column drives routing; the
-- providers table is now just a display/default-base_url registry for the UI dropdown.
ALTER TABLE llms.tenant_provider_keys DROP CONSTRAINT IF EXISTS tenant_provider_keys_provider_fkey;

-- per-tenant connection: base_url + kind (defaults preserve existing OpenAI/Anthropic rows)
ALTER TABLE llms.tenant_provider_keys ADD COLUMN IF NOT EXISTS base_url TEXT;
ALTER TABLE llms.tenant_provider_keys ADD COLUMN IF NOT EXISTS kind     TEXT NOT NULL DEFAULT 'openai_compatible';
ALTER TABLE llms.tenant_provider_keys ADD COLUMN IF NOT EXISTS label    TEXT;  -- optional friendly name for the UI

-- providers registry gains a default base_url + kind (display/default hints; the per-key
-- base_url/kind always win). enabled stays TRUE.
ALTER TABLE llms.providers ADD COLUMN IF NOT EXISTS base_url TEXT;
ALTER TABLE llms.providers ADD COLUMN IF NOT EXISTS kind     TEXT NOT NULL DEFAULT 'openai_compatible';

-- tag the known providers' wire protocol
UPDATE llms.providers SET kind = 'anthropic'         WHERE name = 'anthropic';
UPDATE llms.providers SET kind = 'openai',  base_url = 'https://api.openai.com/v1' WHERE name = 'openai';

-- seed a few common OpenAI-compatible providers for the UI dropdown (display only — a tenant
-- still supplies their own key; base_url here is the default the UI pre-fills). Adding more
-- later is an INSERT (or just register a key with a custom provider name + base_url).
INSERT INTO llms.providers (name, display_name, enabled, default_priority, kind, base_url) VALUES
  ('openrouter', 'OpenRouter',          TRUE, 15, 'openai_compatible', 'https://openrouter.ai/api/v1'),
  ('together',   'Together AI',         TRUE, 30, 'openai_compatible', 'https://api.together.xyz/v1'),
  ('groq',       'Groq',                TRUE, 30, 'openai_compatible', 'https://api.groq.com/openai/v1'),
  ('self-hosted','Self-hosted (OpenAI-compatible)', TRUE, 50, 'openai_compatible', NULL)
ON CONFLICT (name) DO NOTHING;

-- =====================================================================================
-- end 20260614_0007__byok_multi_provider.sql
-- =====================================================================================
