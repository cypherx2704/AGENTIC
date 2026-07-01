package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.jdbc.core.ConnectionCallback
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.time.Instant
import java.util.UUID

/**
 * Access to `auth.upstream_service_issuers` (Component 8b-ext) — federated OIDC issuers trusted to
 * present `client_assertion` JWTs for the OAuth2 `client_credentials` Mode B flow.
 *
 * The table is PLATFORM-scoped (no RLS — listed under platform tables in the init migration), so
 * every access goes through [TenantTx.inPlatform]. The `tenant_id` column is a plain attribute used
 * to attribute the minted token and to filter admin views; it is NOT enforced by RLS here.
 *
 * The `/oauth/token` Mode B flow looks up the issuer by its `iss` (PK) to find the `jwks_uri`,
 * `required_claims`, `allowed_audiences`, and `allowed_scopes`.
 */
@Repository
class UpstreamIssuerRepository(
    private val tenantTx: TenantTx,
    private val objectMapper: ObjectMapper,
) {

    /** Resolve a trusted federated issuer by its `iss` claim (used by the token endpoint). */
    fun findByIss(iss: String): UpstreamIssuerRow? =
        tenantTx.inPlatform { jdbc ->
            jdbc.query(
                "SELECT * FROM auth.upstream_service_issuers WHERE iss = ?",
                { rs, _ -> mapRow(rs) },
                iss,
            ).firstOrNull()
        }

    /** Register (or upsert) a federated issuer (admin path). */
    fun upsert(row: NewUpstreamIssuer): UpstreamIssuerRow =
        tenantTx.inPlatform { jdbc ->
            jdbc.update(
                """
                INSERT INTO auth.upstream_service_issuers
                  (iss, tenant_id, jwks_uri, required_claims, allowed_audiences, allowed_scopes, status, created_at)
                VALUES (?, ?, ?, ?::jsonb, ?, ?, ?, NOW())
                ON CONFLICT (iss) DO UPDATE SET
                  tenant_id         = EXCLUDED.tenant_id,
                  jwks_uri          = EXCLUDED.jwks_uri,
                  required_claims   = EXCLUDED.required_claims,
                  allowed_audiences = EXCLUDED.allowed_audiences,
                  allowed_scopes    = EXCLUDED.allowed_scopes,
                  status            = EXCLUDED.status
                """.trimIndent(),
                row.iss,
                row.tenantId,
                row.jwksUri,
                objectMapper.writeValueAsString(row.requiredClaims),
                textArray(jdbc, row.allowedAudiences),
                textArray(jdbc, row.allowedScopes),
                row.status,
            )
            findByIssOn(jdbc, row.iss) ?: error("upstream issuer ${row.iss} missing after upsert")
        }

    private fun findByIssOn(jdbc: JdbcTemplate, iss: String): UpstreamIssuerRow? =
        jdbc.query(
            "SELECT * FROM auth.upstream_service_issuers WHERE iss = ?",
            { rs, _ -> mapRow(rs) },
            iss,
        ).firstOrNull()

    private fun mapRow(rs: ResultSet): UpstreamIssuerRow {
        val requiredClaimsJson = rs.getString("required_claims") ?: "{}"
        @Suppress("UNCHECKED_CAST")
        val requiredClaims: Map<String, Any?> =
            objectMapper.readValue(requiredClaimsJson, Map::class.java) as Map<String, Any?>
        return UpstreamIssuerRow(
            iss = rs.getString("iss"),
            tenantId = rs.getObject("tenant_id", UUID::class.java),
            jwksUri = rs.getString("jwks_uri"),
            requiredClaims = requiredClaims,
            allowedAudiences = readTextArray(rs, "allowed_audiences"),
            allowedScopes = readTextArray(rs, "allowed_scopes"),
            status = rs.getString("status"),
            createdAt = rs.getTimestamp("created_at")?.toInstant(),
        )
    }

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

/** Upsert payload for a federated issuer. */
data class NewUpstreamIssuer(
    val iss: String,
    val tenantId: UUID,
    val jwksUri: String,
    val requiredClaims: Map<String, Any?>,
    val allowedAudiences: List<String>,
    val allowedScopes: List<String>,
    val status: String = "active",
)

/** A row of `auth.upstream_service_issuers`. */
data class UpstreamIssuerRow(
    val iss: String,
    val tenantId: UUID,
    val jwksUri: String,
    val requiredClaims: Map<String, Any?>,
    val allowedAudiences: List<String>,
    val allowedScopes: List<String>,
    val status: String,
    val createdAt: Instant?,
)
