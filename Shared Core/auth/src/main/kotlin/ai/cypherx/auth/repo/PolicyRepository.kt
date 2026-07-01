package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.util.UUID

/**
 * JDBC access to `auth.policies` for the RBAC decision engine (Component 5 / Component 4).
 *
 * `auth.policies` is tenant-scoped but the platform-default row carries `tenant_id IS NULL`. The
 * RLS policy `p_policies_tenant` is `USING (tenant_id = app.tenant_id OR tenant_id IS NULL)`, so a
 * single read inside [TenantTx.inTenant] returns BOTH the caller's per-tenant rows AND the platform
 * default row in one query — no separate platform read is needed. We order
 * `tenant_id NULLS LAST` so a per-tenant override sorts ahead of the platform default (Phase 2
 * Component 5: "a per-tenant override wins over the platform default").
 *
 * `rules` is a JSONB array of `{ action, resource_pattern?, effect: allow|deny, conditions: [] }`.
 * We parse it eagerly into [PolicyRow.rules] so the service layer can evaluate without touching
 * Jackson again.
 */
@Repository
class PolicyRepository(
    private val tenantTx: TenantTx,
    private val objectMapper: ObjectMapper,
) {

    /**
     * Load every `active` policy applicable to [tenantId]: the tenant's own rows plus the platform
     * default (`tenant_id IS NULL`), most-specific first. Runs inside the tenant transaction so RLS
     * scopes the result; the platform-default row is visible because the RLS predicate ORs in
     * `tenant_id IS NULL`.
     */
    fun findApplicable(tenantId: UUID): List<PolicyRow> = tenantTx.inTenant(tenantId) { jdbc ->
        readApplicable(jdbc)
    }

    /**
     * Same as [findApplicable] but reuses an already-open tenant transaction's [JdbcTemplate]
     * (so a caller that also writes the audit row in the same tx does ONE tenant tx, not two).
     */
    fun readApplicable(jdbc: JdbcTemplate): List<PolicyRow> =
        jdbc.query(
            """
            SELECT policy_id, tenant_id, name, rules
            FROM auth.policies
            WHERE status = 'active'
            ORDER BY tenant_id NULLS LAST, name
            """.trimIndent(),
            rowMapper,
        )

    private val rowMapper = RowMapper { rs: ResultSet, _: Int ->
        val rulesJson = rs.getString("rules")
        PolicyRow(
            policyId = rs.getObject("policy_id", UUID::class.java),
            tenantId = rs.getObject("tenant_id", UUID::class.java),
            name = rs.getString("name"),
            rules = parseRules(rulesJson),
        )
    }

    private fun parseRules(json: String?): List<PolicyRule> {
        if (json.isNullOrBlank()) return emptyList()
        val node: JsonNode = objectMapper.readTree(json)
        if (!node.isArray) return emptyList()
        return node.mapNotNull { rule ->
            val action = rule.get("action")?.asText()?.takeIf { it.isNotBlank() } ?: return@mapNotNull null
            PolicyRule(
                action = action,
                resourcePattern = rule.get("resource_pattern")?.asText()?.takeIf { it.isNotBlank() },
                effect = rule.get("effect")?.asText()?.lowercase()?.takeIf { it.isNotBlank() } ?: "deny",
            )
        }
    }
}

/** A row from `auth.policies` with its `rules` array parsed. `tenantId == null` is the platform default. */
data class PolicyRow(
    val policyId: UUID,
    val tenantId: UUID?,
    val name: String,
    val rules: List<PolicyRule>,
)

/** One entry of a policy's `rules` JSONB array. `conditions` are not evaluated in first cycle. */
data class PolicyRule(
    val action: String,
    val resourcePattern: String?,
    val effect: String, // "allow" | "deny"
)
