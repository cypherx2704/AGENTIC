"""Manifest health poll — state machine + pollable poller (WP11).

Each registered tool exposes a ``GET {base_url}/manifest`` endpoint. The registry:

* polls it EAGERLY at registration (so a freshly-registered tool's health is known
  immediately), and
* polls it every ``health_poll_interval_seconds`` from a lifespan background job.

Each poll sends ``If-None-Match: <last_etag>`` (when an etag is cached):

  * **200** -> the manifest changed (or first fetch): cache the new manifest + etag,
    treat as a SUCCESS.
  * **304 Not Modified** -> unchanged: SUCCESS (manifest/etag untouched).
  * **error / timeout / non-2xx / 5xx** -> FAILURE.

The health STATE MACHINE (pure, unit-testable — :func:`next_health_state`) maps a
consecutive-failure counter to a status:

  * a SUCCESS resets failures to 0 and sets status ``active``.
  * a FAILURE increments the counter; once it reaches ``degrade_after`` the status
    becomes ``degraded``; once it reaches ``offline_after`` it becomes ``offline``.

Everything is FAIL-SOFT: a polling error never raises out of the loop; it just
advances the failure counter and (eventually) flips the tool to degraded/offline.

The poller is decoupled from HTTP via the :class:`HttpClient` protocol so unit tests
can drive the exact 200 / 304 / error sequence with a fake client (no network).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)

STATUS_ACTIVE = "active"
STATUS_DEGRADED = "degraded"
STATUS_OFFLINE = "offline"


@dataclass(frozen=True)
class HealthThresholds:
    """Consecutive-failure thresholds for the state machine."""

    degrade_after: int = 1
    offline_after: int = 3


@dataclass(frozen=True)
class HealthState:
    """The persisted health state of a tool (mirrors the ``tool_health`` row)."""

    status: str = STATUS_ACTIVE
    consecutive_failures: int = 0
    last_etag: str | None = None


def next_health_state(
    current: HealthState,
    *,
    success: bool,
    new_etag: str | None,
    thresholds: HealthThresholds,
) -> HealthState:
    """Pure transition: fold one poll outcome into a new :class:`HealthState`.

    * ``success=True``  -> failures reset to 0, status ``active``; ``last_etag`` updated
      to ``new_etag`` when a fresh one is provided (a 304 passes ``new_etag=None`` so the
      cached etag is preserved).
    * ``success=False`` -> failures += 1; status escalates active -> degraded -> offline
      as the counter crosses ``degrade_after`` then ``offline_after``.
    """
    if success:
        return HealthState(
            status=STATUS_ACTIVE,
            consecutive_failures=0,
            last_etag=new_etag if new_etag is not None else current.last_etag,
        )

    failures = current.consecutive_failures + 1
    if failures >= thresholds.offline_after:
        status = STATUS_OFFLINE
    elif failures >= thresholds.degrade_after:
        status = STATUS_DEGRADED
    else:
        status = STATUS_ACTIVE
    return HealthState(status=status, consecutive_failures=failures, last_etag=current.last_etag)


# ── HTTP poll ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PollResult:
    """Outcome of a single manifest GET."""

    success: bool
    changed: bool  # True on a 200 (new/changed manifest), False on 304/error
    status_code: int | None
    etag: str | None  # the response ETag on a 200, else None
    manifest: dict[str, Any] | None  # parsed manifest on a 200, else None
    error: str | None = None


class HttpResponse(Protocol):
    status_code: int

    @property
    def headers(self) -> Any: ...

    def json(self) -> Any: ...


class HttpClient(Protocol):
    """Minimal async HTTP surface the poller needs (httpx.AsyncClient satisfies it)."""

    async def get(
        self, url: str, *, headers: dict[str, str],
        timeout: float,  # noqa: ASYNC109 — mirrors httpx.AsyncClient.get's per-request timeout
    ) -> HttpResponse: ...


async def poll_manifest(
    client: HttpClient,
    base_url: str,
    *,
    last_etag: str | None,
    timeout: float,  # noqa: ASYNC109 — forwarded to the injected HTTP client (httpx-style)
) -> PollResult:
    """GET ``{base_url}/manifest`` with conditional ``If-None-Match`` and classify it.

    FAIL-SOFT: any exception (timeout, connect error, bad JSON) is captured into a
    failed :class:`PollResult` — it never propagates.
    """
    url = base_url.rstrip("/") + "/manifest"
    headers: dict[str, str] = {}
    if last_etag:
        headers["If-None-Match"] = last_etag
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — poll is fail-soft
        logger.warning("manifest_poll_error", url=url, error=str(exc))
        return PollResult(success=False, changed=False, status_code=None, etag=None, manifest=None,
                          error=str(exc))

    code = resp.status_code
    if code == 304:
        return PollResult(success=True, changed=False, status_code=304, etag=None, manifest=None)
    if 200 <= code < 300:
        etag = resp.headers.get("ETag") or resp.headers.get("etag")
        try:
            manifest = resp.json()
        except Exception as exc:  # noqa: BLE001 — a 200 with unparseable body is a failure
            logger.warning("manifest_poll_bad_json", url=url, error=str(exc))
            return PollResult(success=False, changed=False, status_code=code, etag=None,
                              manifest=None, error=f"bad json: {exc}")
        return PollResult(success=True, changed=True, status_code=code, etag=etag, manifest=manifest)

    # Any non-2xx (4xx/5xx) is a failure.
    return PollResult(success=False, changed=False, status_code=code, etag=None, manifest=None,
                      error=f"http {code}")
