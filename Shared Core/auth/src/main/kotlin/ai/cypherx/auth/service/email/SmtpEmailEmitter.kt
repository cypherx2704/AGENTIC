package ai.cypherx.auth.service.email

import ai.cypherx.auth.config.OnboardingProperties
import org.slf4j.LoggerFactory
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty
import org.springframework.stereotype.Component
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.Socket
import java.nio.charset.StandardCharsets
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import javax.net.ssl.SSLSocketFactory

/**
 * Real SMTP [EmailEmitter] for prod (managed relay) and local (mailhog), selected when
 * `ONBOARDING_EMAIL_PROVIDER=smtp` — see [ai.cypherx.auth.config.OnboardingProperties.emailProvider].
 *
 * Implemented directly over the SMTP wire protocol (RFC 5321) on a JDK [Socket] so the service
 * needs NO extra mail dependency on the classpath: mailhog locally (`localhost:1025`, no auth, no
 * TLS) and an authenticated/STARTTLS relay in prod both work through one small client. Host / port /
 * from / credentials / STARTTLS all come from [OnboardingProperties.Smtp] (env-driven; never
 * hardcoded). A short socket timeout keeps a wedged relay from stalling the signup request.
 *
 * Secret hygiene: the verification URL (which carries the raw token) is written into the message
 * body but is NEVER logged. Auth credentials are sent over the wire only after STARTTLS when
 * configured. A send failure throws so the onboarding service can map it to a Contract 2 error; the
 * already-committed signup row lets the user recover via `POST /v1/onboarding/resend`.
 */
@Component
@ConditionalOnProperty(
    prefix = "cypherx.auth.onboarding",
    name = ["email-provider"],
    havingValue = "smtp",
)
class SmtpEmailEmitter(
    private val props: OnboardingProperties,
) : EmailEmitter {

    override fun sendVerification(message: EmailEmitter.VerificationEmail) {
        val cfg = props.smtp
        val body = buildMessage(
            from = cfg.from,
            to = message.to,
            subject = "Verify your CypherX account",
            text = verificationBody(message),
        )
        try {
            Socket(cfg.host, cfg.port).use { socket ->
                socket.soTimeout = cfg.timeoutMs
                socket.tcpNoDelay = true
                var session = SmtpSession(socket)
                session.expect(220)
                session.command("EHLO ${ehloName(cfg.from)}", 250)

                if (cfg.startTls) {
                    session.command("STARTTLS", 220)
                    // Upgrade the socket to TLS and re-EHLO over the secure channel.
                    val tls = (SSLSocketFactory.getDefault() as SSLSocketFactory)
                        .createSocket(socket, cfg.host, cfg.port, true)
                    (tls as javax.net.ssl.SSLSocket).startHandshake()
                    session = SmtpSession(tls)
                    session.command("EHLO ${ehloName(cfg.from)}", 250)
                }

                if (cfg.username.isNotBlank()) {
                    session.command("AUTH LOGIN", 334)
                    session.command(base64(cfg.username), 334)
                    // Password is sent only here, only over TLS when startTls is on. Never logged.
                    session.command(base64(cfg.password), 235)
                }

                session.command("MAIL FROM:<${cfg.from}>", 250)
                session.command("RCPT TO:<${message.to}>", 250)
                session.command("DATA", 354)
                session.raw(body)
                session.command(".", 250)
                session.quietQuit()
            }
            log.info("smtp-email: sent verification email to {} for tenant '{}'", message.to, message.tenantName)
        } catch (ex: Exception) {
            // Do NOT include the body/URL in the message (token hygiene). Surface a clean failure.
            log.warn("smtp-email: send to {} failed: {}", message.to, ex.message)
            throw IllegalStateException("Failed to send verification email", ex)
        }
    }

    /** Plain-text verification body. The URL carries the raw token — keep it out of logs. */
    private fun verificationBody(m: EmailEmitter.VerificationEmail): String =
        """
        Welcome to CypherX!

        You're almost done setting up '${m.tenantName}'. Confirm your email to finish:

        ${m.verificationUrl}

        If you didn't request this, you can safely ignore this message.
        """.trimIndent()

    /** RFC 5322 message with minimal headers (Date / From / To / Subject). CRLF line endings. */
    private fun buildMessage(from: String, to: String, subject: String, text: String): String {
        val date = DateTimeFormatter.RFC_1123_DATE_TIME.format(ZonedDateTime.now())
        val headers = listOf(
            "Date: $date",
            "From: $from",
            "To: $to",
            "Subject: $subject",
            "MIME-Version: 1.0",
            "Content-Type: text/plain; charset=UTF-8",
        ).joinToString("\r\n")
        // Dot-stuff any line that begins with '.' so DATA termination is unambiguous (RFC 5321 §4.5.2).
        val safeBody = text.replace("\r\n", "\n").split("\n").joinToString("\r\n") { line ->
            if (line.startsWith(".")) ".$line" else line
        }
        return "$headers\r\n\r\n$safeBody\r\n"
    }

    private fun ehloName(from: String): String =
        from.substringAfter('@', "cypherx.local").ifBlank { "cypherx.local" }

    private fun base64(s: String): String =
        java.util.Base64.getEncoder().encodeToString(s.toByteArray(StandardCharsets.UTF_8))

    /** Minimal line-oriented SMTP session over a socket: send a command, assert the reply code. */
    private class SmtpSession(socket: Socket) {
        private val reader: BufferedReader =
            BufferedReader(InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8))
        private val out: OutputStream = socket.getOutputStream()

        /** Read one (possibly multi-line) SMTP reply and assert its 3-digit code equals [expected]. */
        fun expect(expected: Int) {
            var line = reader.readLine() ?: throw IllegalStateException("SMTP: connection closed")
            // Multi-line replies use "<code>-<text>"; the final line uses "<code> <text>".
            while (line.length >= 4 && line[3] == '-') {
                line = reader.readLine() ?: break
            }
            val code = line.take(3).toIntOrNull()
                ?: throw IllegalStateException("SMTP: malformed reply '$line'")
            if (code != expected) throw IllegalStateException("SMTP: expected $expected, got '$line'")
        }

        /** Send a command line (CRLF-terminated) and assert the reply [expected]. */
        fun command(line: String, expected: Int) {
            out.write((line + "\r\n").toByteArray(StandardCharsets.UTF_8))
            out.flush()
            expect(expected)
        }

        /** Write the DATA payload verbatim (no reply read here — the trailing "." asserts 250). */
        fun raw(payload: String) {
            out.write(payload.toByteArray(StandardCharsets.UTF_8))
            out.flush()
        }

        /** Best-effort QUIT; ignore the reply (the message is already accepted). */
        fun quietQuit() {
            runCatching {
                out.write("QUIT\r\n".toByteArray(StandardCharsets.UTF_8))
                out.flush()
            }
        }
    }

    private companion object {
        val log = LoggerFactory.getLogger(SmtpEmailEmitter::class.java)
    }
}
