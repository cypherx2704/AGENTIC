"""Runtime observation (L3/L4): template matching, confidence promotion, and the
invariant that runtime never mutates a static fact."""

from __future__ import annotations

from bkg.runtime import Observation, reconcile
from bkg.service import GraphService


def _endpoints() -> list[dict[str, object]]:
    return [
        {
            "id": "a",
            "method": "GET",
            "resolved_path": "/api/users/{user_id}",
            "verification_status": "unverified",
        },
        {"id": "b", "method": "POST", "resolved_path": "/api/users/", "verification_status": "unverified"},
    ]


def test_observed_path_promotes_matching_endpoint() -> None:
    enriched, runtime_only = reconcile(_endpoints(), [Observation("get", "/api/users/42")])
    by_id = {e["id"]: e for e in enriched}
    assert by_id["a"]["verification_status"] == "runtime-confirmed"
    assert by_id["b"]["verification_status"] == "unverified"  # not observed
    assert runtime_only == []


def test_unmatched_observation_is_runtime_only() -> None:
    _enriched, runtime_only = reconcile(_endpoints(), [Observation("GET", "/api/orders/7")])
    assert runtime_only == [
        {
            "method": "GET",
            "path": "/api/orders/7",
            "source": "runtime",
            "confidence": "runtime-confirmed",
            "verification_status": "runtime-confirmed",
            "static_match": False,
        }
    ]


def test_template_matches_one_segment_only() -> None:
    # {user_id} matches ONE path segment; /1/2 is NOT this endpoint
    enriched, runtime_only = reconcile(_endpoints(), [Observation("GET", "/api/users/1/2")])
    assert all(e["verification_status"] == "unverified" for e in enriched)
    assert len(runtime_only) == 1


def test_reconcile_never_mutates_the_static_input() -> None:
    endpoints = _endpoints()
    reconcile(endpoints, [Observation("GET", "/api/users/1"), Observation("POST", "/api/users/")])
    assert all(e["verification_status"] == "unverified" for e in endpoints)  # inputs untouched


def test_service_reconcile_runtime(fastapi_dto_sources: dict[str, str]) -> None:
    service = GraphService.from_sources(fastapi_dto_sources)
    result = service.reconcile_runtime(
        [{"method": "GET", "path": "/api/users/5"}, {"method": "GET", "path": "/api/other", "status": 200}]
    )
    assert result["confirmed"] == 1
    assert any(ro["path"] == "/api/other" for ro in result["runtime_only"])
    # the static endpoints keep their static confidence; only verification_status rose
    confirmed = [e for e in result["endpoints"] if e["verification_status"] == "runtime-confirmed"]
    assert confirmed and all(e["source"] == "static" for e in confirmed)


def test_404_probe_is_not_evidence_of_a_route() -> None:
    # a scanner hitting /.env, /wp-login.php returns 404 -> must NOT fabricate a route
    _enriched, runtime_only = reconcile(
        _endpoints(), [Observation("GET", "/.env", 404), Observation("POST", "/wp-login.php", 404)]
    )
    assert runtime_only == []


def test_matched_observation_confirms_even_on_a_404_resource() -> None:
    # GET /api/users/999 -> 404 (user not found): the ROUTE dispatched, so it's confirmed
    enriched, runtime_only = reconcile(_endpoints(), [Observation("GET", "/api/users/999", 404)])
    assert {e["id"] for e in enriched if e["verification_status"] == "runtime-confirmed"} == {"a"}
    assert runtime_only == []


def test_path_converter_spans_multiple_segments() -> None:
    endpoints = [
        {
            "id": "f",
            "method": "GET",
            "resolved_path": "/files/{name:path}",
            "verification_status": "unverified",
        }
    ]
    enriched, runtime_only = reconcile(endpoints, [Observation("GET", "/files/docs/readme.md")])
    assert enriched[0]["verification_status"] == "runtime-confirmed"
    assert runtime_only == []


def test_literal_route_wins_over_parametric() -> None:
    # sort-inverted files: parametric in admin.py (sorts first) vs literal in users.py
    endpoints = [
        {
            "id": "admin:/users/{user_id}",
            "method": "GET",
            "resolved_path": "/users/{user_id}",
            "verification_status": "unverified",
        },
        {
            "id": "users:/users/me",
            "method": "GET",
            "resolved_path": "/users/me",
            "verification_status": "unverified",
        },
    ]
    enriched, _ = reconcile(endpoints, [Observation("GET", "/users/me")])
    confirmed = {e["id"] for e in enriched if e["verification_status"] == "runtime-confirmed"}
    assert confirmed == {"users:/users/me"}  # the literal, not the parametric


def test_root_and_trailing_slash_edges() -> None:
    endpoints = [
        {"id": "root", "method": "GET", "resolved_path": "/", "verification_status": "unverified"},
        {"id": "coll", "method": "GET", "resolved_path": "/api/users/", "verification_status": "unverified"},
    ]
    enriched, runtime_only = reconcile(
        endpoints,
        [Observation("GET", "/"), Observation("GET", "/api/users")],  # obs missing trailing slash
    )
    assert {e["id"] for e in enriched if e["verification_status"] == "runtime-confirmed"} == {"root", "coll"}
    assert runtime_only == []
    # an empty path must NOT confirm root
    enriched2, _ = reconcile(endpoints, [Observation("GET", "")])
    assert all(e["verification_status"] == "unverified" for e in enriched2)
