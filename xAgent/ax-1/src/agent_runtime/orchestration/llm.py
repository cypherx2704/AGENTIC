"""Orchestrator LLM glue (phase B5) — the decomposition planner + the synthesis pass.

Both run under the ORCHESTRATOR's own identity/model (not a sub-agent's). Everything is expressed
against a small injectable ``Complete`` seam (messages -> text + usage), so the planner and the
synthesis are unit-tested with a fake and only :func:`make_orchestrator_complete` touches the real
llms-gateway. The planner feeds :func:`decompose.decompose` (its ``planner`` arg); a malformed plan
raises and the decomposer degrades to ``solo`` (never fails the run).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from ..core.auth import Principal
from ..services.llms_client import LlmsClient
from .decompose import Planner

logger = structlog.get_logger(__name__)


@dataclass
class LlmResult:
    """A single orchestrator LLM round-trip: the text + its usage (accrued to the run budget)."""

    content: str
    tokens_used: int = 0
    cost_usd: float = 0.0


#: The injectable orchestrator-LLM seam: chat messages -> completion text + usage.
Complete = Callable[[list[dict[str, Any]]], Awaitable[LlmResult]]

_PLAN_SYSTEM = (
    "You are an orchestrator that decomposes a user's goal into a small DAG of sub-agent steps. "
    "Reply with ONLY a JSON object of the form "
    '{"steps": [{"id": "<slug>", "step": "<what this step does>", '
    '"preset": "researcher|writer|reviewer", "depends_on": ["<upstream id>", ...]}]}. '
    "Keep it minimal (2-5 steps), acyclic, and prefer a researcher -> writer (-> reviewer) shape. "
    "Do not include any prose outside the JSON."
)

_SYNTH_SYSTEM = (
    "You are an orchestrator. Synthesize the sub-agents' findings into a single, coherent, complete "
    "answer to the user's goal. Use ONLY the findings provided; do not invent facts or cite sources "
    "that are not present."
)


def make_orchestrator_complete(
    llms_client: LlmsClient,
    *,
    orchestrator: Principal,
    model: str,
    max_tokens: int = 1024,
    temperature: float = 0.3,
) -> Complete:
    """Build a :data:`Complete` bound to the ORCHESTRATOR's identity + model.

    Calls run under the orchestrator's own agent JWT (``agent_jwt`` + ``on_behalf_of`` both the
    orchestrator), so the gateway confines them to the orchestrator's allowlist and bills the
    orchestrator — exactly the identity discipline the sub-agent executor uses, applied to the lead.
    """

    async def complete(messages: list[dict[str, Any]]) -> LlmResult:
        r = await llms_client.chat(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            agent_jwt=orchestrator.raw_token,
            on_behalf_of=orchestrator.agent_id,
        )
        return LlmResult(
            content=r.content or "",
            tokens_used=r.usage.total_tokens,
            cost_usd=r.usage.cost_usd,
        )

    return complete


def parse_plan(text: str) -> dict[str, Any]:
    """Parse an LLM plan reply into a ``{"steps": [...]}`` dict (tolerant of fences / surrounding prose).

    Raises ``ValueError``/``json.JSONDecodeError`` on an unparseable reply — the caller
    (:func:`decompose.decompose`) then degrades to ``solo`` rather than failing the run.
    """
    raw = text.strip()
    if raw.startswith("```"):  # strip a ```json ... ``` fence
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:  # trim any prose around the JSON object
        raw = raw[start : end + 1]
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError("Plan is not a JSON object.")
    return plan


def make_llm_planner(complete: Complete) -> Planner:
    """Build a :data:`decompose.Planner` (goal -> plan dict) backed by the orchestrator LLM."""

    async def planner(goal: str) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": f"Goal:\n{goal}"},
        ]
        result = await complete(messages)
        return parse_plan(result.content)

    return planner


async def synthesize(goal: str, node_summaries: dict[str, str], *, complete: Complete | None) -> LlmResult:
    """Synthesize sub-agent summaries into a final answer (LLM when available, else joined findings).

    Best-effort: with no ``complete`` (no LLM), or on any LLM error, the findings are joined
    deterministically so a run always yields output. Returns the text + the synthesis LLM usage
    (0 on the fallback path) so the caller can accrue it to the workflow total.
    """
    filled = {k: v for k, v in node_summaries.items() if v}
    if not filled:
        return LlmResult(content="")
    joined = "\n\n".join(filled.values())
    if complete is None:
        return LlmResult(content=joined)

    findings = "\n\n".join(f"[{k}]\n{v}" for k, v in filled.items())
    messages = [
        {"role": "system", "content": _SYNTH_SYSTEM},
        {"role": "user", "content": f"Goal:\n{goal}\n\nSub-agent findings:\n{findings}"},
    ]
    try:
        result = await complete(messages)
    except Exception as exc:  # noqa: BLE001 — synthesis is best-effort; fall back to joined findings
        logger.warning("orchestrator_synthesis_failed", error=str(exc))
        return LlmResult(content=joined)
    if not result.content.strip():
        return LlmResult(content=joined, tokens_used=result.tokens_used, cost_usd=result.cost_usd)
    return result
