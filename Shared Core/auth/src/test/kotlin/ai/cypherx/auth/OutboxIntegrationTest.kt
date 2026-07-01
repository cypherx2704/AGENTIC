package ai.cypherx.auth

import ai.cypherx.auth.kafka.AuthEventPublisher
import ai.cypherx.auth.kafka.AuthTopics
import ai.cypherx.auth.kafka.OutboxRelay
import ai.cypherx.auth.support.AbstractIntegrationTest
import ai.cypherx.auth.support.AuthFlows
import ai.cypherx.auth.support.PLATFORM_TENANT
import ai.cypherx.auth.support.TestHttp
import com.fasterxml.jackson.databind.ObjectMapper
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.http.HttpHeaders
import org.springframework.http.HttpMethod
import org.springframework.http.HttpStatus
import org.springframework.kafka.support.SendResult
import java.time.Instant
import java.util.UUID
import java.util.concurrent.CompletableFuture

/**
 * Transactional outbox (Phase 2 Amendment Log 2026-06 / WP02):
 *  - every durable tenant-lifecycle transition and token revocation writes an `auth.outbox` row
 *    (Contract 5 envelope) committed WITH the state change (a failed change writes no row);
 *  - soft-delete emits `cypherx.tenant.pending_deletion` (NOT `tenant.deleted` — event fidelity);
 *  - the relay publishes unpublished rows, stamps `published_at`, records failures
 *    (`attempts`/`last_error`), backs off, and retries until the broker recovers;
 *  - `agent.updated` publishes to its OWN topic (the historical mis-publish to
 *    `agent.deactivated`'s topic is fixed).
 *
 * The relay loop is disabled in the test profile (`cypherx.auth.outbox.enabled=false`); the tests
 * drain deterministically via [OutboxRelay.relayOnce]. Kafka is the relaxed [kafkaTemplate] mock
 * from [AbstractIntegrationTest], stubbed per scenario.
 */
class OutboxIntegrationTest : AbstractIntegrationTest() {

    @LocalServerPort private var port: Int = 0

    @Autowired private lateinit var outboxRelay: OutboxRelay

    @Autowired private lateinit var eventPublisher: AuthEventPublisher

    private lateinit var http: TestHttp
    private val mapper = ObjectMapper()

    @BeforeEach
    fun setUp() {
        resetState()
        http = TestHttp("http://localhost:$port")
    }

    private fun adminHeaders(jwt: String): HttpHeaders = http.headers {
        set("Authorization", "Bearer $jwt")
        set("X-Tenant-ID", PLATFORM_TENANT.toString())
    }

    /** POST /v1/admin/tenants and return the created tenant_id. */
    private fun createTenant(jwt: String, name: String): String {
        val r = http.post(
            "/v1/admin/tenants",
            body = mapOf("name" to name),
            headers = adminHeaders(jwt),
        )
        check(r.statusCode == HttpStatus.CREATED) { "tenant create failed: ${r.statusCode} ${r.body}" }
        return r.body!!["tenant_id"] as String
    }

    private fun outboxRows(topic: String): List<Map<String, Any?>> =
        superuserJdbc().queryForList(
            "SELECT id, topic, partition_key, payload::text AS payload, published_at, attempts, last_error " +
                "FROM auth.outbox WHERE topic = ? ORDER BY created_at",
            topic,
        ).map { row ->
            @Suppress("UNCHECKED_CAST")
            val envelope = mapper.readValue(row["payload"] as String, Map::class.java) as Map<String, Any?>
            row + mapOf("envelope" to envelope)
        }

    @Suppress("UNCHECKED_CAST")
    private fun envelopeOf(row: Map<String, Any?>): Map<String, Any?> = row["envelope"] as Map<String, Any?>

    @Suppress("UNCHECKED_CAST")
    private fun payloadOf(row: Map<String, Any?>): Map<String, Any?> =
        envelopeOf(row)["payload"] as Map<String, Any?>

    private fun stubKafkaSuccess() {
        every {
            kafkaTemplate.send(any<String>(), any<String>(), any<String>())
        } returns CompletableFuture.completedFuture(mockk<SendResult<String, String>>(relaxed = true))
    }

