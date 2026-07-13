"""Orchestrator LLM glue (phase B5) — the decomposition planner + the synthesis pass.

Both run under the ORCHESTRATOR's own identity/model (not a sub-agent's). Everything is expressed
against a small injectable ``Complete`` seam (messages -> text + usage), so the planner and the
synthesis are unit-tested with a fake and only :func:`make_orchestrator_complete` touches the real
llms-gateway. The planner feeds :func:`decompose.decompose` (its ``planner`` arg); a malformed plan
is handed BACK to the planner once with the rejection reason (the ``feedback`` arg), and if that
also fails the run fails ``ORCHESTRATION_FAILED`` — the backend never picks a substitute agent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from ..core.auth import Principal
from ..services.llms_client import LlmsClient
from .dag import DEFAULT_MAX_DEPTH, DEFAULT_MAX_FANOUT
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


#: Reserved plan target meaning "no delegation — the orchestrator handles this itself".
ORCHESTRATOR_TARGET = "orchestrator"

#: The delegation policy. Sub-agents are the EXCEPTION, not the default: each one costs a token
#: mint, a task row, its own LLM calls, and a summarisation hop. Delegating work a single agent
#: could do is pure waste — and, worse, an over-eager plan invents steps the user never asked for
#: (the "...and do NOT write a brief" goal that still produced a brief-writing step).
_DELEGATION_POLICY = (
    "DELEGATION POLICY — think before you fan out:\n"
    "- The DEFAULT is NO delegation. Prefer the fewest steps that fully satisfy the goal.\n"
    "- Delegate to a sub-agent ONLY when there is a concrete reason:\n"
    "    (a) PARALLELISM — genuinely independent pieces of work that can run at the same time;\n"
    "    (b) SPECIALIZATION — the step needs a tool or expertise a specific sub-agent has;\n"
    "    (c) ISOLATION — the step produces a lot of intermediate material that is better\n"
    "        distilled into a summary than carried around.\n"
    "- If ONE agent can do the whole job, emit exactly ONE step. That is a good plan, not a lazy one.\n"
    "- If the goal needs no specialist at all, emit ONE step targeting "
    f'"{ORCHESTRATOR_TARGET}" and it will be answered directly.\n'
    "- NEVER add a step the goal did not ask for. Obey the goal's constraints and NEGATIONS "
    "exactly: if it says not to do something, no step may do it. Read the goal literally.\n"
)


#: How much of a sub-agent's system prompt to show the planner (enough to convey purpose, not
#: enough to bloat the planning call).
_PURPOSE_CHARS = 240


@dataclass(frozen=True)
class AgentCapability:
    """What the planner needs to know to ROUTE a step to a sub-agent.

    Both fields carry weight, and they answer different questions:

    * :attr:`purpose` — the agent's routing DESCRIPTION ("use me to fetch GitHub repo stats").
      The INTENT signal: it is the only thing that can separate two agents holding the same tools,
      and the only thing at all for toolless agents (a `writer` and a `reviewer` both show
      ``tools: NONE`` — nothing but the description tells them apart).
    * :attr:`tools` — what the agent can physically DO. The GROUND-TRUTH constraint: a step that
      needs external data routed to an agent with no tool to fetch it does not fail, it FABRICATES.
    """

    name: str
    purpose: str = ""
    tools: tuple[str, ...] = ()


def _capability_lines(agents: Sequence[AgentCapability]) -> str:
    """Render the roster as a capability catalogue: description + real tools, one block per agent.

    The ``orchestrator`` pseudo-target is always appended, so "delegate to nobody" is always a
    reachable choice even when the roster is empty.
    """
    lines = []
    for a in sorted(agents, key=lambda x: x.name):
        purpose = " ".join(a.purpose.split())[:_PURPOSE_CHARS]
        tools = ", ".join(a.tools) if a.tools else "NONE (cannot call any tool)"
        # An undescribed agent is named, not explained — say so plainly rather than papering over
        # it, so the planner treats routing there as the guess it would be.
        use_when = purpose or "UNSPECIFIED — no description was configured for this agent"
        lines.append(f"- {a.name}\n    use when: {use_when}\n    tools: {tools}")
    lines.append(
        f"- {ORCHESTRATOR_TARGET}\n"
        f"    use when: no sub-agent is needed — you answer the step yourself\n"
        f"    tools: NONE (cannot call any tool)"
    )
    return "\n".join(lines)


def _plan_system(agents: Sequence[AgentCapability] | None) -> str:
    """Build the planner system prompt: a CAPABILITY CATALOGUE + the delegation policy + the caps.

    Four things this prompt must get right:

    1. **Bind to the real roster, always.** There is deliberately NO roster-free variant. The one
       that used to exist named a fixed researcher/writer/reviewer trio and demanded "2-5 steps" —
       i.e. it told the model to delegate, to agents that may not even exist. Routing is the
       model's decision; the prompt's job is to describe the choices, never to prescribe one.
    2. **Route by CAPABILITY, not by name.** Given only names, the planner guesses from the
       string: it will hand "get the GitHub stats" to `wiki-researcher` (which holds only a
       Wikipedia tool), and that agent then answers from thin air with no tool call at all. It
       must see each agent's description AND its actual tools.
    3. **Make NOT delegating a first-class outcome** — via the ``orchestrator`` target and a
       delegation policy whose default is "don't".
    4. **State the graph limits the validator actually enforces**, so a plan is not rejected for
       a rule the model was never told (and so the limits stay in one place: :mod:`.dag`).
    """
    return (
        "You are an orchestrator. Decide how to accomplish the user's goal, delegating to "
        "sub-agents ONLY when it genuinely helps.\n\n"
        "AVAILABLE TARGETS (route by CAPABILITY — read BOTH the description and the tools):\n"
        f"{_capability_lines(agents or ())}\n\n"
        f"{_DELEGATION_POLICY}\n"
        "ROUTING RULES:\n"
        '- Send a step to the agent whose "use when" describes that kind of work. The description '
        "is what the agent is FOR — match the step to it, not to the agent's name.\n"
        "- A step that needs external data can ONLY go to an agent whose TOOLS can fetch it. "
        "Never assign a step to an agent that lacks the required tool — it cannot do the job and "
        "would invent an answer.\n"
        "- If NO available agent can satisfy the goal, do not fake it: emit a single step "
        f'targeting "{ORCHESTRATOR_TARGET}" whose "step" text states plainly that no sub-agent '
        "has the required capability, and answer from general knowledge only.\n\n"
        "GRAPH LIMITS (a plan that breaks any of these is REJECTED):\n"
        f"- At most {DEFAULT_MAX_FANOUT} steps may run in parallel (i.e. share no dependency).\n"
        f"- The dependency chain may be at most {DEFAULT_MAX_DEPTH} steps deep.\n"
        "- The graph must be ACYCLIC.\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"steps": [{"id": "<slug>", "step": "<what this step does>", '
        '"preset": "<one of the available targets>", "depends_on": ["<upstream id>", ...]}]}\n'
        'EVERY step MUST carry a "preset" that is EXACTLY one of the available target names — '
        "never invent one, never leave it out. A step that needs another step's output must list "
        "it in depends_on; steps with no dependency between them run in parallel. "
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

    Raises ``ValueError``/``json.JSONDecodeError`` on an unparseable reply. The caller
    (:func:`decompose.decompose`) then hands the failure BACK to the planner once with the reason,
    and fails the run ``ORCHESTRATION_FAILED`` if the retry is declined or also fails. It does NOT
    silently degrade to ``solo`` — quietly answering a delegating goal with one orchestrator node
    would be the backend substituting its own plan for the one the model was asked to produce.
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


def make_llm_planner(
    complete: Complete, roster: Sequence[AgentCapability] | None = None
) -> Planner:
    """Build a :data:`decompose.Planner` (goal, feedback -> plan dict) backed by the orchestrator LLM.

    ``roster`` = the CAPABILITIES of the sub-agents this orchestrator owns (name + description +
    tools), not just their names. The planner routes on that catalogue, so a step needing external
    data goes to an agent that actually holds a tool able to fetch it.

    ``feedback`` (second arg, ``None`` on the first attempt) is the REPAIR channel: when a plan is
    rejected, the rejection reason is fed back as another user turn and the model re-plans. That
    keeps routing with the model even on its own mistakes — the alternative (the backend picking a
    substitute agent) is precisely the thing this design forbids.
    """
    system = _plan_system(roster)

    async def planner(goal: str, feedback: str | None = None) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Goal:\n{goal}"},
        ]
        if feedback:
            messages.append({"role": "user", "content": feedback})
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
