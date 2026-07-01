"""SSRF-hardened image fetcher (WP06 multimodal — config-gated, OFF by default).

DEFAULT BEHAVIOUR is URL pass-through: ``image_url`` content parts are forwarded to the
provider as-is and this module is NEVER invoked. It is only used when
``settings.image_inline_required`` is True, in which case the gateway downloads each
``image_url`` to an inline base64 data URI before forwarding (so the provider never sees
a client-supplied URL, and the image is size/type-validated up front).

Because the gateway would then be making an outbound HTTP request to a CLIENT-CONTROLLED
URL, this is a Server-Side Request Forgery (SSRF) sink and is hardened accordingly:

* **Scheme allow-list** — only ``http``/``https``; ``file:``, ``gopher:``, ``data:``, etc.
  are rejected.
* **DNS resolution + IP vetting** — the host is resolved to its IP(s) and EVERY resolved
  address must be a global/public unicast address. Loopback (127/8, ::1), private RFC1918
  (10/8, 172.16/12, 192.168/16), link-local (169.254/16, fe80::/10), the cloud metadata
  address (169.254.169.254), unique-local (fc00::/7), multicast, and reserved ranges are
  all rejected. (A best-effort guard against DNS-rebinding within a single resolve.)
* **Size cap** — ``image_fetch_max_bytes`` enforced via Content-Length AND while streaming
  the body (a missing/lying Content-Length cannot blow memory).
* **Timeout** — a single ``image_fetch_timeout_seconds`` connect+read budget.
* **Content-Type allow-list** — the response ``Content-Type`` must be ``image/*``.
* **No redirects followed by default** — a 30x to an internal host is a classic SSRF
  bypass, so redirects are disabled (the resolved target is the only one we vet).

On ANY violation a Contract-2 :class:`ApiError` (400 VALIDATION_ERROR, a specific
``reason``) is raised so the caller surfaces a clear client error rather than leaking.
"""

from __future__ import annotations

import base64
import ipaddress
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
import structlog

from ..core.errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ..core.config import Settings

logger = structlog.get_logger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _reject(reason: str, message: str) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_ERROR,
        message,
        status_code=400,
        details={"reason": reason},
    )


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally-routable public unicast address.

    Rejects loopback, private (RFC1918 / ULA fc00::/7), link-local (incl. the
    169.254.169.254 metadata address), multicast, reserved, and unspecified addresses.
    """
    if (
        ip.is_loopback
        or ip.is_private  # RFC1918 + ULA fc00::/7 (Python folds ULA into is_private)
        or ip.is_link_local  # 169.254/16, fe80::/10 — also covers the metadata IP
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # is_global is the authoritative "public unicast" check on modern Python.
    return bool(getattr(ip, "is_global", True))


def _resolve_and_vet_host(host: str) -> None:
    """Resolve ``host`` and reject if ANY resolved address is non-public (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise _reject("IMAGE_URL_UNRESOLVABLE", f"Could not resolve image host '{host}'.") from exc

    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise _reject("IMAGE_URL_UNRESOLVABLE", f"Could not resolve image host '{host}'.")

    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])  # strip any IPv6 zone id
        except ValueError:
            raise _reject("IMAGE_URL_BLOCKED", "Image host resolved to an invalid address.") from None
        if not _ip_is_public(ip):
            logger.info("image_fetch_blocked", reason="non_public_ip", host=host, ip=str(ip))
            raise _reject(
                "IMAGE_URL_BLOCKED",
                "Image URL resolves to a non-public (private/loopback/link-local) address.",
            )


async def fetch_image_as_data_uri(url: str, *, settings: Settings) -> str:
    """Download ``url`` to a ``data:<mime>;base64,...`` URI with SSRF + size/type guards.

    Raises a Contract-2 :class:`ApiError` (400) on any policy violation or fetch failure.
    Only ever called when ``settings.image_inline_required`` is True.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise _reject(
            "IMAGE_URL_SCHEME_BLOCKED",
            f"Image URL scheme '{parsed.scheme}' is not allowed (only http/https).",
        )
    host = parsed.hostname
    if not host:
        raise _reject("IMAGE_URL_INVALID", "Image URL has no host.")

    # Vet the resolved IP(s) BEFORE opening any connection.
    _resolve_and_vet_host(host)

    max_bytes = settings.image_fetch_max_bytes
    timeout = settings.image_fetch_timeout_seconds

    try:
        # Redirects OFF: a 30x to an internal host is a classic SSRF bypass — we only
        # vetted the original target. Stream so we can abort once the cap is crossed.
        async with (
            httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise _reject(
                    "IMAGE_FETCH_FAILED",
                    f"Image fetch returned HTTP {resp.status_code}.",
                )
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                raise _reject(
                    "IMAGE_CONTENT_TYPE_BLOCKED",
                    f"Image URL returned a non-image content-type '{content_type or 'unknown'}'.",
                )
            declared = resp.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise _reject(
                    "IMAGE_TOO_LARGE",
                    f"Image exceeds the maximum of {max_bytes} bytes.",
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise _reject(
                        "IMAGE_TOO_LARGE",
                        f"Image exceeds the maximum of {max_bytes} bytes.",
                    )
                chunks.append(chunk)
    except ApiError:
        raise  # our own policy rejection — propagate verbatim
    except httpx.HTTPError as exc:
        logger.info("image_fetch_failed", error=str(exc))
        raise _reject("IMAGE_FETCH_FAILED", "Failed to fetch the image URL.") from exc

    data = b"".join(chunks)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{b64}"
