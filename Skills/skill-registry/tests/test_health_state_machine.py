"""Pure health-poll state machine transitions."""

from __future__ import annotations

from skill_registry.services.health_poll import (
    STATUS_ACTIVE,
    STATUS_DEGRADED,
    STATUS_OFFLINE,
    HealthState,
    HealthThresholds,
    next_health_state,
)

T = HealthThresholds(degrade_after=1, offline_after=3)


def test_success_resets_to_active_and_updates_etag() -> None:
    cur = HealthState(status=STATUS_DEGRADED, consecutive_failures=2, last_etag="old")
    nxt = next_health_state(cur, success=True, new_etag="new", thresholds=T)
    assert nxt.status == STATUS_ACTIVE
    assert nxt.consecutive_failures == 0
    assert nxt.last_etag == "new"


def test_success_304_preserves_cached_etag() -> None:
    cur = HealthState(status=STATUS_ACTIVE, consecutive_failures=0, last_etag="etag-1")
    # A 304 passes new_etag=None — the cached etag must be preserved.
    nxt = next_health_state(cur, success=True, new_etag=None, thresholds=T)
    assert nxt.status == STATUS_ACTIVE
    assert nxt.last_etag == "etag-1"


def test_first_failure_degrades() -> None:
    cur = HealthState(status=STATUS_ACTIVE, consecutive_failures=0)
    nxt = next_health_state(cur, success=False, new_etag=None, thresholds=T)
    assert nxt.status == STATUS_DEGRADED
    assert nxt.consecutive_failures == 1


def test_third_failure_goes_offline() -> None:
    cur = HealthState(status=STATUS_DEGRADED, consecutive_failures=2)
    nxt = next_health_state(cur, success=False, new_etag=None, thresholds=T)
    assert nxt.status == STATUS_OFFLINE
    assert nxt.consecutive_failures == 3


def test_full_cycle_active_degraded_offline_active() -> None:
    s = HealthState()
    assert s.status == STATUS_ACTIVE
    s = next_health_state(s, success=False, new_etag=None, thresholds=T)
    assert s.status == STATUS_DEGRADED
    s = next_health_state(s, success=False, new_etag=None, thresholds=T)
    assert s.status == STATUS_DEGRADED  # failures=2, below offline_after=3
    s = next_health_state(s, success=False, new_etag=None, thresholds=T)
    assert s.status == STATUS_OFFLINE
    # One success recovers straight back to active.
    s = next_health_state(s, success=True, new_etag="e", thresholds=T)
    assert s.status == STATUS_ACTIVE
    assert s.consecutive_failures == 0


def test_thresholds_are_configurable() -> None:
    t2 = HealthThresholds(degrade_after=2, offline_after=4)
    s = HealthState()
    s = next_health_state(s, success=False, new_etag=None, thresholds=t2)
    assert s.status == STATUS_ACTIVE  # 1 failure, below degrade_after=2
    s = next_health_state(s, success=False, new_etag=None, thresholds=t2)
    assert s.status == STATUS_DEGRADED  # 2 failures
