"""Rule functions + metadata for the 11 first-cycle rules (Component 2).

Each rule is a :class:`RuleSpec` carrying its metadata (id, name, direction,
default_action, default_fail_mode, severity, category, timeout_ms) and a pure
``detect`` callable ``(text, ctx) -> list[RuleHit]``. The callable NEVER mutates
state and NEVER raises for ordinary input — it just locates matches.

The classifier-backed toxicity rules receive the classifier via :class:`RuleContext`
(the pipeline injects it), keeping the rule functions free of global state.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..classifier import Category, Classifier
from .signatures import (
    INJECTION_AUTOMATON,
    JAILBREAK_AUTOMATON,
    AhoCorasick,
    signature_matches,
)

Action = str  # 'allow' | 'warn' | 'redact' | 'block'
Direction = str  # 'input' | 'output' | 'both'

# Rule lifecycle statuses (mirrors guardrails.rules.status). 'retired' DISABLES the
# rule — the pipeline skips it. 'deprecated' rules keep enforcing (policies pinned to
# them must keep working); they are only closed to new policy references.
RULE_STATUS_ACTIVE = "active"
RULE_STATUS_RETIRED = "retired"


@dataclass
class RuleContext:
    """Per-evaluation context handed to each rule function."""

    classifier: Classifier | None = None
    # Original input text for the output check (output-pii-email-v1 'not in input' logic).
    input_text: str | None = None
    # Max characters allowed for output-max-length-v1.
    max_output_chars: int = 8000
    # PRE-COMPUTED toxicity categories from the (async) confidence-banded remote cascade
    # (services.classifier_client). When set, the toxicity detectors use these INSTEAD of
    # calling ``classifier.classify`` synchronously — this is how the small/large-LLM
    # cascade stage reaches the synchronous pipeline without making it async. ``None`` means
    # "no pre-computed result" -> fall back to the in-process classifier (today's path).
    precomputed_toxicity: list[Category] | None = None
    # Pre-computed PII spans located by Microsoft Presidio (services.pii_presidio), each a
    # ``(matched_text, category)`` pair. Additive: when set, the PII detectors UNION these
    # with their regex hits to lift recall; the existing HMAC token format is unchanged.
    presidio_spans: list[tuple[str, str]] | None = None
    # Marked UNTRUSTED spans (RAG/tool-provided content) for the prompt-injection spotlight.
    # An injection pattern found INSIDE one of these spans is treated as higher-risk
    # (stricter threshold). Empty/None => no spotlighting (pure no-op; today's verdicts).
    untrusted_spans: list[str] | None = None
    # Injection-risk score in [0,1] produced by the input-side injection detector. Pure
    # METADATA carried for decision-trace; does not by itself change a verdict.
    injection_risk: float = 0.0
    # Canonicalized DETECTION VIEW of the input (B1 — Unicode de-obfuscation). Built once per
    # check by ``core.normalization.canonicalize`` (Layer A always-on; NFKC / confusables
    # opt-in). BLOCK-category detectors (prompt-injection / jailbreak) match against this
    # instead of the raw text, so obfuscated payloads (zero-width / Tags / bidi / fullwidth /
    # homoglyph) collapse back onto the finite patterns. ``None`` => match the raw text
    # (today's behaviour; unit-level callers). NEVER fed to PII redact rules (offset-map /
    # "raw PII never leaves" invariant), so redaction offsets always map to the original.
    detection_text: str | None = None
    # Caller-supplied high-entropy canary token(s) embedded in the caller's own system prompt
    # (B7 — output-canary-leak-v1). Any occurrence in the model OUTPUT (exact + de-spaced /
    # hex / base64 variants) means the system prompt/context leaked -> block. ``None`` =>
    # the detector is inert (byte-identical to today, exactly like ``untrusted_spans``).
    canary_tokens: list[str] | None = None
    # Native context-window PII validation (B8 — default OFF). When ``pii_context_enabled`` a
    # passport-number / name candidate is admitted only when a supporting term from the
    # respective lexicon appears within ``pii_context_window`` chars. ``pii_context_enabled``
    # False (default) => the passport/name context detectors are a pure no-op.
    pii_context_enabled: bool = False
    pii_context_window: int = 40
    pii_context_passport_terms: tuple[str, ...] = ()
    pii_context_name_terms: tuple[str, ...] = ()


@dataclass
class RuleHit:
    """A single match produced by a rule."""

    matched_text: str
    # For PII rules: the redaction category (email | phone | credit_card | ...).
    # For non-PII rules: the rule's category (security | toxicity | jailbreak | length).
    category: str


@dataclass
class RuleSpec:
    """Metadata + detector for one rule.

    The metadata fields (``default_action``, ``default_fail_mode``, ``severity``,
    ``timeout_ms``, ``status``) are MUTABLE: the DB ``guardrails.rules`` registry is
    authoritative for them and the startup/refresh overlay
    (``services.rules.registry``) writes the DB values onto these objects. Code stays
    authoritative for ``detect()`` logic; the in-code values are the documented
    last-resort fallback when the registry cannot be read.
    """

    rule_id: str
    name: str
    direction: Direction
    default_action: Action
    severity: str
    category: str
    detect: Callable[[str, RuleContext], list[RuleHit]]
    default_fail_mode: str = "closed"
    timeout_ms: int = 10
    status: str = RULE_STATUS_ACTIVE
    tags: list[str] = field(default_factory=list)


# ── Shared regexes ───────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phone: optional +country, separators . - or space, 7-14 digits total.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?){2,4}\d{2,4}(?!\d)"
)
# Credit-card candidate: 13-19 digits, optionally grouped by space/hyphen.
_CC_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")
# SSN: dashed 3-2-4 only (a bare 9-digit run is too ambiguous to flag); range-validated below.
_SSN_RE = re.compile(r"(?<!\d)(\d{3})-(\d{2})-(\d{4})(?!\d)")
# IPv4: four dotted octets (each octet range-validated 0-255 in the detector).
_IPV4_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")
# Street address: house number + up to a few words + a street-type suffix. Conservative
# (requires a recognised suffix) to keep false positives low on ordinary numeric text.
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}[A-Za-z]?\s+(?:[A-Za-z0-9.'\-]+\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|Drive|Dr|Court|Ct|"
    r"Way|Place|Pl|Terrace|Ter|Circle|Cir|Highway|Hwy|Parkway|Pkwy|Square|Sq)\b\.?",
    re.IGNORECASE,
)

_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous\s+)?instructions?", re.IGNORECASE),
    re.compile(r"forget\s+(?:all\s+)?(?:your\s+|the\s+)?(?:previous\s+)?instructions?", re.IGNORECASE),
    re.compile(r"new\s+prompt\s*:", re.IGNORECASE),
    re.compile(r"\bDAN\b"),
    re.compile(r"reveal\s+your\s+(?:system\s+)?prompt", re.IGNORECASE),
]

_JAILBREAK_PATTERNS = [
    re.compile(r"do\s+anything\s+now", re.IGNORECASE),
    re.compile(r"developer\s+mode", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"pretend\s+you\s+(?:are|have\s+no)\s+", re.IGNORECASE),
    re.compile(r"without\s+any\s+(?:restrictions?|filters?|rules?)", re.IGNORECASE),
    re.compile(r"act\s+as\s+(?:an?\s+)?unrestricted", re.IGNORECASE),
]

_OUTPUT_JAILBREAK_LEAK_PATTERNS = [
    re.compile(r"i\s+am\s+an?\s+AI\s+language\s+model", re.IGNORECASE),
    re.compile(r"my\s+system\s+prompt", re.IGNORECASE),
    re.compile(r"my\s+instructions\s+are", re.IGNORECASE),
    re.compile(r"as\s+an?\s+AI\s+(?:language\s+)?model", re.IGNORECASE),
    re.compile(r"i\s+(?:was|am)\s+(?:instructed|told)\s+to", re.IGNORECASE),
]


def _luhn_ok(digits: str) -> bool:
    """Return True if the digit string passes the Luhn checksum."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ── ICAO 9303 MRZ passport detection (regex + check digits — B3) ─────────────────
