"""POST /v1/check/input and POST /v1/check/output — the critical-path spine.

Flow: auth dependency -> reject body identity fields (400) -> validate -> reject async mode
(422) -> per-tenant rate limit + byte quota (429, FAIL-CLOSED when enabled) -> resolve
effective policy (Valkey-cached, FAIL-OPEN) -> evaluate the direction's rules (pipeline) ->
RETURN the decision -> ENQUEUE the violation/usage persistence (post-response, off the hot
path). The persistence queue drains to the DB in a background worker.

The endpoints ALWAYS return 200, including ``decision='block'``. The CALLER (xAgent)
translates a block decision into a 422 GUARDRAIL_VIOLATION (phase doc Component 1/5).
Identity (``tenant_id`` / ``agent_id``) comes from the JWT only; ``trace_id`` /
``request_id`` from the trace context only (Contract 13) — never the body.

Hot-path hardening (WP07):
  * **Policy cache** — the resolved EffectivePolicy is cached per (tenant, agent) in Valkey
    for ``policy_cache_ttl_seconds``; a miss / Valkey-down path falls open to a live resolve.
  * **Rate limit + byte quota** — per-tenant atomic limiter (Auth/Contract-19). DISABLED
    when not configured (no Valkey / flag off) so unit tests + keyless dev stay green;
    FAIL-CLOSED (429) when configured and the backend errors (safety default).
  * **Post-response persistence** — violation/usage writes are enqueued and drained in the
    background, so the response returns as soon as the decision is computed. With no DB pool
    (local/unit) the enqueue is a no-op, exactly like the prior inline skip.
  * **Empty tenant policy** — a tenant policy that enables no rules is HONORED as allow (we
    only substitute the built-in default when the resolver returns nothing at all).
  * **Async mode** — ``mode='async'`` is rejected 422 (sync-only first cycle).
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core import metrics, trace
from ..core.auth import Principal, require_principal
from ..core.errors import ApiError, ErrorCode
from ..db.outbox import CheckWrite, ViolationRow
from ..db.persist_queue import PersistenceQueue
from ..models.check import RESERVED_BODY_FIELDS, CheckRequest, CheckResponse, Violation
from ..services import groundedness as groundedness_svc
from ..services import injection_defense
from ..services.classifier_client import RemoteSafetyClassifier
from ..services.pipeline import SAFETY_CATEGORIES, PipelineResult, evaluate
from ..services.policy_cache import PolicyCache, RateLimiter, resolve_contract19_limits
from ..services.policy_engine import EffectivePolicy, PolicyEngine, new_check_id
from ..services.rules import RuleContext

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["check"])


def _reject_reserved_fields(body: dict[str, Any]) -> None:
    """400 VALIDATION_ERROR if the body carries identity/correlation fields (Contract 13)."""
    present = sorted(RESERVED_BODY_FIELDS.intersection(body.keys()))
    if present:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Identity/correlation fields are not accepted in the request body.",
            status_code=400,
            details={"reason": "reserved_body_field", "fields": present},
        )


async def _parse_body(request: Request) -> CheckRequest:
    """Read the raw JSON, reject reserved identity fields, then validate the model."""
    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001 — malformed JSON -> 400
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "Request body must be valid JSON.", status_code=400
        ) from exc
    if not isinstance(raw, dict):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "Request body must be a JSON object.", status_code=400
        )
    _reject_reserved_fields(raw)
    try:
        return CheckRequest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — pydantic validation -> 400 VALIDATION_ERROR
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed.",
            status_code=400,
            details={"reason": "invalid_body", "error": str(exc)},
        ) from exc


def _policy_engine(request: Request) -> PolicyEngine:
    engine = getattr(request.app.state, "policy_engine", None)
    if engine is None:
        return PolicyEngine(None)
    return engine


async def _resolve_policy(
    request: Request, tenant_id: str, agent_id: str | None
) -> EffectivePolicy:
    """Resolve the effective policy, served from the Valkey cache (FAIL-OPEN to live resolve).

    Honors an EMPTY tenant policy (no rules -> allow): the built-in platform default only
    stands in when the resolver yields nothing at all (no row / DB down), not when a tenant
    deliberately enabled zero rules.
    """
    cache: PolicyCache | None = getattr(request.app.state, "policy_cache", None)
    if cache is not None:
        cached = await cache.get(tenant_id, agent_id)
        if cached is not None:
            return cached

    policy = await _policy_engine(request).resolve(tenant_id, agent_id)

    if cache is not None:
        await cache.put(tenant_id, agent_id, policy)
    return policy


async def _redaction_key(request: Request, tenant_id: str) -> str:
    resolver = getattr(request.app.state, "redaction_resolver", None)
    if resolver is not None:
        # DB-backed read-through (cached); falls back to the platform key on any miss/error.
        if hasattr(resolver, "refresh_tenant"):
            return str(await resolver.refresh_tenant(tenant_id))
        return str(resolver.resolve(tenant_id))
    settings = request.app.state.settings
    return str(settings.redaction_hmac_key_platform)


async def _presidio_spans(request: Request, text: str) -> list[tuple[str, str]] | None:
    """Run the Presidio analyzer when wired (flag on + dep available); else ``None``.

    Returns ``None`` (not ``[]``) when Presidio is disabled/unavailable so the RuleContext
    falls through to the pure regex path; ``[]`` means "ran, found nothing".
    """
    analyzer = getattr(request.app.state, "presidio_analyzer", None)
    if analyzer is None:
        return None
    try:
        return analyzer.analyze(text)
    except Exception as exc:  # noqa: BLE001 — analysis is best-effort; never fail a check
        logger.warning("presidio_spans_failed", error=str(exc))
        return None


async def _remote_toxicity(
    request: Request, classifier: object, text: str, direction: str
) -> tuple[list[Any] | None, dict[str, Any] | None]:
    """Run the confidence-banded remote classifier cascade when wired (CLASSIFIER_MODE!=stub).

    Returns ``(precomputed_categories, metadata)``. ``precomputed_categories`` is ``None``
    when the remote seam is not active (so the pipeline uses the in-process classifier, the
    keyless default path) and a (possibly empty) Category list when it ran. ``metadata`` is
    ``None`` when nothing notable happened (no escalation), else carries provenance.
    """
    if not isinstance(classifier, RemoteSafetyClassifier):
        return None, None
    outcome = await classifier.classify_remote(text, direction)
    # Only surface metadata (and the precomputed override) when a remote round-trip actually
    # happened — a confidently benign/toxic stub band short-circuits with used_remote=False
    # and we leave the pipeline on its in-process classify (byte-identical to today).
    if not outcome.used_remote:
        return None, None
    meta = {
        "mode": request.app.state.settings.classifier_mode,
        "used_remote": outcome.used_remote,
        "fell_back": outcome.fell_back,
    }
    if outcome.verdict:
        meta["remote_verdict"] = outcome.verdict
    return list(outcome.categories), meta


async def _groundedness(
    request: Request, body: CheckRequest
) -> tuple[dict[str, Any] | None, float, bool]:
    """Compute the output groundedness signal. Returns (metadata, confidence, high_risk).

    Context = the original input_text plus any caller grounding passages. The heuristic
    backend is keyless + synchronous; the llms_gateway backend falls back to it on trouble.
    """
    settings = request.app.state.settings
    context_parts: list[str] = []
    if body.input_text:
        context_parts.append(body.input_text)
    if body.grounding:
        context_parts.extend(s for s in body.grounding if isinstance(s, str))
    context_text = "\n".join(context_parts)

    signal = groundedness_svc.assess(
        response_text=body.text, context_text=context_text, settings=settings
    )
    meta: dict[str, Any] = {
        "score": signal.score,
        "high_risk": signal.high_risk,
        "backend": signal.backend,
        "min_score": settings.groundedness_min_score,
    }
    # Lower confidence proportional to how ungrounded the output looks.
    conf = signal.score if signal.high_risk else 1.0
    return meta, conf, signal.high_risk


async def _enforce_rate_limit(request: Request, principal: Principal, input_bytes: int) -> None:
    """Per-tenant rate limit + byte quota (429 on limit). No-op when not configured."""
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    checks_limit, bytes_limit = resolve_contract19_limits(principal.raw_claims)
    result = await limiter.check(
        principal.tenant_id,
        input_bytes,
        checks_limit=checks_limit,
        bytes_limit=bytes_limit,
    )
    if not result.allowed:
        retry_after = str(result.retry_after_seconds)
        logger.info(
            "rate_limit_exceeded",
            tenant_id=principal.tenant_id,
            dimension=result.dimension,
            retry_after=retry_after,
        )
        raise ApiError(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            "Per-tenant rate limit or byte quota exceeded.",
            status_code=429,
            details={"dimension": result.dimension} if result.dimension else None,
            headers={"Retry-After": retry_after},
        )


async def _run_check(
    request: Request,
    principal: Principal,
    direction: str,
) -> JSONResponse:
    started = time.monotonic()
    body = await _parse_body(request)

    # Async mode is not supported first cycle — reject explicitly (422).
    if body.mode == "async":
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Asynchronous check mode is not supported.",
            status_code=422,
            details={"reason": "unsupported_mode", "mode": body.mode},
        )

    input_bytes = len(body.text.encode("utf-8"))
    # Rate limit BEFORE doing the work (reject early; FAIL-CLOSED when configured).
    await _enforce_rate_limit(request, principal, input_bytes)

    classifier = getattr(request.app.state, "classifier", None)
    # The resolver ALREADY substitutes the built-in platform default when NOTHING resolves
    # (no pool / DB down / no row — see PolicyEngine.resolve). So a policy with zero rules
    # here means a tenant DELIBERATELY enabled no rules: HONOR it as "allow" (do NOT
    # re-substitute the default, which would silently re-enable all 11 rules).
    policy = await _resolve_policy(request, principal.tenant_id, principal.agent_id)

    # Overlay the tenant's executable custom rules (WP07): the loader registers their specs
    # into the shared registry and appends their rule_ids to the policy so the pipeline runs
    # them. No-op passthrough when no loader is wired / no pool / the tenant has no custom rules.
    loader = getattr(request.app.state, "custom_rule_loader", None)
    if loader is not None:
        policy = await loader.with_custom_rules(policy, principal.tenant_id)

    settings = request.app.state.settings
    metadata: dict[str, Any] = {}
    confidence = 1.0

    # ── Prompt-injection defense (instruction-hierarchy + spotlight) ─────────────
    untrusted_spans: list[str] | None = None
    injection_block_threshold = 1.0  # 1.0 disables spotlight escalation by default
    if settings.injection_defense_enabled and body.untrusted_spans:
        assessment = injection_defense.assess(body.text, body.untrusted_spans)
        untrusted_spans = assessment.untrusted_spans
        injection_block_threshold = settings.injection_spotlight_block_threshold
        if assessment.markers_total or assessment.untrusted_spans:
            metadata["injection"] = {
                "risk": assessment.risk,
                "markers_total": assessment.markers_total,
                "markers_in_untrusted": assessment.markers_in_untrusted,
                "untrusted_span_count": len(assessment.untrusted_spans),
            }
            metrics.injection_spotlight_total.labels(
                "escalated" if assessment.markers_in_untrusted else "observed"
            ).inc()

    # ── PII recall lift via Presidio (optional dep + flag; default off) ──────────
    presidio_spans = await _presidio_spans(request, body.text)
    if presidio_spans:
        metadata["presidio_spans"] = len(presidio_spans)

    # ── Confidence-banded remote classifier cascade (small/large-LLM stage) ──────
    precomputed_toxicity, tox_meta = await _remote_toxicity(
        request, classifier, body.text, direction
    )
    if tox_meta is not None:
        metadata["classifier"] = tox_meta
        if tox_meta.get("fell_back"):
            confidence = min(confidence, 0.7)
            metrics.classifier_cascade_total.labels("remote_fallback").inc()
        else:
            metrics.classifier_cascade_total.labels("remote").inc()
    else:
        metrics.classifier_cascade_total.labels("stub_only").inc()

    ctx = RuleContext(
        classifier=classifier,
        input_text=body.input_text if direction == "output" else None,
        precomputed_toxicity=precomputed_toxicity,
        presidio_spans=presidio_spans,
        untrusted_spans=untrusted_spans,
    )
    redaction_key = await _redaction_key(request, principal.tenant_id)

    # FIX 5: honor policy.fail_mode_override on the LIVE path (gated; default on). Record
    # the applied posture in metadata for observability/parity with the simulation trace.
    honor_fail_mode = settings.live_fail_mode_override_enabled
    if honor_fail_mode and policy.fail_mode_override:
        metadata["fail_mode_applied"] = policy.fail_mode_override

    # SAFETY hardening: a timed-out SAFETY rule (PII/security/jailbreak/toxicity) must never
    # silently allow on the LIVE path, even under a policy fail_mode_override='open'. Force it
    # fail-CLOSED and raise its per-rule budget to a safer floor (both config-driven; default on).
    safety_fail_closed = settings.safety_rule_fail_closed_on_timeout
    safety_categories = _safety_categories(settings)

    result: PipelineResult = evaluate(
        text=body.text,
        policy=policy,
        direction=direction,
        tenant_id=principal.tenant_id,
        redaction_key=redaction_key,
        ctx=ctx,
        honor_fail_mode_override=honor_fail_mode,
        injection_block_threshold=injection_block_threshold,
        safety_fail_closed=safety_fail_closed,
        safety_categories=safety_categories,
        safety_min_timeout_ms=settings.safety_rule_min_timeout_ms,
    )

    # ── Output groundedness / hallucination signal (flagged; default off) ────────
    if direction == "output" and settings.groundedness_enabled:
        gnd_meta, gnd_conf, gnd_review = await _groundedness(request, body)
        if gnd_meta is not None:
            metadata["groundedness"] = gnd_meta
            confidence = min(confidence, gnd_conf)
            metrics.groundedness_checks_total.labels(
                "high_risk" if gnd_review else "grounded"
            ).inc()
            # HIGH-risk escalates to a 'warn' REVIEW signal — never blocks on its own and
            # never downgrades a stricter decision (block/redact stay).
            if gnd_review and result.decision == "allow":
                result = PipelineResult(
                    decision="warn",
                    processed_text=result.processed_text,
                    violations=result.violations,
                    trace=result.trace,
                )

    duration_ms = int((time.monotonic() - started) * 1000)
    check_id = new_check_id()

    rules_evaluated = sum(
        1
        for r in policy.rules
        if _rule_applies(r.rule_id, direction)
    )

    # Post-response persistence: enqueue (non-blocking) instead of writing inline. The
    # decision is already computed and is the security-relevant output; the audit/usage
    # write happens off the hot path.
    _enqueue_persist(
        request,
        principal=principal,
        body=body,
        direction=direction,
        result=result,
        policy=policy,
        check_id=check_id,
        duration_ms=duration_ms,
        rules_evaluated=rules_evaluated,
        input_bytes=input_bytes,
    )

    metrics.checks_total.labels(direction, result.decision).inc()
    metrics.check_duration_seconds.labels(direction).observe(duration_ms / 1000)

    response = CheckResponse(
        decision=result.decision,
        processed_text=result.processed_text,
        violations=[
            Violation(
                rule_id=v.rule_id,
                rule_name=v.rule_name,
                severity=v.severity,  # type: ignore[arg-type]
                category=v.category,
                matched=v.matched,
                action=v.action,  # type: ignore[arg-type]
            )
            for v in result.violations
        ],
        check_id=check_id,
        duration_ms=duration_ms,
        trace_id=trace.trace_id_var.get(),
        confidence=round(confidence, 3),
        metadata=metadata or None,
    )
    return JSONResponse(content=response.model_dump())


def _safety_categories(settings: Any) -> frozenset[str]:
    """Parse the config-driven safety-rule category set (comma-separated).

    Falls back to the pipeline default :data:`SAFETY_CATEGORIES` when unset/blank so the
    safety fail-closed posture still applies to the standard PII/security/jailbreak/toxicity
    rules even if an operator clears the setting by mistake.
    """
    raw = getattr(settings, "safety_rule_categories", "") or ""
    parsed = frozenset(c.strip().lower() for c in raw.split(",") if c.strip())
    return parsed or SAFETY_CATEGORIES


def _rule_applies(rule_id: str, direction: str) -> bool:
    from ..services.rules import RULE_STATUS_RETIRED, RULES_BY_ID

    spec = RULES_BY_ID.get(rule_id)
    return (
        spec is not None
        and spec.direction in (direction, "both")
        and spec.status != RULE_STATUS_RETIRED
    )


def _compute_cost_usd(request: Request, rules_evaluated: int) -> float:
    """Real per-check cost = evaluated rules * the configured per-rule cost (Contract 19.1).

    The DB ``guardrails.rules.cost_usd`` column is the future authoritative per-rule price;
    until per-rule prices are loaded onto the registry, the flat configured rate gives a
    real (non-zero) metering signal rather than a hardcoded 0.
    """
    settings = request.app.state.settings
    return round(rules_evaluated * settings.usage_cost_per_rule_usd, 8)


def _enqueue_persist(
    request: Request,
    *,
    principal: Principal,
    body: CheckRequest,
    direction: str,
    result: PipelineResult,
    policy: EffectivePolicy,
    check_id: str,
    duration_ms: int,
    rules_evaluated: int,
    input_bytes: int,
) -> None:
    """Build the CheckWrite and hand it to the post-response persistence queue.

    Non-blocking. With no queue / no DB pool (local/unit) this is a no-op — the persistence
    path simply skips, exactly like the prior inline fail-soft skip.
    """
    queue: PersistenceQueue | None = getattr(request.app.state, "persist_queue", None)
    if queue is None or not queue.enabled:
        logger.info(
            "check_persist_skipped_no_queue", direction=direction, decision=result.decision
        )
        return

    # output_bytes: bytes of the text the caller will use downstream — the redacted
    # processed_text when redaction applied, else the original checked text.
    out_text = result.processed_text if result.processed_text is not None else body.text
    output_bytes = len(out_text.encode("utf-8"))

    write = CheckWrite(
        check_id=check_id,
        request_id=trace.request_id_var.get(),
        tenant_id=principal.tenant_id,
        trace_id=trace.trace_id_var.get(),
        direction=direction,
        decision=result.decision,
        policy_id=policy.policy_id,
        policy_name=policy.name,
        violations=[
            ViolationRow(
                rule_id=v.rule_id,
                rule_name=v.rule_name,
                severity=v.severity,
                category=v.category,
                matched_text=v.matched,
                action=v.action,
            )
            for v in result.violations
        ],
        agent_id=principal.agent_id,
        api_key_id=principal.api_key_id,
        task_id=body.task_id,
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        rules_evaluated=rules_evaluated,
        cost_usd=_compute_cost_usd(request, rules_evaluated),
        duration_ms=duration_ms,
    )
    queue.enqueue(write)


@router.post("/check/input", response_model=None)
async def check_input(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    return await _run_check(request, principal, "input")


@router.post("/check/output", response_model=None)
async def check_output(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    return await _run_check(request, principal, "output")
