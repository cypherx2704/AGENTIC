package ai.cypherx.auth

import ai.cypherx.auth.support.AbstractIntegrationTest
import ai.cypherx.auth.support.INTEGRATION_TEST_TENANT
import ai.cypherx.auth.support.PLATFORM_TENANT
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.jdbc.core.ConnectionCallback
import java.sql.Connection
import java.util.UUID

/**
 * Contract 13 mandate: cross-tenant isolation is ARCHITECTURAL (PostgreSQL RLS), not policy. With
 * the runtime role (auth_user, NON-superuser ⇒ RLS enforced) and `app.tenant_id` set to tenant B, a
 * read of tenant A's row MUST return 0 rows — the data is invisible, not merely forbidden.
 */
class RlsCrossTenantIntegrationTest : AbstractIntegrationTest() {

    private val systemUser = "00000000-0000-0000-0000-000000000000"

    @BeforeEach
    fun setUp() {
        resetState()
    }

    /** Insert an agent directly (superuser bypasses RLS), returning its id. */
    private fun seedAgent(tenant: UUID, name: String): UUID =
        superuserJdbc().queryForObject(
            "INSERT INTO auth.agents (tenant_id, name, created_by) VALUES (?::uuid, ?, ?::uuid) RETURNING agent_id",
            UUID::class.java,
            tenant.toString(), name, systemUser,
        )!!

    /** Count agents matching [agentId] visible to auth_user when app.tenant_id = [asTenant] (RLS on). */
    private fun visibleCount(agentId: UUID, asTenant: UUID): Int =
        authUserJdbc().execute(
            ConnectionCallback { conn: Connection ->
                conn.createStatement().use { it.execute("SET app.tenant_id = '$asTenant'") }
                conn.prepareStatement("SELECT count(*) FROM auth.agents WHERE agent_id = ?::uuid").use { ps ->
                    ps.setString(1, agentId.toString())
                    ps.executeQuery().use { rs -> rs.next(); rs.getInt(1) }
                }
            },
        )!!

    @Test
    fun `a tenant cannot see another tenant's agent (RLS denial)`() {
        val agentA = seedAgent(PLATFORM_TENANT, "rls-agent-a")
        seedAgent(INTEGRATION_TEST_TENANT, "rls-agent-b")

        // Same-tenant context: agent A is visible (sanity — proves RLS isn't just hiding everything).
        assertThat(visibleCount(agentA, PLATFORM_TENANT)).isEqualTo(1)

        // Cross-tenant context (tenant B): agent A is INVISIBLE — 0 rows under RLS.
        assertThat(visibleCount(agentA, INTEGRATION_TEST_TENANT)).isEqualTo(0)
    }
}
