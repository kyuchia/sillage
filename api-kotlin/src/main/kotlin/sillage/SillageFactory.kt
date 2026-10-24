package sillage

import com.zaxxer.hikari.HikariConfig
import com.zaxxer.hikari.HikariDataSource
import io.micronaut.context.annotation.Bean
import io.micronaut.context.annotation.Factory
import io.micronaut.context.annotation.Value
import jakarta.inject.Singleton

/**
 * Wires the framework-free classes together. Micronaut resolves this at compile time
 * (KSP generates the bean definitions), so there is no classpath scanning or
 * reflection at startup.
 */
@Factory
class SillageFactory {

    /**
     * A Hikari pool built from the libpq-style `PG_DSN`, so one environment variable
     * drives both services. Built lazily (`initializationFailTimeout = -1`): the app
     * still starts when Postgres is down, and /api/health answers 503 instead.
     */
    @Singleton
    @Bean(preDestroy = "close")
    fun dataSource(@Value("\${sillage.pg-dsn}") dsn: String): HikariDataSource {
        val target = libpqToJdbc(dsn)
        val config = HikariConfig().apply {
            jdbcUrl = target.url
            dataSourceProperties = target.properties
            maximumPoolSize = 8
            minimumIdle = 1
            isAutoCommit = false
            connectionTimeout = POOL_WAIT_MILLIS
            initializationFailTimeout = -1
            poolName = "sillage"
        }
        return HikariDataSource(config)
    }

    @Singleton
    fun sillageService(
        dataSource: HikariDataSource,
        @Value("\${sillage.pg-dsn}") dsn: String,
        @Value("\${sillage.max-points}") maxPoints: Int,
    ): SillageService = SillageService(Database(dataSource::getConnection, dsn), maxPoints, dsn)
}
