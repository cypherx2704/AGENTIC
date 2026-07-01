"""Prompt-injection defense: instruction-hierarchy tagging + spotlighting (ADDITIVE).

Two complementary, additive signals raise injection risk for the prompt-injection /
jailbreak rules WITHOUT changing benign-input verdicts:

1. **Instruction-hierarchy / role tagging** — caller-marked UNTRUSTED spans (RAG passages,
   tool outputs) are the lowest-trust content. The detector knows which spans are untrusted
   so a later injection-pattern match inside one can be treated as higher-risk (the pipeline
   escalates such a hit to 'block' via the spotlight threshold).

2. **Spotlight risk score** — a bounded [0,1] score combining whether injection/jailbreak
   markers appear at all and whether they sit inside untrusted spans. Pure DECISION
   METADATA: it is surfaced on the response for observability and feeds the stricter
   threshold for untrusted content; it does NOT by itself flip a verdict.

Default-safe: with NO marked untrusted spans the risk reflects only ordinary pattern
presence and the spotlight escalation is inert (the prompt-injection rule already blocks on
its own), so existing verdicts on benign and on plain injection input are unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Lightweight markers reused for the risk score (kept separate from the rule regexes so the
# rule logic stays authoritative for the actual verdict; this is only a risk heuristic).
_RISK_MARKERS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous\s+)?instructions?", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"\bjailbreak\b", re.IGNORECASE),
    re.compile(r"developer\s+mode", re.IGNORECASE),
    re.compile(r"do\s+anything\s+now", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"new\s+(?:instructions?|prompt)\s*:", re.IGNORECASE),
]


@dataclass(frozen=True)
class InjectionAssessment:
    """Output of the injection-defense pass (additive decision metadata)."""

    risk: float                    # bounded [0,1]
    untrusted_spans: list[str]     # normalised marked-untrusted spans
    markers_in_untrusted: int      # how many risk markers sit inside an untrusted span
    markers_total: int             # total risk markers seen anywhere


def assess(text: str, untrusted_spans: list[str] | None) -> InjectionAssessment:
    """Compute the injection-risk assessment for ``text`` given marked untrusted spans.

    The risk score is conservative: a marker present anywhere contributes a small amount;
    a marker INSIDE an untrusted span contributes more (the spotlight). Bounded to [0,1].
    """
    spans = [s for s in (untrusted_spans or []) if isinstance(s, str) and s]
    total = 0
    in_untrusted = 0
    for pat in _RISK_MARKERS:
        for m in pat.finditer(text):
            total += 1
            matched = m.group(0)
            if any(matched in span for span in spans):
                in_untrusted += 1
    # Score: untrusted markers dominate; plain presence adds a smaller base. Capped at 1.0.
    risk = min(1.0, 0.6 * (1 if in_untrusted else 0) + 0.2 * in_untrusted + 0.1 * total)
    return InjectionAssessment(
        risk=round(risk, 3),
        untrusted_spans=spans,
        markers_in_untrusted=in_untrusted,
        markers_total=total,
    )
