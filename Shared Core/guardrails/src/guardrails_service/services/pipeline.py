"""Check pipeline (Component 1/5 + Audit #2/#8).

Evaluates a direction's rules against text under the effective policy:

* Rules run in LEXICOGRAPHIC order by ``rule_id`` (Audit #2 — deterministic).
* Decision precedence: BLOCK > REDACT > WARN > ALLOW.
* SHORT-CIRCUIT on the first BLOCK (evaluation halts).
* REDACT rules apply the deterministic redaction token to ``processed_text``.
* Per-rule ``timeout_ms``: on timeout apply ``default_fail_mode`` (closed = block) and
  emit a WARN log + metric (Audit #8). (Synchronous regex/heuristic rules effectively
  never time out in-process; the guard is structural so the contract holds when a rule
  is later backed by a slow classifier.)
* FIX C — every ``violation.matched`` is SAFE to log: the redaction TOKEN for PII
  categories, or a <=64-char truncation for non-PII. The SAME value is what the DB
  ``violations.matched_text`` stores. Raw PII NEVER leaves the pipeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from ..core import metrics
from ..core.redaction import PII_CATEGORIES, redaction_token
from ..models.check import Decision
from .policy_engine import EffectivePolicy
from .rules import RULE_STATUS_RETIRED, RULES_BY_ID, RuleContext, RuleHit, RuleSpec

logger = structlog.get_logger(__name__)

_MATCHED_TRUNCATE = 64
# Max redaction-safe match samples kept per rule in a simulation trace (bounds payload size).
_TRACE_SAMPLE_LIMIT = 5

_PRECEDENCE: dict[str, int] = {"allow": 0, "warn": 1, "redact": 2, "block": 3}

# Spotlight (prompt-injection defense): a hit whose category is one of these marks an
# injection/jailbreak pattern found INSIDE marked untrusted (RAG/tool) content. Such a hit
# is escalated to a STRICTER action — see ``_spotlight_action``. The base category is
# recorded on the violation so the response shape (category) is unchanged for callers.
_UNTRUSTED_SUFFIX = "_untrusted"
_SPOTLIGHT_CATEGORIES: frozenset[str] = frozenset({"security_untrusted", "jailbreak_untrusted"})

# Default set of rule categories treated as SAFETY-critical for the fail-closed-on-timeout
# rule (a timed-out safety rule must never silently allow). 'length' is intentionally
# excluded. The live check path passes the config-driven set; this is the fallback.
SAFETY_CATEGORIES: frozenset[str] = frozenset({"security", "pii", "jailbreak", "toxicity"})


@dataclass
class EvaluatedViolation:
    """A violation produced by the pipeline (already redaction-safe in ``matched``)."""

    rule_id: str
    rule_name: str
    severity: str
    category: str
    matched: str  # SAFE: token for PII, <=64-char truncation otherwise (FIX C)
    action: str


@dataclass
class RuleTraceEntry:
    """Per-rule evaluation trace (simulation only — never on the normal check path).

    ``matched`` is the redaction-SAFE value (token for PII, <=64-char truncation otherwise),
    so a trace is always safe to return to a policy author / log.
    """

    rule_id: str
    rule_name: str
    direction: str
    evaluated: bool          # False == skipped (short-circuited by an earlier block)
    matched: bool            # did the detector produce >=1 hit?
    action: str              # the effective action for this rule (override or default)
    effective_fail_mode: str  # policy fail_mode_override or the rule's default_fail_mode
    timed_out: bool
    timing_ms: float
    hit_count: int
    matched_samples: list[str] = field(default_factory=list)  # redaction-safe samples


@dataclass
class PipelineResult:
    """Outcome of evaluating one direction's rules.

    ``trace`` is populated ONLY when ``evaluate(..., trace=True)`` (simulation); the normal
    check path leaves it ``None`` and pays no extra cost.
    """

    decision: Decision
    processed_text: str | None
    violations: list[EvaluatedViolation] = field(default_factory=list)
    trace: list[RuleTraceEntry] | None = None


def _safe_matched(category: str, matched_text: str, *, key: str, tenant_id: str) -> str:
    """Render the SAFE ``matched`` value per FIX C / Component 4.

    PII categories -> redaction token; everything else -> <=64-char truncation.
    """
    if category in PII_CATEGORIES:
        return redaction_token(key, category, tenant_id, matched_text)
    return matched_text[:_MATCHED_TRUNCATE]


def _effective_action(spec: RuleSpec, override: str | None) -> str:
    return override if override else spec.default_action


def _spotlight_action(action: str, hits: list[RuleHit], *, threshold: float) -> str:
    """Escalate ``action`` to 'block' when any hit is from a marked untrusted span.

    The spotlight (prompt-injection defense) treats injection/jailbreak patterns inside
    RAG/tool-provided content as the high-risk case and applies a STRICTER action than the
    policy's (possibly downgraded) override. ``threshold`` >= 1.0 disables escalation. The
    base action is returned unchanged when no hit is from an untrusted span — so benign
    input and trusted-only matches keep today's verdict.
    """
    if threshold >= 1.0:
        return action
    if any(h.category in _SPOTLIGHT_CATEGORIES for h in hits):
        return "block"
    return action


def _base_category(category: str) -> str:
    """Strip the spotlight ``_untrusted`` marker so the violation category is unchanged."""
    if category.endswith(_UNTRUSTED_SUFFIX):
        return category[: -len(_UNTRUSTED_SUFFIX)]
    return category


def _run_detect(
    spec: RuleSpec, text: str, ctx: RuleContext, *, budget_ms: float
) -> tuple[list[RuleHit], bool]:
    """Run a rule's detector, returning (hits, timed_out).

    The detectors are synchronous and pure; we measure wall-clock and treat an overrun
    of ``budget_ms`` (the rule's ``timeout_ms``, raised to a safety floor for safety rules)
    as a timeout (fail-mode applied by the caller). Any unexpected exception is also treated
    as a timeout-equivalent failure so a buggy rule fails per its ``default_fail_mode``
    rather than 500-ing the request.
    """
    started = time.monotonic()
    try:
        hits = spec.detect(text, ctx)
    except Exception as exc:  # noqa: BLE001 — a rule failure must apply fail-mode, not crash
        logger.warning("rule_detect_error", rule_id=spec.rule_id, error=str(exc))
        return [], True
    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms > budget_ms:
        logger.warning(
            "rule_timeout", rule_id=spec.rule_id, elapsed_ms=round(elapsed_ms, 2),
            timeout_ms=budget_ms,
        )
        metrics.rule_evaluations_timeout_total.labels(spec.rule_id).inc()
        return hits, True
    return hits, False


def evaluate(
    *,
    text: str,
    policy: EffectivePolicy,
    direction: str,
    tenant_id: str,
    redaction_key: str,
    ctx: RuleContext,
    trace: bool = False,
    honor_fail_mode_override: bool = True,
    injection_block_threshold: float = 1.0,
    safety_fail_closed: bool = False,
    safety_categories: frozenset[str] = SAFETY_CATEGORIES,
    safety_min_timeout_ms: int = 0,
) -> PipelineResult:
    """Evaluate the policy's rules for ``direction`` against ``text``.

    Returns the aggregate decision, the (possibly redacted) processed text, and the
    list of redaction-safe violations. When ``trace=True`` (simulation only) the result
    also carries a per-rule :class:`RuleTraceEntry` list — matched?, action, fail-mode,
    timing, hit count + redaction-safe samples — including rules SKIPPED by a short-circuit.
    The normal check path passes ``trace=False`` and the trace bookkeeping is skipped
    entirely so the hot path is unchanged.

    ``honor_fail_mode_override`` (FIX 5): when True the policy's ``fail_mode_override``
    governs the per-rule timeout fail posture on the LIVE path too (previously only
    simulation honoured it). Simulation always passes True (unchanged); the live path passes
    ``settings.live_fail_mode_override_enabled`` (default True). Set False to restore the
    prior live behaviour (each rule's own ``default_fail_mode``).

    ``injection_block_threshold`` (< 1.0 enables the spotlight): an injection/jailbreak
    pattern found inside a marked untrusted span is escalated to 'block'. Defaults to 1.0
    (disabled) so callers that do not opt in keep today's verdicts.

    ``safety_fail_closed`` (safety hardening): when True, a timed-out/failed rule whose
    ``category`` is in ``safety_categories`` is treated as a VIOLATION (block) regardless of
    the resolved ``fail_mode`` — a timed-out SAFETY rule must never silently allow (e.g. a
    PII rule overrunning its budget under load). NON-safety rules keep their fail_mode posture.
    Defaults to False so simulation + unit-level callers stay byte-identical; the LIVE check
    path opts in via ``settings.safety_rule_fail_closed_on_timeout`` (default on).
    ``safety_min_timeout_ms`` raises a safety rule's effective per-rule budget to this floor
    (over its own ``timeout_ms``) so transient load no longer trips a false timeout that would
    skip PII redaction; 0 (default) keeps each rule's own ``timeout_ms``.
    """
    # Resolve the policy's enabled rules that apply to this direction, lexicographic.
    # Rules the DB registry marks 'retired' are disabled (WP02 metadata overlay).
    enabled = {er.rule_id: er.action_override for er in policy.rules}
    specs = sorted(
        (
            RULES_BY_ID[rid]
            for rid in enabled
            if rid in RULES_BY_ID
            and RULES_BY_ID[rid].direction in (direction, "both")
            and RULES_BY_ID[rid].status != RULE_STATUS_RETIRED
        ),
        key=lambda s: s.rule_id,
    )

    processed_text = text
    violations: list[EvaluatedViolation] = []
    overall = "allow"
    trace_entries: list[RuleTraceEntry] | None = [] if trace else None
    short_circuited = False

    for spec in specs:
        action = _effective_action(spec, enabled.get(spec.rule_id))

        # Once an earlier rule BLOCKED, remaining rules are skipped (short-circuit). On the
        # normal path we simply `break`; in trace mode we still record them as not-evaluated.
        if short_circuited:
            if trace_entries is not None:
                trace_entries.append(
                    _skipped_trace(spec, action, policy.fail_mode_override)
                )
            continue

        # SAFETY hardening: a safety rule (PII/security/jailbreak/toxicity) gets a higher
        # per-rule timeout floor so transient load does not trip a false timeout that would
        # (under fail-open) silently skip e.g. PII redaction.
        is_safety = spec.category in safety_categories
        budget_ms = float(spec.timeout_ms)
        if is_safety and safety_min_timeout_ms > 0:
            budget_ms = max(budget_ms, float(safety_min_timeout_ms))

        started = time.monotonic()
        # Detect against the ORIGINAL text, NOT the progressively-redacted processed_text, so an
        # earlier rule's redaction TOKEN can never be re-matched by a later rule (e.g. the phone
        # rule matching the digits inside an email token -> [REDACTED:email:[REDACTED:phone:..]]).
        # Each redaction still accumulates into processed_text below; detecting on the original
        # also makes PII detection order-independent and keeps each token's HMAC over the raw PII.
        hits, timed_out = _run_detect(spec, text, ctx, budget_ms=budget_ms)
        timing_ms = (time.monotonic() - started) * 1000
        # FIX 5: honor the policy fail_mode_override on the LIVE path (gated). Simulation
        # passes honor_fail_mode_override=True (unchanged). When disabled we revert to the
        # rule's own default_fail_mode, matching the prior live behaviour exactly.
        if honor_fail_mode_override:
            fail_mode = policy.fail_mode_override or spec.default_fail_mode
        else:
            fail_mode = spec.default_fail_mode
        # SAFETY hardening: a timed-out/failed SAFETY rule must NEVER silently allow. Force
        # fail-CLOSED for it regardless of the resolved fail_mode (a policy fail_mode_override
        # of 'open' would otherwise skip a timed-out PII/injection rule and leak). NON-safety
        # rules (length) keep their fail_mode posture. Opt-in (live path) — default off keeps
        # simulation + unit-level callers byte-identical.
        if timed_out and safety_fail_closed and is_safety and fail_mode != "closed":
            logger.warning(
                "safety_rule_fail_closed_on_timeout",
                rule_id=spec.rule_id, category=spec.category, resolved_fail_mode=fail_mode,
            )
            fail_mode = "closed"
        samples: list[str] = []

        if timed_out:
            # Fail-mode: closed -> block. open -> skip the rule's contribution.
            if fail_mode == "closed":
                violations.append(
                    EvaluatedViolation(
                        rule_id=spec.rule_id,
                        rule_name=spec.name,
                        severity=spec.severity,
                        category=spec.category,
                        matched=f"timeout:{spec.rule_id}"[:_MATCHED_TRUNCATE],
                        action="block",
                    )
                )
                overall = "block"
                if trace_entries is not None:
                    trace_entries.append(
                        RuleTraceEntry(
                            rule_id=spec.rule_id, rule_name=spec.name, direction=spec.direction,
                            evaluated=True, matched=False, action="block",
                            effective_fail_mode=fail_mode, timed_out=True,
                            timing_ms=round(timing_ms, 3), hit_count=0, matched_samples=[],
                        )
                    )
                short_circuited = True
                if trace_entries is None:
                    break  # short-circuit on block (fast path)
                continue
            if trace_entries is not None:
                trace_entries.append(
                    RuleTraceEntry(
                        rule_id=spec.rule_id, rule_name=spec.name, direction=spec.direction,
                        evaluated=True, matched=False, action=action,
                        effective_fail_mode=fail_mode, timed_out=True,
                        timing_ms=round(timing_ms, 3), hit_count=0, matched_samples=[],
                    )
                )
            continue

        if not hits:
            if trace_entries is not None:
                trace_entries.append(
                    RuleTraceEntry(
                        rule_id=spec.rule_id, rule_name=spec.name, direction=spec.direction,
                        evaluated=True, matched=False, action=action,
                        effective_fail_mode=fail_mode, timed_out=False,
                        timing_ms=round(timing_ms, 3), hit_count=0, matched_samples=[],
                    )
                )
            continue

        # Spotlight (prompt-injection defense): escalate to 'block' when a hit came from a
        # marked untrusted span. No-op (threshold>=1.0 / no untrusted hits) keeps ``action``.
        action = _spotlight_action(action, hits, threshold=injection_block_threshold)

        for hit in hits:
            safe = _safe_matched(_base_category(hit.category), hit.matched_text,
                                 key=redaction_key, tenant_id=tenant_id)
            violations.append(
                EvaluatedViolation(
                    rule_id=spec.rule_id,
                    rule_name=spec.name,
                    severity=spec.severity,
                    category=hit.category if hit.category in PII_CATEGORIES else spec.category,
                    matched=safe,
                    action=action,
                )
            )
            if trace_entries is not None and len(samples) < _TRACE_SAMPLE_LIMIT:
                samples.append(safe)
            if action == "redact":
                # Replace every occurrence of the raw match with its deterministic token.
                processed_text = processed_text.replace(hit.matched_text, safe)

        if _PRECEDENCE.get(action, 0) > _PRECEDENCE[overall]:
            overall = action

        if trace_entries is not None:
            trace_entries.append(
                RuleTraceEntry(
                    rule_id=spec.rule_id, rule_name=spec.name, direction=spec.direction,
                    evaluated=True, matched=True, action=action,
                    effective_fail_mode=fail_mode, timed_out=False,
                    timing_ms=round(timing_ms, 3), hit_count=len(hits), matched_samples=samples,
                )
            )

        if action == "block":
            overall = "block"
            short_circuited = True
            if trace_entries is None:
                break  # short-circuit on the first block (Audit #2)

    decision: Decision = overall  # type: ignore[assignment]
    out_text = processed_text if decision == "redact" else None
    return PipelineResult(
        decision=decision, processed_text=out_text, violations=violations, trace=trace_entries
    )


def _skipped_trace(spec: RuleSpec, action: str, fail_override: str | None) -> RuleTraceEntry:
    """Trace entry for a rule that was NOT evaluated (an earlier rule short-circuited)."""
    return RuleTraceEntry(
        rule_id=spec.rule_id,
        rule_name=spec.name,
        direction=spec.direction,
        evaluated=False,
        matched=False,
        action=action,
        effective_fail_mode=fail_override or spec.default_fail_mode,
        timed_out=False,
        timing_ms=0.0,
        hit_count=0,
        matched_samples=[],
    )
