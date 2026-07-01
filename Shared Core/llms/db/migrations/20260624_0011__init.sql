-- =====================================================================================
-- llms-gateway — native vs emulated tool-calling capability + small-model catalog.
-- PostgreSQL 16. Idempotent: safe to re-run top-to-bottom. Apply AFTER 20260623_0010.
--
-- WHY: xAgent's tool loop offers tools to the model via the NATIVE tools[] function
-- -calling API and reads back message.tool_calls. Small (≈7-8B) open models either do
-- not support that API or do it unreliably, so they silently never call a tool. This
-- migration adds the capability flag the gateway uses to decide, PER MODEL, whether to
-- pass tools natively or to EMULATE tool-calling (inject the tool schemas + a strict
-- tool-call protocol into the prompt and parse the model's text back into normalized
-- tool_calls). Emulation makes EVERY model — small or large — able to use platform
-- tools through the exact same /v1/chat/completions `tools` contract.
--
--   native_tool_use = true   -> pass tools[] to the provider (frontier models).
--   native_tool_use = false  -> gateway emulates tool-calling in the prompt (small models).
--
-- Also seeds a representative catalog of small open models served via an OpenAI
-- -compatible endpoint (BYOK base_url: Ollama / vLLM / Together / Groq / self-hosted),
-- all native_tool_use=false, plus a `small` platform alias. No provider_pricing rows
-- (self-hosted/local models meter at cost 0 unless the tenant adds a pricing row).
-- =====================================================================================

-- ── 1) native_tool_use capability flag (defaults true => existing rows stay native) ──
ALTER TABLE llms.model_capabilities
  ADD COLUMN IF NOT EXISTS native_tool_use BOOLEAN NOT NULL DEFAULT true;

-- ── 2) Seed small (≈7-8B) open models — OpenAI-compatible shape, emulated tools ──────
INSERT INTO llms.model_capabilities
  (model_id, provider, max_tokens_cap, context_window,
   supports_vision, supports_tools, supports_streaming, embedding_dim, native_tool_use)
VALUES
  ('llama-3.1-8b-instruct', 'openai', 4096, 128000, false, true, true, NULL, false),
  ('qwen2.5-7b-instruct',   'openai', 8192,  32768, false, true, true, NULL, false),
  ('mistral-7b-instruct',   'openai', 4096,  32768, false, true, true, NULL, false)
ON CONFLICT (model_id) DO NOTHING;

-- ── 3) `small` platform alias (tenant_id IS NULL) -> the llama 8B default ────────────
INSERT INTO llms.model_aliases (tenant_id, alias, model_id, provider)
VALUES (NULL, 'small', 'llama-3.1-8b-instruct', 'openai')
ON CONFLICT (alias) WHERE tenant_id IS NULL DO NOTHING;
