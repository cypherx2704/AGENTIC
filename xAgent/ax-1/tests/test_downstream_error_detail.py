"""A downstream failure must EXPLAIN ITSELF.

Every platform service answers with the Contract-2 envelope naming exactly what went wrong. Logging
only the status code discards that and turns a precise rejection into an unfalsifiable mystery: a
bare 401 from guardrails has FIVE distinct causes and the status alone cannot tell you which. These
lock in that the message survives to the caller.
"""

from __future__ import annotations

import httpx

from agent_runtime.services.errors import MAX_DETAIL_CHARS, error_detail


def _resp(status: int, **kw: object) -> httpx.Response:
    return httpx.Response(status, **kw)  # type: ignore[arg-type]


def test_contract2_envelope_yields_code_and_message() -> None:
    resp = _resp(401, json={"error": {
        "code": "UNAUTHORIZED",
        "message": "Service token on_behalf_of does not match forwarded agent JWT agent_id.",
    }})
    detail = error_detail(resp)
    assert "UNAUTHORIZED" in detail
    assert "on_behalf_of does not match" in detail


def test_the_five_distinct_401_causes_are_each_distinguishable() -> None:
    causes = [
        "Missing or malformed Authorization header.",
        "Service token requires X-Forwarded-Agent-JWT header.",
        "Service token on_behalf_of does not match forwarded agent JWT agent_id.",
        "Agent token missing tenant_id claim.",
        "Invalid token: Signature has expired",
    ]
    details = {error_detail(_resp(401, json={"error": {"code": "UNAUTHORIZED", "message": c}}))
               for c in causes}
    assert len(details) == len(causes)  # a bare "401" collapses all five into one


def test_message_only_envelope() -> None:
    assert error_detail(_resp(403, json={"error": {"message": "Token missing scope 'mem:write'."}})) \
        == "Token missing scope 'mem:write'."


def test_code_only_envelope_falls_back_to_the_code() -> None:
    assert error_detail(_resp(401, json={"error": {"code": "UNAUTHORIZED"}})) == "UNAUTHORIZED"


def test_flat_shapes_and_raw_text() -> None:
    assert error_detail(_resp(400, json={"message": "bad input"})) == "bad input"
    assert error_detail(_resp(400, json={"detail": "nope"})) == "nope"
    assert error_detail(_resp(500, text="boom")) == "boom"


def test_empty_and_unparseable_are_safe() -> None:
    assert error_detail(_resp(500, text="")) == ""
    assert error_detail(_resp(500, json={})) == ""
    assert error_detail(_resp(500, json=[1, 2])) == ""


def test_detail_is_bounded() -> None:
    detail = error_detail(_resp(500, json={"error": {"message": "x" * 5000}}))
    assert len(detail) <= MAX_DETAIL_CHARS
