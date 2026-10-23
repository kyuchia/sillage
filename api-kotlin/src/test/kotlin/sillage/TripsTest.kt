package sillage

import io.micronaut.test.extensions.junit5.annotation.MicronautTest
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestInstance

/** /api/trips on good input: the payload shape and the filters, against the seed. */
@MicronautTest
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TripsTest : ApiTestBase() {

    private fun pointsById(r: Reply, mode: String): Map<String, Int> =
        r.json["trips"].filter { it["mode"].asText() == mode }
            .associate { it["id"].asText() to it["path"].size() }

    @Test
    fun `default layer returns both recorded modes`() {
        val r = get("/api/trips", Seed.WINDOW)
        assertEquals(200, r.status)
        assertEquals(listOf("aircraft", "bus"), r.json["modes"].map { it.asText() })
        assertEquals(Seed.BUS_TRIPS, pointsById(r, "bus"))
        assertEquals(Seed.AIRCRAFT_TRIPS, pointsById(r, "aircraft"))
    }

    @Test
    fun `timestamps are seconds from the shared start, in Montreal time`() {
        val r = get("/api/trips", Seed.WINDOW)
        assertEquals("2026-08-20T08:00:00-04:00", r.json["start_time"].asText())
        assertEquals(Seed.WINDOW_END, r.json["end_time"].asText())
        assertEquals(600.0, r.json["duration_sec"].asDouble())
        val b1 = r.json["trips"].first { it["id"].asText() == "B1" }
        assertEquals(listOf(0.0, 20.0, 40.0), b1["timestamps"].take(3).map { it.asDouble() })
        assertEquals(listOf(-73.6, 45.5), b1["path"][0].map { it.asDouble() })   // [lon, lat]
    }

    @Test
    fun `labels are stripped and a missing label is an explicit null`() {
        val trips = get("/api/trips", Seed.WINDOW).json["trips"].associateBy { it["id"].asText() }
        assertEquals("ACA123", trips.getValue("c0ffee")["label"].asText())
        val def456 = trips.getValue("def456")
        assertTrue(def456.has("label"), "label key must be present")
        assertTrue(def456["label"].isNull)
    }

    @Test
    fun `parked aircraft are hidden unless include_ground`() {
        assertTrue("abc123" !in pointsById(get("/api/trips", Seed.WINDOW), "aircraft"))
        val shown = get("/api/trips", Seed.WINDOW + ("include_ground" to "true"))
        assertEquals(10, pointsById(shown, "aircraft")["abc123"])
    }

    @Test
    fun `route filters the bus layer and leaves aircraft alone`() {
        val r = get("/api/trips", Seed.WINDOW + ("route" to "51"))
        assertEquals(setOf("B1", "B8"), pointsById(r, "bus").keys)        // B3 is culled
        assertEquals(Seed.AIRCRAFT_TRIPS, pointsById(r, "aircraft"))
    }

    @Test
    fun `min_points zero keeps the short trace`() {
        assertEquals(2, pointsById(get("/api/trips", Seed.WINDOW + ("min_points" to "0")), "bus")["B3"])
    }

    @Test
    fun `every spelling of the same layer set gives the same payload`() {
        val expected = get("/api/trips", Seed.WINDOW).text
        for (spelling in listOf("both", "bus,aircraft", "aircraft,bus", " bus , aircraft,bus ")) {
            assertEquals(expected, get("/api/trips", Seed.WINDOW + ("layer" to spelling)).text, spelling)
        }
    }

    @Test
    fun `rows that are all culled give 200 with no trips`() {
        val r = get("/api/trips", mapOf("start" to Seed.WINDOW_START, "end" to "2026-08-20T08:00:20-04:00", "min_points" to "4"))
        assertEquals(200, r.status)
        assertEquals(0, r.json["trips"].size())
    }

    @Test
    fun `large responses are gzipped when asked`() {
        val r = get("/api/trips", Seed.WINDOW, headers = mapOf("Accept-Encoding" to "gzip"))
        assertEquals(200, r.status)
        assertEquals("gzip", r.header("content-encoding"))
    }
}