    @Test
    fun `tenant lifecycle transitions write durable outbox rows with Contract 5 envelopes`() {
        val jwt = AuthFlows.adminJwt(http)
        val tenantId = createTenant(jwt, "outbox-lifecycle")

        // created — written in the same transaction as the INSERT.
        val created = outboxRows(AuthTopics.TENANT_CREATED).single { it["partition_key"] == tenantId }
        assertThat(created["published_at"]).isNull()
        assertThat(created["attempts"]).isEqualTo(0)
        val createdEnvelope = envelopeOf(created)
        assertThat(createdEnvelope["event_type"]).isEqualTo(AuthTopics.TENANT_CREATED)
        assertThat(createdEnvelope["producer_service"]).isEqualTo("auth")
        assertThat(createdEnvelope["tenant_id"]).isEqualTo(tenantId)
        assertThat(createdEnvelope["partition_key"]).isEqualTo(tenantId)
        assertThat(createdEnvelope["event_id"]).isNotNull()
        val createdPayload = payloadOf(created)
        assertThat(createdPayload["tenant_id"]).isEqualTo(tenantId)
        assertThat(createdPayload["plan"]).isEqualTo("free")
        assertThat(createdPayload["source"]).isEqualTo("manual-seed")

        // suspended (carries the reason).
        val suspend = http.exchange(
            HttpMethod.PATCH,
            "/v1/admin/tenants/$tenantId/suspend",
            body = mapOf("reason" to "billing-overdue"),
            headers = adminHeaders(jwt),
        )
        assertThat(suspend.statusCode).isEqualTo(HttpStatus.OK)
        val suspended = outboxRows(AuthTopics.TENANT_SUSPENDED).single { it["partition_key"] == tenantId }
        assertThat(payloadOf(suspended)["reason"]).isEqualTo("billing-overdue")

        // resumed.
        val resume = http.exchange(
            HttpMethod.PATCH,
            "/v1/admin/tenants/$tenantId/resume",
            headers = adminHeaders(jwt),
        )
        assertThat(resume.statusCode).isEqualTo(HttpStatus.OK)
        assertThat(outboxRows(AuthTopics.TENANT_RESUMED).filter { it["partition_key"] == tenantId }).hasSize(1)
    }

    @Test
    fun `soft-delete emits pending_deletion with grace_until - tenant deleted stays reserved`() {
        val jwt = AuthFlows.adminJwt(http)
        val tenantId = createTenant(jwt, "outbox-softdelete")

        val del = http.delete("/v1/admin/tenants/$tenantId", headers = adminHeaders(jwt))
        assertThat(del.statusCode).isEqualTo(HttpStatus.OK)
        assertThat(del.body!!["status"]).isEqualTo("pending_deletion")

        // Event fidelity (amended 2026-06): pending_deletion fires; tenant.deleted does NOT.
        val pending = outboxRows(AuthTopics.TENANT_PENDING_DELETION).single { it["partition_key"] == tenantId }
        val graceUntil = Instant.parse(payloadOf(pending)["grace_until"] as String)
        // Configured grace window is 30 days (test profile); allow generous clock slack.
        val expected = Instant.now().plusSeconds(30L * 24 * 3600)
        assertThat(graceUntil).isBetween(expected.minusSeconds(600), expected.plusSeconds(600))
        assertThat(outboxRows(AuthTopics.TENANT_DELETED)).isEmpty()
    }

    @Test
    fun `failed tenant create writes no outbox row - same transaction guarantee`() {
        val jwt = AuthFlows.adminJwt(http)
        val before = outboxRows(AuthTopics.TENANT_CREATED).size

        // The platform tenant already exists -> 409 CONFLICT; the INSERT rolled back, so the
        // outbox row written in the same transaction must roll back with it.
        val r = http.post(
            "/v1/admin/tenants",
            body = mapOf("name" to "dup", "tenant_id" to PLATFORM_TENANT.toString()),
            headers = adminHeaders(jwt),
        )
        assertThat(r.statusCode).isEqualTo(HttpStatus.CONFLICT)
        assertThat(outboxRows(AuthTopics.TENANT_CREATED)).hasSize(before)
    }

