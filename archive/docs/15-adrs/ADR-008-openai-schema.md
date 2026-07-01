# ADR-008 · OpenAI-Superset Schema as the LLM Gateway Wire Format

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX's `llms-gateway` service is the single choke point for all LLM calls across the platform. It needs a wire format that callers (xAgent, cypherx-a1, future Skills) use to submit chat completions and receive responses — regardless of which actual provider (Anthropic Claude, OpenAI GPT, OpenRouter, self-hosted models) the gateway routes the request to. The gateway must also normalize tool-calling (function-call) syntax and streaming (SSE) responses across providers. The question is which schema to adopt as the external-facing canonical format.

## Decision

`llms-gateway` exposes a **superset of the OpenAI Chat Completions API** (`POST /v1/chat/completions`) as its external wire format. Callers submit requests in the OpenAI schema (`model`, `messages`, `tools`, `stream`, etc.); the gateway's response follows the OpenAI schema (`choices[].message`, `usage`, `tool_calls`, SSE `data: {"choices":[...]}` chunks). Internally the gateway translates to the target provider's native format: for Anthropic, it translates to the Anthropic Messages API (system prompt extraction, `content` block conversion, `tool_use`/`tool_result` normalization); for OpenAI-compatible providers, it passes through with minor normalization. CypherX-specific extensions (metering, tenant context) are carried in HTTP headers (`X-Tenant-ID`, `X-Model-Alias`) rather than in the request body, keeping the body schema OpenAI-compatible. The gateway logs full normalized request/response (minus raw content for PII) and emits a `cypherx.llms.request.completed` Kafka event with token counts and cost.

## Rationale

### Why This

OpenAI's Chat Completions API is the de facto industry standard for LLM interaction: it is implemented natively by OpenAI, natively supported by OpenRouter, and has compatibility layers in most open-source serving stacks (vLLM, Ollama, LM Studio, Llama.cpp). By adopting it as the external schema, callers can use standard OpenAI client libraries without modification, and the platform can swap the underlying provider without any changes to caller code.

The alternative of exposing the Anthropic Messages API externally was considered, but Anthropic's schema (`content` block arrays, `tool_use`/`tool_result` block types, system as a top-level parameter) is not natively understood by OpenRouter or most tooling. Using a provider-specific schema as the external format would lock callers to that provider's ergonomics even when using a different provider underneath.

The "superset" framing is important: CypherX adds fields not in the OpenAI schema (e.g., `model_alias` resolution, `guardrails_enabled` flag as a header) while remaining backward-compatible with tools that speak plain OpenAI.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| Anthropic Messages API as the external schema | Less widely adopted; not natively supported by OpenRouter or vLLM; callers would need Anthropic-specific client code. The `content` block array model is more expressive than OpenAI's but requires more effort to integrate for simple use cases. |
| Custom CypherX LLM schema | Requires every caller to learn a new API; no existing client libraries; increases friction for onboarding external developers. The standardization benefit of OpenAI compatibility is lost. |
| Pass-through (no normalization, caller picks provider format) | Eliminates the gateway's provider-abstraction value proposition. Callers become coupled to the specific provider; switching providers requires updating all callers. Tool-calling normalization cannot be provided. |
| gRPC / protobuf instead of REST/JSON | Would enable streaming with bidirectional streaming RPCs, but gRPC is not natively supported by browsers (requires grpc-web proxy), adding gateway complexity. OpenAI-compatible SSE streaming is well-understood and widely tooled. |
| LangChain / LlamaIndex unified interface | Framework-level abstraction, not a wire format. Services are not allowed to share code libraries across language boundaries; a Python framework cannot help Kotlin callers. The wire format must be language-agnostic. |

## Consequences

### Positive

- Any tool or library that speaks OpenAI Chat Completions API (LangChain, LlamaIndex, Semantic Kernel, raw `openai` Python SDK with `base_url` override) can call `llms-gateway` without modification.
- Provider swaps (Anthropic → OpenAI → OpenRouter) are transparent to all callers; model aliases (`gpt-4o`, `claude-3-7-sonnet`) are resolved to the actual provider+model in the gateway.
- Tool-calling normalization is centralized: xAgent emits OpenAI `tools` arrays; the gateway translates to Anthropic `tools` blocks internally. New providers are supported by adding a translation layer in the gateway only.
- SSE streaming normalization (OpenAI `delta` chunks) is handled once in the gateway; all callers get a consistent streaming protocol regardless of provider.
- Token usage and cost metering are centralized: every LLM call, regardless of provider, produces a normalized `usage` object and a billing event. No per-provider billing logic in callers.

### Negative / Trade-offs

- The gateway must maintain up-to-date translation layers for every supported provider. When Anthropic releases a new `content` block type (e.g., `image`, `document`) or a new tool-call response format, the gateway translation must be updated before callers can use it.
- OpenAI schema has some ambiguities (e.g., `finish_reason` values vary slightly by provider). The gateway must normalize these; a normalization bug can produce subtly wrong responses that are hard to debug.
- CypherX-specific metadata (tenant, model alias, guardrails toggle) carried in HTTP headers means callers cannot use the OpenAI Python SDK's `client.chat.completions.create()` directly (it doesn't pass custom headers) — they must either use `httpx` directly or the SDK's `extra_headers` parameter.
- The translation layer adds ~1–3 ms latency to every LLM request (JSON parsing + field mapping). Acceptable given LLM inference latency is 200–2000+ ms.
- Anthropic's extended thinking blocks and multi-turn tool-result conversations require non-trivial translation to OpenAI's flatter message array model; edge cases in multi-turn agentic tool loops may require special handling.
