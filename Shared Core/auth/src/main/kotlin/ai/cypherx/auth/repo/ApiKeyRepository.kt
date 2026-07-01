package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.ApiKeyStatus
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * Persistence for `auth.api_keys` (Component 2). TENANT-SCOPED — every access goes through
 * [TenantTx.inTenant] so PostgreSQL RLS (`USING tenant_id = app.tenant_id`) confines reads/writes
 * to the caller's tenant. The raw key is NEVER stored: only its SHA-256 hex [ApiKeyRow.keyHash]
 * (64 chars) and an 8-char display [ApiKeyRow.keyPrefix].
 *
 * The table layout is the Phase-2 migration shape (key_id, agent_id, tenant_id, key_hash,
 * key_prefix, name, scopes[], status, expires_at, last_used_at, created_at, revoked_at,
 * revoked_by). See db/migrations/20260606_0001__init.sql.
 */
@Repository
class ApiKeyRepository(
    private val tenantTx: TenantTx,
) {

    /** A persisted API-key row WITHOUT the raw secret (which is unrecoverable). */
    data class ApiKeyRow(
        val keyId: UUID,
        val agentId: UUID,
        val tenantId: UUID,
        val keyHash: String,
        val keyPrefix: String,
        val name: String?,
        val scopes: List<String>,
        val status: String,
        val expiresAt: Instant?,
        val lastUsedAt: Instant?,
        val createdAt: Instant,
        val revokedAt: Instant?,
        val revokedBy: UUID?,
    )

    /**
     * Insert a new API key for [agentId] in [tenantId]. [keyHash] is the SHA-256 hex of the raw
     * key; [keyPrefix] the 8-char display prefix; [scopes] the granted scopes; [expiresAt] optional
     * expiry. Returns the generated key_id.
     */
    fun insert(
        tenantId: UUID,
        agentId: UUID,
        keyHash: String,
        keyPrefix: String,
        name: String?,
        scopes: List<String>,
        expiresAt: Instant?,
    ): UUID = tenantTx.inTenant(tenantId) { jdbc ->
        val keyId = UUID.randomUUID()
        jdbc.update(
            """
            INSERT INTO auth.api_keys
                (key_id, agent_id, tenant_id, key_hash, key_prefix, name, scopes, status, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """.trimIndent(),
            keyId,
            agentId,
            tenantId,
            keyHash,
            keyPrefix,
            name,
            scopes.toTypedArray(),
            ApiKeyStatus.ACTIVE.value,
            expiresAt?.let { Timestamp.from(it) },
        )
        keyId
    }

    /** Count ACTIVE api keys for [agentId] in [tenantId] (RLS). Backs `api_keys_per_agent_max`. */
    fun countActiveByAgent(tenantId: UUID, agentId: UUID): Long =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.queryForObject(
                "SELECT COUNT(*) FROM auth.api_keys WHERE agent_id = ? AND status = ?",
                Long::class.java,
                agentId,
                ApiKeyStatus.ACTIVE.value,
            ) ?: 0L
        }

    /** List every key for [agentId] in [tenantId], newest first (no secret material is exposed). */
    fun listByAgent(tenantId: UUID, agentId: UUID): List<ApiKeyRow> =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                """
                SELECT key_id, agent_id, tenant_id, key_hash, key_prefix, name, scopes, status,
                       expires_at, last_used_at, created_at, revoked_at, revoked_by
                  FROM auth.api_keys
                 WHERE agent_id = ?
                 ORDER BY created_at DESC
                """.trimIndent(),
                ROW_MAPPER,
                agentId,
            )
        }

    /** Fetch a single key by id within [tenantId], or null. */
    fun findById(tenantId: UUID, keyId: UUID): ApiKeyRow? =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                """
                SELECT key_id, agent_id, tenant_id, key_hash, key_prefix, name, scopes, status,
                       expires_at, last_used_at, created_at, revoked_at, revoked_by
                  FROM auth.api_keys
                 WHERE key_id = ?
                """.trimIndent(),
                ROW_MAPPER,
                keyId,
            ).firstOrNull()
        }

    /**
     * Look up an ACTIVE-or-expirable key by its SHA-256 [keyHash] within [tenantId]. Used by the
     * token-exchange flow. Returns the row regardless of status/expiry so the caller can render a
     * precise error (revoked vs expired vs not-found); status/expiry checks live in the service.
     */
    fun findByHash(tenantId: UUID, keyHash: String): ApiKeyRow? =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                """
                SELECT key_id, agent_id, tenant_id, key_hash, key_prefix, name, scopes, status,
                       expires_at, last_used_at, created_at, revoked_at, revoked_by
                  FROM auth.api_keys
                 WHERE key_hash = ?
                """.trimIndent(),
                ROW_MAPPER,
                keyHash,
            ).firstOrNull()
        }

    /**
     * Revoke [keyId] (status -> revoked) within [tenantId], stamping revoked_at/by. No-op (returns
     * false) if the key does not exist or is already revoked. Idempotent at the SQL level via the
     * status guard.
     */
    fun revoke(tenantId: UUID, keyId: UUID, revokedBy: UUID): Boolean =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                """
                UPDATE auth.api_keys
                   SET status = ?, revoked_at = NOW(), revoked_by = ?
                 WHERE key_id = ? AND status <> ?
                """.trimIndent(),
                ApiKeyStatus.REVOKED.value,
                revokedBy,
                keyId,
                ApiKeyStatus.REVOKED.value,
            ) > 0
        }

    /**
     * Rotation grace: set [keyId]'s `expires_at` to [graceUntil] WITHOUT changing its status — the
     * old key stays `active` and remains usable until [graceUntil] passes (dual-validity window). The
     * token-exchange path rejects it the moment `now > expires_at`, so no background sweep is needed.
     * Only narrows the lifetime: a guard keeps the new `expires_at` from EXTENDING an already-shorter
     * expiry (`expires_at IS NULL OR expires_at > graceUntil`) and skips revoked/expired keys. Returns
     * true when a row was updated.
     */
    fun expireAt(tenantId: UUID, keyId: UUID, graceUntil: Instant): Boolean =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                """
                UPDATE auth.api_keys
                   SET expires_at = ?
                 WHERE key_id = ?
                   AND status = ?
                   AND (expires_at IS NULL OR expires_at > ?)
                """.trimIndent(),
                Timestamp.from(graceUntil),
                keyId,
                ApiKeyStatus.ACTIVE.value,
                Timestamp.from(graceUntil),
            ) > 0
        }

    /**
     * Revoke EVERY not-yet-revoked key of [agentId] in [tenantId] in one statement (agent-deactivate
     * cascade), stamping revoked_at/by. Returns the number of keys revoked. Idempotent via the status
     * guard (already-revoked keys are untouched).
     */
    fun revokeAllByAgent(tenantId: UUID, agentId: UUID, revokedBy: UUID): Int =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                """
                UPDATE auth.api_keys
                   SET status = ?, revoked_at = NOW(), revoked_by = ?
                 WHERE agent_id = ? AND status <> ?
                """.trimIndent(),
                ApiKeyStatus.REVOKED.value,
                revokedBy,
                agentId,
                ApiKeyStatus.REVOKED.value,
            )
        }

    /** Stamp last_used_at = NOW() on a successful exchange (best-effort usage telemetry). */
    fun touchLastUsed(tenantId: UUID, keyId: UUID) {
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update("UPDATE auth.api_keys SET last_used_at = NOW() WHERE key_id = ?", keyId)
        }
    }

    private companion object {
        @Suppress("UNCHECKED_CAST")
        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            ApiKeyRow(
                keyId = rs.getObject("key_id", UUID::class.java),
                agentId = rs.getObject("agent_id", UUID::class.java),
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                keyHash = rs.getString("key_hash"),
                keyPrefix = rs.getString("key_prefix"),
                name = rs.getString("name"),
                scopes = readTextArray(rs, "scopes"),
                status = rs.getString("status"),
                expiresAt = rs.getTimestamp("expires_at")?.toInstant(),
                lastUsedAt = rs.getTimestamp("last_used_at")?.toInstant(),
                createdAt = rs.getTimestamp("created_at").toInstant(),
                revokedAt = rs.getTimestamp("revoked_at")?.toInstant(),
                revokedBy = rs.getObject("revoked_by", UUID::class.java),
            )
        }

        private fun readTextArray(rs: ResultSet, column: String): List<String> {
            val arr = rs.getArray(column) ?: return emptyList()
            @Suppress("UNCHECKED_CAST")
            val raw = arr.array as? Array<Any?> ?: return emptyList()
            return raw.filterNotNull().map { it.toString() }
        }
    }
}
