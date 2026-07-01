# Multi-stage: build (gradle) -> runtime (jre). Non-root, distroless-ish.
# ── Build ──────────────────────────────────────────────────────────────────────
FROM eclipse-temurin:21-jdk AS build
WORKDIR /workspace
COPY gradlew settings.gradle.kts build.gradle.kts gradle.properties ./
COPY gradle ./gradle
# Warm the dependency cache (best-effort; ignored if offline).
RUN ./gradlew --no-daemon dependencies >/dev/null 2>&1 || true
COPY src ./src
RUN ./gradlew --no-daemon clean bootJar -x test

# ── Runtime ────────────────────────────────────────────────────────────────────
FROM eclipse-temurin:21-jre AS runtime
WORKDIR /app
# curl is used only by the container HEALTHCHECK below (the JRE image has no curl).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
# Non-root (Contract / base-chart security posture).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
COPY --from=build /workspace/build/libs/auth-service.jar app.jar
USER 10001
EXPOSE 8080
ENV JAVA_OPTS="-XX:MaxRAMPercentage=75.0"
# SPRING_PROFILES_ACTIVE defaults to 'local'; deployments override to '' (cloud/default profile)
# and supply DATABASE_URL (JDBC Neon URL) + the cloud secrets. Overridable at run time.
ENV SPRING_PROFILES_ACTIVE="local"
# Contract 7 readiness: /readyz checks DB SELECT 1 + signing-key encryptor. Generous start-period
# tolerates Neon pooled-endpoint cold-start (auto-suspend) and the lazy signing-key bootstrap.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
  CMD curl -fsS http://localhost:8080/readyz || exit 1
ENTRYPOINT ["sh","-c","java $JAVA_OPTS -jar app.jar"]
