"""BYOK (bring-your-own-key) sealed-envelope crypto + key resolution (WP06).

A tenant registers their own provider API key; the gateway stores it as an opaque
``secret_ref`` (never raw material) and, at provider-call time, resolves the
highest-priority active (or in-grace) tenant key for the provider — falling back to
the platform key when none exists or anything goes wrong.

secret_ref forms (self-describing — the prefix selects the resolver):

* ``env:NAME``            — resolve to ``os.environ[NAME]`` (platform-style references).
* ``sealed:v1:<base64>``  — an AES-256-GCM envelope: a fresh random 256-bit DEK encrypts
  the plaintext, then the DEK is itself wrapped (AES-256-GCM) by the KEK. The base64 blob
  packs ``dek_nonce | wrapped_dek | ct_nonce | ciphertext`` (the wrapped_dek length is
  fixed: 32-byte DEK + 16-byte GCM tag = 48 bytes), so unseal needs only the KEK.

Crypto lib: ``cryptography`` (``cryptography.hazmat.primitives.ciphers.aead.AESGCM`` +
HKDF). It is present transitively via ``pyjwt[crypto]`` (verified: cryptography 48.x in
the venv). No stub fallback is needed.

The KEK comes from ``settings.byok_kek`` (env ``LLMS_BYOK_KEK``) and is HKDF-SHA256
derived to a 32-byte key — so any passphrase works, not only a raw 32-byte value. When
the KEK is empty, BYOK is DISABLED: ``seal`` raises ``ByokDisabledError`` and
``resolve_provider_key`` returns ``None`` (platform-key fallback). A secret value is
NEVER logged.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from psycopg.rows import dict_row

from ..core.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)

# Envelope constants. The version is baked into the prefix so the format can evolve.
SEALED_PREFIX = "sealed:v1:"
ENV_PREFIX = "env:"


@dataclass(frozen=True)
class ResolvedKey:
    """A resolved per-tenant provider connection: the unsealed secret + how to reach the
    provider. ``base_url`` (nullable) + ``kind`` ('openai_compatible' | 'anthropic') let the
    router build the right adaptor for ANY provider (OpenAI/OpenRouter/self-hosted/Anthropic)
    with no per-provider code."""

    secret: str
    base_url: str | None
    kind: str

_DEK_BYTES = 32  # AES-256 data-encryption key
_NONCE_BYTES = 12  # AES-GCM standard nonce length
_GCM_TAG_BYTES = 16  # AES-GCM authentication tag length
# A wrapped DEK is exactly the DEK + the GCM tag (no AAD adds nothing to the ciphertext).
_WRAPPED_DEK_BYTES = _DEK_BYTES + _GCM_TAG_BYTES  # 48
# HKDF salt/info — fixed, public context-binding (NOT secret). Binds derived keys to BYOK.
_HKDF_INFO = b"cypherx-llms-byok-kek-v1"


class ByokDisabledError(RuntimeError):
    """Raised when a sealing operation is attempted while BYOK is disabled (no KEK)."""


class ByokCryptoError(ValueError):
    """Raised on a malformed envelope or a KEK that cannot decrypt it."""


def is_enabled(settings: Settings) -> bool:
    """True when a non-empty KEK is configured (BYOK sealing/unsealing is available)."""
    return bool(settings.byok_kek and settings.byok_kek.strip())


def _derive_kek(raw_kek: str) -> bytes:
    """HKDF-SHA256 the configured KEK material down to a 32-byte AES-256 key.

    Accepts any passphrase (>= 1 byte): HKDF stretches/contracts it to exactly 32 bytes,
    so operators are not forced to supply a precise raw-32-byte value. The derivation is
    deterministic (fixed salt/info) so the same KEK always unwraps the same envelopes.
    """
    material = raw_kek.encode("utf-8")
    hkdf = HKDF(algorithm=SHA256(), length=_DEK_BYTES, salt=None, info=_HKDF_INFO)
    return hkdf.derive(material)


def seal(plaintext: str, settings: Settings) -> str:
    """Seal ``plaintext`` into a ``sealed:v1:<base64>`` envelope.

    Generates a fresh random 256-bit DEK, AES-256-GCM-encrypts the plaintext under it,
    then wraps the DEK under the (HKDF-derived) KEK. Raises :class:`ByokDisabledError`
    when no KEK is configured. The plaintext is never logged.
    """
    if not is_enabled(settings):
        raise ByokDisabledError(
            "BYOK is disabled: set LLMS_BYOK_KEK to register or seal a tenant key."
        )
    kek = _derive_kek(settings.byok_kek)

    dek = AESGCM.generate_key(bit_length=256)
    ct_nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(ct_nonce, plaintext.encode("utf-8"), None)

    dek_nonce = os.urandom(_NONCE_BYTES)
    wrapped_dek = AESGCM(kek).encrypt(dek_nonce, dek, None)

    blob = dek_nonce + wrapped_dek + ct_nonce + ciphertext
    return SEALED_PREFIX + base64.b64encode(blob).decode("ascii")


def _unseal_sealed(ref: str, settings: Settings) -> str:
    if not is_enabled(settings):
        raise ByokDisabledError("BYOK is disabled: cannot unseal a sealed secret without a KEK.")
    try:
        blob = base64.b64decode(ref[len(SEALED_PREFIX) :], validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise ByokCryptoError("Malformed sealed envelope: bad base64.") from exc

    min_len = _NONCE_BYTES + _WRAPPED_DEK_BYTES + _NONCE_BYTES
    if len(blob) < min_len:
        raise ByokCryptoError("Malformed sealed envelope: too short.")

    offset = 0
    dek_nonce = blob[offset : offset + _NONCE_BYTES]
    offset += _NONCE_BYTES
    wrapped_dek = blob[offset : offset + _WRAPPED_DEK_BYTES]
    offset += _WRAPPED_DEK_BYTES
    ct_nonce = blob[offset : offset + _NONCE_BYTES]
    offset += _NONCE_BYTES
    ciphertext = blob[offset:]

    kek = _derive_kek(settings.byok_kek)
    try:
        dek = AESGCM(kek).decrypt(dek_nonce, wrapped_dek, None)
        plaintext = AESGCM(dek).decrypt(ct_nonce, ciphertext, None)
    except Exception as exc:  # noqa: BLE001 — wrong KEK / tampered blob -> InvalidTag etc.
        raise ByokCryptoError("Failed to unseal envelope (bad KEK or corrupt blob).") from exc
    return plaintext.decode("utf-8")


def unseal(ref: str, settings: Settings) -> str:
    """Resolve a ``secret_ref`` to its plaintext secret.

    * ``env:NAME``           -> ``os.environ[NAME]`` (raises if the var is unset).
    * ``sealed:v1:<base64>`` -> AES-GCM-decrypt with the KEK.

    Raises :class:`ByokCryptoError` on a malformed/unknown reference or a bad KEK, and
    :class:`ByokDisabledError` when a sealed reference is seen with no KEK configured. The
    returned secret is never logged.
    """
    if ref.startswith(ENV_PREFIX):
        name = ref[len(ENV_PREFIX) :]
        value = os.environ.get(name)
        if value is None:
            raise ByokCryptoError(f"env secret reference points at an unset variable '{name}'.")
        return value
    if ref.startswith(SEALED_PREFIX):
        return _unseal_sealed(ref, settings)
    raise ByokCryptoError("Unknown secret_ref scheme (expected 'env:' or 'sealed:v1:').")


# Selects the candidate BYOK keys for (tenant, provider): active keys always, plus
# 'rotating' keys still inside their grace window. Ordered so the resolver takes the
# first row: lowest priority number wins, active before rotating on a tie, newest first.
_SELECT_KEYS_SQL = """
    SELECT secret_ref, priority, status, grace_until, base_url, kind
      FROM llms.tenant_provider_keys
     WHERE provider = %s
       AND (
             status = 'active'
          OR (status = 'rotating' AND grace_until IS NOT NULL AND grace_until > NOW())
           )
     ORDER BY priority ASC,
              (status = 'active') DESC,
              created_at DESC
