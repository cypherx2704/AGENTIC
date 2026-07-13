"""Shared downstream-error surfacing.

Every platform service answers a failure with the Contract-2 envelope::

    {"error": {"code": "UNAUTHORIZED", "message": "<exactly what went wrong>", ...}}

That ``message`` is the whole diagnosis. Reporting only the status code throws it away and turns a
precise, actionable rejection into an unfalsifiable mystery — a bare ``401`` from guardrails has
FIVE distinct causes (bad service token, bad/expired forwarded agent JWT, ``on_behalf_of``
mismatch, missing forwarded JWT, missing ``tenant_id`` claim) and the status alone cannot tell you
which. This helper pulls the message back out so the failure explains itself.
"""

from __future__ import annotations

from typing import Any

import httpx

#: Bound on the echoed detail — it is another service's message, not a payload to relay wholesale.
MAX_DETAIL_CHARS = 300
#: Keys a service may carry its human-readable failure in (Contract-2 first, then common fallbacks).
_FALLBACK_KEYS = ("message", "detail", "error", "reason")


def _clean(value: Any) -> str:
    return " ".join(str(value).split())[:MAX_DETAIL_CHARS] if isinstance(value, str) else ""


def error_detail(resp: httpx.Response) -> str:
    """Extract the downstream service's own failure message from ``resp``.

    Handles the Contract-2 envelope first (``{"error": {"code", "message"}}``), then the common
    flat shapes, then raw text. Returns ``""`` when there is nothing useful to say.
    """
    try:
        body = resp.json()
    except ValueError:
        return _clean(resp.text.strip())

    if isinstance(body, dict):
        envelope = body.get("error")
        if isinstance(envelope, dict):  # Contract-2
            message = _clean(envelope.get("message"))
            code = _clean(envelope.get("code"))
            if message and code and code not in message:
                return f"{code}: {message}"
            if message:
                return message
            if code:
                return code
        if isinstance(envelope, str) and envelope.strip():
            return _clean(envelope)
        for key in _FALLBACK_KEYS:
            detail = _clean(body.get(key))
            if detail:
                return detail
    return ""
