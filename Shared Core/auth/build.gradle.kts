// auth-service — CypherX SharedCore. Authenticates AGENTS (not end users; end-user auth = px0).
// Stack: Kotlin + Spring Boot + Gradle (KTS), JDK 21. See archive/Manoj/stack.md.
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm") version "2.0.21"
    kotlin("plugin.spring") version "2.0.21"
    id("org.springframework.boot") version "3.3.5"
    id("io.spring.dependency-management") version "1.1.6"
}

group = "ai.cypherx"
version = "0.1.0"

java {
    toolchain { languageVersion = JavaLanguageVersion.of(21) }
}

repositories { mavenCentral() }

extra["springCloudVersion"] = "2023.0.3"

dependencies {
    // ── Web / security / observability ────────────────────────────────────────
    implementation("org.springframework.boot:spring-boot-starter-web")
    implementation("org.springframework.boot:spring-boot-starter-security")
    implementation("org.springframework.boot:spring-boot-starter-validation")
    implementation("org.springframework.boot:spring-boot-starter-actuator")
    implementation("io.micrometer:micrometer-registry-prometheus")

    // ── Persistence (plain JDBC — explicit txns for RLS SET LOCAL, Contract 13) ─
    implementation("org.springframework.boot:spring-boot-starter-jdbc")
    runtimeOnly("org.postgresql:postgresql:42.7.4")

    // ── Messaging (Kafka — Contract 5 envelopes) ───────────────────────────────
    implementation("org.springframework.kafka:spring-kafka")

    // ── Cache (Valkey == Redis wire protocol; Lettuce) ─────────────────────────
    implementation("org.springframework.boot:spring-boot-starter-data-redis")

    // ── JOSE: RS256 signing, JWK/JWKS (Contract 1) ─────────────────────────────
    implementation("com.nimbusds:nimbus-jose-jwt:9.40")

    // ── Crypto: Argon2id for service-client secrets (Contract 12 / 18) ─────────
    implementation("de.mkammerer:argon2-jvm:2.11")

    // ── KMS envelope encryption of signing keys (cloud); local provider for dev ─
    implementation("software.amazon.awssdk:kms:2.28.16")

    // ── JSON / Kotlin ──────────────────────────────────────────────────────────
    implementation("com.fasterxml.jackson.module:jackson-module-kotlin")
    implementation("org.jetbrains.kotlin:kotlin-reflect")

    // ── Structured JSON logs to stdout (Contract 6) ────────────────────────────
    implementation("net.logstash.logback:logstash-logback-encoder:8.0")

    // ── OpenAPI (Contract 10) ──────────────────────────────────────────────────
    implementation("org.springdoc:springdoc-openapi-starter-webmvc-ui:2.6.0")

    // ── Test ───────────────────────────────────────────────────────────────────
    testImplementation("org.springframework.boot:spring-boot-starter-test")
    testImplementation("org.springframework.security:spring-security-test")
    testImplementation("org.springframework.kafka:spring-kafka-test")
    testImplementation("io.mockk:mockk:1.13.13")
    testImplementation("com.ninja-squad:springmockk:4.0.2")
    testImplementation("org.testcontainers:junit-jupiter:1.20.3")
    testImplementation("org.testcontainers:postgresql:1.20.3")
    testImplementation("org.testcontainers:kafka:1.20.3")
}

dependencyManagement {
    imports { mavenBom("org.springframework.cloud:spring-cloud-dependencies:${property("springCloudVersion")}") }
}

kotlin {
    compilerOptions {
        freeCompilerArgs.addAll("-Xjsr305=strict")
        jvmTarget = JvmTarget.JVM_21
    }
}

tasks.withType<Test> {
    useJUnitPlatform()
    // Testcontainers needs the Docker daemon; skip gracefully where absent via tags.
    systemProperty("spring.profiles.active", "test")
}

tasks.named<org.springframework.boot.gradle.tasks.bundling.BootJar>("bootJar") {
    archiveFileName.set("auth-service.jar")
}
