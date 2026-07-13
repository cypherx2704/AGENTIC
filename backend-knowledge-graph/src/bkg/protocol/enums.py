"""Frozen enums for the bkg protocol.

`Confidence`, `Provenance`, and `VerificationStatus` include the `ai*`/`runtime*`
values so the schema is stable, but the CORE never produces them — they are
declared-but-unused until the runtime and AI-proposer layers land.
"""

from __future__ import annotations

from enum import StrEnum


class Confidence(StrEnum):
    AI_INFERRED = "ai-inferred"
    AI_PROPOSED = "ai-proposed"
    INFERRED = "inferred"
    RUNTIME_CONFIRMED = "runtime-confirmed"
    STATIC_CERTAIN = "static-certain"
    CONFLICT = "conflict"


# Tier ordering (higher = more trusted). `conflict` is orthogonal and omitted.
CONFIDENCE_RANK: dict[Confidence, int] = {
    Confidence.AI_INFERRED: 0,
    Confidence.AI_PROPOSED: 1,
    Confidence.INFERRED: 2,
    Confidence.RUNTIME_CONFIRMED: 3,
    Confidence.STATIC_CERTAIN: 3,
}


class Provenance(StrEnum):
    STATIC = "static"
    RUNTIME = "runtime"
    AI = "ai"
    MERGED = "merged"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    STATIC_CORROBORATED = "static-corroborated"
    RUNTIME_CONFIRMED = "runtime-confirmed"
    DEVELOPER_CONFIRMED = "developer-confirmed"
    CONFLICT = "conflict"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


class NodeKind(StrEnum):
    FILE = "File"
    SYMBOL = "Symbol"
    ROUTE = "Route"
    HANDLER = "Handler"
    MIDDLEWARE = "Middleware"
    SCHEMA_REF = "SchemaRef"
    FIELD = "Field"
    ENDPOINT = "Endpoint"
    CONFIG = "Config"
    SECURITY_SCHEME = "SecurityScheme"


class EdgeKind(StrEnum):
    HANDLES = "HANDLES"
    GUARDED_BY = "GUARDED_BY"
    VALIDATES_WITH = "VALIDATES_WITH"
    RETURNS = "RETURNS"
    MOUNTS = "MOUNTS"
