"""API-layer contract tests (network-free). Health, the Contract-2 error envelope, and the
reserved-key / validation guard (which short-circuits BEFORE any DB access)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_livez_ok(client: TestClient) -> None:
    r = client.get("/livez")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_metrics_exposed(client: TestClient) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "cypherxa1_" in r.text or r.text == ""  # registry may be empty before any metric fires


def test_unknown_route_renders_contract2_envelope(client: TestClient) -> None:
    r = client.get("/v1/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    assert set(body["error"]) >= {"code", "message", "request_id", "trace_id", "timestamp"}


def test_copilot_rejects_reserved_and_unknown_body_keys(client: TestClient) -> None:
    # extra="forbid": identity/unknown keys in the body -> 422 before the handler touches the DB.
    r = client.post("/v1/copilot/ask", json={"question": "hi", "tenant_id": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_copilot_requires_question(client: TestClient) -> None:
    r = client.post("/v1/copilot/ask", json={})
    assert r.status_code == 422


def test_request_id_echoed(client: TestClient) -> None:
    r = client.get("/livez", headers={"X-Request-ID": "abc-123"})
    assert r.headers.get("x-request-id") == "abc-123"


def test_ui_console_served(client: TestClient) -> None:
    # The self-contained UI-1 console is served same-origin at /ui (no auth).
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "Engineering Memory" in r.text


def test_malformed_non_json_body_is_422_not_500(client: TestClient) -> None:
    # A non-JSON body (wrong content-type) makes pydantic's RequestValidationError carry a
    # BYTES `input`; the Contract-2 handler must still render a clean 422 (never crash 500
    # on bytes serialization). Regression for the bug caught by live HTTP testing.
    r = client.post("/v1/copilot/ask", content=b"not json at all", headers={"content-type": "text/plain"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
