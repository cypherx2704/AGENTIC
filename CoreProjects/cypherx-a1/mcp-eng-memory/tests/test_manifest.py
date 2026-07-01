"""Manifest endpoint + Contract-4 conformance (network-free)."""

from __future__ import annotations

import json
import pathlib

from fastapi.testclient import TestClient

_CYPHER_ROOT = pathlib.Path(__file__).resolve().parents[4]  # .../Cypher
_SCHEMA = _CYPHER_ROOT / "contracts" / "mcp" / "manifest.schema.json"


def test_manifest_served_with_etag(client: TestClient) -> None:
    r = client.get("/manifest")
    assert r.status_code == 200
    etag = r.headers.get("ETag")
    assert etag
    body = r.json()
    assert body["name"] == "mcp-eng-memory"
    assert {t["name"] for t in body["tools"]} >= {"who_owns", "what_breaks_if_changed", "experts_on"}

    # If-None-Match -> 304.
    r2 = client.get("/manifest", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_manifest_conforms_to_contract4_schema(client: TestClient) -> None:
    manifest = client.get("/manifest").json()
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # validator optional; structural check otherwise
        for key in schema.get("required", []):
            assert key in manifest
        return
    errors = list(Draft202012Validator(schema).iter_errors(manifest))
    assert not errors, [(list(e.path), e.message) for e in errors]


def test_required_scopes(client: TestClient) -> None:
    manifest = client.get("/manifest").json()
    assert manifest["required_scopes"] == ["tool:invoke", "tool:mcp-eng-memory:invoke"]
