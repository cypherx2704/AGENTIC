"""Rule functions + metadata for the 11 first-cycle rules (Component 2).

Each rule is a :class:`RuleSpec` carrying its metadata (id, name, direction,
default_action, default_fail_mode, severity, category, timeout_ms) and a pure
``detect`` callable ``(text, ctx) -> list[RuleHit]``. The callable NEVER mutates
state and NEVER raises for ordinary input — it just locates matches.

The classifier-backed toxicity rules receive the classifier via :class:`RuleContext`
(the pipeline injects it), keeping the rule functions free of global state.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..classifier import Category, Classifier

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


def _regex_hits(patterns: list[re.Pattern[str]], text: str, category: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for pat in patterns:
        for m in pat.finditer(text):
            hits.append(RuleHit(matched_text=m.group(0), category=category))
    return hits


# ── INPUT rule detectors ───────────────────────────────────────────────────────
def detect_prompt_injection(text: str, ctx: RuleContext) -> list[RuleHit]:
    """Detect prompt-injection patterns.

    Spotlight (ADDITIVE): an injection pattern that appears INSIDE a marked untrusted span
    (RAG/tool-provided content, ``ctx.untrusted_spans``) is the high-risk case and is
    emitted with the dedicated ``security_untrusted`` category so the pipeline can apply a
    STRICTER action (block) regardless of any policy downgrade. With no marked spans this is
    byte-identical to the prior regex behaviour (all hits use ``security``).
    """
    untrusted = ctx.untrusted_spans or []
    hits: list[RuleHit] = []
    for pat in _PROMPT_INJECTION_PATTERNS:
        for m in pat.finditer(text):
            matched = m.group(0)
            in_untrusted = any(matched in span for span in untrusted)
            hits.append(RuleHit(matched_text=matched, category=
                                "security_untrusted" if in_untrusted else "security"))
    return hits


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
        if raw.startswith("+") and 8 <= len(digits) <= 15:
            pass
        elif has_sep and 10 <= len(digits) <= 11:
            pass
        else:
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
    untrusted = ctx.untrusted_spans or []
    hits: list[RuleHit] = []
    for pat in _JAILBREAK_PATTERNS:
        for m in pat.finditer(text):
            matched = m.group(0)
            in_untrusted = any(matched in span for span in untrusted)
            hits.append(RuleHit(matched_text=matched, category=
                                "jailbreak_untrusted" if in_untrusted else "jailbreak"))
    return hits


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
