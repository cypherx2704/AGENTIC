"""Object storage client (MinIO/S3) — presigned PUT, HeadObject, PutObject, delete.

Env-driven (``S3_BUCKET`` / ``S3_ENDPOINT`` / ``S3_SSE_MODE``); bucket names are NEVER
schema literals. Implements AWS SigV4 presigning with the stdlib only (no boto3) so the
dependency surface stays minimal. The key layout is ``<tenant_id>/<doc_id>/<filename>`` in
both the cloud and compose forms.

For the test suite / offline local there is no live MinIO: ``head_object`` and
``put_object`` degrade to a no-op success when ``S3_ENDPOINT`` is empty or the client is
constructed in mock mode, so the inline-ingest + finalize flows are exercisable without
object storage. Presigning is pure (no network) and always available.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import re
import urllib.parse
from dataclasses import dataclass

import httpx
import structlog

from ..core.config import Settings

logger = structlog.get_logger(__name__)

_UNRESERVED = re.compile(r"[^A-Za-z0-9._~-]")


def sanitize_filename(name: str) -> str:
    """Strip path separators + unsafe chars from a client filename."""
    base = name.replace("\\", "/").split("/")[-1]
    base = base.replace("..", "")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    return cleaned or "file"


def object_key(tenant_id: str, doc_id: str, filename: str) -> str:
    """Canonical key: <tenant_id>/<doc_id>/<sanitised_filename>."""
    return f"{tenant_id}/{doc_id}/{sanitize_filename(filename)}"


def build_source_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


@dataclass
class HeadResult:
    exists: bool
    size_bytes: int | None = None


def _uri_encode(value: str, *, encode_slash: bool = True) -> str:
    out = []
    for ch in value:
        if _UNRESERVED.match(ch):
            out.append(f"%{ord(ch):02X}")
        elif ch == "/" and not encode_slash:
            out.append(ch)
        else:
            out.append(ch)
    # Fall back to urllib for the actual encoding of reserved chars.
    return urllib.parse.quote(value, safe="" if encode_slash else "/")


class ObjectStore:
    """Minimal S3/MinIO client. Access/secret keys come from env via Settings extension."""

    def __init__(
        self,
        settings: Settings,
        *,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        # Default to the env-driven Settings creds (S3_ACCESS_KEY / S3_SECRET_KEY); an explicit
        # arg still wins (tests). Previously these defaulted to a hardcoded 'minioadmin' that
        # never matched the running MinIO, so the presigned upload/download path always failed.
        self._access_key = access_key if access_key is not None else settings.s3_access_key
        self._secret_key = secret_key if secret_key is not None else settings.s3_secret_key
        self._region = region
        self._client = client
        self._owns_client = client is None

    @property
    def bucket(self) -> str:
        return self._settings.s3_bucket

    @property
    def _enabled(self) -> bool:
        """True only when a live endpoint is configured (network ops degrade otherwise)."""
        return bool(self._settings.s3_endpoint)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # ── Presigned PUT (pure; no network) ─────────────────────────────────────
    def presign_put(
        self, key: str, *, content_type: str, expires: int | None = None
    ) -> str:
        """Return a SigV4 presigned PUT URL for ``key`` (15-min default expiry)."""
        expires = expires or self._settings.presign_expiry_seconds
        endpoint = self._settings.s3_endpoint.rstrip("/") or "http://localhost:9000"
        host = urllib.parse.urlparse(endpoint).netloc
        now = _dt.datetime.now(_dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        canonical_uri = "/" + self.bucket + "/" + urllib.parse.quote(key, safe="/")

        params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self._access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        if self._settings.s3_sse_mode == "kms":
            params["x-amz-server-side-encryption"] = "aws:kms"
            if self._settings.s3_kms_key_id:
                params["x-amz-server-side-encryption-aws-kms-key-id"] = self._settings.s3_kms_key_id
        canonical_qs = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted(params.items())
        )
        canonical_headers = f"host:{host}\n"
        canonical_request = "\n".join(
            ["PUT", canonical_uri, canonical_qs, canonical_headers, "host", "UNSIGNED-PAYLOAD"]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signing_key = self._signing_key(date_stamp)
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        return f"{endpoint}{canonical_uri}?{canonical_qs}&X-Amz-Signature={signature}"

    def _signing_key(self, date_stamp: str) -> bytes:
        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = _sign(("AWS4" + self._secret_key).encode(), date_stamp)
        k_region = _sign(k_date, self._region)
        k_service = _sign(k_region, "s3")
        return _sign(k_service, "aws4_request")

    # ── Network ops (degrade to success when no endpoint configured) ──────────
    async def head_object(self, key: str) -> HeadResult:
        if not self._enabled:
            # No live storage (tests / offline) — treat as present so finalize proceeds.
            return HeadResult(exists=True, size_bytes=None)
        url = f"{self._settings.s3_endpoint.rstrip('/')}/{self.bucket}/{urllib.parse.quote(key, safe='/')}"
        try:
            resp = await self._http().head(url)
        except httpx.HTTPError as exc:
            logger.warning("s3_head_failed", key=key, error=str(exc))
            return HeadResult(exists=False)
        if resp.status_code == 200:
            size = resp.headers.get("Content-Length")
            return HeadResult(exists=True, size_bytes=int(size) if size else None)
        return HeadResult(exists=False)

    async def get_object(self, key: str) -> bytes:
        """Fetch an object's bytes (used by the ingestion worker to read the upload).

        When no endpoint is configured (tests/offline) this raises so the worker treats
        it as a retryable fetch failure — tests inject text via the payload's
        ``inline_text`` field to bypass the network read entirely.
        """
        if not self._enabled:
            raise RuntimeError("no object-storage endpoint configured")
        url = f"{self._settings.s3_endpoint.rstrip('/')}/{self.bucket}/{urllib.parse.quote(key, safe='/')}"
        resp = await self._http().get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"object fetch returned {resp.status_code}")
        return resp.content

    async def put_object(self, key: str, body: bytes, *, content_type: str) -> None:
        if not self._enabled:
            logger.info("s3_put_skipped_no_endpoint", key=key)
            return
        url = f"{self._settings.s3_endpoint.rstrip('/')}/{self.bucket}/{urllib.parse.quote(key, safe='/')}"
        # Best-effort unsigned PUT for local MinIO with anonymous write; production uses the
        # IRSA-scoped path / presigned flow. Failures are logged, not fatal to the request.
        try:
            await self._http().put(url, content=body, headers={"Content-Type": content_type})
        except httpx.HTTPError as exc:
            logger.warning("s3_put_failed", key=key, error=str(exc))

    async def delete_prefix(self, prefix: str) -> bool:
        """Best-effort delete of all objects under a prefix; True on success/no-op."""
        if not self._enabled:
            return True
        # Single-object common case (one doc prefix). A full ListObjects+DeleteObjects is the
        # cloud form; first cycle the sweeper retries on failure.
        url = f"{self._settings.s3_endpoint.rstrip('/')}/{self.bucket}/{urllib.parse.quote(prefix, safe='/')}"
        try:
            await self._http().delete(url)
            return True
        except httpx.HTTPError as exc:
            logger.warning("s3_delete_failed", prefix=prefix, error=str(exc))
            return False
