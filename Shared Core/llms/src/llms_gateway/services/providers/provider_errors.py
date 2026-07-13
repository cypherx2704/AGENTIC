"""Upstream-provider exception → Contract-2 ApiError mapping (hardening, 2026-07-05).

Both the ``openai`` and ``anthropic`` SDKs (and every OpenAI-compatible provider we
reach through the OpenAI SDK — OpenRouter, Together, Groq, vLLM, …) raise typed
exceptions that carry the *real* upstream HTTP status and an error body. Historically
every provider adaptor swallowed those into a single opaque
``SERVICE_UNAVAILABLE`` (503) with a generic "provider call failed" message — so a
429 rate-limit, a 401 bad key, a 402 out-of-credit, and a genuine 5xx outage were
all indistinguishable to the caller and to the Task Runner UI.

This module maps the SDK exception to the **correct** Contract-2 error code + HTTP
status + a specific, actionable message, and preserves the upstream status/code in
``details`` (never in the user-facing headline — curated messages, per design
choice 1(A)). The mapper is duck-typed on the SDK exception's public attributes
(``status_code``, ``code``, ``body``, ``response``) rather than importing the
concrete SDK classes, so it works across SDK versions and even when the SDK's own
class hierarchy shifts.

Status remap (design choice 2(A) — a rate-limit is NOT an outage):

    upstream 429  -> 429 RATE_LIMIT_EXCEEDED  (+ Retry-After passthrough if present)
    upstream 401  -> 401 UNAUTHORIZED
    upstream 403  -> 403 FORBIDDEN
    upstream 402  -> 402 BUDGET_EXCEEDED
    upstream 404  -> 422 MODEL_UNSUPPORTED    (model retired / not on this provider)
    upstream 400  -> 400 VALIDATION_ERROR     (bad request; include upstream reason)
    upstream 408  -> 504 SERVICE_UNAVAILABLE  (upstream timeout)
    upstream 5xx  -> 503 SERVICE_UNAVAILABLE  (genuine upstream outage)
    timeout/network/unknown -> 503 SERVICE_UNAVAILABLE

Usage (in a provider adaptor)::

    try:
        completion = await client.chat.completions.create(**kwargs)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise map_provider_exception(exc, provider=self.provider, model_id=model_id) from exc
"""

from __future__ import annotations

from typing import Any

from ...core.errors import ApiError, ErrorCode


def _extract_status(exc: Exception) -> int | None:
    """Best-effort upstream HTTP status from an SDK exception.

    Both SDKs expose ``status_code`` on APIStatusError subclasses; some transport
    errors instead carry a ``response`` with ``.status_code``. Returns None for
    connection/timeout errors that never reached an HTTP status.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        rs = getattr(response, "status_code", None)
        if isinstance(rs, int):
            return rs
    return None


def _extract_upstream_message(exc: Exception) -> str | None:
    """Best-effort human-readable reason from the SDK exception's error body.

    Order: parsed ``body["error"]["message"]`` (OpenAI/OpenRouter + Anthropic both
    nest under ``error``), then ``body["message"]``, then the exception's own str().
    Kept for ``details`` / logs only — NOT the user-facing headline (design 1(A)).
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
        top = body.get("message")
        if isinstance(top, str) and top.strip():
            return top.strip()
    text = str(exc).strip()
    return text or None


def _extract_upstream_code(exc: Exception) -> str | None:
    """Best-effort provider-specific error code (e.g. 'insufficient_quota')."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            c = err.get("code") or err.get("type")
            if isinstance(c, str) and c.strip():
                return c.strip()
    return None


def _retry_after_header(exc: Exception) -> dict[str, str] | None:
    """Pass through the upstream ``Retry-After`` header on a 429, if present."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # noqa: BLE001 — headers may not be a Mapping on some transports
        return None
    if retry_after:
        return {"Retry-After": str(retry_after)}
    return None


def map_provider_exception(
    exc: Exception,
    *,
    provider: str,
    model_id: str,
    operation: str = "chat",
) -> ApiError:
    """Translate an upstream-provider SDK exception into a Contract-2 :class:`ApiError`.

    ``provider`` is the resolved provider key (``openrouter`` | ``openai`` |
    ``anthropic`` | ``together`` | …) so the message names the *real* provider, not a
    hard-coded "OpenAI". ``details`` always carries ``{provider, model, upstream_status,
    upstream_code}`` for traces + the Task Runner UI; the headline stays curated + safe.
    """
    status = _extract_status(exc)
    upstream_msg = _extract_upstream_message(exc)
    upstream_code = _extract_upstream_code(exc)

    details: dict[str, Any] = {
        "provider": provider,
        "model": model_id,
        "operation": operation,
        "upstream_status": status,
    }
    if upstream_code:
        details["upstream_code"] = upstream_code
    if upstream_msg:
        details["upstream_message"] = upstream_msg

    # ── 429: rate-limited — NOT an outage. Real 429 so client retry logic behaves. ──
    if status == 429:
        headers = _retry_after_header(exc)
        return ApiError(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            f"Upstream provider '{provider}' rate-limited the request. Free-tier models "
            "share a low shared limit — retry after a short wait, add provider credit, or "
            "use a paid model.",
            status_code=429,
            details=details,
            headers=headers,
        )

    # ── 402 / insufficient credit — surface as a budget problem, not an outage. ──
    if status == 402 or (upstream_code or "").lower() in {"insufficient_quota", "insufficient_credits"}:
        return ApiError(
            ErrorCode.BUDGET_EXCEEDED,
            f"Upstream provider '{provider}' rejected the request for insufficient credit "
            "on the account behind this connection. Add credit or switch to a funded model.",
            status_code=402,
            details=details,
        )

    # ── 401 — the API key stored for this connection was rejected upstream. ──
    if status == 401:
        return ApiError(
            ErrorCode.UNAUTHORIZED,
            f"Upstream provider '{provider}' rejected the API key for this connection "
            "(invalid, revoked, or lacking access to the requested model).",
            status_code=401,
            details=details,
        )

    # ── 403 — key valid but not permitted for this model/route upstream. ──
    if status == 403:
        return ApiError(
            ErrorCode.FORBIDDEN,
            f"Upstream provider '{provider}' forbade this request — the key is valid but "
            f"not permitted to use model '{model_id}'.",
            status_code=403,
            details=details,
        )

    # ── 404 — the model id is unknown to this provider (commonly a retired :free model). ──
    if status == 404:
        return ApiError(
            ErrorCode.MODEL_UNSUPPORTED,
            f"Upstream provider '{provider}' does not recognize model '{model_id}'. It may "
            "have been retired or renamed by the provider — pick a currently available model.",
            status_code=422,
            details=details,
        )

    # ── 400 / 422 — malformed request; the upstream reason is genuinely useful here. ──
    if status in (400, 422):
        reason = f": {upstream_msg}" if upstream_msg else "."
        return ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Upstream provider '{provider}' rejected the request as invalid{reason}",
            status_code=400,
            details=details,
        )

    # ── 408 / upstream timeout — surface as a gateway-timeout flavored 504. ──
    if status == 408:
        return ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            f"Upstream provider '{provider}' timed out handling the request.",
            status_code=504,
            details=details,
        )

    # ── 5xx / no-status (connection/timeout/DNS) / anything else — genuine outage. ──
    reason = f" ({upstream_msg})" if upstream_msg else ""
    return ApiError(
        ErrorCode.SERVICE_UNAVAILABLE,
        f"Upstream provider '{provider}' is temporarily unavailable{reason}.",
        status_code=503,
        details=details,
    )
