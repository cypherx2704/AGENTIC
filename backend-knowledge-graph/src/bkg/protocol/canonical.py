"""Canonical serialization + content-addressed fingerprinting.

This is the bedrock of the determinism oracle: the whole oracle reduces to
`digest(incremental) == digest(rebuild)`, so if a fact -> bytes mapping is not
bit-stable the oracle is unfalsifiable. Rules enforced here:

- deterministic key ordering (``sort_keys=True``, recursive),
- compact separators (no incidental whitespace),
- ``ensure_ascii=True`` for byte stability across locales,
- no wall-clock, no floats, and (by upstream convention) repo-relative POSIX
  paths only — so a snapshot is identical across machines and OSes.

Pure module: it depends on nothing in bkg except the standard library + blake3.
"""

from __future__ import annotations

import json
from typing import Any

from blake3 import blake3


def _reject_noncanonical(x: Any) -> None:
    """Enforce the canonical value contract at the boundary. Rejects anything that
    would make ``canonical_bytes`` nondeterministic or ambiguous:

    - **floats** (platform-variant repr; NaN/Inf are not valid JSON; -0.0 != 0.0),
    - **tuples** (would serialize identically to lists, so a tuple<->list edit
      would be judged 'unchanged' and skip recompute),
    - **bytes / other types**, and **non-string dict keys** (mixed-key dicts also
      crash ``json.dumps(sort_keys=True)``).

    Turning these into loud errors keeps 'equal canonical bytes' == 'equal value'.
    """
    if x is None or isinstance(x, str) or isinstance(x, bool) or isinstance(x, int):
        return  # note: bool is a subclass of int; both are fine, float is not
    if isinstance(x, list):
        for item in x:
            _reject_noncanonical(item)
        return
    if isinstance(x, dict):
        for key, value in x.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string dict key {key!r}: canonical objects require string keys")
            _reject_noncanonical(value)
        return
    raise TypeError(f"non-canonical value of type {type(x).__name__}: {x!r}")


def canonical_bytes(payload: Any) -> bytes:
    """Deterministically serialize a canonical JSON value (dict/list/str/int/bool/None) to bytes."""
    _reject_noncanonical(payload)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def fingerprint(data: bytes) -> bytes:
    """The 32-byte BLAKE3 digest used as a fact's early-cutoff fingerprint."""
    return blake3(data).digest()


def hexdigest(data: bytes) -> str:
    """Hex BLAKE3 digest — used for whole-graph snapshot digests."""
    return blake3(data).hexdigest()
