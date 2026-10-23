package sillage

import java.math.BigDecimal
import java.math.BigInteger
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneOffset
import java.time.ZonedDateTime

/** The query string as it arrived: every value still a string, absent = null. */
data class RawTripsParams(
    val start: String? = null,
    val end: String? = null,
    val hours: String? = null,
    val layer: String? = null,
    val route: String? = null,
    val minPoints: String? = null,
    val includeGround: String? = null,
)

/** Validated filters, ready for [buildWhere]. */
data class Filters(
    val start: String? = null,
    val end: String? = null,
    val hours: Double? = null,
    val route: String? = null,
    val minPoints: Int = 3,
    val includeGround: Boolean = false,
)

/**
 * Validate the typed parameters the way FastAPI/pydantic does, and answer 422 with
 * every problem at once if any fail.
 *
 * Done by hand rather than with Micronaut's validation so the accepted spellings match
 * the Python service exactly (pydantic takes `1_000`, ` 3 `, `3.0`, `yes`, `on`, `t` …;
 * measured, not guessed). start/end/route stay strings: Postgres parses the times,
 * exactly as on the Python side.
 */
fun parseFilters(raw: RawTripsParams): Filters {
    val problems = mutableListOf<ParamProblem>()
    val hours = raw.hours?.let { parseHours(it, problems) }
    val minPoints = raw.minPoints?.let { parseMinPoints(it, problems) } ?: 3
    val includeGround = raw.includeGround?.let { parseBool(it, "include_ground", problems) } ?: false
    if (problems.isNotEmpty()) throw ApiException(422, problems)
    return Filters(raw.start, raw.end, hours, raw.route, minPoints, includeGround)
}

// digits with single underscores between them, as Python's float() and int() allow
private const val DIGITS = """\d(?:_?\d)*"""
private val FLOAT = Regex("""[+-]?(?:$DIGITS(?:\.(?:$DIGITS)?)?|\.$DIGITS)(?:[eE][+-]?$DIGITS)?""")
private val NON_FINITE = Regex("""[+-]?(?:inf|infinity|nan)""", RegexOption.IGNORE_CASE)
private val INT = Regex("""[+-]?$DIGITS(?:\.0+)?""")

private fun parseHours(text: String, problems: MutableList<ParamProblem>): Double? {
    val s = text.trim()
    fun fail(type: String, msg: String): Double? {
        problems += ParamProblem(type, listOf("query", "hours"), msg, text)
        return null
    }
    if (NON_FINITE.matches(s)) return fail("finite_number", "Input should be a finite number")
    if (!FLOAT.matches(s)) {
        return fail("float_parsing", "Input should be a valid number, unable to parse string as a number")
    }
    val value = s.replace("_", "").toDouble()
    if (value.isInfinite()) return fail("finite_number", "Input should be a finite number")
    if (value <= 0.0) return fail("greater_than", "Input should be greater than 0")
    return value
}

private fun parseMinPoints(text: String, problems: MutableList<ParamProblem>): Int? {
    val s = text.trim()
    if (!INT.matches(s)) {
        problems += ParamProblem("int_parsing", listOf("query", "min_points"),
            "Input should be a valid integer, unable to parse string as an integer", text)
        return null
    }
    val value = BigInteger(s.substringBefore('.').replace("_", "").removePrefix("+"))
    if (value.signum() < 0) {
        problems += ParamProblem("greater_than_equal", listOf("query", "min_points"),
            "Input should be greater than or equal to 0", text)
        return null
    }
    // Python ints are unbounded; past Int.MAX_VALUE every trace is culled either way.
    return if (value.bitLength() < 32) value.toInt() else Int.MAX_VALUE
}

private val TRUE_WORDS = setOf("1", "on", "t", "true", "y", "yes")
private val FALSE_WORDS = setOf("0", "off", "f", "false", "n", "no")

private fun parseBool(text: String, field: String, problems: MutableList<ParamProblem>): Boolean? =
    when (text.lowercase()) {
        in TRUE_WORDS -> true
        in FALSE_WORDS -> false
        else -> {
            problems += ParamProblem("bool_parsing", listOf("query", field),
                "Input should be a valid boolean, unable to interpret input", text)
            null
        }
    }

/**
 * Python's repr() of a float: the text the Python service puts in `INTERVAL '… hours'`.
 * Shortest round-trip digits (Java 19+ Double.toString gives the same digits), fixed
 * notation for exponents -4..15, otherwise `1e-07` / `1.5e+16`.
 */
fun pyFloatRepr(x: Double): String {
    val bd = BigDecimal(x.toString()).stripTrailingZeros()
    val digits = bd.unscaledValue().abs().toString()
    val exp10 = digits.length - 1 - bd.scale()
    val sign = if (x < 0) "-" else ""
    if (exp10 in -4..15) {
        val plain = bd.abs().toPlainString()
        return sign + if ('.' in plain) plain else "$plain.0"
    }
    val mantissa = if (digits.length > 1) digits[0] + "." + digits.substring(1) else digits
    val e = if (exp10 < 0) "-" else "+"
    return "$sign${mantissa}e$e${"%02d".format(Math.abs(exp10))}"
}

// ---------------------------------------------------------------- window order (F3)

private val ISO = Regex(
    """(\d{4})-(\d{2})-(\d{2})""" +
        """(?:[T ](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?""" +
        """(Z|[+-]\d{2}(?::?\d{2})?)?)?""",
)

/**
 * Parse the common ISO-8601 forms Python's `datetime.fromisoformat` accepts; a missing
 * offset means Montréal time, as in `gtfs_sim.parse_dt`. Null when not understood —
 * the caller then leaves the string for Postgres to judge, as the Python service does.
 */
fun parseIsoInstant(text: String): Instant? {
    val m = ISO.matchEntire(text.trim()) ?: return null
    val g = m.groupValues
    return try {
        val date = LocalDate.of(g[1].toInt(), g[2].toInt(), g[3].toInt())
        val nanos = g[7].takeIf { it.isNotEmpty() }?.take(6)?.padEnd(9, '0')?.toInt() ?: 0
        val time = if (g[4].isEmpty()) LocalTime.MIDNIGHT else LocalTime.of(
            g[4].toInt(), g[5].ifEmpty { "0" }.toInt(), g[6].ifEmpty { "0" }.toInt(), nanos,
        )
        val local = LocalDateTime.of(date, time)
        when (val off = g[8]) {
            "" -> ZonedDateTime.of(local, MONTREAL).toInstant()
            "Z" -> local.toInstant(ZoneOffset.UTC)
            else -> local.toInstant(ZoneOffset.of(normaliseOffset(off)))
        }
    } catch (e: java.time.DateTimeException) {
        null
    }
}

private fun normaliseOffset(off: String): String = when (off.length) {
    3 -> off                                   // +HH
    5 -> off.substring(0, 3) + ":" + off.substring(3)  // +HHMM
    else -> off                                // +HH:MM
}

/**
 * A window whose start is after its end is invalid, not empty (F3). Only checked when
 * both ends parse; anything else goes to Postgres, which reports bad strings itself.
 */
fun checkWindowOrder(start: String?, end: String?) {
    if (start.isNullOrEmpty() || end.isNullOrEmpty()) return
    val lo = parseIsoInstant(start) ?: return
    val hi = parseIsoInstant(end) ?: return
    if (lo > hi) throw ApiException(400, "start must be before end")
}
