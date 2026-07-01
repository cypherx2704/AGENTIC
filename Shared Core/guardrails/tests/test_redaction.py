"""Redaction determinism, per-tenant unlinkability, and token format (Component 5)."""

from __future__ import annotations

import re

from guardrails_service.core.redaction import (
    RedactionKeyResolver,
    compute_token,
    redaction_token,
)

PLATFORM_KEY = "platform-key"
TENANT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_TOKEN_RE = re.compile(r"^\[REDACTED:[a-z_]+:[0-9a-f]{8}\]$")


def test_token_format() -> None:
    token = redaction_token(PLATFORM_KEY, "email", TENANT_A, "alice@example.com")
    assert _TOKEN_RE.match(token), token


def test_determinism_same_input_same_token() -> None:
    a = compute_token(PLATFORM_KEY, TENANT_A, "alice@example.com")
    b = compute_token(PLATFORM_KEY, TENANT_A, "alice@example.com")
    assert a == b
    assert len(a) == 8


def test_per_tenant_unlinkability() -> None:
    a = compute_token(PLATFORM_KEY, TENANT_A, "alice@example.com")
    b = compute_token(PLATFORM_KEY, TENANT_B, "alice@example.com")
    assert a != b  # same value, different tenant -> different token


def test_key_rotation_changes_token() -> None:
    a = compute_token("key-v1", TENANT_A, "alice@example.com")
    b = compute_token("key-v2", TENANT_A, "alice@example.com")
    assert a != b


def test_resolver_falls_back_to_platform_key() -> None:
    resolver = RedactionKeyResolver(PLATFORM_KEY)
    assert resolver.resolve(TENANT_A) == PLATFORM_KEY


def test_resolver_uses_registered_tenant_key() -> None:
    resolver = RedactionKeyResolver(PLATFORM_KEY)
    resolver.register_tenant_key(TENANT_A, "tenant-a-key")
    assert resolver.resolve(TENANT_A) == "tenant-a-key"
    assert resolver.resolve(TENANT_B) == PLATFORM_KEY
