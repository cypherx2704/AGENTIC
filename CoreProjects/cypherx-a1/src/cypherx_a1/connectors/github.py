"""GitHub connector (MVP).

Normalizes GitHub repos / pull-requests / issues / pushes into the canonical model. Two
modes (``CONNECTOR_MODE``):

* ``mock`` (default, keyless local) — replays a small bundled fixture repo so the whole
  ingest → graph → RAG → copilot path runs end-to-end with no GitHub token.
* ``live`` — calls the GitHub REST API (best-effort first cycle: pulls + issues), and
  verifies real webhook signatures.

The fixtures deliberately include explicit ``depends_on`` / ``owns`` edges so the demo
queries (``who_owns``, ``what_breaks_if_changed``, ``experts_on``, ``why_built``) all work
without an LLM — knowledge EXTRACTION (the LLM pass) then enriches the graph with more
edges when a real provider is configured.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from ..core.config import Settings
from ..models.canonical import CanonicalEdge, CanonicalNode, CanonicalRecord, NodeRef, RagDoc
from .base import Connector, SyncBatch

logger = structlog.get_logger(__name__)


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def _person(login: str, name: str, email: str) -> CanonicalNode:
    """A person node keyed by canonical email (cross-tool identity anchor)."""
    return CanonicalNode(
        kind="person",
        source="github",
        natural_key=email.lower(),
        title=name or login,
        search_text=f"{name} {login} {email}",
        external_id=login,
        attrs={"login": login, "email": email},
        identity_handles=[("github", login.lower()), ("email", email.lower())],
    )


def _repo_record(full_name: str, description: str, html_url: str) -> CanonicalRecord:
    repo = CanonicalNode(
        kind="repo",
        source="github",
        natural_key=full_name,
        title=full_name,
        search_text=f"{full_name} {description}",
        external_id=full_name,
        attrs={"url": html_url, "description": description},
    )
    return CanonicalRecord(
        source="github",
        record_type="repository",
        external_id=full_name,
        content_sha=_sha("repo", full_name, description),
        nodes=[repo],
    )


def _pr_record(
    *,
    full_name: str,
    number: int,
    title: str,
    body: str,
    author: CanonicalNode,
    reviewers: list[CanonicalNode],
    html_url: str,
    state: str,
) -> CanonicalRecord:
    pr_key = f"{full_name}#{number}"
    pr = CanonicalNode(
        kind="pr",
        source="github",
        natural_key=pr_key,
        title=title,
        search_text=f"{title}\n{body}",
        external_id=str(number),
        attrs={"number": number, "url": html_url, "state": state, "repo": full_name},
    )
    repo_ref = NodeRef(kind="repo", natural_key=full_name)
    nodes = [pr, author, *reviewers]
    edges = [
        CanonicalEdge(rel="authored", src=author.ref, dst=pr.ref, metadata={"via": "github"}),
        CanonicalEdge(rel="part_of", src=pr.ref, dst=repo_ref),
    ]
    edges += [CanonicalEdge(rel="reviewed", src=r.ref, dst=pr.ref) for r in reviewers]
    docs = [
        RagDoc(
            kb="eng-code",
            name=f"PR {pr_key}: {title}",
            content=f"# {title}\n\n{body}",
            node=pr.ref,
            metadata={
                "repo": full_name,
                "pr_number": number,
                "author": author.attrs.get("login"),
                "url": html_url,
            },
        )
    ]
    return CanonicalRecord(
        source="github",
        record_type="pull_request",
        external_id=pr_key,
        content_sha=_sha("pr", pr_key, title, body, state),
        nodes=nodes,
        edges=edges,
        docs=docs,
    )


def _issue_record(
    *, full_name: str, number: int, title: str, body: str, author: CanonicalNode, html_url: str
) -> CanonicalRecord:
    key = f"{full_name}#issue-{number}"
    ticket = CanonicalNode(
        kind="ticket",
        source="github",
        natural_key=key,
        title=title,
        search_text=f"{title}\n{body}",
        external_id=str(number),
        attrs={"number": number, "url": html_url, "repo": full_name},
    )
    repo_ref = NodeRef(kind="repo", natural_key=full_name)
    return CanonicalRecord(
        source="github",
        record_type="issue",
        external_id=key,
        content_sha=_sha("issue", key, title, body),
        nodes=[ticket, author],
        edges=[
            CanonicalEdge(rel="authored", src=author.ref, dst=ticket.ref),
            CanonicalEdge(rel="part_of", src=ticket.ref, dst=repo_ref),
        ],
        docs=[
            RagDoc(
                kb="eng-docs",
                name=f"Issue {key}: {title}",
                content=f"# {title}\n\n{body}",
                node=ticket.ref,
                metadata={"repo": full_name, "issue_number": number, "url": html_url},
            )
        ],
    )


def _change_record(
    *, full_name: str, sha: str, message: str, author: CanonicalNode, ts_iso: str, files: list[str]
) -> CanonicalRecord:
    """A discrete CHANGE event (a commit). The activity timeline orders these by time and
    attributes them to their author; `touched` links the change to the repo it changed."""
    key = f"{full_name}@{sha}"
    change = CanonicalNode(
        kind="change", source="github", natural_key=key, title=message,
        search_text=f"{message} {' '.join(files)}", external_id=sha,
        attrs={"sha": sha, "message": message, "timestamp": ts_iso, "repo": full_name,
               "author": author.attrs.get("login"), "files": files,
               "url": f"https://github.com/{full_name}/commit/{sha}"},
    )
    repo_ref = NodeRef(kind="repo", natural_key=full_name)
    return CanonicalRecord(
        source="github", record_type="commit", external_id=key,
        content_sha=_sha("change", key, message),
        nodes=[change, author],
        edges=[
            CanonicalEdge(rel="authored", src=author.ref, dst=change.ref, metadata={"ts": ts_iso}),
            CanonicalEdge(rel="touched", src=change.ref, dst=repo_ref, metadata={"files": files}),
        ],
    )


def _fixture_records(granularity: str = "auto") -> list[CanonicalRecord]:
    """A small, self-contained engineering history for the keyless MVP demo. When
    ``granularity`` is 'commit' or 'auto', commit-level `change` events are included."""
    alice = _person("alice", "Alice Ng", "alice@acme.io")
    bob = _person("bob", "Bob Reyes", "bob@acme.io")
    carol = _person("carol", "Carol Sun", "carol@acme.io")
    full = "acme/payments"

    records: list[CanonicalRecord] = []

    # Repos + services (with explicit ownership + dependency edges).
    records.append(_repo_record(full, "Payment processing service", "https://github.com/acme/payments"))
    auth_svc = CanonicalNode(kind="service", source="github", natural_key="auth-service",
                             title="auth-service", search_text="auth-service token validation JWT")
    pay_db = CanonicalNode(kind="service", source="github", natural_key="payments-db",
                           title="payments-db", search_text="payments-db postgres ledger")
    records.append(
        CanonicalRecord(
            source="github", record_type="topology", external_id="acme/payments:topology",
            content_sha=_sha("topology", "acme/payments", "v1"),
            nodes=[auth_svc, pay_db, alice, carol],
            edges=[
                CanonicalEdge(rel="owns", src=alice.ref, dst=NodeRef("repo", full)),
                CanonicalEdge(rel="owns", src=carol.ref, dst=NodeRef("service", "auth-service")),
                CanonicalEdge(rel="depends_on", src=NodeRef("repo", full), dst=NodeRef("service", "auth-service"),
                              metadata={"reason": "validates request tokens via auth-service"}),
                CanonicalEdge(rel="depends_on", src=NodeRef("repo", full), dst=NodeRef("service", "payments-db")),
            ],
        )
    )

    records.append(
        _pr_record(
            full_name=full, number=101,
            title="Add Stripe webhook handler",
            body=("Adds a Stripe webhook receiver. Validates the incoming signature and the "
                  "caller token via auth-service before recording the charge in payments-db."),
            author=alice, reviewers=[bob], html_url="https://github.com/acme/payments/pull/101",
            state="merged",
        )
    )
    records.append(
        _pr_record(
            full_name=full, number=102,
            title="Refactor payment retry with exponential backoff",
            body="Reworks the payment retry loop to use exponential backoff and honour Retry-After.",
            author=bob, reviewers=[alice], html_url="https://github.com/acme/payments/pull/102",
            state="merged",
        )
    )
    records.append(
        _issue_record(
            full_name=full, number=5,
            title="Payment retries fail on 429 from auth-service",
            body="When auth-service returns 429 the retry loop gives up immediately instead of backing off.",
            author=bob, html_url="https://github.com/acme/payments/issues/5",
        )
    )

    # Commit-level change events (Phase B) — included unless granularity is forced to pr_ticket.
    if granularity in ("auto", "commit"):
        records.append(_change_record(
            full_name=full, sha="c0ffee1", message="Add Stripe webhook signature verification",
            author=alice, ts_iso="2026-06-10T09:00:00Z", files=["src/webhooks/stripe.py"]))
        records.append(_change_record(
            full_name=full, sha="beef002", message="Tune payment retry exponential backoff",
            author=bob, ts_iso="2026-06-12T14:30:00Z", files=["src/payments/retry.py"]))
    return records


class GitHubConnector(Connector):
    kind = "github"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def streams(self) -> list[str]:
        return ["fixtures"] if self._settings.connector_mode == "mock" else ["pulls", "issues"]

    async def full_sync(self, *, stream: str, cursor: str | None) -> SyncBatch:
        if self._settings.connector_mode == "mock":
            recs = _fixture_records(self._settings.connector_change_granularity)
            return SyncBatch(records=recs, next_cursor=None, done=True)
        return await self._live_sync(stream=stream, cursor=cursor)

    async def incremental_sync(self, *, stream: str, cursor: str | None) -> SyncBatch:
        # First cycle: incremental == a bounded full re-pull (content_sha dedup skips
        # unchanged objects at the landing stage, so re-pulling is cheap and correct).
        return await self.full_sync(stream=stream, cursor=cursor)

    async def _live_sync(self, *, stream: str, cursor: str | None) -> SyncBatch:
        """Best-effort live GitHub pull (PRs / issues). Requires GITHUB_TOKEN + a repo in
        the connector config; returns an empty done batch if unconfigured."""
        token = self._settings.github_token
        repo = (cursor or "").strip()  # the pipeline passes "owner/name" as the stream cursor seed
        if not token or "/" not in repo:
            logger.info("github_live_sync_unconfigured", stream=stream)
            return SyncBatch(records=[], next_cursor=None, done=True)
        base = self._settings.github_api_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        records: list[CanonicalRecord] = [_repo_record(repo, "", f"https://github.com/{repo}")]
        try:
            if stream == "pulls":
                resp = await self._http().get(
                    f"{base}/repos/{repo}/pulls?state=all&per_page={self._settings.backfill_page_size}",
                    headers=headers,
                )
                for pr in resp.json() if resp.status_code < 400 else []:
                    user = pr.get("user") or {}
                    author = _person(user.get("login", "unknown"), user.get("login", ""), "")
                    records.append(
                        _pr_record(
                            full_name=repo, number=pr["number"], title=pr.get("title", ""),
                            body=pr.get("body") or "", author=author, reviewers=[],
                            html_url=pr.get("html_url", ""), state=pr.get("state", "open"),
                        )
                    )
            elif stream == "issues":
                resp = await self._http().get(
                    f"{base}/repos/{repo}/issues?state=all&per_page={self._settings.backfill_page_size}",
                    headers=headers,
                )
                for issue in resp.json() if resp.status_code < 400 else []:
                    if "pull_request" in issue:
                        continue  # the issues endpoint also returns PRs
                    user = issue.get("user") or {}
                    author = _person(user.get("login", "unknown"), user.get("login", ""), "")
                    records.append(
                        _issue_record(
                            full_name=repo, number=issue["number"], title=issue.get("title", ""),
                            body=issue.get("body") or "", author=author, html_url=issue.get("html_url", ""),
                        )
                    )
        except httpx.HTTPError as exc:
            logger.warning("github_live_sync_failed", stream=stream, error=str(exc))
        return SyncBatch(records=records, next_cursor=None, done=True)

    def verify_signature(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        """Verify ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body)."""
        secret = self._settings.github_webhook_secret
        if not secret:
            return False
        sig = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256") or ""
        if not sig.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def parse_webhook(self, *, event: str, payload: dict[str, Any]) -> list[CanonicalRecord]:
        full = (payload.get("repository") or {}).get("full_name", "unknown/unknown")
        if event == "pull_request":
            pr = payload.get("pull_request") or {}
            user = pr.get("user") or {}
            author = _person(user.get("login", "unknown"), user.get("login", ""), "")
            return [
                _pr_record(
                    full_name=full, number=pr.get("number", 0), title=pr.get("title", ""),
                    body=pr.get("body") or "", author=author, reviewers=[],
                    html_url=pr.get("html_url", ""), state=pr.get("state", "open"),
                )
            ]
        if event == "issues":
            issue = payload.get("issue") or {}
            user = issue.get("user") or {}
            author = _person(user.get("login", "unknown"), user.get("login", ""), "")
            return [
                _issue_record(
                    full_name=full, number=issue.get("number", 0), title=issue.get("title", ""),
                    body=issue.get("body") or "", author=author, html_url=issue.get("html_url", ""),
                )
            ]
        if event == "push":
            return [_repo_record(full, "", (payload.get("repository") or {}).get("html_url", ""))]
        logger.info("github_webhook_ignored_event", event=event)
        return []
