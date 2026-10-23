package sillage

import io.micronaut.test.extensions.junit5.annotation.MicronautTest
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestInstance
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.CsvSource
import org.junit.jupiter.params.provider.ValueSource

/** /api/trips on bad input. Cases ported from tests/api/test_errors.py. */
@MicronautTest
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TripsErrorsTest : ApiTestBase() {

    @ParameterizedTest
    @ValueSource(strings = ["boat", "BUS", "bus,boat", "bus;DROP TABLE vehicle_positions"])
    fun `unknown layer is 400 and names it`(layer: String) {
        val r = get("/api/trips", Seed.WINDOW + ("layer" to layer))
        assertEquals(400, r.status)
        assertTrue(r.detail.startsWith("unknown layer '"), r.detail)
        assertEquals(Seed.BUS_ROWS, TestDatabase.count("vehicle_positions"))
    }

    @ParameterizedTest
    @ValueSource(strings = ["", " , "])
    fun `empty layer is 400`(layer: String) {
        val r = get("/api/trips", Seed.WINDOW + ("layer" to layer))
        assertEquals(400, r.status)
        assertEquals("layer must not be empty", r.detail)
    }

    @ParameterizedTest
    @ValueSource(strings = ["metro", "rem", "all", "both,train"])
    fun `simulated layers are refused rather than faked`(layer: String) {
        val r = get("/api/trips", Seed.WINDOW + ("layer" to layer))
        assertEquals(400, r.status)
        assertTrue("recorded layers only" in r.detail, r.detail)
    }

    @ParameterizedTest
    @ValueSource(strings = ["notadate", "2026-13-45 08:00", "2026-08-20T25:00:00-04:00"])
    fun `unparseable timestamp is 400 without SQL in the message`(start: String) {
        val r = get("/api/trips", mapOf("start" to start, "end" to Seed.WINDOW_END))
        assertEquals(400, r.status)
        assertTrue(r.detail.startsWith("bad query parameter"), r.detail)
        assertFalse("LINE" in r.detail || "BETWEEN" in r.detail || "Position" in r.detail, r.detail)
    }

    @Test
    fun `a 5000-character timestamp reaches Postgres and is a 400`() {
        val r = get("/api/trips", mapOf("start" to Seed.WINDOW_START + "0".repeat(5000), "end" to Seed.WINDOW_END))
        assertEquals(400, r.status)
        assertTrue(r.detail.startsWith("bad query parameter"), r.detail)
    }

    @Test
    fun `NUL byte in a timestamp is 400 not 500`() {
        assertEquals(400, get("/api/trips", mapOf("start" to "2026-08-20\u0000", "end" to Seed.WINDOW_END)).status)
    }

    @Test
    fun `start after end is 400`() {
        val r = get("/api/trips", mapOf("start" to Seed.WINDOW_END, "end" to Seed.WINDOW_START))
        assertEquals(400, r.status)
        assertEquals("start must be before end", r.detail)
    }

    @ParameterizedTest
    @CsvSource(
        "hours, abc", "hours, 0", "hours, -1", "hours, nan", "hours, inf",
        "min_points, -1", "min_points, three", "include_ground, maybe",
    )
    fun `wrongly typed parameter is 422 naming the field`(field: String, value: String) {
        val r = get("/api/trips", Seed.WINDOW + (field to value))
        assertEquals(422, r.status)
        assertEquals(listOf("query", field), r.json["detail"][0]["loc"].map { it.asText() })
    }

    @Test
    fun `route is a bound parameter, not SQL`() {
        val r = get("/api/trips", Seed.WINDOW + mapOf("layer" to "bus", "route" to "51' OR '1'='1"))
        assertEquals(404, r.status)
        assertEquals(Seed.BUS_ROWS, TestDatabase.count("vehicle_positions"))
    }

    @Test
    fun `window with no rows is 404 naming the empty layers`() {
        val r = get("/api/trips", mapOf("start" to "2026-08-25T08:00:00-04:00", "end" to "2026-08-25T09:00:00-04:00"))
        assertEquals(404, r.status)
        assertEquals("no data for this query (empty layers: bus, aircraft)", r.detail)
    }

    @Test
    fun `hours on a closed archive is 404`() {
        assertEquals(404, get("/api/trips", mapOf("hours" to "1")).status)
    }
}

/** The point budget, with MTL_MAX_POINTS one below what the seed window holds (119). */
@MicronautTest
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PointBudgetTest : ApiTestBase() {

    override fun extraProperties() = mapOf("sillage.max-points" to "118")

    @Test
    fun `payload over the point budget is 413`() {
        val r = get("/api/trips", Seed.WINDOW)
        assertEquals(413, r.status)
        assertTrue(r.detail.startsWith("payload too large: 119 points exceeds the 118 budget."), r.detail)
        assertTrue("Narrow the window" in r.detail)
    }

    @Test
    fun `a smaller window under the budget is allowed`() {
        assertEquals(200, get("/api/trips", Seed.WINDOW + ("layer" to "bus")).status)   // 83 points
    }
}
