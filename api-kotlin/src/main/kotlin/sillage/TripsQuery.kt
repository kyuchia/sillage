package sillage

import com.fasterxml.jackson.annotation.JsonProperty
import com.fasterxml.jackson.annotation.JsonPropertyOrder
import java.sql.Connection
import java.time.Instant
import java.time.OffsetDateTime

/** WHERE fragments and the values bound to their `?` placeholders, in order. */
data class Where(val clauses: List<String>, val params: List<Any>)

/**
 * Build the WHERE clause for one layer. Port of `build_where` in export/trips_query.py.
 *
 * Every user-supplied value becomes a bound `?` parameter; nothing from the request
 * is ever spliced into the SQL text. The `::timestamptz` / `::interval` casts are
 * needed because JDBC sends strings typed as varchar, where psycopg2 sent untyped
 * literals that Postgres coerced on its own. Postgres still does the parsing.
 */
fun buildWhere(layer: Layer, f: Filters): Where {
    val clauses = mutableListOf(
        "latitude IS NOT NULL", "longitude IS NOT NULL",
        "longitude BETWEEN ? AND ?", "latitude BETWEEN ? AND ?",
    )
    val params = mutableListOf<Any>(
        PlausibleBox.MIN_LON, PlausibleBox.MAX_LON, PlausibleBox.MIN_LAT, PlausibleBox.MAX_LAT,
    )

    if (f.hours != null) {
        clauses += "fetched_at >= NOW() - ?::interval"
        params += "${pyFloatRepr(f.hours)} hours"
    }
    if (!f.start.isNullOrEmpty()) {
        clauses += "fetched_at >= ?::timestamptz"
        params += f.start
    }
    if (!f.end.isNullOrEmpty()) {
        clauses += "fetched_at <= ?::timestamptz"
        params += f.end
    }

    if (layer == Layer.BUS && !f.route.isNullOrEmpty()) {
        clauses += "route_id = ?"
        params += f.route
    }

    // Parked aircraft clump into a blob over Dorval; hidden unless asked for.
    if (layer == Layer.AIRCRAFT && !f.includeGround) {
        clauses += "(on_ground IS NULL OR on_ground = false)"
    }
    return Where(clauses, params)
}

/** One position row, as `fetch_layer` returns it. */
data class Fix(
    val fetchedAt: Instant,
    val uid: String?,
    val label: String?,
    val latitude: Double,
    val longitude: Double,
)

/** Raw rows for one layer, ordered by vehicle then time. */
fun fetchLayer(conn: Connection, layer: Layer, f: Filters): List<Fix> {
    val where = buildWhere(layer, f)
    // Only names from the Layer enum reach the SQL text.
    val sql = """
        SELECT fetched_at, ${layer.idCol} AS uid, ${layer.labelCol} AS label,
               latitude, longitude
        FROM ${layer.table}
        WHERE ${where.clauses.joinToString(" AND ")}
        ORDER BY ${layer.idCol}, fetched_at
    """.trimIndent()

    conn.prepareStatement(sql).use { ps ->
        where.params.forEachIndexed { i, value -> ps.setObject(i + 1, value) }
        ps.executeQuery().use { rs ->
            val rows = mutableListOf<Fix>()
            while (rs.next()) {
                rows += Fix(
                    fetchedAt = rs.getObject("fetched_at", OffsetDateTime::class.java).toInstant(),
                    uid = rs.getString("uid"),
                    label = rs.getString("label"),
                    latitude = rs.getDouble("latitude"),
                    longitude = rs.getDouble("longitude"),
                )
            }
            return rows
        }
    }
}

@JsonPropertyOrder("mode", "id", "label", "path", "timestamps")
data class Trip(
    val mode: String,
    val id: String?,
    val label: String?,
    val path: List<List<Double>>,
    val timestamps: List<Double>,
)

@JsonPropertyOrder("start_time", "end_time", "duration_sec", "modes", "trips")
data class TripsPayload(
    @get:JsonProperty("start_time") val startTime: String,
    @get:JsonProperty("end_time") val endTime: String,
    @get:JsonProperty("duration_sec") val durationSec: Double,
    val modes: List<String>,
    val trips: List<Trip>,
)

/**
 * Assemble the trips payload. Port of `build_trips_with_summary`.
 *
 * Every layer shares one timeline: the origin is the earliest fix across all layers,
 * and each trip's `timestamps` are seconds from it. Traces shorter than `minPoints`
 * are dropped. Throws [NoDataException] when every layer is empty.
 */
fun buildTrips(conn: Connection, layers: List<Layer>, f: Filters): TripsPayload {
    val raw = linkedMapOf<Layer, List<Fix>>()
    val empty = mutableListOf<Layer>()
    for (layer in layers) {
        val rows = fetchLayer(conn, layer, f)
        if (rows.isEmpty()) empty += layer else raw[layer] = rows
    }
    if (raw.isEmpty()) throw NoDataException(empty)

    val all = raw.values.flatten()
    val start = all.minOf { it.fetchedAt }
    val end = all.maxOf { it.fetchedAt }

    val trips = mutableListOf<Trip>()
    for ((layer, rows) in raw) {
        // groupBy keeps first-seen order, like Python's defaultdict(list): rows arrive
        // sorted by id, so trips come out in the same order in both services.
        for ((uid, points) in rows.groupBy { it.uid }) {
            if (points.size < f.minPoints) continue
            trips += Trip(
                mode = layer.key,
                id = uid,
                label = points.first().label?.trim()?.takeIf { it.isNotEmpty() },
                path = points.map { listOf(it.longitude, it.latitude) },
                timestamps = points.map { secondsBetween(start, it.fetchedAt) },
            )
        }
    }

    return TripsPayload(
        startTime = isoMontreal(start),
        endTime = isoMontreal(end),
        durationSec = secondsBetween(start, end),
        modes = raw.keys.map { it.key }.sorted(),
        trips = trips,
    )
}
