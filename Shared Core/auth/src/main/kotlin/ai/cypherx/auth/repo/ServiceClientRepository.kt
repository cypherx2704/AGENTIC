package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.ServiceClientStatus
import org.springframework.jdbc.core.ConnectionCallback
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * Access to `auth.service_clients` (Component 8b-ext) — external OAuth2 `client_credentials`
 * clients. The table is tenant-scoped (RLS `USING tenant_id = app.tenant_id`), so:
 *
 *  - Admin CRUD (caller's tenant known from their JWT) goes through [TenantTx.inTenant].
 *  - The `/oauth/token` lookup-by-client_id has NO tenant context (the endpoint is public and the
 *    tenant is only learnt from the row itself), so [findByIdForTokenIssuance] runs in
 *    [TenantTx.inPlatform]. NOTE: this read depends on the deployment NOT forcing RLS to error on
 *    an unset `app.tenant_id` (see migrations README — platform reads of tenant tables are by
 *    design). It is the only safe path for an unauthenticated token endpoint to resolve a client.
 */
@Repository
class ServiceClientRepository(
    private val tenantTx: TenantTx,
) {

    /** Insert a new service client (admin path, tenant known). */
    fun insert(tenantId: UUID, client: NewServiceClient): ServiceClientRow =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                """
                INSERT INTO auth.service_clients
                  (client_id, tenant_id, name, client_secret_hash, allowed_grant_types,
                   allowed_audiences, allowed_scopes, status, created_by, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NOW(), ?)
                """.trimIndent(),
                client.clientId,
                tenantId,
                client.name,
                client.clientSecretHash,
                textArray(jdbc, client.allowedGrantTypes),
                textArray(jdbc, client.allowedAudiences),
                textArray(jdbc, client.allowedScopes),
                ServiceClientStatus.ACTIVE.value,
                client.createdBy,
                client.expiresAt?.let { Timestamp.from(it) },
            )
            findById(jdbc, client.clientId)
                ?: error("service client ${client.clientId} not found immediately after insert")
        }

    /** List all service clients for the caller's tenant (admin path). Secret hash never returned. */
    fun listByTenant(tenantId: UUID): List<ServiceClientRow> =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                "SELECT * FROM auth.service_clients ORDER BY created_at DESC",
                { rs, _ -> mapRow(rs) },
            )
        }

    /** Mark a client revoked (admin path). Returns true if a row was updated. */
    fun revoke(tenantId: UUID, clientId: UUID): Boolean =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                "UPDATE auth.service_clients SET status = ? WHERE client_id = ?",
                ServiceClientStatus.REVOKED.value,
                clientId,
            ) > 0
        }

    /** Rotate a client's secret hash (admin path). Returns true if a row was updated. */
    fun updateSecretHash(tenantId: UUID, clientId: UUID, newHash: String): Boolean =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                "UPDATE auth.service_clients SET client_secret_hash = ?, status = ? WHERE client_id = ?",
                newHash,
                ServiceClientStatus.ACTIVE.value,
                clientId,
            ) > 0
        }

    /** Fetch a single client within the caller's tenant (admin path). */
    fun findByIdInTenant(tenantId: UUID, clientId: UUID): ServiceClientRow? =
        tenantTx.inTenant(tenantId) { jdbc -> findById(jdbc, clientId) }

    /**
     * Look up a client by its global UUID PK for the public `/oauth/token` flow — no tenant context.
     * Runs in a platform transaction. Returns null if not found.
     */
    fun findByIdForTokenIssuance(clientId: UUID): ServiceClientRow? =
        tenantTx.inPlatform { jdbc -> findById(jdbc, clientId) }

    /** Best-effort last_used_at stamp after a successful token issuance (platform path). */
    fun touchLastUsed(clientId: UUID) {
        tenantTx.inPlatform { jdbc ->
            jdbc.update("UPDATE auth.service_clients SET last_used_at = NOW() WHERE client_id = ?", clientId)
        }
    }

    private fun findById(jdbc: JdbcTemplate, clientId: UUID): ServiceClientRow? =
        jdbc.query(
            "SELECT * FROM auth.service_clients WHERE client_id = ?",
            { rs, _ -> mapRow(rs) },
            clientId,
        ).firstOrNull()

    private fun mapRow(rs: ResultSet): ServiceClientRow =
        ServiceClientRow(
            clientId = rs.getObject("client_id", UUID::class.java),
            tenantId = rs.getObject("tenant_id", UUID::class.java),
            name = rs.getString("name"),
            clientSecretHash = rs.getString("client_secret_hash"),
            allowedGrantTypes = readTextArray(rs, "allowed_grant_types"),
            allowedAudiences = readTextArray(rs, "allowed_audiences"),
            allowedScopes = readTextArray(rs, "allowed_scopes"),
            status = rs.getString("status"),
            createdBy = rs.getObject("created_by", UUID::class.java),
            createdAt = rs.getTimestamp("created_at")?.toInstant(),
            expiresAt = rs.getTimestamp("expires_at")?.toInstant(),
            lastUsedAt = rs.getTimestamp("last_used_at")?.toInstant(),
        )

    private fun textArray(jdbc: JdbcTemplate, values: List<String>): java.sql.Array =
        jdbc.execute(ConnectionCallback { con -> con.createArrayOf("text", values.toTypedArray()) })
            ?: error("failed to build SQL text[] array")

    private fun readTextArray(rs: ResultSet, column: String): List<String> {
        val array = rs.getArray(column) ?: return emptyList()
        @Suppress("UNCHECKED_CAST")
        val raw = array.array as? Array<Any?> ?: return emptyList()
        return raw.filterNotNull().map { it.toString() }
    }
}

/** Insert payload for a new service client. */
data class NewServiceClient(
    val clientId: UUID,
    val name: String,
    val clientSecretHash: String?,
    val allowedGrantTypes: List<String>,
    val allowedAudiences: List<String>,
    val allowedScopes: List<String>,
    val createdBy: UUID,
    val expiresAt: Instant?,
)

/** A row of `auth.service_clients`. */
data class ServiceClientRow(
    val clientId: UUID,
    val tenantId: UUID,
    val name: String,
    val clientSecretHash: String?,
    val allowedGrantTypes: List<String>,
    val allowedAudiences: List<String>,
    val allowedScopes: List<String>,
    val status: String,
    val createdBy: UUID,
    val createdAt: Instant?,
    val expiresAt: Instant?,
    val lastUsedAt: Instant?,
)