# MRZ documents are fixed-width lines over the alphabet [A-Z0-9<]. TD3 (passport) = two
# 44-char lines; TD2 = two 36-char lines; TD1 (ID card) = three 30-char lines. Detection is
# an anchored fixed-width match (ReDoS-safe) confirmed by the ICAO Doc 9303 7-3-1 mod-10
# check digits over the document number, DOB, expiry, and a composite field — the SAME
# "regex + deterministic validation" shape as the Luhn credit-card rule. Because FOUR
# independent check digits must all pass, false positives are astronomically unlikely.
_MRZ_LINE_RE = re.compile(r"[A-Z0-9<]+")
_MRZ_WEIGHTS = (7, 3, 1)


def _mrz_char_value(ch: str) -> int:
    """ICAO 9303 character value: '0'-'9' -> 0-9, 'A'-'Z' -> 10-35, '<' -> 0; else -1."""
    if ch == "<":
        return 0
    if "0" <= ch <= "9":
        return ord(ch) - 48
    if "A" <= ch <= "Z":
        return ord(ch) - 55  # 'A' (65) -> 10
    return -1


def _mrz_check_digit(data: str) -> int:
    """Compute the ICAO 9303 7-3-1 mod-10 check digit over ``data`` (mirrors ``_luhn_ok``).

    Returns -1 if any character is outside the MRZ alphabet (so an invalid field never
    accidentally validates).
    """
    total = 0
    for i, ch in enumerate(data):
        value = _mrz_char_value(ch)
        if value < 0:
            return -1
        total += value * _MRZ_WEIGHTS[i % 3]
    return total % 10


