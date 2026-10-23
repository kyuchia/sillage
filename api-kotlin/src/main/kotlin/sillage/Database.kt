package sillage

import java.net.URI
import java.net.URLDecoder
import java.sql.Connection
import java.sql.SQLException
import java.util.Properties

/**
 * Something that hands out a JDBC connection: a Hikari pool in the app, a plain
 * DriverManager call in a quick check. The query code depends on this interface, not
 * on the pool (dependency inversion), so it can be exercised without Micronaut.
 */
fun interface ConnectionSource {
    fun open(): Connection
}

/**
 * Borrows a connection per request, in Montréal time, and always rolls back.
 *
 * Same contract as `db_conn()` in api/main.py: every query is read-only, so the
 * transaction is rolled back on the way out either way, and `SET LOCAL` (F7) therefore
 * ends with it — a pooled connection is never left in a changed time zone.
 */
class Database(private val source: ConnectionSource, private val dsnLabel: String) {

    fun <T> readOnly(block: (Connection) -> T): T {
        val conn = try {
            source.open()
        } catch (e: SQLException) {
            throw ApiException(503, "cannot connect to PostgreSQL ($dsnLabel): ${e.message}")
        }
        conn.use { c ->
            c.autoCommit = false
            try {
                c.createStatement().use { it.execute("SET LOCAL TIME ZONE '${MONTREAL.id}'") }
                return block(c)
            } finally {
                c.rollback()
            }
        }
    }
}

/** A JDBC URL plus the properties the driver needs (user, password, options …). */
data class JdbcTarget(val url: String, val properties: Properties)

/**
 * Turn the same `PG_DSN` the Python service reads into a JDBC target, so one
 * environment variable configures both services (and the parity test).
 *
 * Accepts libpq keyword strings (`dbname=sillage`, `host=localhost user=postgres
 * options='-c TimeZone=UTC'`) and `postgresql://` URIs. Missing values fall back the way
 * libpq does: PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE, then localhost:5432 and the OS
 * user. JDBC cannot use a Unix socket, so a socket-directory host becomes localhost.
 */
fun libpqToJdbc(dsn: String, env: Map<String, String> = System.getenv()): JdbcTarget {
    val kv = if (dsn.startsWith("postgres://") || dsn.startsWith("postgresql://")) {
        parseUri(dsn)
    } else {
        parseKeywords(dsn)
    }

    val host = (kv["host"] ?: env["PGHOST"] ?: "localhost").let { if (it.startsWith("/")) "localhost" else it }
    val port = kv["port"] ?: env["PGPORT"] ?: "5432"
    val user = kv["user"] ?: env["PGUSER"] ?: System.getProperty("user.name")
    val dbname = kv["dbname"] ?: env["PGDATABASE"] ?: user

    val props = Properties()
    props["user"] = user
    (kv["password"] ?: env["PGPASSWORD"])?.let { props["password"] = it }
    kv["options"]?.let { props["options"] = it }
    kv["sslmode"]?.let { props["sslmode"] = it }
    kv["connect_timeout"]?.let { props["connectTimeout"] = it }
    kv["application_name"]?.let { props["ApplicationName"] = it }

    return JdbcTarget("jdbc:postgresql://$host:$port/$dbname", props)
}

/** libpq `key=value` pairs; values may be single-quoted with \' and \\ escapes. */
internal fun parseKeywords(dsn: String): Map<String, String> {
    val out = linkedMapOf<String, String>()
    var i = 0
    fun skipSpaces() { while (i < dsn.length && dsn[i].isWhitespace()) i++ }

    while (true) {
        skipSpaces()
        if (i >= dsn.length) break
        val keyStart = i
        while (i < dsn.length && dsn[i] != '=' && !dsn[i].isWhitespace()) i++
        val key = dsn.substring(keyStart, i)
        skipSpaces()
        require(i < dsn.length && dsn[i] == '=') { "missing \"=\" after \"$key\" in connection string" }
        i++
        skipSpaces()
        val value = StringBuilder()
        if (i < dsn.length && dsn[i] == '\'') {
            i++
            while (i < dsn.length && dsn[i] != '\'') {
                if (dsn[i] == '\\' && i + 1 < dsn.length) i++
                value.append(dsn[i++])
            }
            require(i < dsn.length) { "unterminated quoted string in connection string" }
            i++
        } else {
            while (i < dsn.length && !dsn[i].isWhitespace()) {
                if (dsn[i] == '\\' && i + 1 < dsn.length) i++
                value.append(dsn[i++])
            }
        }
        out[key] = value.toString()
    }
    return out
}

private fun parseUri(dsn: String): Map<String, String> {
    val uri = URI(dsn)
    val out = linkedMapOf<String, String>()
    fun dec(s: String) = URLDecoder.decode(s, Charsets.UTF_8)
    uri.host?.let { out["host"] = it }
    if (uri.port != -1) out["port"] = uri.port.toString()
    uri.rawUserInfo?.let { info ->
        out["user"] = dec(info.substringBefore(':'))
        if (':' in info) out["password"] = dec(info.substringAfter(':'))
    }
    uri.rawPath?.removePrefix("/")?.takeIf { it.isNotEmpty() }?.let { out["dbname"] = dec(it) }
    uri.rawQuery?.split('&')?.filter { '=' in it }?.forEach {
        out[dec(it.substringBefore('='))] = dec(it.substringAfter('='))
    }
    return out
}
