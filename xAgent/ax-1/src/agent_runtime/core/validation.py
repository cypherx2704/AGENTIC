"""API-layer validation for path / query parameters (BUG 1 + MINOR fix).

The repositories bind their string arguments straight onto Postgres ``uuid`` /
``timestamptz`` columns (``WHERE task_id = %s``, ``WHERE agent_id = %s``,
``created_at >= %s``). A non-castable value (a non-UUID path segment, a non-RFC-3339
``?since``) therefore reached the DB as a raw string and produced a libpq
``InvalidTextRepresentation`` / ``DatatypeMismatch`` — surfacing to the client as a
generic 500 (and, for the agents cross-validate path, a misleading 503).

These helpers validate those values at the edge BEFORE any repo / downstream call, so a
malformed identifier becomes a clean Contract-2 error instead of a 5xx:

  * ``parse_uuid_path``  — a malformed ``{task_id}`` / ``{agent_id}`` cannot name any
    existing (RLS-scoped) row, so it is a 404 NOT_FOUND — the SAME answer an unknown /
    cross-tenant id already gets (never leak existence, never 5xx).
  * ``parse_uuid_query`` — a malformed UUID *filter* (``?agent_id``) is a client input
    error -> 422 VALIDATION_ERROR.
  * ``parse_rfc3339_query`` — a malformed ``?since`` instant is a client input error
    -> 422 VALIDATION_ERROR.

All three return the CANONICAL string form (``str(uuid.UUID(...))`` / a normalised
RFC-3339 instant) so the repo layer keeps binding plain strings exactly as before — the
fix is additive and contract-compatible (the canonical form of an already-canonical id
is itself).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from .errors import ApiError, ErrorCode


def parse_uuid_path(value: str, *, param: str) -> str:
    """Validate a UUID PATH segment; a malformed value -> 404 NOT_FOUND (never 5xx).

    Returns the canonical lowercase UUID string for binding to a ``uuid`` column. A path
    id that is not a UUID cannot match any row, so it is surfaced as NOT_FOUND — the same
    outcome an unknown / RLS-hidden id already produces (so existence is never leaked and
    the DB never sees an uncastable value).
    """
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"{param} {value!r} not found.",
        ) from exc


def parse_uuid_query(value: str | None, *, param: str) -> str | None:
    """Validate an OPTIONAL UUID QUERY filter; a malformed value -> 422 VALIDATION_ERROR.

    ``None`` (filter absent) passes through unchanged. A present-but-malformed value is a
    client input error (422) so the malformed filter never reaches the ``uuid`` column.
    """
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"{param} must be a valid UUID.",
            details={"reason": "INVALID_UUID", "param": param},
        ) from exc


def parse_rfc3339_query(value: str | None, *, param: str) -> str | None:
    """Validate an OPTIONAL RFC-3339 instant QUERY filter; malformed -> 422.

    ``None`` (filter absent) passes through unchanged. A present value is parsed with
    ``datetime.fromisoformat`` (accepting a trailing ``Z``); a value that does not parse
    is a 422 VALIDATION_ERROR so it never reaches the ``timestamptz`` column as a raw
    uncastable string. The ORIGINAL string is returned on success — psycopg binds a
    valid RFC-3339 string to ``timestamptz`` directly, so the wire value is unchanged.
    """
    if value is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"{param} must be an RFC 3339 timestamp.",
            details={"reason": "INVALID_TIMESTAMP", "param": param},
        ) from exc
    return value
