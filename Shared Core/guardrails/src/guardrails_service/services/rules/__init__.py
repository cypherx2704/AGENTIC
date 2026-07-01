"""First-cycle rule registry (Component 2).

Pure regex / heuristic rule functions plus their metadata. A rule produces zero or
more :class:`RuleHit` objects, each describing a matched span. The pipeline
(``services/pipeline``) is responsible for redaction-token rendering, the
``matched_text`` content rule (FIX C), precedence, short-circuiting, and timeouts —
the rule functions themselves only locate matches and report the raw matched text +
category + action.

Rule IDs are compiled-in (the phase doc treats them as code). The DB ``guardrails.rules``
registry is the source of truth at the DB level and is seeded with these same 11 rows;
the WP02 overlay (``registry.RuleRegistryOverlay``) makes the DB authoritative for the
mutable METADATA (action/fail-mode/severity/timeout/status) at runtime.
"""

from __future__ import annotations

from .custom import (
    CUSTOM_TYPE_CLASSIFIER_THRESHOLD,
    CUSTOM_TYPE_REGEX,
    UNSAFE_REGEX,
    CustomRuleRow,
    UnsafeRegexError,
    assert_regex_safe,
    build_custom_spec,
)
from .definitions import (
    ALL_RULES,
    INPUT_RULES,
    OUTPUT_RULES,
    RULE_STATUS_ACTIVE,
    RULE_STATUS_RETIRED,
    RULES_BY_ID,
    RuleContext,
    RuleHit,
    RuleSpec,
)
from .registry import CustomRuleLoader, RuleRegistryOverlay

__all__ = [
    "ALL_RULES",
    "CUSTOM_TYPE_CLASSIFIER_THRESHOLD",
    "CUSTOM_TYPE_REGEX",
    "INPUT_RULES",
    "OUTPUT_RULES",
    "RULES_BY_ID",
    "RULE_STATUS_ACTIVE",
    "RULE_STATUS_RETIRED",
    "UNSAFE_REGEX",
    "CustomRuleLoader",
    "CustomRuleRow",
    "RuleContext",
    "RuleHit",
    "RuleRegistryOverlay",
    "RuleSpec",
    "UnsafeRegexError",
    "assert_regex_safe",
    "build_custom_spec",
]
