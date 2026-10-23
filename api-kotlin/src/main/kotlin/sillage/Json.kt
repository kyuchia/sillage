package sillage

import com.fasterxml.jackson.annotation.JsonInclude
import com.fasterxml.jackson.databind.ObjectMapper
import com.fasterxml.jackson.databind.SerializationFeature
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * The one ObjectMapper every response goes through.
 *
 * Kept separate from Micronaut's own so its defaults cannot change the payload:
 * nulls are always written (`"label": null`, `"overlap": null` are part of the
 * contract), and the response is built once, explicitly — the same reason the Python
 * service returns a JSONResponse instead of letting FastAPI re-encode the payload.
 */
object Json {
    private val mapper: ObjectMapper = ObjectMapper()
        .setSerializationInclusion(JsonInclude.Include.ALWAYS)
        .disable(SerializationFeature.FAIL_ON_EMPTY_BEANS)

    fun write(value: Any?): String = mapper.writeValueAsString(value)
}

/** The recording, the schedules and the frontend all live in Montréal time (F7). */
val MONTREAL: ZoneId = ZoneId.of("America/Montreal")

private val SECONDS = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")

/**
 * An instant as Python's `datetime.isoformat()` writes it in Montréal time:
 * `2026-08-20T08:00:00-04:00`, microseconds only when non-zero (`…:00.250000-04:00`).
 *
 * The JDBC driver hands timestamptz back in UTC whatever the session time zone is, so
 * the offset is applied here. Java's ISO formatter differs from Python in three small
 * ways (trailing-zero trimming of fractions, `Z` for UTC, optional seconds), which is
 * why this is written out instead.
 */
fun isoMontreal(t: Instant): String = pyIsoFormat(t.atZone(MONTREAL).toOffsetDateTime())

fun pyIsoFormat(t: OffsetDateTime): String {
    val sb = StringBuilder(SECONDS.format(t))
    val micros = t.nano / 1_000
    if (micros != 0) sb.append(".%06d".format(micros))
    sb.append(pyOffset(t.offset))
    return sb.toString()
}

private fun pyOffset(offset: ZoneOffset): String {
    val total = offset.totalSeconds
    val sign = if (total < 0) "-" else "+"
    val abs = Math.abs(total)
    val base = "%s%02d:%02d".format(sign, abs / 3600, abs / 60 % 60)
    return if (abs % 60 != 0) base + ":%02d".format(abs % 60) else base
}

/** Seconds between two instants, as Python's `timedelta.total_seconds()` computes it. */
fun secondsBetween(from: Instant, to: Instant): Double {
    val micros = Math.multiplyExact(to.epochSecond - from.epochSecond, 1_000_000L) +
        (to.nano / 1_000 - from.nano / 1_000)
    return micros / 1_000_000.0
}
