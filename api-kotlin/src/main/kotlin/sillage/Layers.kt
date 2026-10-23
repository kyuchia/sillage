package sillage

/**
 * The recorded layers, and where each one lives in Postgres.
 *
 * Mirrors `LAYERS` in export/trips_query.py. Table and column names in SQL come only
 * from here — never from the request — so they can be spliced into the query text
 * safely. Adding a recorded mode is one more entry, provided its table also has
 * fetched_at / latitude / longitude columns.
 */
enum class Layer(val table: String, val idCol: String, val labelCol: String) {
    BUS("vehicle_positions", "vehicle_id", "route_id"),
    AIRCRAFT("aircraft_positions", "icao24", "callsign");

    /** The name used in the API and in the payload's `mode` field. */
    val key: String = name.lowercase()
}

/**
 * Positions outside greater Montréal are GPS failures, not places a vehicle went.
 * Same box as PLAUSIBLE_BOX in export/trips_query.py; see the reasoning there.
 */
object PlausibleBox {
    const val MIN_LON = -75.0
    const val MAX_LON = -72.5
    const val MIN_LAT = 44.5
    const val MAX_LAT = 46.5
}

/** Simulated from GTFS by the Python service only. Named here so errors can say so. */
val SIMULATED_LAYERS = listOf("metro", "train", "rem")

/** Every layer name the API contract knows, in the Python service's order. */
val ALL_LAYER_NAMES: List<String> = Layer.entries.map { it.key } + SIMULATED_LAYERS

/**
 * Turn the `layer` parameter into an ordered, de-duplicated list of recorded layers.
 *
 * Same rules as `_resolve_layers` in api/main.py: comma-separated, blanks ignored,
 * `both` = the recorded pair, `all` = everything. Output order follows [Layer], not
 * the order given, so one set always produces the same payload.
 *
 * Names are validated against the full contract first, so `boat` gets the same message
 * from both services; only then are simulated layers refused, because this port does
 * not materialise GTFS schedules and must not pretend to.
 */
fun resolveLayers(param: String?): List<Layer> {
    val parts = (param ?: "both").split(",").map { it.trim() }.filter { it.isNotEmpty() }
    if (parts.isEmpty()) throw ApiException(400, "layer must not be empty")

    val want = mutableSetOf<String>()
    for (part in parts) {
        when (part) {
            "both" -> want += Layer.entries.map { it.key }
            "all" -> want += ALL_LAYER_NAMES
            in ALL_LAYER_NAMES -> want += part
            else -> throw ApiException(
                400,
                "unknown layer ${pyRepr(part)}. Valid: ${ALL_LAYER_NAMES.joinToString(", ")}, " +
                    "both, all — or a comma-separated list of them",
            )
        }
    }

    val simulated = SIMULATED_LAYERS.filter { it in want }
    if (simulated.isNotEmpty()) {
        throw ApiException(
            400,
            "Kotlin port supports recorded layers only (bus, aircraft, both); " +
                "simulated layers requested: ${simulated.joinToString(", ")}",
        )
    }
    return Layer.entries.filter { it.key in want }
}

/**
 * Python's repr() of a string, so `unknown layer 'boat'` reads the same in both
 * services. Single quotes unless the text holds a ' and no ", like CPython.
 */
fun pyRepr(s: String): String {
    val quote = if ('\'' in s && '"' !in s) '"' else '\''
    val sb = StringBuilder().append(quote)
    for (c in s) {
        when {
            c == quote || c == '\\' -> sb.append('\\').append(c)
            c == '\n' -> sb.append("\\n")
            c == '\r' -> sb.append("\\r")
            c == '\t' -> sb.append("\\t")
            c.code < 0x20 || c.code == 0x7f -> sb.append("\\x%02x".format(c.code))
            else -> sb.append(c)
        }
    }
    return sb.append(quote).toString()
}
