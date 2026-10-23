package sillage

import com.fasterxml.jackson.annotation.JsonProperty
import com.fasterxml.jackson.annotation.JsonPropertyOrder
import java.sql.Connection
import java.time.Instant
import java.time.OffsetDateTime

@JsonPropertyOrder("table", "rows", "min_time", "max_time")
data class LayerRange(
    val table: String,
    val rows: Long,
    @get:JsonProperty("min_time") val minTime: String?,
    @get:JsonProperty("max_time") val maxTime: String?,
)

@JsonPropertyOrder("start", "end")
data class Window(val start: String, val end: String)

@JsonPropertyOrder("start", "end", "rows")
data class BusiestHour(val start: String, val end: String, val rows: Long)

@JsonPropertyOrder("layers", "overlap", "busiest_hour")
data class RangePayload(
    val layers: Map<String, LayerRange>,
    val overlap: Window?,
    @get:JsonProperty("busiest_hour") val busiestHour: BusiestHour?,
)

/**
 * What data exists, per layer, plus the window where every layer has data. Port of
 * `get_range`. The overlap is intersected on instants, not on ISO strings.
 */
fun getRange(conn: Connection, busiest: BusiestHour?): RangePayload {
    val layers = linkedMapOf<String, LayerRange>()
    val bounds = mutableListOf<Pair<Instant, Instant>>()

    for (layer in Layer.entries) {
        conn.createStatement().use { st ->
            st.executeQuery(
                "SELECT COUNT(*) AS rows, MIN(fetched_at) AS min_time, MAX(fetched_at) AS max_time " +
                    "FROM ${layer.table}",
            ).use { rs ->
                rs.next()
                val lo = rs.getObject("min_time", OffsetDateTime::class.java)?.toInstant()
                val hi = rs.getObject("max_time", OffsetDateTime::class.java)?.toInstant()
                layers[layer.key] = LayerRange(
                    table = layer.table,
                    rows = rs.getLong("rows"),
                    minTime = lo?.let(::isoMontreal),
                    maxTime = hi?.let(::isoMontreal),
                )
                if (lo != null && hi != null) bounds += lo to hi
            }
        }
    }

    var overlap: Window? = null
    if (bounds.size == Layer.entries.size) {
        val lo = bounds.maxOf { it.first }
        val hi = bounds.minOf { it.second }
        if (lo < hi) overlap = Window(isoMontreal(lo), isoMontreal(hi))
    }
    return RangePayload(layers, overlap, busiest)
}

/**
 * The one-hour bucket holding the most rows across the recorded layers. Port of
 * `busiest_hour`. `date_trunc` follows the session time zone, which [Database] has set
 * to Montréal, so buckets start on Montréal hours. Ties go to the later bucket.
 */
fun busiestHour(conn: Connection): BusiestHour? {
    val counts = HashMap<Instant, Long>()
    for (layer in Layer.entries) {
        conn.createStatement().use { st ->
            st.executeQuery(
                "SELECT date_trunc('hour', fetched_at) AS bucket, COUNT(*) AS n " +
                    "FROM ${layer.table} WHERE fetched_at IS NOT NULL GROUP BY 1",
            ).use { rs ->
                while (rs.next()) {
                    val bucket = rs.getObject("bucket", OffsetDateTime::class.java).toInstant()
                    counts.merge(bucket, rs.getLong("n"), Long::plus)
                }
            }
        }
    }
    val best = counts.entries.maxWithOrNull(compareBy<Map.Entry<Instant, Long>> { it.value }.thenBy { it.key })
        ?: return null
    // Python adds the hour to a fixed-offset datetime, so `end` keeps the bucket's offset
    // even across a DST change; OffsetDateTime.plusHours does the same.
    val start = best.key.atZone(MONTREAL).toOffsetDateTime()
    return BusiestHour(pyIsoFormat(start), pyIsoFormat(start.plusHours(1)), best.value)
}
