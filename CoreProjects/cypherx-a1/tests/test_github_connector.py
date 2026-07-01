"""GitHub connector — pure (mock mode), no network. Validates the connector SPI contract:
the fixtures normalize into the canonical model, the webhook signature verifies, and a
webhook delivery parses into canonical records."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from cypherx_a1.connectors.github import GitHubConnector
from cypherx_a1.connectors.registry import get_connector, supported_kinds
from cypherx_a1.core.config import get_settings
from cypherx_a1.models.canonical import CanonicalRecord


@pytest.fixture
def connector() -> GitHubConnector:
    return GitHubConnector(get_settings())


async def test_registry_knows_github() -> None:
    assert "github" in supported_kinds()
    assert isinstance(get_connector("github", get_settings()), GitHubConnector)


async def test_full_sync_mock_returns_canonical_records(connector: GitHubConnector) -> None:
    batch = await connector.full_sync(stream="fixtures", cursor=None)
    assert batch.done
    assert batch.records, "mock fixtures should yield records"
    kinds = {n.kind for r in batch.records for n in r.nodes}
    assert {"repo", "service", "person", "pr", "ticket"} <= kinds
    rels = {e.rel for r in batch.records for e in r.edges}
    # The demo graph must include ownership + dependency so who_owns / what_breaks work keyless.
    assert {"owns", "authored", "depends_on", "part_of"} <= rels


async def test_records_are_idempotent_by_content_sha(connector: GitHubConnector) -> None:
    a = await connector.full_sync(stream="fixtures", cursor=None)
    b = await connector.full_sync(stream="fixtures", cursor=None)
    sha_a = sorted(r.content_sha for r in a.records)
    sha_b = sorted(r.content_sha for r in b.records)
    assert sha_a == sha_b, "content_sha must be deterministic across syncs (re-ingest dedup)"


async def test_pr_record_has_rag_doc_and_author_edge(connector: GitHubConnector) -> None:
    batch = await connector.full_sync(stream="fixtures", cursor=None)
    prs = [r for r in batch.records if r.record_type == "pull_request"]
    assert prs
    pr = prs[0]
    assert pr.docs and pr.docs[0].kb == "eng-code"
    assert any(e.rel == "authored" for e in pr.edges)
    assert any(e.rel == "part_of" for e in pr.edges)


def test_verify_signature_roundtrip(connector: GitHubConnector) -> None:
    settings = get_settings()
    body = b'{"zen":"ok"}'
    sig = "sha256=" + hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert connector.verify_signature(headers={"x-hub-signature-256": sig}, body=body)
    assert not connector.verify_signature(headers={"x-hub-signature-256": "sha256=deadbeef"}, body=body)
    assert not connector.verify_signature(headers={}, body=body)


def test_parse_webhook_pull_request(connector: GitHubConnector) -> None:
    payload = {
        "repository": {"full_name": "acme/web", "html_url": "https://github.com/acme/web"},
        "pull_request": {"number": 7, "title": "Add login", "body": "adds login",
                         "user": {"login": "dana"}, "html_url": "u", "state": "open"},
    }
    records = connector.parse_webhook(event="pull_request", payload=payload)
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, CanonicalRecord)
    assert any(n.kind == "pr" and n.natural_key == "acme/web#7" for n in rec.nodes)
