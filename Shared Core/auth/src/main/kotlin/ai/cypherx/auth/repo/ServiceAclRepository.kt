package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.stereotype.Repository
import java.sql.ResultSet

/**
 * Platform-scoped access to `auth.service_acl` (Component 8b) — the allow-list of which caller
 * service may talk to which target service and with what internal scopes.
 *
 * The table has NO `tenant_id` and NO RLS, so every access goes through [TenantTx.inPlatform].
 * PK is `(caller_service, target_service)`; a single caller may have several target rows, so the
 * effective scope grant for a caller is the UNION of `allowed_scopes` across all its rows.
 */
@Repository
class ServiceAclRepository(
    private val tenantTx: TenantTx,
) {

    /**
     * All ACL edges for [callerService]. Empty list means the caller has no service_acl row at
     * all (the caller may call nothing) — the service layer turns that into a 403.
     */
    fun findByCaller(callerService: String): List<ServiceAclEdge> =
        tenantTx.inPlatform { jdbc -> findByCaller(jdbc, callerService) }

    /**
     * The union of `allowed_scopes` across every edge whose `caller_service = [callerService]`.
     * Distinct, order-preserving. Empty when the caller has no rows.
     */
    fun unionAllowedScopes(callerService: String): List<String> =
        findByCaller(callerService)
            .flatMap { it.allowedScopes }
            .distinct()

    private fun findByCaller(jdbc: JdbcTemplate, callerService: String): List<ServiceAclEdge> =
        jdbc.query(
            "SELECT caller_service, target_service, allowed_scopes " +
                "FROM auth.service_acl WHERE caller_service = ?",
            { rs, _ -> mapEdge(rs) },
            callerService,
        )

    private fun mapEdge(rs: ResultSet): ServiceAclEdge =
        ServiceAclEdge(
            callerService = rs.getString("caller_service"),
            targetService = rs.getString("target_service"),
            allowedScopes = readTextArray(rs, "allowed_scopes"),
        )

    private fun readTextArray(rs: ResultSet, column: String): List<String> {
        val array = rs.getArray(column) ?: return emptyList()
        @Suppress("UNCHECKED_CAST")
        val raw = array.array as? Array<Any?> ?: return emptyList()
        return raw.filterNotNull().map { it.toString() }
    }
}

/** One row of `auth.service_acl`. */
data class ServiceAclEdge(
    val callerService: String,
    val targetService: String,
    val allowedScopes: List<String>,
)