def _mrz_field_ok(data: str, check_char: str) -> bool:
    """True if ``check_char`` is a digit equal to the computed check digit of ``data``."""
    if not check_char.isdigit():
        return False
    computed = _mrz_check_digit(data)
    return computed >= 0 and computed == int(check_char)


def _mrz_td3_valid(line2: str) -> bool:
    """Validate a TD3 (passport) second line: doc-number, DOB, expiry, composite check digits."""
    if len(line2) != 44:
        return False
    doc_ok = _mrz_field_ok(line2[0:9], line2[9])
    dob_ok = _mrz_field_ok(line2[13:19], line2[19])
    exp_ok = _mrz_field_ok(line2[21:27], line2[27])
    composite = line2[0:10] + line2[13:20] + line2[21:28] + line2[28:43]
    comp_ok = _mrz_field_ok(composite, line2[43])
    return doc_ok and dob_ok and exp_ok and comp_ok


def _mrz_td2_valid(line2: str) -> bool:
    """Validate a TD2 second line (doc-number, DOB, expiry, composite check digits)."""
    if len(line2) != 36:
        return False
    doc_ok = _mrz_field_ok(line2[0:9], line2[9])
    dob_ok = _mrz_field_ok(line2[13:19], line2[19])
    exp_ok = _mrz_field_ok(line2[21:27], line2[27])
    composite = line2[0:10] + line2[13:20] + line2[21:28] + line2[28:35]
    comp_ok = _mrz_field_ok(composite, line2[35])
    return doc_ok and dob_ok and exp_ok and comp_ok


def _mrz_td1_valid(line1: str, line2: str) -> bool:
    """Validate a TD1 pair (doc-number, DOB, expiry, composite check digits across lines)."""
    if len(line1) != 30 or len(line2) != 30:
        return False
    doc_ok = _mrz_field_ok(line1[5:14], line1[14])
    dob_ok = _mrz_field_ok(line2[0:6], line2[6])
    exp_ok = _mrz_field_ok(line2[8:14], line2[14])
    composite = line1[5:30] + line2[0:7] + line2[8:15] + line2[18:29]
    comp_ok = _mrz_field_ok(composite, line2[29])
    return doc_ok and dob_ok and exp_ok and comp_ok


def _mrz_lines_with_spans(text: str) -> list[tuple[int, int, str]]:
    """Return one ``(start, end, line)`` per full line that is pure MRZ chars, else skipped."""
    out: list[tuple[int, int, str]] = []
    idx = 0
    for raw_line in text.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        end = idx + len(content)
        m = _MRZ_LINE_RE.fullmatch(content)
        if m is not None:
            out.append((idx, end, content))
        else:
            out.append((idx, end, ""))  # placeholder keeps adjacency but is not MRZ
        idx += len(raw_line)
    return out