    @Test
    fun `token revoke writes the durable row and its outbox event in one transaction`() {
        val jwt = AuthFlows.adminJwt(http)
        val jti = UUID.randomUUID()

        val r = http.post(
            "/v1/tokens/revoke",
            body = mapOf("jti" to jti.toString(), "reason" to "compromised"),
            headers = adminHeaders(jwt),
        )
        assertThat(r.statusCode).isEqualTo(HttpStatus.NO_CONTENT)

        val durable = superuserJdbc().queryForObject(
            "SELECT COUNT(*) FROM auth.revoked_tokens WHERE jti = ?",
            Long::class.java,
            jti,
        )
        assertThat(durable).isEqualTo(1L)

        val row = outboxRows(AuthTopics.TOKEN_REVOKED).single()
        assertThat(row["partition_key"]).isEqualTo(PLATFORM_TENANT.toString())
        val payload = payloadOf(row)
        assertThat(payload["jti"]).isEqualTo(jti.toString())
        assertThat(payload["reason"]).isEqualTo("compromised")

        // Idempotent re-revoke: no state change -> no second event.
        val again = http.post(
            "/v1/tokens/revoke",
            body = mapOf("jti" to jti.toString(), "reason" to "compromised"),
            headers = adminHeaders(jwt),
        )
        assertThat(again.statusCode).isEqualTo(HttpStatus.NO_CONTENT)
        assertThat(outboxRows(AuthTopics.TOKEN_REVOKED)).hasSize(1)
    }

    @Test
    fun `relay publishes unpublished rows and stamps published_at`() {
        val jwt = AuthFlows.adminJwt(http)
        val tenantId = createTenant(jwt, "outbox-relay-ok")
        stubKafkaSuccess()

        val published = outboxRelay.relayOnce()

        assertThat(published).isGreaterThanOrEqualTo(1)
        verify { kafkaTemplate.send(eq(AuthTopics.TENANT_CREATED), eq(tenantId), any<String>()) }
        val row = outboxRows(AuthTopics.TENANT_CREATED).single { it["partition_key"] == tenantId }
        assertThat(row["published_at"]).isNotNull()
        // Nothing left behind, and the backoff gate is open.
        assertThat(superuserJdbc().queryForObject(
            "SELECT COUNT(*) FROM auth.outbox WHERE published_at IS NULL",
            Long::class.java,
        )).isEqualTo(0L)
        assertThat(outboxRelay.nextAttemptAt()).isEqualTo(Instant.EPOCH)
    }

    @Test
    fun `relay records failures with attempts and last_error then retries until the broker recovers`() {
        val jwt = AuthFlows.adminJwt(http)
        val tenantId = createTenant(jwt, "outbox-relay-retry")

        // Broker down: nothing publishes; the row records the failure and stays unpublished.
        every { kafkaTemplate.send(any<String>(), any<String>(), any<String>()) } throws
            RuntimeException("broker down")
        assertThat(outboxRelay.relayOnce()).isEqualTo(0)

        var row = outboxRows(AuthTopics.TENANT_CREATED).single { it["partition_key"] == tenantId }
        assertThat(row["published_at"]).isNull()
        assertThat(row["attempts"]).isEqualTo(1)
        assertThat(row["last_error"] as String).contains("broker down")
        // A fully-failed pass arms the backoff gate (tick() skips until it elapses).
        assertThat(outboxRelay.nextAttemptAt()).isAfter(Instant.now().minusSeconds(1))

        // Second failed pass: attempts keep counting — retry forever, no drop.
        assertThat(outboxRelay.relayOnce()).isEqualTo(0)
        row = outboxRows(AuthTopics.TENANT_CREATED).single { it["partition_key"] == tenantId }
        assertThat(row["attempts"]).isEqualTo(2)

        // Broker recovers: the same row finally publishes and the gate resets.
        stubKafkaSuccess()
        assertThat(outboxRelay.relayOnce()).isGreaterThanOrEqualTo(1)
        row = outboxRows(AuthTopics.TENANT_CREATED).single { it["partition_key"] == tenantId }
        assertThat(row["published_at"]).isNotNull()
        assertThat(outboxRelay.nextAttemptAt()).isEqualTo(Instant.EPOCH)
    }

    @Test
    fun `agent updated publishes to its own topic not agent deactivated`() {
        stubKafkaSuccess()
        val agentId = UUID.randomUUID()

        eventPublisher.agentUpdated(agentId = agentId, tenantId = PLATFORM_TENANT, status = "suspended")

        // Event-fidelity fix (amended 2026-06): cypherx.auth.agent.updated owns its own topic,
        // keyed by agent_id (compact topic).
        verify(exactly = 1) {
            kafkaTemplate.send(eq(AuthTopics.AGENT_UPDATED), eq(agentId.toString()), any<String>())
        }
        verify(exactly = 0) {
            kafkaTemplate.send(eq(AuthTopics.AGENT_DEACTIVATED), any<String>(), any<String>())
        }
    }
}
