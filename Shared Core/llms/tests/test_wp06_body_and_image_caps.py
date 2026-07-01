"""WP06 — request body-size cap (middleware) + multimodal image caps (chat).

Deterministic, no live infra (mock provider, ``db_pool=None``).

Body cap (``core/body_limit.py``): the ``BodySizeLimitMiddleware`` rejects a request
whose ``Content-Length`` exceeds ``max_request_body_bytes`` with a 413
``PAYLOAD_TOO_LARGE`` envelope. The middleware captures its cap at app construction
(from ``get_settings()``), so to avoid sending 25 MiB we patch the cap LOW on the
``Middleware`` kwargs BEFORE the stack is built, then send a normal small JSON body that
exceeds the (tiny) cap. A normal request under the cap still 200s.

Image caps (``api/chat.py``): a chat request with more image parts than
``max_images_per_request`` -> 413 ``IMAGE_COUNT_EXCEEDED``; inline (data-URI) image bytes
over ``max_image_bytes`` -> 413 ``IMAGE_BYTES_EXCEEDED``; a text-only request is
unaffected. Both per-handler caps read ``request.app.state.settings`` live, so we flip
them on ``app.state.settings`` per-case.
"""

from __future__ import annotations

import base64
import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.core.config import Settings  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"


def _fake_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT, agent_id="b", scopes=["llm:invoke"], principal_type="agent",
    )


def _txt(t: str) -> dict:
    return {"type": "text", "text": t}


def _img(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def _patch_body_cap(app, max_bytes: int) -> None:
    """Patch the BodySizeLimitMiddleware cap before the stack is built (first request)."""
    for mw in app.user_middleware:
        if mw.cls.__name__ == "BodySizeLimitMiddleware":
            mw.kwargs["max_bytes"] = max_bytes


@pytest_asyncio.fixture
async def make_client():  # type: ignore[no-untyped-def]
    """Factory yielding (app, ac); allows patching the body cap before lifespan start."""
    managers: list = []

    async def _factory(body_cap: int | None = None):  # type: ignore[no-untyped-def]
        app = create_app()
        app.dependency_overrides[require_principal] = _fake_principal
        if body_cap is not None:
            _patch_body_cap(app, body_cap)
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        managers.append(lm)
        app.state.db_pool = None
        app.state.valkey = None
        # Fresh, per-test settings so live cap mutations don't leak via the shared
        # get_settings() lru_cache singleton across tests.
        app.state.settings = Settings()
        ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await ac.__aenter__()
        managers.append(ac)
        return app, ac

    yield _factory
    for m in reversed(managers):
        await m.__aexit__(None, None, None)


# ── body cap ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_body_over_cap_413(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(body_cap=10)  # 10-byte cap; any real JSON body exceeds it
    resp = await ac.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": "well over ten bytes"}]},
    )
    assert resp.status_code == 413, resp.text
    err = resp.json()["error"]
    assert err["code"] == "PAYLOAD_TOO_LARGE"
    assert err["details"]["reason"] == "BODY_BYTES_EXCEEDED"
    assert err["details"]["max_bytes"] == 10


@pytest.mark.asyncio
async def test_small_body_under_cap_200(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(body_cap=None)  # default 25 MiB cap
    resp = await ac.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text


# ── image caps ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_image_count_exceeded_413(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client()
    cap = app.state.settings.max_images_per_request
    parts = [_txt("look")] + [_img("http://example.com/a.png") for _ in range(cap + 1)]
    resp = await ac.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": parts}]},
    )
    assert resp.status_code == 413, resp.text
    err = resp.json()["error"]
    # The chat image-cap path raises VALIDATION_ERROR with a 413 status override (distinct
    # from the body-limit middleware's PAYLOAD_TOO_LARGE code) — the reason is the signal.
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["reason"] == "IMAGE_COUNT_EXCEEDED"
    assert err["details"]["images"] == cap + 1


@pytest.mark.asyncio
async def test_inline_image_bytes_exceeded_413(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client()
    app.state.settings.max_image_bytes = 10  # tiny cap so a small inline image trips it
    big = base64.b64encode(b"X" * 200).decode()
    parts = [_img(f"data:image/png;base64,{big}")]
    resp = await ac.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": parts}]},
    )
    assert resp.status_code == 413, resp.text
    err = resp.json()["error"]
    assert err["details"]["reason"] == "IMAGE_BYTES_EXCEEDED"
    assert err["details"]["max_bytes"] == 10


@pytest.mark.asyncio
async def test_text_only_request_unaffected_by_image_caps(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client()
    app.state.settings.max_images_per_request = 0  # would block any image, but there are none
    resp = await ac.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": "text only please"}]},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_url_image_under_count_cap_does_not_count_bytes(make_client) -> None:  # type: ignore[no-untyped-def]
    # A single remote (non-inline) image: counts toward the count cap but contributes 0
    # inline bytes, so with image_inline_required=False (default) it passes to the mock.
    app, ac = await make_client()
    app.state.settings.max_image_bytes = 1  # 1 byte; the URL image must still not trip it
    parts = [_txt("hi"), _img("https://example.com/pic.png")]
    resp = await ac.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": parts}]},
    )
    assert resp.status_code == 200, resp.text
