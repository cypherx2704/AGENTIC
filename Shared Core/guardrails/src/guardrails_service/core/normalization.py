"""Unicode input canonicalization — the pre-detection de-obfuscation view (B1).

All 18 built-in detectors match raw bytes / regex. An attacker who splices a zero-width
space into ``ig<U+200B>nore previous instructions``, Tag-encodes the payload
("ASCII smuggling"), wraps it in bidi controls (Trojan Source), writes a card/SSN in
fullwidth digits, or swaps in Cyrillic/Greek homoglyphs defeats every regex while the
downstream LLM still reads the intended text. :func:`canonicalize` collapses that
unbounded obfuscation space back onto the finite patterns the rules already cover, in
layers:

* **Layer A — always on (no flag).** Strip the Unicode default-ignorable / format(Cf) /
  control(Cc) code points used to hide or fragment a payload: zero-width
  (U+200B–200D, U+FEFF), the **Tags block** (U+E0000–E007F), and **bidi** overrides /
  isolates (U+202A–202E, U+2066–2069). One linear ``str.translate`` scan; a NO-OP on clean
  ASCII (single-digit microseconds), no false-positive risk on legitimate text.
* **Layer B — opt-in (``INJECTION_NORMALIZE``).** ``NFKC`` compatibility fold
  (fullwidth / ligature / superscript -> canonical ASCII/BMP).
* **Layer C — opt-in (``GUARDRAILS_CONFUSABLES_FOLD``).** UTS #39 confusables *skeleton*
  fold (cross-script homoglyphs -> Latin skeleton) that NFKC provably cannot do. The map
  is built ONCE from a checked-in Unicode confusables data file (:func:`build_confusables_map`).

Pure CPU — no LLM/network — so it cannot breach the hot-path SLO. The RAW text is left
untouched for PII redaction/HMAC; only the block-action injection/jailbreak detectors read
the canonicalized *detection view* (via ``RuleContext.detection_text``), which avoids any
offset-map problem for PII redaction and preserves the "raw PII never leaves" invariant.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# ── Layer A: default-ignorable / format / bidi code points stripped from the view ──
# Zero-width joiners/non-joiners/space + BOM (used to fragment a payload mid-token).
_ZERO_WIDTH = (0x200B, 0x200C, 0x200D, 0xFEFF)
# Bidirectional overrides + isolates (Trojan Source, CVE-2021-42574).
_BIDI_CONTROLS = (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)


def _build_layer_a_table() -> dict[int, None]:
    """Translation table (code point -> None) removing every Layer-A obfuscation char."""
    table: dict[int, None] = {}
    for cp in _ZERO_WIDTH:
        table[cp] = None
    for cp in _BIDI_CONTROLS:
        table[cp] = None
    # Tags block U+E0000–E007F ("ASCII smuggling" — invisible tag chars mirror ASCII).
    for cp in range(0xE0000, 0xE0080):
        table[cp] = None
    return table


_LAYER_A_TABLE = _build_layer_a_table()

# Data file: a curated genuine subset of the Unicode UTS #39 confusables MA mappings.
_DEFAULT_CONFUSABLES_PATH = Path(__file__).resolve().parent.parent / "data" / "confusables.txt"


def strip_ignorables(text: str) -> str:
    """Layer A only: remove zero-width / Tags-block / bidi control code points."""
    return text.translate(_LAYER_A_TABLE)


def _fold_confusables(text: str, mapping: dict[int, str]) -> str:
    """Layer C: replace each source code point with its Latin skeleton (identity otherwise)."""
    return "".join(mapping.get(ord(ch), ch) for ch in text)


def canonicalize(
    text: str,
    *,
    nfkc: bool = False,
    confusables: dict[int, str] | None = None,
) -> str:
    """Return the canonicalized DETECTION VIEW of ``text``.

    Layer A is always applied. ``nfkc=True`` adds the NFKC fold (Layer B). A non-empty
    ``confusables`` map adds the UTS #39 skeleton fold (Layer C). Deterministic + pure.
    On clean ASCII with both layers off this returns ``text`` unchanged (byte-identical).
    """
    if not text:
        return text
    cleaned = text.translate(_LAYER_A_TABLE)
    if nfkc:
        cleaned = unicodedata.normalize("NFKC", cleaned)
    if confusables:
        cleaned = _fold_confusables(cleaned, confusables)
    return cleaned


def build_confusables_map(path: Path | None = None) -> dict[int, str]:
    """Parse the checked-in Unicode confusables data file into a ``{source_cp: skeleton}`` map.

    Format (identical to the published ``confusables.txt``)::

        <source hex> ; <target hex...> ; MA  # comment

    Fail-soft: an unreadable / malformed file yields an EMPTY map (Layer C then becomes a
    no-op) rather than raising — canonicalization must never fabricate or crash a decision.
    """
    src_path = path or _DEFAULT_CONFUSABLES_PATH
    mapping: dict[int, str] = {}
    try:
        raw = src_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("confusables_load_failed", path=str(src_path), error=str(exc))
        return mapping
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        core = stripped.split("#", 1)[0]
        parts = [p.strip() for p in core.split(";")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        try:
            source = int(parts[0], 16)
            target = "".join(chr(int(t, 16)) for t in parts[1].split())
        except ValueError:
            logger.warning("confusables_bad_row", row=stripped[:48])
            continue
        if target:
            mapping[source] = target
    logger.info("confusables_map_built", entries=len(mapping))
    return mapping
