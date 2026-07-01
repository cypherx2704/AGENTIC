package ai.cypherx.auth.support

import com.ninjasquad.springmockk.MockkBean
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.datasource.DriverManagerDataSource
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.test.context.ActiveProfiles
import org.springframework.test.context.DynamicPropertyRegistry
import org.springframework.test.context.DynamicPropertySource
import org.testcontainers.containers.PostgreSQLContainer
import org.testcontainers.utility.DockerImageName

/**
 * Base class for every integration test. DUAL-MODE Postgres:
 *
 *  - **External** (env `CYPHERX_TEST_DB_URL` set): connect to an already-migrated Postgres (the
 *    local dev/local stack on Windows, where the Testcontainers↔Docker-Desktop npipe bridge is
 *    unreliable). Schema/roles/migrations are assumed already applied (Phase-1 bootstrap); nothing
 *    is created here. Superuser + app creds come from env (defaults: cypherx_admin / auth_user).
 *  - **Testcontainers** (no env): start a singleton `pgvector/pgvector:pg16` container, create the
 *    `auth_user`/`auth_ddl` roles (LOGIN, NOSUPERUSER, NOBYPASSRLS so RLS is enforced — Contract 13)
 *    and apply the Atlas migrations. This is the hermetic path used in Linux CI.
 *
 * The app connects as the NON-superuser runtime role so Row Level Security is actually enforced.
 * Tests that need RLS-bypassing access (reset, cross-tenant seeding) use [superuserJdbc]; the RLS
 * cross-tenant denial test uses [authUserJdbc].
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
abstract class AbstractIntegrationTest {

    @Autowired
    protected lateinit var jdbc: JdbcTemplate

    /** Relaxed mock so the context needs no Kafka broker (AuthEventPublisher sends best-effort). */
    @Suppress("unused")
    @MockkBean(relaxed = true)
    protected lateinit var kafkaTemplate: KafkaTemplate<String, String>

    /** Relaxed mock so the context needs no Valkey/Redis (authorize cache + jti checks fail-open). */
    @Suppress("unused")
    @MockkBean(relaxed = true)
    protected lateinit var stringRedisTemplate: StringRedisTemplate

    companion object {

        private const val DB_NAME = "cypherx_platform"

        private val EXTERNAL_URL: String? = System.getenv("CYPHERX_TEST_DB_URL")?.takeIf { it.isNotBlank() }
        private val EXTERNAL: Boolean = EXTERNAL_URL != null

        // Resolved connection settings (populated in init for whichever mode is active).
        private val baseUrl: String
        private val suUser: String
        private val suPw: String
        private val appUser: String
        private val appPw: String

        /** Non-null only in Testcontainers mode (kept referenced so the JVM does not GC it). */
        @JvmStatic
        private val POSTGRES: PostgreSQLContainer<*>?

        init {
            if (EXTERNAL) {
                baseUrl = EXTERNAL_URL!!
                suUser = System.getenv("CYPHERX_TEST_DB_SU_USER") ?: "cypherx_admin"
                suPw = System.getenv("CYPHERX_TEST_DB_SU_PW") ?: "localdev"
                appUser = System.getenv("CYPHERX_TEST_DB_APP_USER") ?: "auth_user"
                appPw = System.getenv("CYPHERX_TEST_DB_APP_PW") ?: "localdev"
                POSTGRES = null
                // dev/local schema + roles + migrations are already applied — nothing to bootstrap.
            } else {
                val pg = PostgreSQLContainer(
                    DockerImageName.parse("pgvector/pgvector:pg16").asCompatibleSubstituteFor("postgres"),
                )
                    .withDatabaseName(DB_NAME)
                    .withUsername("postgres")
                    .withPassword("postgres")
                    .withReuse(false)
                pg.start()
                POSTGRES = pg
                baseUrl = pg.jdbcUrl
                suUser = pg.username
                suPw = pg.password
                appUser = "auth_user"
                appPw = "auth_user_pw"
                bootstrapDatabase()
            }
        }

        private fun withSchema(url: String): String =
            if (url.contains("?")) "$url&currentSchema=auth" else "$url?currentSchema=auth"

        private fun dsOf(url: String, user: String, pw: String): DriverManagerDataSource =
            DriverManagerDataSource().apply {
                setDriverClassName("org.postgresql.Driver")
                this.url = url
                username = user
                password = pw
            }

        /** Testcontainers-only: create roles + apply migrations as the container superuser. */
        private fun bootstrapDatabase() {
            val sj = JdbcTemplate(dsOf(baseUrl, suUser, suPw))
            createRole(sj, appUser, appPw)
            createRole(sj, "auth_ddl", "auth_ddl_pw")
            runSqlFile(sj, "20260606_0001__init.sql")
            runSqlFile(sj, "20260606_0002__seed.sql")
            runSqlFile(sj, "20260610_0003__outbox.sql")
            runSqlFile(sj, "20260610_0004__wp03_auth_completion.sql")
            runSqlFile(sj, "20260611_0006__onboarding.sql")
            runSqlFile(sj, "20260611_0007__webhooks.sql")
            runSqlFile(sj, "20260611_0008__audit_pipeline.sql")
            sj.execute("ALTER ROLE $appUser NOSUPERUSER NOBYPASSRLS LOGIN")
        }

        private fun createRole(sj: JdbcTemplate, role: String, password: String) {
            sj.execute(
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') THEN
                    CREATE ROLE $role LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '$password';
                  ELSE
                    ALTER ROLE $role LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '$password';
                  END IF;
                END
                $$;
                """.trimIndent(),
            )
        }

        private fun runSqlFile(sj: JdbcTemplate, fileName: String) {
            sj.execute(readMigration(fileName))
        }

        private fun readMigration(fileName: String): String {
            val candidates = listOf(
                java.nio.file.Paths.get("db", "migrations", fileName),
                java.nio.file.Paths.get("..", "db", "migrations", fileName),
            )
            val path = candidates.firstOrNull { java.nio.file.Files.exists(it) }
                ?: error("migration file not found: $fileName (cwd=${java.nio.file.Paths.get("").toAbsolutePath()})")
            return java.nio.file.Files.readString(path)
        }

        /** Superuser JdbcTemplate (BYPASSRLS) for state reset / cross-tenant seeding. */
        @JvmStatic
        fun superuserJdbc(): JdbcTemplate = JdbcTemplate(dsOf(withSchema(baseUrl), suUser, suPw))

        /** NON-superuser (auth_user) JdbcTemplate — RLS IS enforced. For the cross-tenant denial test. */
        @JvmStatic
        fun authUserJdbc(): JdbcTemplate = JdbcTemplate(dsOf(withSchema(baseUrl), appUser, appPw))

        @JvmStatic
        @DynamicPropertySource
        fun datasourceProps(registry: DynamicPropertyRegistry) {
            registry.add("spring.datasource.url") { withSchema(baseUrl) }
            registry.add("spring.datasource.username") { appUser }
            registry.add("spring.datasource.password") { appPw }
        }
    }

    /**
     * Reset mutable DB state between tests via a SUPERUSER connection (bypasses RLS / append-only
     * grant). Leaves the seeded tenants/policies/service_acl + signing keys intact.
     */
    protected fun resetState() {
        val su = superuserJdbc()
        su.execute("TRUNCATE auth.agents CASCADE")
        su.update("DELETE FROM auth.bootstrap_state")
        su.execute("TRUNCATE auth.audit_log")
        su.execute("TRUNCATE auth.outbox")
        su.execute("TRUNCATE auth.revoked_tokens")
    }
}
