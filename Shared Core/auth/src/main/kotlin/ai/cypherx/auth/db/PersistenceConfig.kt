package ai.cypherx.auth.db

import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.transaction.PlatformTransactionManager
import org.springframework.transaction.support.TransactionTemplate

/**
 * Persistence wiring. Spring Boot auto-configures the [PlatformTransactionManager]
 * (DataSourceTransactionManager) and [org.springframework.jdbc.core.JdbcTemplate] /
 * [org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate] from the configured
 * DataSource; here we only add the [TransactionTemplate] that [TenantTx] depends on.
 */
@Configuration
class PersistenceConfig {

    @Bean
    fun transactionTemplate(txManager: PlatformTransactionManager): TransactionTemplate =
        TransactionTemplate(txManager)
}
