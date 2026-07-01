"""WP06 — SSRF-hardened image fetcher (``services/image_fetch.py``).

Unit tests of the vetting logic — NO network. DNS resolution is monkeypatched so
``_resolve_and_vet_host`` sees deterministic IPs, and ``fetch_image_as_data_uri``'s
scheme/host gates are tested directly (they reject BEFORE any connection). The actual
streamed download is exercised against a monkeypatched ``httpx.AsyncClient`` so we cover
the content-type / size-cap / happy-path branches without touching the network.

Asserted SSRF rejections (all -> 400 VALIDATION_ERROR with a specific ``reason``):
* loopback (127.0.0.1, ::1), private RFC1918 (10/8, 192.168/16), link-local
  (169.254/16), the cloud-metadata IP (169.254.169.254), IPv6 ULA (fc00::/7);
* non-http(s) schemes (file:, data:), a URL with no host;
A normal PUBLIC host passes the IP-vetting gate and (with a mocked transport) returns a
``data:<mime>;base64,...`` URI; an over-size or non-image response is rejected.
"""

from __future__ import annotations

import socket

import pytest

from llms_gateway.core.config import Settings
from llms_gateway.core.errors import ApiError
from llms_gateway.services import image_fetch

_SETTINGS = Settings(image_inline_required=True, image_fetch_max_bytes=1024)

# host -> resolved IP for the fake resolver.
_IP_BY_HOST = {
    "loopback.test": "127.0.0.1",
    "v6loop.test": "::1",
    "private10.test": "10.0.0.5",
    "private192.test": "192.168.1.10",
    "linklocal.test": "169.254.1.1",
    "metadata.test": "169.254.169.254",
    "ula6.test": "fc00::1",
    "public.test": "93.184.216.34",  # example.com — a real public unicast addr
}


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, *a, **k):  # type: ignore[no-untyped-def]
        ip = _IP_BY_HOST.get(host)
        if ip is None:
            raise socket.gaierror(f"unknown host {host}")
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _reason(host: str) -> str:
    try:
        image_fetch._resolve_and_vet_host(host)
        return "PASS"
    except ApiError as e:
        return e.details["reason"]


@pytest.mark.parametrize(
    "host",
    ["loopback.test", "v6loop.test", "private10.test", "private192.test",
     "linklocal.test", "metadata.test", "ula6.test"],
)
def test_non_public_ips_rejected(host: str) -> None:
    assert _reason(host) == "IMAGE_URL_BLOCKED"


def test_public_host_passes_vetting() -> None:
    assert _reason("public.test") == "PASS"


def test_unresolvable_host_rejected() -> None:
    assert _reason("does-not-exist.test") == "IMAGE_URL_UNRESOLVABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "IMAGE_URL_SCHEME_BLOCKED"),
        ("data:image/png;base64,AAAA", "IMAGE_URL_SCHEME_BLOCKED"),
        ("gopher://public.test/x", "IMAGE_URL_SCHEME_BLOCKED"),
        ("http:///no-host-here", "IMAGE_URL_INVALID"),
    ],
)
async def test_scheme_and_host_gates(url: str, reason: str) -> None:
    with pytest.raises(ApiError) as ei:
        await image_fetch.fetch_image_as_data_uri(url, settings=_SETTINGS)
    assert ei.value.details["reason"] == reason


@pytest.mark.asyncio
async def test_blocked_host_rejected_before_fetch() -> None:
    with pytest.raises(ApiError) as ei:
        await image_fetch.fetch_image_as_data_uri(
            "http://metadata.test/latest/meta-data/", settings=_SETTINGS
        )
    assert ei.value.details["reason"] == "IMAGE_URL_BLOCKED"


# ── streamed download branches (mocked httpx transport) ─────────────────────────────
class _FakeResp:
    def __init__(self, *, status_code=200, headers=None, chunks=(b"hello",)) -> None:
        self.status_code = status_code
        self.headers = headers or {"content-type": "image/png"}
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeStreamCtx:
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self._resp
    async def __aexit__(self, *exc): return False


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; returns a pre-canned streamed response."""
    _resp: _FakeResp

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    def stream(self, method, url): return _FakeStreamCtx(type(self)._resp)


def _install_client(monkeypatch: pytest.MonkeyPatch, resp: _FakeResp) -> None:
    cls = type("_C", (_FakeAsyncClient,), {"_resp": resp})
    monkeypatch.setattr(image_fetch.httpx, "AsyncClient", cls)


@pytest.mark.asyncio
async def test_public_image_fetch_returns_data_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(
        monkeypatch,
        _FakeResp(headers={"content-type": "image/png"}, chunks=(b"\x89PNG", b"data")),
    )
    uri = await image_fetch.fetch_image_as_data_uri("http://public.test/a.png", settings=_SETTINGS)
    assert uri.startswith("data:image/png;base64,")
    import base64

    payload = uri.split(",", 1)[1]
    assert base64.b64decode(payload) == b"\x89PNGdata"


@pytest.mark.asyncio
async def test_non_image_content_type_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, _FakeResp(headers={"content-type": "text/html"}))
    with pytest.raises(ApiError) as ei:
        await image_fetch.fetch_image_as_data_uri("http://public.test/x", settings=_SETTINGS)
    assert ei.value.details["reason"] == "IMAGE_CONTENT_TYPE_BLOCKED"


@pytest.mark.asyncio
async def test_oversize_streamed_body_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    big = b"X" * (_SETTINGS.image_fetch_max_bytes + 1)
    _install_client(monkeypatch, _FakeResp(headers={"content-type": "image/jpeg"}, chunks=(big,)))
    with pytest.raises(ApiError) as ei:
        await image_fetch.fetch_image_as_data_uri("http://public.test/big.jpg", settings=_SETTINGS)
    assert ei.value.details["reason"] == "IMAGE_TOO_LARGE"


@pytest.mark.asyncio
async def test_oversize_via_content_length_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(
        monkeypatch,
        _FakeResp(
            headers={
                "content-type": "image/jpeg",
                "content-length": str(_SETTINGS.image_fetch_max_bytes + 1),
            },
            chunks=(b"x",),
        ),
    )
    with pytest.raises(ApiError) as ei:
        await image_fetch.fetch_image_as_data_uri("http://public.test/big.jpg", settings=_SETTINGS)
    assert ei.value.details["reason"] == "IMAGE_TOO_LARGE"


@pytest.mark.asyncio
async def test_http_error_status_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, _FakeResp(status_code=404))
    with pytest.raises(ApiError) as ei:
        await image_fetch.fetch_image_as_data_uri("http://public.test/missing", settings=_SETTINGS)
    assert ei.value.details["reason"] == "IMAGE_FETCH_FAILED"
