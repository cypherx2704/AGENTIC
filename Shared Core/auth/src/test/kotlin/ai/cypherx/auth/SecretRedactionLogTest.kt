package ai.cypherx.auth

import ai.cypherx.auth.support.AbstractIntegrationTest
import net.logstash.logback.argument.StructuredArguments
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.extension.ExtendWith
import org.slf4j.LoggerFactory
import org.springframework.boot.test.system.CapturedOutput
import org.springframework.boot.test.system.OutputCaptureExtension

/**
 * Secret-redaction fixture (Phase 2 checklist: "Secret redaction policy enforced in logger
 * middleware + CI test").
 *
 * Boots the real Spring context so the REAL logback-spring.xml pipeline runs — LogstashEncoder +
 * MaskingJsonGeneratorDecorator configured from `cypherx.auth.logging.*` — then deliberately logs
 * secrets and asserts they reach stdout MASKED. Covers all three leak shapes:
 *  - secret-shaped `name=value` fragments inside the message text,
 *  - bare `Bearer <token>` fragments,
 *  - structured-argument JSON fields whose NAME is secret-like (api_key, password, ...).
 *
 * Logs at ERROR so the test profile's WARN root threshold never filters the fixture lines.
 */
@ExtendWith(OutputCaptureExtension::class)
class SecretRedactionLogTest : AbstractIntegrationTest() {

    private val log = LoggerFactory.getLogger(SecretRedactionLogTest::class.java)

    @Test
    fun `secret key=value fragments in the message text are masked`(output: CapturedOutput) {
        log.error("key issued api_key=cx_test_supersecret123 for tenant t1")
        log.error("upstream call failed password: hunter2-do-not-log")

        assertThat(output.out).contains("[REDACTED]")
        assertThat(output.out).doesNotContain("cx_test_supersecret123")
        assertThat(output.out).doesNotContain("hunter2-do-not-log")
    }

    @Test
    fun `authorization headers and bearer tokens are masked`(output: CapturedOutput) {
        log.error("rejected header Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.fixture.sig")
        log.error("retrying with Bearer raw.bearer.fixture-token")

        assertThat(output.out).contains("[REDACTED]")
        assertThat(output.out).doesNotContain("eyJhbGciOiJSUzI1NiJ9.fixture.sig")
        assertThat(output.out).doesNotContain("raw.bearer.fixture-token")
    }

    @Test
    fun `structured-argument fields with secret-like names are masked`(output: CapturedOutput) {
        log.error(
            "issuing credentials {} {}",
            StructuredArguments.keyValue("api_key", "structured-secret-material-000"),
            StructuredArguments.keyValue("client_secret", "structured-secret-material-111"),
        )

        assertThat(output.out).contains("[REDACTED]")
        assertThat(output.out).doesNotContain("structured-secret-material-000")
        assertThat(output.out).doesNotContain("structured-secret-material-111")
    }

    @Test
    fun `ordinary fields and messages are not over-masked`(output: CapturedOutput) {
        log.error("agent registered name=worker version=1.0.0 tokens_issued_per_min=60")

        assertThat(output.out).contains("name=worker")
        assertThat(output.out).contains("version=1.0.0")
        assertThat(output.out).contains("tokens_issued_per_min=60")
    }
}
