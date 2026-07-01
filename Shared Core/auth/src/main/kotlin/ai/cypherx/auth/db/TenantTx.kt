package ai.cypherx.auth.db

import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.stereotype.Component
import org.springframework.transaction.support.TransactionTemplate
import java.util.UUID

/**
 * Core tenant-transaction helper (Contract 13).
 *
 * Every tenant-scoped DB access goes through [inTenant]. It opens a transaction and runs
 *
 *     SET LOCAL app.tenant_id = '<uuid>';
 *
 * so PostgreSQL Row Level Security (`USING tenant_id = current_setting('app.tenant_id')::uuid`)
 * confines the work to that tenant. `SET LOCAL` is transaction-scoped — the setting is gone the
 * moment the tx commits/rolls back, so a pooled connection cannot leak tenant context to the next
 * borrower. We use `set_config(name, value, is_local := true)` (a function) rather than literal
 * `SET LOCAL`, because the function form takes a *bind parameter* — no string interpolation, no
 * SQL injection surface, even though the value here is always a server-side-parsed [UUID].
 *
 * Platform-scoped tables (signing_keys, service_acl, tenants, bootstrap_state, plan_defaults,
 * upstream_identity, upstream_service_issuers, revoked_tokens, signup_attempts) have NO RLS — use
 * [inPlatform], which runs a plain transaction with NO `app.tenant_id` set. (Touching a
 * tenant-scoped table inside [inPlatform] yields zero rows by RLS default-deny — intentional.)
 *
 * Both helpers run inside a Spring-managed transaction so the JdbcTemplate participates in the
 * same connection/transaction the `SET LOCAL` was issued on.
 *
 * Usage:
 * ```
 * val agents = tenantTx.inTenant(tenantId) { jdbc ->
 *     jdbc.query("SELECT agent_id FROM auth.agents", rowMapper)
 * }
 * val keys = tenantTx.inPlatform { jdbc ->
 *     jdbc.queryForObject("SELECT count(*) FROM auth.signing_keys", Long::class.java)
 * }
 * ```
 */
@Component
class TenantTx(
    private val jdbcTemplate: JdbcTemplate,
    private val transactionTemplate: TransactionTemplate,
) {

    /**
     * Run [block] inside a transaction scoped to [tenantId] via `SET LOCAL app.tenant_id`.
     * The same [JdbcTemplate] used to set the tenant is handed to [block] so all queries run on
     * the bound connection. Returns whatever [block] returns.
     */
    fun <T> inTenant(tenantId: UUID, block: (JdbcTemplate) -> T): T =
        transactionTemplate.execute {
            // set_config(...) RETURNS the applied value, so it must be queried, not .update()'d
            // (jdbcTemplate.update on a result-returning statement throws "A result was returned
            // when none was expected"). is_local := true scopes it to this transaction (Contract 13).
            jdbcTemplate.queryForObject(
                "SELECT set_config('app.tenant_id', ?, true)",
                String::class.java,
                tenantId.toString(),
            )
            // Wrap so a block that legitimately returns null (e.g. findByHash -> not found) is
            // preserved. Without the Holder, TransactionTemplate.execute returns null and a naive
            // `?: error(...)` would turn every not-found lookup into a 500.
            Holder(block(jdbcTemplate))
        }!!.value

    /**
     * Run [block] inside a plain transaction with NO tenant context (for platform-scoped tables).
     */
    fun <T> inPlatform(block: (JdbcTemplate) -> T): T =
        transactionTemplate.execute { Holder(block(jdbcTemplate)) }!!.value

    /** Non-null wrapper so a nullable block result survives TransactionTemplate.execute. */
    private class Holder<T>(val value: T)
}
