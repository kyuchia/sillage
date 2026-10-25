package sillage

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test
import java.time.Instant
import java.time.OffsetDateTime

/** The framework-free helpers: no server, no database. */
class ParsingTest {

    @Test
    fun `libpq keyword strings become JDBC targets`() {
        val t = libpqToJdbc("host=db port=6543 user=postgres password='p w\\'d' dbname=sillage_test options='-c TimeZone=UTC'", emptyMap())
        assertEquals("jdbc:postgresql://db:6543/sillage_test", t.url)
        assertEquals("postgres", t.properties["user"])
        assertEquals("p w'd", t.properties["password"])
        assertEquals("-c TimeZone=UTC", t.properties["options"])
    }

    @Test
    fun `a bare dbname uses libpq defaults`() {
        val t = libpqToJdbc("dbname=sillage", mapOf("PGUSER" to "chia"))
        assertEquals("jdbc:postgresql://localhost:5432/sillage", t.url)
        assertEquals("chia", t.properties["user"])
    }

    @Test
    fun `postgresql URIs are accepted`() {
        val t = libpqToJdbc("postgresql://u:s%40cret@h:5433/d?sslmode=require", emptyMap())
        assertEquals("jdbc:postgresql://h:5433/d", t.url)
        assertEquals("s@cret", t.properties["password"])
        assertEquals("require", t.properties["sslmode"])
    }

    @Test
    fun `the DSN label keeps only host, port and dbname`() {
        // The same five cases, and the same expected strings, as test_dsn_label_* in
        // tests/api/test_endpoints.py: /api/health must answer identically on both services.
        assertEquals("host=localhost port=5432 dbname=sillage",
            dsnLabel("host=localhost port=5432 user=bob password=secret dbname=sillage"))
        assertEquals("host=db.example.com port=5433 dbname=sillage",
            dsnLabel("postgresql://bob:secret@db.example.com:5433/sillage"))
        assertEquals("dbname=sillage", dsnLabel("dbname=sillage"))
        assertEquals("(default)", dsnLabel(""))
        assertEquals("(unparseable)", dsnLabel("not a dsn"))
    }

    @Test
    fun `the DSN label never reveals the user or the password`() {
        for (dsn in listOf("host=localhost user=bob password=hunter2 dbname=sillage",
                           "postgresql://bob:hunter2@localhost/sillage")) {
            val label = dsnLabel(dsn)
            assertFalse(label.contains("hunter2"), dsn)
            assertFalse(label.contains("bob"), dsn)
            assertFalse(label.contains("password"), dsn)
        }
    }

    @Test
    fun `floats are written the way Python's repr writes them`() {
        val cases = mapOf(1.0 to "1.0", 0.5 to "0.5", 1000.0 to "1000.0", 1e-7 to "1e-07",
            0.0001 to "0.0001", 1e16 to "1e+16", 1.5e16 to "1.5e+16", 123456789012345.0 to "123456789012345.0")
        cases.forEach { (x, expected) -> assertEquals(expected, pyFloatRepr(x), "$x") }
    }

    @Test
    fun `timestamps are written the way Python's isoformat writes them`() {
        assertEquals("2026-08-20T08:00:00-04:00", isoMontreal(Instant.parse("2026-08-20T12:00:00Z")))
        assertEquals("2026-01-20T07:00:00.250000-05:00", isoMontreal(Instant.parse("2026-01-20T12:00:00.25Z")))
        assertEquals("2026-08-20T12:00:00+00:00", pyIsoFormat(OffsetDateTime.parse("2026-08-20T12:00:00Z")))
    }

    @Test
    fun `ISO strings parse with Montreal as the default zone`() {
        assertEquals(Instant.parse("2026-08-20T12:00:00Z"), parseIsoInstant("2026-08-20 08:00"))
        assertEquals(Instant.parse("2026-08-20T12:00:00Z"), parseIsoInstant("2026-08-20T20:00:00+0800"))
        assertEquals(Instant.parse("2026-08-20T04:00:00Z"), parseIsoInstant("2026-08-20"))
        assertNull(parseIsoInstant("2026-13-45 08:00"))
        assertNull(parseIsoInstant("notadate"))
    }

    @Test
    fun `seconds between instants keep microseconds`() {
        assertEquals(20.000001, secondsBetween(Instant.parse("2026-08-20T12:00:00Z"), Instant.parse("2026-08-20T12:00:20.000001Z")))
    }
}
