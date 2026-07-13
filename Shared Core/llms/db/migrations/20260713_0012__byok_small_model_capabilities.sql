-- 0012 — register capabilities for BYOK models whose ids are NOT in the built-in table.
--
-- WHY THIS EXISTS
-- ---------------
-- `model_capabilities.native_tool_use` defaults to TRUE ("assume frontier, native tool use"), and
-- CapabilityRegistry.get() returns NULL for an unregistered model_id — so ANY BYOK model that is
-- not listed here silently gets NATIVE tool-calling. For a small open model that is wrong and
-- fails hard:
--
--   Groq's `llama-3.1-8b-instant` is the SAME model as the built-in `llama-3.1-8b-instruct`
--   (which is already registered with native_tool_use=false) — but under a different id, so the
--   lookup misses. Sent tools natively, the 8B model emits one of Llama-3.1's THREE BUILT-IN
--   tools (`brave_search`, `wolfram_alpha`, `code_interpreter` — baked into its training) instead
--   of the tool it was offered. Groq validates every tool call against request.tools, does not
--   find `brave_search`, and rejects the WHOLE request:
--
--     "tool call validation failed: attempted to call tool 'brave_search'
--      which was not in request.tools"
--
-- Setting native_tool_use=false routes the model through the gateway's tool-calling EMULATION: no
-- `tools` array is sent to the provider, so the provider's validation cannot fire, and a tool name
-- the model invents is handled by our own parser (recorded `tool_not_allowed`, loop continues)
-- rather than killing the task.
--
-- Rule of thumb encoded below: <= ~30B open model -> emulate; >= ~70B / frontier -> native.

INSERT INTO llms.model_capabilities
  (model_id, provider, max_tokens_cap, context_window,
   supports_vision, supports_tools, supports_streaming, native_tool_use)
VALUES
  -- Groq's id for llama-3.1-8b (INSTANT, not INSTRUCT — the id that was missing).
  ('llama-3.1-8b-instant',                   'groq',       8192,  131072, false, true, true, false),
  -- Groq Qwen 27B — capable, but not reliable enough at native tool-calling; emulate.
  ('qwen/qwen3.6-27b',                       'groq',       8192,  32768,  false, true, true, false),
  -- OpenRouter free tier used for local testing.
  ('google/gemma-4-31b-it:free',             'openrouter', 8192,  32768,  false, true, true, false),
  ('poolside/laguna-xs-2.1:free',            'openrouter', 4096,  32768,  false, true, true, false),
  -- 70B handles the native tool API reliably.
  ('meta-llama/llama-3.3-70b-instruct:free', 'openrouter', 8192,  131072, false, true, true, true)
ON CONFLICT (model_id) DO UPDATE SET
  provider           = EXCLUDED.provider,
  max_tokens_cap     = EXCLUDED.max_tokens_cap,
  context_window     = EXCLUDED.context_window,
  supports_vision    = EXCLUDED.supports_vision,
  supports_tools     = EXCLUDED.supports_tools,
  supports_streaming = EXCLUDED.supports_streaming,
  native_tool_use    = EXCLUDED.native_tool_use,
  updated_at         = now();
