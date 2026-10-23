package sillage

import java.sql.SQLException
import java.util.Locale

/**
 * What the three endpoints do, without any HTTP framework in sight.
 *
 * The Micronaut controller only binds query parameters and serialises the result, so
 * this class is the port of api/main.py's handlers. Errors are thrown as
 * [ApiException] and turned into responses in one place.
 */
class SillageService(
    private val db: Database,
    private val maxPoints: Int,
    private val dsnLabel: String,
    private val busiestTtlMillis: Long = 300_000,
) {

    fun trips(raw: RawTripsParams): TripsPayload {
        // Same order as FastAPI: parameter validation (422) first, then the handler body.
        val filters = parseFilters(raw)
        val layers = resolveLayers(raw.layer)
        checkWindowOrder(filters.start, filters.end)

        val payload = db.readOnly { conn ->
            try {
                checkBudget(conn, layers, filters, maxPoints)
                buildTrips(conn, layers, filters)
            } catch (e: OverBudgetException) {
                throw ApiException(413, overBudget(String.format(Locale.US, "more than %,d points match this query, which", e.budget)))
            } catch (e: NoDataException) {
                throw ApiException(404, "no data for this query (empty layers: ${e.empty.joinToString(", ") { it.key }})")
            } catch (e: SQLException) {
                // Class 22 is "data exception": in practice an unparseable start/end, or a
                // NUL byte the driver refuses to send (F2). First line only, so the SQL
                // text never reaches the client (F5). Anything else is a real 500.
                if (e.sqlState?.startsWith("22") != true) throw e
                throw ApiException(400, "bad query parameter: ${firstLine(e)}")
            }
        }

        // Recorded rows were checked before fetching. Kept as in the Python service, where
        // simulated layers can still push the combined payload over; here it cannot fire.
        val totalPoints = payload.trips.sumOf { it.path.size }
        if (totalPoints > maxPoints) {
            throw ApiException(413, overBudget(String.format(Locale.US, "%,d points", totalPoints)))
        }
        return payload
    }

    private fun overBudget(what: String) = String.format(
        Locale.US,
        "payload too large: %s exceeds the %,d budget. Narrow the window " +
            "(start/end or hours), pick a single layer, or raise MTL_MAX_POINTS.",
        what, maxPoints,
    )

    fun range(): RangePayload = db.readOnly { conn -> getRange(conn, cachedBusiestHour(conn)) }

    /** 200 + `database: up`, or 503 + `database: down` when Postgres cannot be reached. */
    fun health(): Pair<Int, Map<String, Any?>> = try {
        db.readOnly { conn -> conn.createStatement().use { it.executeQuery("SELECT 1").use { rs -> rs.next() } } }
        200 to mapOf("status" to "ok", "database" to "up", "dsn" to dsnLabel)
    } catch (e: ApiException) {
        503 to mapOf("status" to "degraded", "database" to "down", "detail" to e.detail)
    }

    // A bucketed COUNT over millions of rows: fine once, wasteful on every page load.
    @Volatile private var busiestAt = 0L
    @Volatile private var busiest: BusiestHour? = null
    @Volatile private var busiestComputed = false

    private fun cachedBusiestHour(conn: java.sql.Connection): BusiestHour? {
        val now = System.nanoTime() / 1_000_000
        if (busiestComputed && now - busiestAt < busiestTtlMillis) return busiest
        return busiestHour(conn).also {
            busiest = it
            busiestAt = now
            busiestComputed = true
        }
    }

    private fun firstLine(e: SQLException): String {
        // The server's own message, without the driver's "ERROR: " prefix and
        // "Position: …" suffix — the same text psycopg2 gives the Python service.
        val server = (e as? org.postgresql.util.PSQLException)?.serverErrorMessage?.message
        return (server ?: e.message ?: e.toString()).trim().lines().first()
    }
}