def detect_pii_passport_mrz(text: str, _ctx: RuleContext) -> list[RuleHit]:
    """Detect ICAO 9303 MRZ passport / travel-document blocks (TD1/TD2/TD3) via check digits.

    Runs on the ORIGINAL ``text`` (redaction offsets map to the original — the "raw PII never
    leaves" invariant). The matched span is the exact source substring covering the MRZ block
    so the pipeline's ``processed_text.replace`` swaps in the deterministic ``[REDACTED:
    passport:hex8]`` token. Only VALIDATED blocks (all four check digits pass) are emitted, so
    a checksum-failing near-miss is never flagged.
    """
    lines = _mrz_lines_with_spans(text)
    hits: list[RuleHit] = []
    consumed = 0  # index up to which lines are already part of an emitted block
    i = 0
    n = len(lines)
    while i < n:
        if i < consumed:
            i += 1
            continue
        start, _end, content = lines[i]
        width = len(content)
        # TD3 (passport) — two 44-char lines.
        if width == 44 and i + 1 < n and len(lines[i + 1][2]) == 44 and _mrz_td3_valid(lines[i + 1][2]):
            block_end = lines[i + 1][1]
            hits.append(RuleHit(text[start:block_end], "passport"))
            consumed = i + 2
            i += 2
            continue
        # TD2 — two 36-char lines.
        if width == 36 and i + 1 < n and len(lines[i + 1][2]) == 36 and _mrz_td2_valid(lines[i + 1][2]):
            block_end = lines[i + 1][1]
            hits.append(RuleHit(text[start:block_end], "passport"))
            consumed = i + 2
            i += 2
            continue
        # TD1 — three 30-char lines.
        if (
            width == 30
            and i + 2 < n
            and len(lines[i + 1][2]) == 30
            and len(lines[i + 2][2]) == 30
            and _mrz_td1_valid(content, lines[i + 1][2])
        ):
            block_end = lines[i + 2][1]
            hits.append(RuleHit(text[start:block_end], "passport"))
            consumed = i + 3
            i += 3
            continue
        i += 1
    return hits


# ── Per-request canary-token leak detector (output rule — B7) ────────────────────
def _canary_variants(token: str) -> list[str]:
    """Precompute lowercase match variants of a canary token: exact + hex + base64.

    The de-spaced comparison is done against a whitespace-stripped copy of the OUTPUT (not
    against a variant here), so "AB CD EF" style spacing is also caught.
    """
    variants: list[str] = [token.lower()]
    raw = token.encode("utf-8")
    variants.append(raw.hex().lower())
    with_pad = base64.b64encode(raw).decode("ascii").lower()
    variants.append(with_pad)
    variants.append(with_pad.rstrip("="))  # unpadded base64
    return [v for v in variants if v]


def detect_output_canary_leak(text: str, ctx: RuleContext) -> list[RuleHit]:
    """Block the output when a caller-supplied canary token leaks (exact / de-spaced / hex /
    base64). Inert (returns ``[]``) when ``ctx.canary_tokens`` is unset — byte-identical to
    today. The matched value stored is a SAFE fixed label, never the raw token.
    """
    tokens = ctx.canary_tokens or []
    if not tokens:
        return []
    lowered = text.lower()
    despaced = re.sub(r"\s+", "", lowered)
    hits: list[RuleHit] = []
    for token in tokens:
        if not token:
            continue
        token_despaced = re.sub(r"\s+", "", token.lower())
        leaked = any(v in lowered for v in _canary_variants(token))
        if not leaked and token_despaced and token_despaced in despaced:
            leaked = True
        if leaked:
            hits.append(RuleHit(matched_text="canary_token_leak", category="security"))
    return hits


# ── Native context-window PII validation -> passport/name (B8) ───────────────────
# A bare passport-number pattern (6-9 alphanumerics with >=1 digit) is a false-positive
# machine on its own; gating it on a proximity keyword window (Presidio context enhancer /
# Google DLP hotword mechanism) makes it deployable as a microsecond substring scan.
_PASSPORT_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]{6,9}(?![A-Za-z0-9])")
# A name candidate: an honorific ("Mr.", "Dr.") immediately followed by 1-3 capitalized words.
_NAME_CANDIDATE_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Miss|Sir)\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"
)


def _context_supported(
    text: str, start: int, end: int, terms: tuple[str, ...], window: int
) -> bool:
    """True if any ``terms`` entry appears within ``window`` chars of the ``[start, end)`` span.

    Case-insensitive substring proximity — the exact precision mechanism Presidio's context
    enhancer and Google DLP hotword rules use, implemented natively (no spaCy / model).
    """
    if not terms:
        return False
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    ctx_window = text[lo:hi].lower()
    return any(term in ctx_window for term in terms if term)


def detect_pii_passport_context(text: str, ctx: RuleContext) -> list[RuleHit]:
    """Passport-number-in-prose, admitted only with a supporting term nearby (B8; default off)."""
    if not ctx.pii_context_enabled:
        return []
    hits: list[RuleHit] = []
    for m in _PASSPORT_CANDIDATE_RE.finditer(text):
        candidate = m.group(0)
        if not any(c.isdigit() for c in candidate):
            continue  # a passport number carries at least one digit
        if _context_supported(
            text, m.start(), m.end(), ctx.pii_context_passport_terms, ctx.pii_context_window
        ):
            hits.append(RuleHit(candidate, "passport"))
    return hits


