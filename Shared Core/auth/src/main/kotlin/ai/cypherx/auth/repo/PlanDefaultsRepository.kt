package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet

/**
 * JDBC access to `auth.plan_defaults` (Contract 19 — the default quota `limits` document per plan).
 *
 * `auth.plan_defaults` is PLATFORM-scoped (no RLS — see migration 20260606_0001), so every read
 * goes through [TenantTx.inPlatform] (a plain transaction with NO `app.tenant_id` set). The
 * `limits` column is a JSONB document of per-service blocks (`auth`, `llms`, `guardrails`, `rag`,
 * `memory`, `tools`, `skills`, `xagent`); we surface it as a parsed Jackson [JsonNode] so the
 * service layer can deep-merge it with a per-tenant override without re-parsing.
 */
@Repository
class PlanDefaultsRepository(
    private val tenantTx: TenantTx,
    private val objectMapper: ObjectMapper,
) {

    /**
     * Fetch the default quota `limits` document for [plan], parsed into a Jackson [JsonNode]
     * (always an object node), or null when the plan is unknown.
     */
    fun limitsFor(plan: String): JsonNode? = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "SELECT limits::text AS limits FROM auth.plan_defaults WHERE plan = ?",
            { rs: ResultSet, _: Int -> rs.getString("limits") },
            plan,
        ).firstOrNull()?.let { json -> objectMapper.readTree(json) }
    }

    /**
     * Fetch the raw default quota `limits` JSON string for [plan] (un-parsed; useful when the
     * caller wants to persist the document verbatim), or null when the plan is unknown.
     */
    fun limitsJsonFor(plan: String): String? = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "SELECT limits::text AS limits FROM auth.plan_defaults WHERE plan = ?",
            { rs: ResultSet, _: Int -> rs.getString("limits") },
            plan,
        ).firstOrNull()
    }

    /** List every plan with its parsed default `limits` document, ordered by plan name. */
    fun listAll(): List<PlanDefault> = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "SELECT plan, limits::text AS limits FROM auth.plan_defaults ORDER BY plan",
            ROW_MAPPER,
        ).map { (plan, json) -> PlanDefault(plan = plan, limits = objectMapper.readTree(json)) }
    }

    private companion object {
        /** Raw `(plan, limits-json)` projection; parsing to [JsonNode] happens outside the mapper. */
        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            rs.getString("plan") to rs.getString("limits")
        }
    }
}

/** A row from `auth.plan_defaults`: the plan key plus its parsed default `limits` document. */
data class PlanDefault(
    val plan: String,
    val limits: JsonNode,
)
