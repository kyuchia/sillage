package sillage

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.net.ConnectException
import java.sql.SQLTransientConnectionException

/** What a Hikari timeout becomes (F14): busy and unreachable are both 503, worded apart. */
class PoolBusyTest {

    private fun failingWith(e: Exception) = Database({ throw e }, "dbname=sillage_test", 100)

    @Test
    fun `every connection in use is a 503 saying the database is busy`() {
        val db = failingWith(SQLTransientConnectionException("sillage - Connection is not available"))
        val e = assertThrows(ApiException::class.java) { db.readOnly { } }
        assertEquals(503, e.status)
        assertEquals("database busy: no connection became free within 0.1 s. Try again shortly.", e.detail)
    }

    @Test
    fun `a timeout caused by a failed connect still says cannot connect`() {
        val db = failingWith(SQLTransientConnectionException("timed out", ConnectException("refused")))
        val e = assertThrows(ApiException::class.java) { db.readOnly { } }
        assertEquals(503, e.status)
        assertTrue((e.detail as String).startsWith("cannot connect to PostgreSQL (dbname=sillage_test)"), e.detail.toString())
    }

    @Test
    fun `wait times are written like Python's g format`() {
        assertEquals("3", seconds(3_000))
        assertEquals("0.1", seconds(100))
        assertEquals("2.5", seconds(2_500))
    }
}
