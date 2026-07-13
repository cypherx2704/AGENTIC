"""Runtime observation (L3/L4) — reconcile observed traffic with the static graph.

Observations are ``(method, path, status)`` triples from an access log / APM /
tracing tap. They are matched against the static endpoints by **path template**
(an observed ``/api/users/123`` matches the static ``/api/users/{user_id}``).

This layer only ever **RAISES** confidence on the provenance schema — it never
overrides a static fact's *value*:
- a static endpoint that was observed -> ``verification_status: runtime-confirmed``
  (the fact is now corroborated, not just inferred);
- an observed path with NO static match, whose status shows the request actually
  reached a handler (not a 404 probe), -> a **runtime-only** endpoint (a dynamic
  route static analysis missed), tagged ``source: runtime`` — surfaced separately,
  never merged into the static set.

Like the AI layer, this is additive enrichment computed off the query path; the
deterministic graph is unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Observation:
    method: str
    path: str
    status: int = 200


# 404 means "no such route/resource" (probes, typos, scanners) — NOT evidence a
# route exists. A sub-100 status is a connection failure, also not evidence.
def _indicates_route(status: int) -> bool:
    return status >= 100 and status != 404


def _template_regex(resolved_path: str) -> re.Pattern[str]:
    """Match one concrete segment per ``{name}`` placeholder (so ``/users/{id}``
    does NOT match ``/users`` or ``/users/1/2``); a ``{name:path}`` converter
    matches across ``/`` (Flask ``<path:name>`` / FastAPI ``{name:path}``)."""
    path = resolved_path.rstrip("/") or "/"
    if path == "/":
        return re.compile("^/$")
    out: list[str] = []
    for part in re.split(r"(\{[^}]*\})", path):
        if part.startswith("{") and part.endswith("}"):
            out.append(".+" if part.rstrip("}").endswith(":path") else "[^/]+")
        elif part:
            out.append(re.escape(part))
    return re.compile("^" + "".join(out) + "/?$")


def _specificity(resolved_path: str) -> tuple[int, int]:
    # fewer placeholders + longer literal = more specific -> checked first, so a
    # literal /users/me wins over a parametric /users/{id} (framework precedence)
    return (resolved_path.count("{"), -len(resolved_path))


def reconcile(
    endpoints: list[dict[str, Any]],
    observations: Iterable[Observation],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(enriched_endpoints, runtime_only)``. Static facts are copied, never
    mutated; only ``verification_status`` is raised on confirmed endpoints."""
    matchers = sorted(
        ((ep, _template_regex(ep["resolved_path"])) for ep in endpoints),
        key=lambda t: _specificity(t[0]["resolved_path"]),
    )
    confirmed: set[str] = set()
    unmatched: set[tuple[str, str]] = set()
    for obs in observations:
        method = obs.method.upper()
        hit = next((ep for ep, rx in matchers if ep["method"] == method and rx.match(obs.path)), None)
        if hit is not None:
            confirmed.add(hit["id"])  # the route dispatched (any status: 200, 404-on-resource, 500…)
        elif _indicates_route(obs.status):
            unmatched.add((method, obs.path))
        # else: a 404 / connection failure on an unknown path is not evidence of a route

    enriched: list[dict[str, Any]] = []
    for ep in endpoints:
        item = dict(ep)
        if ep["id"] in confirmed:
            item["verification_status"] = "runtime-confirmed"
        enriched.append(item)

    runtime_only = [
        {
            "method": method,
            "path": path,
            "source": "runtime",
            "confidence": "runtime-confirmed",
            "verification_status": "runtime-confirmed",
            "static_match": False,
        }
        for method, path in sorted(unmatched)
    ]
    return enriched, runtime_only


def to_observations(records: Iterable[dict[str, Any]]) -> list[Observation]:
    """Coerce ``{method, path, status?}`` dicts (e.g. parsed access-log lines)."""
    return [
        Observation(method=r["method"], path=r["path"], status=int(r.get("status", 200)))
        for r in records
    ]
