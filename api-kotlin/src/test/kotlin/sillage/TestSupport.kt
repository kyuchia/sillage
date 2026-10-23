package sillage

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import io.micronaut.runtime.server.EmbeddedServer
import io.micronaut.test.support.TestPropertyProvider
import jakarta.inject.Inject
import java.net.URI
import java.net.URLEncoder
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.sql.Connection
import java.sql.DriverManager

/** Every number here comes from tests/fixtures/seed.sql — same as SEED in tests/conftest.py. */
object Seed {
    const val WINDOW_START = "2026-08-20T08:00:00-04:00"
    const val WINDOW_END = "2026-08-20T08:10:00-04:00"
    const val NIGHT_END = "2026-08-21T00:05:00-04:00"
    const val BUS_ROWS = 118L
    const val AIRCRAFT_ROWS = 46L
    val BUS_TRIPS = mapOf("B1" to 31, "B2" to 31, "B4" to 9, "B5" to 4, "B7" to 4, "B8" to 4)
    val AIRCRAFT_TRIPS = mapOf("c0ffee" to 31, "def456" to 5)
    val WINDOW = mapOf("start" to WINDOW_START, "end" to WINDOW_END)
}

/**
 * Loads db/schema.sql and the seed into the throwaway test database, once per JVM.
 *
 * The seed TRUNCATEs both tables, so — like tests/conftest.py — this refuses a
 * database called `sillage`, or one holding more rows than a seed ever could.
 */
object TestDatabase {
    val dsn: String = System.getenv("SILLAGE_TEST_DSN") ?: "dbname=sillage_test"
    private const val GUARD_MAX_ROWS = 10_000

    // Gradle runs the tests from api-kotlin/, so the repository root is one level up.
    private val root: Path = Path.of("..").toAbsolutePath().normalize()
    private var seeded = false

    fun connect(): Connection {
        val target = libpqToJdbc(dsn)
        return DriverManager.getConnection(target.url, target.properties)
    }

    @Synchronized
    fun seed() {
        if (seeded) return
        connect().use { conn ->
            conn.autoCommit = true
            guard(conn)
            val schema = Files.readAllLines(root.resolve("db/schema.sql"))
                .filterNot { it.startsWith("\\") }          // psql meta-commands are not SQL
                .joinToString("\n")
            conn.createStatement().use { st ->
                st.execute(schema)
                st.execute(Files.readString(root.resolve("tests/fixtures/seed.sql")))
            }
        }
        seeded = true
    }

    private fun guard(conn: Connection) {
        conn.createStatement().use { st ->
            val name = st.executeQuery("SELECT current_database()").use { it.next(); it.getString(1) }
            check(name != "sillage") { "refusing to seed the recorded archive ('$name'); set SILLAGE_TEST_DSN" }
            for (table in listOf("vehicle_positions", "aircraft_positions")) {
                val exists = st.executeQuery("SELECT to_regclass('$table')").use { it.next(); it.getString(1) != null }
                if (!exists) continue
                val n = st.executeQuery("SELECT COUNT(*) FROM (SELECT 1 FROM $table LIMIT ${GUARD_MAX_ROWS + 1}) s")
                    .use { it.next(); it.getLong(1) }
                check(n <= GUARD_MAX_ROWS) { "$name.$table holds more than $GUARD_MAX_ROWS rows — not a test database" }
            }
        }
    }

    fun count(table: String): Long =
        connect().use { c -> c.createStatement().use { st -> st.executeQuery("SELECT COUNT(*) FROM $table").use { it.next(); it.getLong(1) } } }
}

/** A response, with the body parsed lazily so non-JSON bodies can still be inspected. */
class Reply(val status: Int, val headers: java.net.http.HttpHeaders, val text: String) {
    val json: JsonNode by lazy { MAPPER.readTree(text) }
    val detail: String get() = json["detail"].asText()
    fun header(name: String): String? = headers.firstValue(name).orElse(null)

    companion object {
        val MAPPER = ObjectMapper()
    }
}

/**
 * Base for the HTTP tests: seeds the database, points the app at it, and talks to the
 * embedded server with the JDK's own client — so headers (gzip, CORS) are seen exactly
 * as a browser would see them, not after a framework client has decoded them.
 *
 * Subclasses carry `@MicronautTest` and `@TestInstance(PER_CLASS)` themselves.
 */
abstract class ApiTestBase : TestPropertyProvider {

    @Inject
    lateinit var server: EmbeddedServer

    private val http: HttpClient = HttpClient.newHttpClient()

    /** The database DSN this test class's application uses. */
    open fun dsn(): String = TestDatabase.dsn

    /** Extra configuration for this test class's application context. */
    open fun extraProperties(): Map<String, String> = emptyMap()

    override fun getProperties(): MutableMap<String, String> {
        TestDatabase.seed()
        return (mapOf("sillage.pg-dsn" to dsn()) + extraProperties()).toMutableMap()
    }

    fun get(path: String, params: Map<String, String> = emptyMap(), headers: Map<String, String> = emptyMap()): Reply =
        send("GET", path, params, headers)

    fun send(method: String, path: String, params: Map<String, String> = emptyMap(), headers: Map<String, String> = emptyMap()): Reply {
        val query = params.entries.joinToString("&") { (k, v) ->
            URLEncoder.encode(k, Charsets.UTF_8) + "=" + URLEncoder.encode(v, Charsets.UTF_8)
        }
        val uri = URI.create(server.url.toString() + path + if (query.isEmpty()) "" else "?$query")
        val request = HttpRequest.newBuilder(uri).method(method, HttpRequest.BodyPublishers.noBody())
        headers.forEach { (k, v) -> request.header(k, v) }
        val response = http.send(request.build(), HttpResponse.BodyHandlers.ofByteArray())
        val bytes = response.body()
        val text = if (response.headers().firstValue("content-encoding").orElse("") == "gzip") {
            java.util.zip.GZIPInputStream(bytes.inputStream()).readBytes().decodeToString()
        } else {
            bytes.decodeToString()
        }
        return Reply(response.statusCode(), response.headers(), text)
    }
}