def detect_pii_name_context(text: str, ctx: RuleContext) -> list[RuleHit]:
    """Honorific-gated personal name, admitted only with a supporting term nearby (B8; off)."""
    if not ctx.pii_context_enabled:
        return []
    hits: list[RuleHit] = []
    for m in _NAME_CANDIDATE_RE.finditer(text):
        # The honorific itself is the supporting context; still gate on the lexicon so an
        # operator can tune/disable it. The captured group is the name (redacted), not the title.
        if _context_supported(
            text, m.start(), m.end(), ctx.pii_context_name_terms, ctx.pii_context_window
        ):
            hits.append(RuleHit(m.group(1), "name"))
    return hits


def _regex_hits(patterns: list[re.Pattern[str]], text: str, category: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for pat in patterns:
        for m in pat.finditer(text):
            hits.append(RuleHit(matched_text=m.group(0), category=category))
    return hits


# ── INPUT rule detectors ───────────────────────────────────────────────────────
def _detect_view(ctx: RuleContext, text: str) -> str:
    """Return the canonicalized detection view when set (B1), else the raw text.

    Block-category detectors match against this view so obfuscated payloads (zero-width /
    Tags / bidi / fullwidth / homoglyph) are caught. ``None`` (unit-level callers) => raw
    text, so the default path is byte-identical.
    """
    return ctx.detection_text if ctx.detection_text is not None else text


def _detect_injection_style(
    text: str,
    ctx: RuleContext,
    *,
    patterns: list[re.Pattern[str]],
    automaton: AhoCorasick,
    base_category: str,
) -> list[RuleHit]:
    """Shared regex + Aho-Corasick signature detection for injection / jailbreak (B1 + B2).

    Regex hits run over the canonicalized detection VIEW exactly as before (byte-identical
    when the view equals the raw text). The corpus signature pack (compiled once at import)
    is scanned in a single O(n) pass over the lowercased view and its WORD-BOUNDED matches
    are appended, de-duplicated against the regex matches. The spotlight ``*_untrusted``
    category marks any hit found inside a marked untrusted span so the pipeline can escalate.
    """
    view = _detect_view(ctx, text)
    untrusted = ctx.untrusted_spans or []
    untrusted_suffix = f"{base_category}_untrusted"
    hits: list[RuleHit] = []
    seen_lower: set[str] = set()

    for pat in patterns:
        for m in pat.finditer(view):
            matched = m.group(0)
            in_untrusted = any(matched in span for span in untrusted)
            hits.append(RuleHit(matched, untrusted_suffix if in_untrusted else base_category))
            seen_lower.add(matched.lower())

    lowered = view.lower()
    untrusted_lower = [s.lower() for s in untrusted]
    for _start, _end, matched in signature_matches(automaton, lowered):
        if matched in seen_lower:
            continue
        seen_lower.add(matched)
        in_untrusted = any(matched in span for span in untrusted_lower)
        hits.append(RuleHit(matched, untrusted_suffix if in_untrusted else base_category))
    return hits


def detect_prompt_injection(text: str, ctx: RuleContext) -> list[RuleHit]:
    """Detect prompt-injection patterns (regex + corpus signature pack, over the B1 view).

    Spotlight (ADDITIVE): an injection pattern that appears INSIDE a marked untrusted span
    (RAG/tool-provided content, ``ctx.untrusted_spans``) is the high-risk case and is
    emitted with the dedicated ``security_untrusted`` category so the pipeline can apply a
    STRICTER action (block) regardless of any policy downgrade. With no marked spans / no
    detection view this is byte-identical to the prior regex behaviour (all hits ``security``).
    """
    return _detect_injection_style(
        text, ctx,
        patterns=_PROMPT_INJECTION_PATTERNS,
        automaton=INJECTION_AUTOMATON,
        base_category="security",
    )


def detect_pii_email(text: str, ctx: RuleContext) -> list[RuleHit]:
    hits = [RuleHit(m.group(0), "email") for m in _EMAIL_RE.finditer(text)]
    return _union_presidio(hits, ctx, only=("email",))


def detect_pii_phone(text: str, ctx: RuleContext) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for m in _PHONE_RE.finditer(text):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if not (7 <= len(digits) <= 15):
            continue
        # A phone must LOOK like a phone — not a bare number, version string, error code,
        # or constant. Accept only an international form (leading '+', 8-15 digits) OR a
        # domestic form that is grouped with separators AND is NANP-ish in length (10-11
        # digits). This rejects the false positives a loose digit-run match produced
        # (e.g. 20260613, 3.14159265, 0x80070005 -> 80070005, 1234567, '1111 2222 3333')
        # while still catching '+1 234 567 8900', '(123) 456-7890', '555-867-5309'.
        has_sep = bool(re.search(r"[\s.()\-]", raw))
        is_international = raw.startswith("+") and 8 <= len(digits) <= 15
        is_domestic = has_sep and 10 <= len(digits) <= 11
        if not (is_international or is_domestic):
            continue
        hits.append(RuleHit(raw, "phone"))
    return _union_presidio(hits, ctx, only=("phone",))


def _cc_hits(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for m in _CC_CANDIDATE_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            hits.append(RuleHit(raw.strip(), "credit_card"))
    return hits


def detect_pii_credit_card(text: str, ctx: RuleContext) -> list[RuleHit]:
    return _union_presidio(_cc_hits(text), ctx, only=("credit_card",))


def detect_pii_ssn(text: str, ctx: RuleContext) -> list[RuleHit]:
    """US SSN in dashed 3-2-4 form, with structural validation (no 000/666/9xx area,
    no 00 group, no 0000 serial) to avoid flagging arbitrary dashed numbers."""
    hits: list[RuleHit] = []
    for m in _SSN_RE.finditer(text):
        area, group, serial = m.group(1), m.group(2), m.group(3)
        if area in ("000", "666") or area[0] == "9":
            continue
        if group == "00" or serial == "0000":
            continue
        hits.append(RuleHit(m.group(0), "ssn"))
    return _union_presidio(hits, ctx, only=("ssn", "us_ssn"))


def detect_pii_ip(text: str, ctx: RuleContext) -> list[RuleHit]:
    """IPv4 address with each octet range-validated (0-255)."""
    hits: list[RuleHit] = []
    for m in _IPV4_RE.finditer(text):
        if all(o.isdigit() and int(o) <= 255 for o in m.group(1).split(".")):
            hits.append(RuleHit(m.group(1), "ip"))
    return _union_presidio(hits, ctx, only=("ip", "ip_address"))


def detect_pii_address(text: str, ctx: RuleContext) -> list[RuleHit]:
    """Conservative street-address detector (house number + words + street-type suffix)."""
    hits = [RuleHit(m.group(0).strip(), "address") for m in _ADDRESS_RE.finditer(text)]
    return _union_presidio(hits, ctx, only=("address", "location"))


def detect_jailbreak(text: str, ctx: RuleContext) -> list[RuleHit]:
    """Detect jailbreak patterns (regex + corpus signature pack, over the B1 detection view)."""
    return _detect_injection_style(
        text, ctx,
        patterns=_JAILBREAK_PATTERNS,
        automaton=JAILBREAK_AUTOMATON,
        base_category="jailbreak",
    )


def _toxicity_hits(text: str, ctx: RuleContext) -> list[RuleHit]:
    # Prefer the pre-computed categories from the confidence-banded remote cascade (the
    # small/large-LLM stage). ``None`` means the cascade did not run (mode='stub' or the
    # async pre-compute was skipped) -> fall back to the in-process classifier (today's path).
    if ctx.precomputed_toxicity is not None:
        return [RuleHit(cat.label, "toxicity") for cat in ctx.precomputed_toxicity]
    if ctx.classifier is None:
        return []
    return [RuleHit(cat.label, "toxicity") for cat in ctx.classifier.classify(text)]


def _union_presidio(hits: list[RuleHit], ctx: RuleContext, *, only: tuple[str, ...]) -> list[RuleHit]:
    """Add Presidio-located spans (categories in ``only``) to ``hits``, de-duplicated.

    Presidio only LOCATES spans; rendering to the deterministic HMAC token happens in the
    pipeline exactly as for regex hits, so the redaction token format is unchanged.
    """
    if not ctx.presidio_spans:
        return hits
    seen = {(h.matched_text, h.category) for h in hits}
    for matched_text, category in ctx.presidio_spans:
        if category in only and (matched_text, category) not in seen:
            hits.append(RuleHit(matched_text=matched_text, category=category))
            seen.add((matched_text, category))
    return hits


def detect_toxicity(text: str, ctx: RuleContext) -> list[RuleHit]:
    return _toxicity_hits(text, ctx)


# ── OUTPUT rule detectors ─────────────────────────────────────────────────────
def detect_output_pii_email(text: str, ctx: RuleContext) -> list[RuleHit]:
    """Redact emails in the output that were NOT in the input.

    If ``input_text`` is omitted the rule degrades to "ANY email -> redact"
    (over-redaction is the safe failure mode).
    """
    input_emails: set[str] = set()
    if ctx.input_text is not None:
        input_emails = {m.group(0).lower() for m in _EMAIL_RE.finditer(ctx.input_text)}
    hits: list[RuleHit] = []
    for m in _EMAIL_RE.finditer(text):
        if m.group(0).lower() not in input_emails:
            hits.append(RuleHit(m.group(0), "email"))
    return hits


def detect_output_pii_credit_card(text: str, _ctx: RuleContext) -> list[RuleHit]:
    return _cc_hits(text)


def detect_output_jailbreak_leak(text: str, _ctx: RuleContext) -> list[RuleHit]:
    return _regex_hits(_OUTPUT_JAILBREAK_LEAK_PATTERNS, text, "jailbreak")


def detect_output_toxicity(text: str, ctx: RuleContext) -> list[RuleHit]:
    return _toxicity_hits(text, ctx)


def detect_output_max_length(text: str, ctx: RuleContext) -> list[RuleHit]:
    if len(text) > ctx.max_output_chars:
        return [RuleHit(f"length={len(text)} > max={ctx.max_output_chars}", "length")]
    return []


# ── CUSTOM rule detector factories (WP07) ──────────────────────────────────────
# Tenant-authored rules (loaded from guardrails.rules by the dynamic loader in
# ``registry``) are turned into the SAME ``(text, ctx) -> list[RuleHit]`` detector shape
# the built-in rules use, so they execute through the UNMODIFIED pipeline. The detector
# is a pure closure: it never mutates state and never raises for ordinary input. The
# regex is compiled ONCE here (the loader already validated/compiled it under the ReDoS
# guard at save time); a compile error degrades to a no-op detector rather than raising
# on the hot path.

# Custom rule types (mirrors guardrails.rules.custom_type).
CUSTOM_TYPE_REGEX = "regex"
CUSTOM_TYPE_CLASSIFIER_THRESHOLD = "classifier-threshold"


def make_regex_detector(pattern: str, category: str) -> Callable[[str, RuleContext], list[RuleHit]]:
    """Build a detector that emits one hit per match of ``pattern`` (compiled once)."""
    try:
        compiled = re.compile(pattern)
    except re.error:
        # Should not happen (the save-time guard rejects uncompilable patterns), but a
        # corrupt row must not crash the loader — degrade to a no-op detector.
        def _noop(_text: str, _ctx: RuleContext) -> list[RuleHit]:
            return []

        return _noop

    def _detect(text: str, _ctx: RuleContext) -> list[RuleHit]:
        return [RuleHit(matched_text=m.group(0), category=category) for m in compiled.finditer(text)]

    return _detect


def make_classifier_threshold_detector(
    target_category: str, threshold: float, category: str
) -> Callable[[str, RuleContext], list[RuleHit]]:
    """Build a detector that fires when the classifier scores ``target_category`` >= threshold.

    The classifier is supplied via :class:`RuleContext` (the pipeline injects it). With no
    classifier wired the detector degrades to a no-op (it cannot score). The matched text
    is a SAFE label (``category:score``) — never raw input.
    """

    def _detect(text: str, ctx: RuleContext) -> list[RuleHit]:
        if ctx.classifier is None:
            return []
        for cat in ctx.classifier.classify(text):
            if cat.label == target_category and cat.score >= threshold:
                return [RuleHit(matched_text=f"{cat.label}:{cat.score:.2f}", category=category)]
        return []

    return _detect


# ── Rule registry (the 11 first-cycle rules) ──────────────────────────────────
INPUT_RULES: list[RuleSpec] = [
    RuleSpec(
        rule_id="prompt-injection-v1",
        name="Prompt Injection Detector",
        direction="input",
        default_action="block",
        severity="critical",
        category="security",
        detect=detect_prompt_injection,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-email-v1",
        name="PII Email Detector",
        direction="input",
        default_action="redact",
        severity="medium",
        category="pii",
        detect=detect_pii_email,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-phone-v1",
        name="PII Phone Detector",
        direction="input",
        default_action="redact",
        severity="medium",
        category="pii",
        detect=detect_pii_phone,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-credit-card-v1",
        name="PII Credit Card Detector",
        direction="input",
        default_action="block",
        severity="high",
        category="pii",
        detect=detect_pii_credit_card,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-ssn-v1",
        name="PII SSN Detector",
        direction="input",
        default_action="redact",
        severity="high",
        category="pii",
        detect=detect_pii_ssn,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-ip-v1",
        name="PII IP Address Detector",
        direction="input",
        default_action="redact",
        severity="low",
        category="pii",
        detect=detect_pii_ip,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-address-v1",
        name="PII Street Address Detector",
        direction="input",
        default_action="redact",
        severity="medium",
        category="pii",
        detect=detect_pii_address,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-passport-mrz-v1",
        name="PII Passport MRZ Detector",
        direction="input",
        default_action="redact",
        severity="high",
        category="pii",
        detect=detect_pii_passport_mrz,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-passport-v1",
        name="PII Passport (context-gated) Detector",
        direction="input",
        default_action="redact",
        severity="high",
        category="pii",
        detect=detect_pii_passport_context,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="pii-name-v1",
        name="PII Name (honorific-gated) Detector",
        direction="input",
        default_action="redact",
        severity="medium",
        category="pii",
        detect=detect_pii_name_context,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="jailbreak-v1",
        name="Jailbreak Detector",
        direction="input",
        default_action="block",
        severity="critical",
        category="jailbreak",
        detect=detect_jailbreak,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="toxicity-v1",
        name="Toxicity Classifier",
        direction="input",
        default_action="block",
        severity="high",
        category="toxicity",
        detect=detect_toxicity,
        timeout_ms=50,
    ),
]

OUTPUT_RULES: list[RuleSpec] = [
    RuleSpec(
        rule_id="output-pii-email-v1",
        name="Output PII Email Detector",
        direction="output",
        default_action="redact",
        severity="medium",
        category="pii",
        detect=detect_output_pii_email,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="output-pii-credit-card-v1",
        name="Output PII Credit Card Detector",
        direction="output",
        default_action="block",
        severity="high",
        category="pii",
        detect=detect_output_pii_credit_card,
        timeout_ms=10,
    ),
    # Output PII detectors for phone/SSN/IP/address — these were MISSING, so those PII types
    # leaked through /v1/check/output. They reuse the (tightened) input detectors, which redact
    # ANY occurrence, so model-introduced PII of these kinds is now redacted on egress.
    RuleSpec(
        rule_id="output-pii-phone-v1",
        name="Output PII Phone Detector",
        direction="output",
        default_action="redact",
        severity="medium",
        category="pii",
        detect=detect_pii_phone,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="output-pii-ssn-v1",
        name="Output PII SSN Detector",
        direction="output",
        default_action="redact",
        severity="high",
        category="pii",
        detect=detect_pii_ssn,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="output-pii-ip-v1",
        name="Output PII IP Address Detector",
        direction="output",
        default_action="redact",
        severity="low",
        category="pii",
        detect=detect_pii_ip,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="output-pii-address-v1",
        name="Output PII Street Address Detector",
        direction="output",
        default_action="redact",
        severity="medium",
        category="pii",
        detect=detect_pii_address,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="output-pii-passport-mrz-v1",
        name="Output PII Passport MRZ Detector",
        direction="output",
        default_action="redact",
        severity="high",
        category="pii",
        detect=detect_pii_passport_mrz,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="output-canary-leak-v1",
        name="Output Canary Token Leak Detector",
        direction="output",
        default_action="block",
        severity="high",
        category="security",
        detect=detect_output_canary_leak,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="output-jailbreak-leak-v1",
        name="Output Jailbreak Leak Detector",
        direction="output",
        default_action="block",
        severity="high",
        category="jailbreak",
        detect=detect_output_jailbreak_leak,
        timeout_ms=10,
    ),
    RuleSpec(
        rule_id="output-toxicity-v1",
        name="Output Toxicity Classifier",
        direction="output",
        default_action="block",
        severity="high",
        category="toxicity",
        detect=detect_output_toxicity,
        timeout_ms=50,
    ),
    RuleSpec(
        rule_id="output-max-length-v1",
        name="Output Max Length",
        direction="output",
        default_action="block",
        severity="low",
        category="length",
        detect=detect_output_max_length,
        timeout_ms=10,
    ),
]

ALL_RULES: list[RuleSpec] = [*INPUT_RULES, *OUTPUT_RULES]
RULES_BY_ID: dict[str, RuleSpec] = {r.rule_id: r for r in ALL_RULES}
