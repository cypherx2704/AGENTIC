"""LLMs-gateway client (LLM stage).

Calls ``POST /v1/chat/completions`` on the llms-gateway and normalises the unified
response into a :class:`ChatCompletion`. Identity flows via HEADERS only (Contract 13):

  * ``Authorization: Bearer <xAgent service JWT>``     (Contract 12, on_behalf_of=agent)
  * ``X-Forwarded-Agent-JWT: <inbound agent JWT>``      (verbatim forward, Phase 9 rule)
  * ``traceparent: <current trace>``                    (Contract 8 propagation)

Body: ``{ model, messages, max_tokens, temperature, tools:[], stream:false }`` — NO
identity. First cycle is a single round-trip (no tool loop): ``tools`` is always empty
and ``stream`` is always false.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@dataclass
class Usage:
    """Token + cost accounting from the gateway's unified usage block."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class ToolCall:
    """A single tool call the model requested (normalised from the unified response)."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatCompletion:
    """Normalised single-choice completion."""

    content: str | None
    finish_reason: str | None
    model: str
    usage: Usage = field(default_factory=Usage)
    # Tool calls the model requested this turn (WP12 tool loop). Empty for a plain
    # completion — the first-cycle single round-trip never sets it.
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class LlmsClient:
    """Thin async client for the llms-gateway chat-completions endpoint."""

    def __init__(
        self,
        settings: Settings,
        token_provider: ServiceTokenProvider,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        tool_mode: str | None = None,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> ChatCompletion:
        """Single non-streaming round-trip. Body carries NO identity (Contract 13).

        ``tools`` (WP12) is the OPTIONAL list of tool schemas the model may call; it
        defaults to ``None`` which sends ``tools: []`` — byte-identical to the first-cycle
        single round-trip. When the model requests tools the parsed ``tool_calls`` are
        surfaced on the :class:`ChatCompletion` for the tool-loop stage to dispatch.

        ``tool_mode`` (auto|native|emulated) is forwarded to the gateway only when set; it
        selects native vs. emulated tool-calling. Omitted -> the gateway decides per-model
        via ``auto`` (emulate small/non-native models so they can use tools too).
        """
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        headers = {
            "Authorization": f"Bearer {service_jwt}",
            "X-Forwarded-Agent-JWT": agent_jwt,
            "traceparent": trace.current_traceparent(),
            "X-Request-ID": trace.request_id_var.get(),
        }
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools or [],  # empty unless the tool-loop stage supplies schemas
            "stream": False,
        }
        if tool_mode is not None:
            body["tool_mode"] = tool_mode
        url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/chat/completions"
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("llms", "error").inc()
            logger.warning("llms_call_failed", error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "LLMs gateway unavailable.") from exc
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("llms", "rejected").inc()
            raise self._error_from_response(resp)
        metrics.downstream_calls_total.labels("llms", "ok").inc()
        return self._parse(resp.json())

    @staticmethod
    def _error_from_response(resp: httpx.Response) -> ApiError:
        """Map a gateway non-2xx into an ApiError that PRESERVES a client/config code.

        BUG 2 — do NOT collapse every gateway error into SERVICE_UNAVAILABLE:

          * a **4xx** is a client / config error owned by THIS request (e.g. 422
            ``MODEL_UNSUPPORTED`` — the agent's configured model is not supported). The
            gateway returns a Contract-2 envelope ``{"error": {"code", "message"}}``; we
            surface that upstream ``code`` (+ message) on the task result so the failure is
            actionable, instead of masking it as an availability error. A missing/garbled
            envelope falls back to VALIDATION_ERROR (still a 4xx-family client error).
          * a **5xx** (or a 408/429 throttle) is a genuine gateway availability/transport
            problem -> SERVICE_UNAVAILABLE (retryable), as before.
        """
        status = resp.status_code
        upstream_code, upstream_msg = LlmsClient._parse_error_envelope(resp)
        # 5xx, plus the retryable 408/429, are availability problems -> SERVICE_UNAVAILABLE.
        if status >= 500 or status in (408, 429):
            logger.warning("llms_call_unavailable", status=status, upstream_code=upstream_code)
            return ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"LLMs gateway returned {status}.",
            )
        # 4xx client/config error: surface the upstream code (do not mask as availability).
        code = upstream_code or ErrorCode.VALIDATION_ERROR
        logger.warning("llms_call_rejected", status=status, upstream_code=code)
        return ApiError(
            code,
            upstream_msg or f"LLMs gateway rejected the request ({status}).",
            status_code=status,
            details={"upstream_status": status, "upstream_code": upstream_code},
        )

    @staticmethod
    def _parse_error_envelope(resp: httpx.Response) -> tuple[str | None, str | None]:
        """Extract ``(code, message)`` from a Contract-2 gateway error body (best-effort).

        Tolerates the canonical ``{"error": {"code", "message"}}`` envelope, a flat
        ``{"code", "message"}`` body, and an unparsable / non-JSON body (-> (None, None)).
        Never raises — error mapping must never itself error.
        """
        try:
            data = resp.json()
        except (ValueError, TypeError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        nested = data.get("error")
        err: dict[str, Any] = nested if isinstance(nested, dict) else data
        code = err.get("code")
        message = err.get("message")
        return (
            str(code) if isinstance(code, str) and code else None,
            str(message) if isinstance(message, str) and message else None,
        )

    @staticmethod
    def _parse(data: dict[str, Any]) -> ChatCompletion:
        choices = data.get("choices") or [{}]
        choice = choices[0]
        message = choice.get("message") or {}
        u = data.get("usage") or {}
        return ChatCompletion(
            content=message.get("content"),
            finish_reason=choice.get("finish_reason"),
            model=data.get("model", ""),
            usage=Usage(
                prompt_tokens=int(u.get("prompt_tokens", 0)),
                completion_tokens=int(u.get("completion_tokens", 0)),
                total_tokens=int(u.get("total_tokens", 0)),
                cost_usd=float(u.get("cost_usd", 0.0)),
            ),
            tool_calls=LlmsClient._parse_tool_calls(message),
            raw=data,
        )

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
        """Normalise the unified ``message.tool_calls`` into :class:`ToolCall` objects.

        Tolerates the OpenAI-style ``{id, function:{name, arguments}}`` shape (arguments
        as a JSON string) AND a flat ``{id, name, arguments}`` shape. A malformed/empty
        block yields an empty list (a plain completion). Never raises.
        """
        raw_calls = message.get("tool_calls") or []
        calls: list[ToolCall] = []
        for rc in raw_calls:
            if not isinstance(rc, dict):
                continue
            fn = rc.get("function") if isinstance(rc.get("function"), dict) else rc
            name = str(fn.get("name", "") or "")
            if not name:
                continue
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except ValueError:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(id=str(rc.get("id", "") or ""), name=name, arguments=args))
        return calls
