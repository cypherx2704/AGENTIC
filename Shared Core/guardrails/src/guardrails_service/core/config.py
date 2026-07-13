"""Application settings (pydantic-settings).

All configuration is read from the process environment (no prefix), matching the
Doppler-injected env-var convention from the Phase 4 K8s spec. Defaults target a
local developer machine so the service boots without a populated environment and
without the heavy ML classifier (``classifier_mode='stub'``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the guardrails service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "guardrails-service"
    service_version: str = "0.1.0"
    environment: str = "local"

    # ── PostgreSQL (PgBouncer -> guardrails schema, runtime user grd_user) ─────
    database_url: str = (
        "postgresql://grd_user:localdev@localhost:5432/cypherx_platform"
    )
    # DB pool sizing (env DB_POOL_MIN_SIZE / DB_POOL_MAX_SIZE). Defaults = the prior hardcoded
    # 1/10; raise max_size to lift per-instance throughput (the measured concurrency ceiling).
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # ── Kafka ──────────────────────────────────────────────────────────────────
    kafka_brokers: str = "localhost:9092"

    # ── Valkey (soft dependency — WP02 foundation; WP07 cache + rate-limit use) ──
    valkey_url: str = "redis://localhost:6379/0"
    valkey_timeout_seconds: float = 2.0

    # ── Hot-path policy cache (WP07 — Component 3 amendment) ────────────────────
    # Cache the resolved EffectivePolicy per (tenant, agent) in Valkey for this TTL.
    # FAIL-OPEN: a cache miss / Valkey-down path always falls back to a live resolve, so
    # this is purely a latency optimisation and never changes the decision. 0 disables it.
    policy_cache_ttl_seconds: int = 60
    policy_cache_key_prefix: str = "cypherx:grd:pol:"
    # Independent SHORT hot-path budget for the cache GET/SET (a slow/absent Valkey must not
    # stall a check — on timeout we fall open to a live resolve). Kept well under the 2s
    # readiness-ping timeout so a Valkey outage costs the hot path ~150ms, not 2s.
    policy_cache_valkey_timeout_seconds: float = 0.15

    # ── Per-tenant rate limiting + byte quota (WP07 — Auth/Contract-19, FAIL-CLOSED) ──
    # Master enable flag. DEFAULT OFF: with no flag set (unit/local, no Valkey wired) the
    # limiter is disabled so the existing check tests stay green and keyless dev works.
    # Enable it in staging/prod (RATE_LIMIT_ENABLED=true) where Valkey is present.
    # Semantics when ENABLED:
    #   * "no limiter configured" (no Valkey client on app.state) => DISABLED (skip), and
    #   * "limiter configured but the backend errors" => FAIL-CLOSED (429), because this is
    #     a safety service and the safe default when we cannot account for usage is to reject.
    # ``rate_limit_fail_open`` flips that erroring-backend posture back to fail-open if an
    # operator decides availability beats strict accounting (documented override).
    rate_limit_enabled: bool = False
    rate_limit_fail_open: bool = False
    rate_limit_key_prefix: str = "cypherx:grd:rl:"
    # Independent, short hot-path budget for the atomic limiter call (a slow Valkey must
    # not stall a check); on timeout the configured fail posture applies.
    rate_limit_valkey_timeout_seconds: float = 0.15
    # Fallback per-tenant limits used ONLY when Auth/Contract-19 limit claims are absent
    # from the principal (the JWT ``limits``/``checks_per_min``/``input_bytes_per_min``
    # claims are authoritative when present). 0/negative on a dimension => that dimension
    # is uncapped.
    rate_limit_default_checks_per_min: int = 600
    rate_limit_default_input_bytes_per_min: int = 5_000_000

    # ── Post-response persistence queue (WP07 — Component 4 amendment) ──────────
    # Violation/usage writes are enqueued to an in-process async queue drained by a
    # background worker so the decision returns without waiting on the DB. ``maxsize`` is
    # the backlog cap; on overflow the item is dropped (log + counter) — the security
    # decision is already returned, so a dropped audit write degrades metering, not safety.
    persist_queue_maxsize: int = 10_000
    # How long shutdown waits for the queue to drain before giving up (best-effort).
    persist_queue_drain_timeout_seconds: float = 5.0

    # ── Outbox purge (WP07 — Ops). Retire PUBLISHED outbox rows older than this. ──
    outbox_purge_enabled: bool = True
    outbox_retention_hours: int = 72
    outbox_purge_interval_seconds: float = 3600.0

    # ── Token revocation mirror (WP03 — shared kill-switch, defense-in-depth) ───
    # Verifier-side mirror of the Auth-owned Valkey revocation scheme. Keys live under
    # this prefix: ``{prefix}jti:{jti}`` / ``{prefix}kid:{kid}`` / ``{prefix}agent:{agent_id}``.
    # All four services MUST agree on the prefix — keep this default in lockstep.
    revocation_key_prefix: str = "cypherx:rev:"
    # Master enable flag (default ON). When false the verifier skips the revocation
    # lookup entirely (e.g. a break-glass disable if the kill-switch misbehaves).
    revocation_check_enabled: bool = True
    # Short, INDEPENDENT timeout for the hot-path revocation lookup so a slow Valkey
    # never stalls a verify; on timeout/error the check FAILS OPEN (accepts the token).
    revocation_valkey_timeout_seconds: float = 0.15

    # ── Rules registry overlay (WP02 — DB authoritative for rule METADATA) ──────
    # Interval between re-reads of guardrails.rules onto the in-code RuleSpec objects.
    rules_refresh_interval_seconds: float = 60.0

    # ── Custom rules (WP07 — tenant-authored regex / classifier-threshold rules) ─
    # The dynamic loader caches a tenant's active custom rules for this many seconds
    # before re-reading guardrails.rules (the per-tenant equivalent of the metadata
    # overlay's refresh). Short TTL: a freshly saved rule takes effect within the window
    # without a per-request DB read on the hot check path.
    custom_rules_cache_ttl_seconds: float = 30.0
    # ReDoS guard (applied at SAVE time only). A candidate regex is rejected (422
    # UNSAFE_REGEX) if its source exceeds the size limit, fails to compile, or — when run
    # against a 64 KiB adversarial string — exceeds the wall-clock budget below.
    custom_rule_regex_max_length: int = 1000
    custom_rule_regex_budget_ms: float = 50.0
    # Per-tenant quota on ACTIVE custom rules (Auth/Contract-19 limit). The effective
    # limit is resolved from Auth/Contract-19 when available; this is the fallback
    # default used when the limit cannot be resolved. 0/negative => fail-open (no cap).
    custom_rules_max: int = 100

    # ── Auth / JWKS (Contract 1) ────────────────────────────────────────────────
    # In-cluster: http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"

    # ── Redaction (Component 5) ──────────────────────────────────────────────────
    # Per-env platform fallback HMAC key. Per-tenant override (when present) is
    # resolved from guardrails.tenant_redaction_keys at request time.
    redaction_hmac_key_platform: str = "local-dev-platform-redaction-key-change-me"
    # Redaction-key lifecycle (WP07). On rotation the previous key stays valid for this
    # grace window so tokens minted just before rotation still verify; a lifespan-scheduled
    # retirement job retires rows past grace.
    redaction_key_grace_days: int = 30
    redaction_key_retire_interval_seconds: float = 3600.0
    # Cache a tenant's resolved key material for this TTL so the hot path does not read
    # the DB on every check. 0 disables the cache (always read-through).
    redaction_key_cache_ttl_seconds: float = 60.0
    # Pluggable key_ref scheme (WP07). A row's ``key_ref`` is resolved to raw key material
    # by its prefix:
    #   * ``env:NAME``         -> the value of environment variable NAME (first-cycle / dev).
    #   * ``env:``  (no name)  -> the platform key (``redaction_hmac_key_platform``).
    #   * ``sealed:<blob>``    -> a sealed-secret blob unsealed via the platform secret store
    #                            (AWS Secrets Manager / SOPS in prod). Not wired first cycle:
    #                            unresolvable refs FALL BACK to the platform key (fail-soft).
    #   * ``secretsmanager:<arn>`` -> legacy alias for sealed (same prod path).
    # The resolver NEVER fails a check on an unresolvable ref — it falls back to platform.
    redaction_key_ref_default_scheme: str = "env"

    # ── Classifier (Component 2) ─────────────────────────────────────────────────
    # 'stub' = in-process keyword/lexicon heuristic (default; no torch/detoxify).
    # 'detoxify' = load the detoxify RoBERTa model EAGERLY at lifespan (PROD; requires the
    # `ml` extra). When the dep/model is unavailable the build GRACEFULLY falls back to the
    # stub classifier so tests + keyless dev still work.
    classifier_mode: str = "stub"
    # detoxify confidence threshold: a head scoring >= this is surfaced as a category.
    detoxify_threshold: float = 0.5
    # Pinned detoxify checkpoint variant (kept here so the model is reproducible/auditable
    # rather than hardcoded in the classifier). 'original' is the first-cycle pin.
    detoxify_checkpoint: str = "original"

    # ── Real classifier seam via llms-gateway POST /v1/classify (ADDITIVE / flagged) ──
    # When ``classifier_mode`` is neither 'stub' nor 'detoxify' it names the REMOTE
    # classifier transport (currently only 'llms_gateway'). The default stays 'stub' so
    # keyless local dev never reaches the network. The remote classifier is consulted at
    # the small/large-LLM cascade stages ONLY for the UNCERTAIN confidence band — a clearly
    # benign or clearly toxic stub signal short-circuits without a remote round-trip
    # (latency guard). On ANY remote error/timeout the cascade FALLS BACK to the stub
    # verdict (fail-closed safety is preserved: the stub still blocks what it detects).
    # Base URL of the llms-gateway (in-cluster default; unused while mode='stub').
    llms_gateway_url: str = "http://localhost:8000"
    # The classify model alias to request from the gateway ('safety-default' is the
    # platform alias -> the gateway's own stub/local safety provider).
    classifier_remote_model: str = "safety-default"
    # Short, INDEPENDENT hot-path budget for the remote classify call. A slow gateway must
    # not blow the 30/50ms-in / 60/100ms-out SLOs — on timeout the cascade falls back to
    # the stub verdict. Kept well under the per-rule classifier timeout_ms (50ms).
    classifier_remote_timeout_seconds: float = 0.045
    # Confidence band that ESCALATES to the remote classifier. A stub max-score strictly
    # BELOW the low bound is treated as confidently-benign (no escalation); a score at/above
    # the high bound is confidently-toxic (no escalation — the stub already fires). Only the
    # [low, high) uncertain band pays the remote round-trip. Defaults chosen so the stub's
    # fixed scores (0.9/0.95) sit ABOVE the band => with mode='stub' nothing escalates and
    # verdicts are byte-identical to today.
    classifier_escalate_low: float = 0.30
    classifier_escalate_high: float = 0.85
    # The remote classify verdict score that maps onto a positive toxicity category for the
    # classifier-backed rules (mirrors detoxify_threshold's role for the remote path).
    classifier_remote_threshold: float = 0.5

    # ── PII via Microsoft Presidio (OPTIONAL dep + flag; default OFF) ─────────────
    # When ON, the Presidio analyzer runs BEFORE the existing regex/HMAC redaction to lift
    # PII recall (names, locations, IBANs, … the regexes miss). Presidio only LOCATES spans;
    # the existing deterministic ``[REDACTED:cat:hex8]`` HMAC token format is unchanged. When
    # the optional ``presidio`` extra is not installed the flag GRACEFULLY degrades to the
    # current regex-only path (logged), so keyless dev and CI are unaffected. Default OFF
    # keeps today's exact behaviour.
    guardrails_pii_presidio: bool = False
    # Minimum Presidio analyzer confidence for a span to be treated as PII (drops low-conf
    # noise). Presidio entity types are mapped onto our redaction categories below.
    presidio_score_threshold: float = 0.5
    # Comma-separated Presidio entity types to request (empty => the analyzer's defaults).
    presidio_entities: str = ""

    # ── Unicode input canonicalization (de-obfuscation detection view — B1) ──────
    # A detection VIEW is built from the raw input before the block-category injection /
    # jailbreak detectors run, so an attacker who splices zero-width spaces, Tag-block
    # "ASCII smuggling", bidi controls, fullwidth digits, or cross-script homoglyphs into a
    # payload can no longer slip past the regex/signature layer while the downstream LLM still
    # reads the intended text. RAW text is left untouched for PII redaction/HMAC (the offset
    # map to the original is preserved), so ONLY the block-action detectors read the view.
    #
    # Layer A (always-on, no flag): strip zero-width (U+200B–200D, U+FEFF), the Tags block
    #   (U+E0000–E007F), and bidi controls/isolates (U+202A–202E, U+2066–2069). Deterministic,
    #   no false-positive risk on legitimate text — a no-op on clean ASCII.
    # Layer B (opt-in, this flag): NFKC compatibility fold (fullwidth/ligature/superscript ->
    #   canonical ASCII/BMP). Default OFF because it can alter legitimate non-ASCII text.
    injection_normalize: bool = False
    # Layer C (opt-in): UTS #39 confusables skeleton fold (cross-script homoglyphs — Cyrillic/
    #   Greek/Cherokee look-alikes -> Latin skeleton) that NFKC provably cannot do. The map is
    #   built ONCE from a checked-in Unicode confusables data file at lifespan. Default OFF
    #   (precision risk on legitimate non-Latin text; the CI precision floor guards it).
    guardrails_confusables_fold: bool = False

    # ── Per-request canary-token leak detector (output rule — B7; default OFF) ───
    # When ON, ``output-canary-leak-v1`` scans model OUTPUT for the caller-supplied
    # high-entropy canary token(s) (body field ``canary_tokens``) the caller embedded in its
    # own system prompt; any occurrence (exact + de-spaced/hex/base64 variants) means the
    # system prompt/context leaked -> block. Field absent OR flag off => byte-identical to
    # today (the detector is inert exactly like ``untrusted_spans``). No LLM/network.
    canary_leak_enabled: bool = False

    # ── Native context-window PII validation -> default-path passport/name (B8; OFF) ──
    # A deterministic, spaCy-free context enhancer: each regex passport-number / name
    # candidate is admitted ONLY when a configurable supporting term appears within
    # ``pii_context_window`` chars — the mechanism Presidio's context enhancer / Google DLP
    # hotword rules use, implemented natively. Unlocks a hot-path-safe built-in passport /
    # honorific-gated name detector that bare regex cannot do at acceptable precision. Default
    # OFF (fail-soft) so the default path stays byte-identical; the rules are inert unless on.
    guardrails_pii_context_validation: bool = False
    # Proximity window (chars, either side of the candidate span) for a supporting term.
    pii_context_window: int = 40
    # Supporting-term lexicons (comma-separated). A passport-number candidate is admitted only
    # with a passport term nearby; a name candidate only with an honorific/name term nearby.
    pii_context_passport_terms: str = (
        "passport,passport number,passport no,document number,document no,travel document,"
        "mrz,nationality,date of issue,place of birth"
    )
    pii_context_name_terms: str = (
        "mr,mrs,ms,dr,prof,mr.,mrs.,ms.,dr.,prof.,my name is,name:,full name,i am,i'm,"
        "signed,sincerely,regards"
    )

    # ── Prompt-injection defense (instruction-hierarchy + spotlighting) ──────────
    # ADDITIVE input-side detector that tags RAG/tool-provided spans (the "spotlight") and
    # raises an injection-risk score, applying STRICTER thresholds to retrieved content.
    # Default ON but TUNED so benign input keeps today's verdicts: it only contributes
    # decision METADATA (and escalates the prompt-injection rule's action to block) when an
    # injection pattern appears INSIDE a marked untrusted span. With no marked spans the
    # detector is a pure no-op (so existing tests/verdicts are byte-identical).
    injection_defense_enabled: bool = True
    # The injection-risk score at/above which a marked untrusted span's hit is escalated to
    # block (stricter than trusted-content handling). 1.0 disables escalation entirely.
    injection_spotlight_block_threshold: float = 0.5

    # ── Output groundedness / hallucination signal (flagged; default OFF) ─────────
    # On ``/v1/check/output`` an OPTIONAL groundedness signal (NLI/entailment-style or a
    # classify-variant) scores how well the response is supported by the provided context
    # (``input_text`` + any caller-supplied grounding). HIGH-risk (low groundedness) escalates
    # to a 'warn' review signal in decision metadata. Default OFF => output checks are
    # byte-identical to today. The signal NEVER blocks on its own (review, not enforcement).
    groundedness_enabled: bool = False
    # Below this groundedness score (0..1) the output is flagged high hallucination risk.
    groundedness_min_score: float = 0.4
    # Backend for the groundedness signal: 'heuristic' (keyless lexical-overlap NLI proxy,
    # default) or 'llms_gateway' (a classify-variant via the gateway, reusing the remote
    # classifier transport + its timeout/fallback).
    groundedness_backend: str = "heuristic"

    # ── LIVE check-path fail-mode + per-stage timeout (FIX 5) ────────────────────
    # Honor ``policy.fail_mode_override`` on the LIVE check path (today only simulation did).
    # Default ON: a policy that set fail_mode_override now applies it live, exactly as the
    # simulation trace already reported. Turning it OFF restores the prior live behaviour
    # (each rule's own ``default_fail_mode``) as a documented break-glass.
    live_fail_mode_override_enabled: bool = True

    # ── SAFETY rules fail-CLOSED on timeout (research-aligned safety hardening) ────
    # A timed-out SAFETY rule (PII / security / jailbreak / toxicity) must NEVER silently
    # allow: under load a 10ms per-rule budget could let e.g. output-pii-email overrun and
    # be SKIPPED (fail-open) when a policy set fail_mode_override='open', leaking PII. With
    # this ON (default) a timed-out safety rule is treated as a VIOLATION (block) regardless
    # of fail_mode — the fail-OPEN posture is still honoured for NON-safety rules (length).
    # Turning it OFF reverts to each rule's resolved fail_mode for safety rules too (the
    # prior behaviour) as a documented break-glass. Only the LIVE check path opts in; the
    # pipeline default keeps simulation + unit-level callers byte-identical.
    safety_rule_fail_closed_on_timeout: bool = True
    # Rule CATEGORIES treated as safety-critical for the fail-closed-on-timeout rule above
    # (comma-separated). 'length' is intentionally excluded (a missed length cap is not a
    # safety leak), so an over-long output still honours its policy fail_mode.
    safety_rule_categories: str = "security,pii,jailbreak,toxicity"
    # A SAFETY rule's effective per-rule timeout budget on the LIVE path is at least this many
    # milliseconds (a floor over the rule's own ``timeout_ms``). Raising the safety budget
    # above the tight 10ms default means transient load no longer trips a false timeout that
    # would (pre-fix) silently skip PII redaction. 0 disables the floor (use the rule's own
    # timeout_ms). Non-safety rules are unaffected.
    safety_rule_min_timeout_ms: int = 25

    # ── Usage metering cost model (WP07 — Contract 19.1) ─────────────────────────
    # Real per-rule-evaluation cost (USD) used to compute a check's ``cost_usd`` when the
    # DB ``guardrails.rules.cost_usd`` column has no per-rule price. ``cost_usd`` for a
    # check = sum of evaluated rules' DB cost (when present) else this fallback per rule.
    # Default is a tiny non-zero amount so metering is real rather than always 0.
    usage_cost_per_rule_usd: float = 0.00001

    # ── Policy simulation (WP07 — authoring + simulation) ────────────────────────
    # Per-tenant simulations/hour cap (a fixed 1-hour Valkey window counter). FAILS OPEN
    # when no Valkey client is wired or the backend errors — a cache outage must never
    # block policy authoring. 0/negative disables the limit entirely.
    simulation_rate_limit_per_hour: int = 1000
    simulation_rate_limit_key_prefix: str = "cypherx:grd:simrate:"
    # Short, INDEPENDENT timeout for the atomic sim-rate lookup (a slow Valkey must not
    # stall a simulate); on timeout/error the limiter fails OPEN.
    simulation_rate_limit_valkey_timeout_seconds: float = 0.15
    # Max characters of input text accepted by a simulate call (bounds the trace payload).
    simulation_max_text_chars: int = 50_000
    # Usage operation tag recorded for simulate calls. Cost is ALWAYS 0 (never billed).
    simulation_usage_operation: str = "simulate"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
