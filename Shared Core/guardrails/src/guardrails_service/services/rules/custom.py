"""Custom (tenant-authored) rules: ReDoS guard, persistence model, dynamic loader (WP07).

Two custom rule types live in ``guardrails.rules`` alongside the platform rows (the table
is MIXED-scope; custom rows carry a non-NULL ``tenant_id``):

* ``regex``                — a user-supplied pattern + category/severity/default_action/direction.
* ``classifier-threshold`` — a category + a threshold compared against the classifier score.

This module owns:

* :func:`assert_regex_safe` — the SAVE-time ReDoS guard (size + compile + adversarial-string
  match budget enforced in a hard-killable SUBPROCESS). Raises 422 ``UNSAFE_REGEX`` on any
  failure. Documented inline.
* :class:`CustomRuleRow` — the persisted shape (one row of ``guardrails.rules``).
* :func:`build_custom_spec` — turn a row into an executable :class:`RuleSpec` (via the
  detector factories in ``definitions``) so it runs through the UNMODIFIED pipeline.
* :class:`CustomRuleLoader` — per-tenant cache (TTL) of active custom :class:`RuleSpec`
  objects, read from the DB via the same RLS-scoped ``in_tenant`` seam everything else uses.

HOW CUSTOM RULES REACH THE PIPELINE (no pipeline.py edit):
  The pipeline evaluates ``RULES_BY_ID[rid]`` for each ``rid`` in the EFFECTIVE policy's
  ``rules``. The loader therefore (a) registers each tenant custom spec into the SAME
  ``RULES_BY_ID`` dict object the pipeline imported (mutation, not rebinding) AND
  (b) exposes the tenant's active rule_ids so the check path can append them to the
  effective policy. See :meth:`CustomRuleLoader.with_custom_rules` and the one-line hook
  documented in the module-level note of ``registry``.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass

import structlog

from .definitions import (
    CUSTOM_TYPE_CLASSIFIER_THRESHOLD,
    CUSTOM_TYPE_REGEX,
    RuleSpec,
    make_classifier_threshold_detector,
    make_regex_detector,
)

logger = structlog.get_logger(__name__)

# Reason code surfaced to the API layer (-> 422) when a regex fails the guard.
UNSAFE_REGEX = "UNSAFE_REGEX"

# Adversarial probe length (the subprocess builds the string itself to keep stdin small):
# a 64 KiB run that maximises backtracking for the classic (a+)+ / nested-quantifier
# catastrophes, with a trailing non-match so a backtracker must exhaust every path.
_ADVERSARIAL_LEN = 64 * 1024  # 64 KiB

# Minimal stdin-driven probe: read ``budget_ms`` (first line) + the pattern (rest) from
# stdin, compile it, and time a search of the 64 KiB adversarial string. The probe itself
# enforces the MATCH budget (exit 2 when the match wall-clock exceeds ``budget_ms``) so
# process-spawn cost is NOT charged against the 50 ms match budget; the parent's hard
# subprocess timeout is only the catastrophic backstop (a match that never returns at all).
# Kept inline (no temp file) and dependency-free (stdlib ``re`` only).
_PROBE_SOURCE = (
    "import sys,re,time\n"
    "data=sys.stdin.buffer.read().decode('utf-8','surrogateescape')\n"
    "budget_ms,_,p=data.partition('\\n')\n"
    f"s='a'*{_ADVERSARIAL_LEN}+'!'\n"
    "c=re.compile(p)\n"
    "t=time.monotonic()\n"
    "c.search(s)\n"
    "el=(time.monotonic()-t)*1000\n"
    "sys.exit(2 if el>float(budget_ms) else 0)\n"
)

# Hard cap on how long we wait for the probe SUBPROCESS, on top of the match budget, to
# absorb interpreter-startup cost. A match that runs LONGER than this never returns (truly
# catastrophic) and is hard-killed; a match that returns within it is judged on the probe's
# OWN measured match time vs ``budget_ms`` (so spawn cost is not charged to the 50 ms budget).
_SUBPROCESS_STARTUP_ALLOWANCE_SECONDS = 2.0


class UnsafeRegexError(ValueError):
    """A candidate custom-rule regex failed the ReDoS / compile guard."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def assert_regex_safe(
    pattern: str,
    *,
    max_length: int,
    budget_ms: float,
) -> re.Pattern[str]:
    """Validate a custom-rule regex at SAVE time; return the compiled pattern.

    The guard is THREE layered checks (cheapest first):

      1. **Size** — reject a source longer than ``max_length`` (a giant pattern is both a
         memory risk and a strong smell for a hand-rolled catastrophic backtracker).
      2. **Compile** — reject anything ``re.compile`` cannot parse (in-process).
      3. **Adversarial budget** — run the pattern against a 64 KiB adversarial string in a
         throwaway SUBPROCESS bounded by a hard timeout; reject if it does not finish within
         the budget. Catastrophic backtracking (``(a+)+$`` and friends) never returns in
         time; a linear pattern finishes in microseconds.

    CRITICAL — why a subprocess, not a thread: CPython's ``re`` does NOT release the GIL
    during a match, so a pathological pattern run in-thread (even a daemon worker) wedges the
    whole interpreter — ``Thread.join(timeout)`` never wakes because the runaway match holds
    the GIL. A subprocess is independently schedulable and HARD-KILLABLE, so
    ``assert_regex_safe`` ALWAYS returns within ``budget_ms`` + a fixed spawn allowance,
    regardless of the pattern, and the runaway probe is terminated. If the subprocess cannot
    be spawned at all (locked-down env) the guard FAILS CLOSED (reject) — an unvalidated
    regex must never be persisted.

    This is a best-effort guard against catastrophic backtracking, not a proof of
    linearity — documented as such.

    Raises :class:`UnsafeRegexError` (``reason == UNSAFE_REGEX``) on any failure.
    """
    if len(pattern) > max_length:
        logger.info("custom_rule_regex_rejected", reason="too_long", length=len(pattern))
        raise UnsafeRegexError(UNSAFE_REGEX)

    # Cheap in-process compile first (rejects syntactically invalid patterns without a spawn).
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        logger.info("custom_rule_regex_rejected", reason="compile_error", error=str(exc))
        raise UnsafeRegexError(UNSAFE_REGEX) from exc

    timeout_seconds = max(budget_ms / 1000.0, 0.001) + _SUBPROCESS_STARTUP_ALLOWANCE_SECONDS
    started = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv (our own interpreter + inline probe), pattern via stdin
            [sys.executable, "-I", "-c", _PROBE_SOURCE],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 — cannot spawn the probe -> FAIL CLOSED (reject)
        logger.warning("custom_rule_regex_probe_unavailable", error=str(exc))
        raise UnsafeRegexError(UNSAFE_REGEX) from exc

    probe_input = f"{budget_ms}\n{pattern}".encode("utf-8", "surrogateescape")
    try:
        proc.communicate(input=probe_input, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # The match never returned within the hard backstop -> truly catastrophic.
        proc.kill()
        with contextlib.suppress(Exception):
            proc.communicate(timeout=1.0)
        logger.info("custom_rule_regex_rejected", reason="timeout_hard", budget_ms=budget_ms)
        raise UnsafeRegexError(UNSAFE_REGEX) from None

    if proc.returncode == 2:
        # The probe's OWN measured match time exceeded the budget -> too slow / backtracking.
        logger.info("custom_rule_regex_rejected", reason="over_budget", budget_ms=budget_ms)
        raise UnsafeRegexError(UNSAFE_REGEX)
    if proc.returncode != 0:
        # The probe raised (e.g. a compile/resource error in the child) -> fail closed.
        logger.info("custom_rule_regex_rejected", reason="probe_nonzero", code=proc.returncode)
        raise UnsafeRegexError(UNSAFE_REGEX)

    logger.debug("custom_rule_regex_ok", elapsed_ms=round((time.monotonic() - started) * 1000, 2))
    return compiled


# ── Persisted shape ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CustomRuleRow:
    """One persisted custom-rule row (subset of guardrails.rules columns used here)."""

    rule_id: str
    tenant_id: str
    version: int
    name: str
    custom_type: str  # 'regex' | 'classifier-threshold'
    direction: str  # 'input' | 'output' | 'both'
    default_action: str
    default_fail_mode: str
    severity: str
    category: str
    timeout_ms: int
    status: str
    pattern: str | None  # regex source (regex type)
    classifier_category: str | None  # target category (classifier-threshold type)
    threshold: float | None  # score threshold (classifier-threshold type)


def build_custom_spec(row: CustomRuleRow) -> RuleSpec | None:
    """Turn a persisted custom-rule row into an executable :class:`RuleSpec`.

    Returns ``None`` for a row whose definition is incomplete/unknown (the loader skips
    it + logs) so a bad row never breaks the whole tenant's rule set.
    """
    if row.custom_type == CUSTOM_TYPE_REGEX:
        if not row.pattern:
            logger.warning("custom_rule_skipped", rule_id=row.rule_id, reason="missing_pattern")
            return None
        detect = make_regex_detector(row.pattern, row.category)
    elif row.custom_type == CUSTOM_TYPE_CLASSIFIER_THRESHOLD:
        if not row.classifier_category or row.threshold is None:
            logger.warning("custom_rule_skipped", rule_id=row.rule_id, reason="missing_threshold")
            return None
        detect = make_classifier_threshold_detector(
            row.classifier_category, float(row.threshold), row.category
        )
    else:
        logger.warning(
            "custom_rule_skipped", rule_id=row.rule_id, reason="unknown_type", type=row.custom_type
        )
        return None

    return RuleSpec(
        rule_id=row.rule_id,
        name=row.name,
        direction=row.direction,
        default_action=row.default_action,
        severity=row.severity,
        category=row.category,
        detect=detect,
        default_fail_mode=row.default_fail_mode,
        timeout_ms=row.timeout_ms,
        status=row.status,
        tags=["custom", f"tenant:{row.tenant_id}"],
    )


__all__ = [
    "CUSTOM_TYPE_CLASSIFIER_THRESHOLD",
    "CUSTOM_TYPE_REGEX",
    "CustomRuleRow",
    "UNSAFE_REGEX",
    "UnsafeRegexError",
    "assert_regex_safe",
    "build_custom_spec",
]
