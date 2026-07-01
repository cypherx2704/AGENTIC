package ai.cypherx.auth

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.context.properties.ConfigurationPropertiesScan
import org.springframework.boot.runApplication
import org.springframework.scheduling.annotation.EnableScheduling

/**
 * auth-service — CypherX SharedCore.
 *
 * Authenticates AGENTS (not end users — end-user auth lives in px0). Issues agent JWTs
 * (Contract 1), service tokens (Contract 12), API keys (Contract 18); publishes JWKS +
 * OIDC discovery; owns tenant lifecycle (Contract 13) and the /authorize decision.
 */
@SpringBootApplication
@ConfigurationPropertiesScan
@EnableScheduling
class AuthApplication

fun main(args: Array<String>) {
    runApplication<AuthApplication>(*args)
}
