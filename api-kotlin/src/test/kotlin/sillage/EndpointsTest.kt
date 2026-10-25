package sillage

import io.micronaut.test.extensions.junit5.annotation.MicronautTest
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestInstance
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.ValueSource
import java.time.OffsetDateTime

/** /api/range, /api/health, CORS. Ported from tests/api/test_endpoints.py. */
@MicronautTest
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class EndpointsTest : ApiTestBase() {

    private fun instant(s: String) = OffsetDateTime.parse(s).toInstant()

    @Test
    fun `range reports rows and extent per layer`() {
        val layers = get("/api/range").json["layers"]
        assertEquals("vehicle_positions", layers["bus"]["table"].asText())
        assertEquals(Seed.BUS_ROWS, layers["bus"]["rows"].asLong())
        assertEquals(Seed.AIRCRAFT_ROWS, layers["aircraft"]["rows"].asLong())
        assertEquals(instant(Seed.WINDOW_START), instant(layers["bus"]["min_time"].asText()))
        assertEquals(instant(Seed.NIGHT_END), instant(layers["bus"]["max_time"].asText()))
    }

    @Test
    fun `overlap is the intersection of the layers`() {
        val overlap = get("/api/range").json["overlap"]
        assertEquals(instant(Seed.WINDOW_START), instant(overlap["start"].asText()))
        assertEquals(instant(Seed.WINDOW_END), instant(overlap["end"].asText()))
    }

    @Test
    fun `busiest hour is the densest bucket across layers`() {
        val busiest = get("/api/range").json["busiest_hour"]
        assertEquals(Seed.BUS_ROWS - 31 + Seed.AIRCRAFT_ROWS, busiest["rows"].asLong())   // all but night route B6
        assertEquals("2026-08-20T08:00:00-04:00", busiest["start"].asText())
        assertEquals("2026-08-20T09:00:00-04:00", busiest["end"].asText())
    }

    @Test
    fun `health reports the database up`() {
        val r = get("/api/health")
        assertEquals(200, r.status)
        assertEquals("ok", r.json["status"].asText())
        assertEquals("up", r.json["database"].asText())
    }

    @ParameterizedTest
    @ValueSource(strings = ["http://localhost:5173", "http://127.0.0.1:8000"])
    fun `CORS allows localhost`(origin: String) {
        assertEquals(origin, get("/api/health", headers = mapOf("Origin" to origin)).header("access-control-allow-origin"))
    }

    @ParameterizedTest
    @ValueSource(strings = ["https://evil.example", "http://localhost.evil.example"])
    fun `CORS refuses other origins`(origin: String) {
        assertNull(get("/api/health", headers = mapOf("Origin" to origin)).header("access-control-allow-origin"))
    }

    @Test
    fun `the API is read-only`() {
        assertEquals(405, send("POST", "/api/trips").status)
    }
}

/**
 * Offset-less start/end mean Montréal time whatever the server's TimeZone is (F7).
 * The DSN forces the session to Asia/Taipei; the app must override it per request.
 */
@MicronautTest
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ServerTimeZoneTest : ApiTestBase() {

    override fun dsn() = "${TestDatabase.dsn} options='-c TimeZone=Asia/Taipei'"

    @Test
    fun `naive timestamps are Montreal time on a server in Taipei`() {
        val r = get("/api/trips", mapOf("layer" to "bus", "start" to "2026-08-20 08:00", "end" to "2026-08-20 08:10"))
        assertEquals(200, r.status, r.text)
        assertEquals(Seed.BUS_TRIPS.keys, r.json["trips"].map { it["id"].asText() }.toSet())
        assertEquals("2026-08-20T08:00:00-04:00", r.json["start_time"].asText())
    }
}

/** Postgres unreachable: health degrades to 503, data endpoints answer 503 not 500. */
@MicronautTest
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DatabaseDownTest : ApiTestBase() {

    override fun dsn() = "host=127.0.0.1 port=1 dbname=nowhere connect_timeout=1"

    @Test
    fun `health degrades to 503`() {
        val r = get("/api/health")
        assertEquals(503, r.status)
        assertEquals("degraded", r.json["status"].asText())
        assertEquals("down", r.json["database"].asText())
    }

    @ParameterizedTest
    @ValueSource(strings = ["/api/range", "/api/trips"])
    fun `data endpoints answer 503`(path: String) {
        val r = get(path, mapOf("layer" to "bus"))
        assertEquals(503, r.status)
        assert("cannot connect to PostgreSQL" in r.detail) { r.detail }
    }
}

/**
 * F17: Postgres goes away *after* the pool is warm. Hikari's borrow-time validation has
 * a bypass window for a connection used moments ago, so the failure lands inside
 * readOnly() rather than at borrow time. Mirrors
 * test_health_degrades_to_503_when_postgres_dies_after_startup in tests/api/.
 */
@MicronautTest
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DatabaseDiesMidLifeTest : ApiTestBase() {

    /** A distinctive application_name so the test can find this app's own sessions. */
    override fun dsn() = "${TestDatabase.dsn} application_name=$APP_NAME"

    @Test
    fun `health degrades to 503 when the server goes away, then recovers`() {
        assertEquals(200, get("/api/health").status)
        assertTrue(terminateAppBackends() > 0, "the pool held no session to terminate")

        val r = get("/api/health")
        assertEquals(503, r.status, r.text)
        assertEquals("degraded", r.json["status"].asText())
        assertEquals("down", r.json["database"].asText())

        // The dead connection was discarded rather than handed out again.
        assertEquals(200, get("/api/health").status)
    }

    /** Only this application's own sessions: the other test classes share the database. */
    private fun terminateAppBackends(): Int =
        TestDatabase.connect().use { c ->
            c.autoCommit = true
            c.prepareStatement(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = ?"
            ).use { ps ->
                ps.setString(1, APP_NAME)
                var terminated = 0
                ps.executeQuery().use { rs -> while (rs.next()) terminated++ }
                terminated
            }
        }

    companion object {
        const val APP_NAME = "sillage-f17-dies-mid-life"
    }
}
