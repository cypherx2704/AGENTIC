"""Cost calculation (Component 1 formula).

::

    cost_usd = prompt/1000     * input_rate
             + completion/1000 * output_rate
             + cached/1000      * cached_rate
             + creation/1000    * creation_rate

Rates come from ``llms.provider_pricing`` — the DB is the single authority, loaded
at startup and refreshed every 60 s by the lifespan refresh loop (Amendment Log
2026-06). The in-code ``_FALLBACK_PRICING`` table is a documented LAST-RESORT
COLD-START FALLBACK only: it mirrors the seed migration (drift-guarded by
tests/test_config_registry.py) and is superseded as soon as a DB load succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PricingRow:
    input_per_1k: float
    output_per_1k: float
    cached_input_per_1k: float = 0.0
    cache_creation_per_1k: float = 0.0


# Cold-start fallback ONLY — mirrors db/migrations seed exactly (a test parses the
# seed SQL and asserts equality so the two can never drift). The DB table is the
# single authority at runtime; this map is consulted only until the first
# successful DB load (llms_config_source{source} reports which is live).
_FALLBACK_PRICING: dict[tuple[str, str], PricingRow] = {
    ("anthropic", "claude-opus-4-8"): PricingRow(0.015, 0.075, 0.0015, 0.01875),
    ("anthropic", "claude-sonnet-4-6"): PricingRow(0.003, 0.015, 0.0003, 0.00375),
    ("anthropic", "claude-haiku-4-5"): PricingRow(0.0008, 0.004, 0.00008, 0.001),
    ("openai", "gpt-4o"): PricingRow(0.005, 0.015),
    ("openai", "gpt-4o-mini"): PricingRow(0.00015, 0.0006),
    # Embeddings (WP06): price on input tokens only; output rate 0 by convention.
    ("openai", "text-embedding-3-small"): PricingRow(0.00002, 0.0),
    # Rerank + safety-classify (cypherx mock/stub class): metered by UNITS, not token
    # cost — all rates 0 so cost_usd stays 0 (NO cost rewrite; Contract-19 units only).
    ("cypherx", "rerank-mock-v1"): PricingRow(0.0, 0.0),
    ("cypherx", "classify-stub-v1"): PricingRow(0.0, 0.0),
}


class CostCalculator:
    """Loads + caches provider pricing and computes ``cost_usd``."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], PricingRow] = dict(_FALLBACK_PRICING)

    async def load_from_db(self, pool: AsyncConnectionPool) -> bool:
        """Refresh the pricing cache from ``llms.provider_pricing`` (latest effective row).

        Returns True on a successful load (drives ``llms_config_source``).
        """
        try:
            from ..db.pool import fetch_pricing  # local import to avoid cycle

            rows = await fetch_pricing(pool)
        except Exception as exc:  # noqa: BLE001 — keep fallback table on any failure
            logger.warning("pricing_load_failed", error=str(exc))
            return False
        for provider, model, ip, op, cip, ccp in rows:
            self._cache[(provider, model)] = PricingRow(
                input_per_1k=float(ip),
                output_per_1k=float(op),
                cached_input_per_1k=float(cip),
                cache_creation_per_1k=float(ccp),
            )
        logger.info("pricing_loaded", rows=len(rows))
        return True

    def rate_for(self, provider: str, model: str) -> PricingRow:
        row = self._cache.get((provider, model))
        if row is None:
            logger.warning("pricing_missing", provider=provider, model=model)
            return PricingRow(0.0, 0.0)
        return row

    def compute(
        self,
        provider: str,
        model: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cached_prompt_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float:
        r = self.rate_for(provider, model)
        cost = (
            prompt_tokens / 1000 * r.input_per_1k
            + completion_tokens / 1000 * r.output_per_1k
            + cached_prompt_tokens / 1000 * r.cached_input_per_1k
            + cache_creation_tokens / 1000 * r.cache_creation_per_1k
        )
        return round(cost, 8)


# Process-wide instance.
cost_calculator = CostCalculator()