"""


async def resolve_provider_key(
    pool: AsyncConnectionPool | None,
    tenant_id: str,
    provider: str,
    settings: Settings,
) -> ResolvedKey | None:
    """Return the resolved BYOK connection (secret + base_url + kind) for
    ``(tenant_id, provider)`` or ``None``.

    Selection: the highest-priority key that is ``active`` OR ``rotating`` within its
    grace window (during a rotation both the old and new key are acceptable). Returns a
    :class:`ResolvedKey`, or ``None`` when the tenant has no usable connection.

    FAIL-OPEN: returns ``None`` (never raises) when BYOK is disabled, the pool is absent
    (unit-test path), the DB query fails, or the chosen row cannot be unsealed — so a BYOK
    problem degrades to the platform key rather than failing the request. The secret value
    is NEVER logged; only non-secret metadata (provider, priority, status) is.
    """
    if not is_enabled(settings):
        return None
    if pool is None:
        # No DB wired (e.g. a minimal/unit test app) — cannot resolve a tenant key.
        return None

    try:
        from ..db.pool import in_tenant

        async def _query(conn: object) -> list[dict]:
            cur = await conn.cursor(row_factory=dict_row).execute(  # type: ignore[attr-defined]
                _SELECT_KEYS_SQL, (provider,)
            )
            return await cur.fetchall()

        rows = await in_tenant(pool, tenant_id, _query)
    except Exception as exc:  # noqa: BLE001 — BYOK lookup failure must fall back, not 5xx
        logger.warning("byok_lookup_failed", provider=provider, error=str(exc))
        return None

    # Walk candidates best-first; the first one that unseals wins. A single bad envelope
    # does not poison the whole resolution — we skip it and try the next.
    for row in rows:
        ref = row["secret_ref"]
        try:
            secret = unseal(ref, settings)
        except Exception as exc:  # noqa: BLE001 — never log the ref/secret; skip + try next
            logger.warning(
                "byok_unseal_failed",
                provider=provider,
                priority=row.get("priority"),
                status=row.get("status"),
                error=str(exc),
            )
            continue
        if secret:
            logger.info(
                "byok_key_selected",
                provider=provider,
                priority=row.get("priority"),
                status=row.get("status"),
                kind=row.get("kind"),
            )
            return ResolvedKey(
                secret=secret,
                base_url=row.get("base_url"),
                kind=row.get("kind") or "openai_compatible",
            )

    # No usable tenant key for this provider.
    return None
