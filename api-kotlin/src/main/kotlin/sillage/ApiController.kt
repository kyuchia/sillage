package sillage

import io.micronaut.http.HttpRequest
import io.micronaut.http.HttpResponse
import io.micronaut.http.HttpStatus
import io.micronaut.http.MediaType
import io.micronaut.http.annotation.Controller
import io.micronaut.http.annotation.Get
import io.micronaut.http.annotation.Produces
import io.micronaut.http.annotation.QueryValue
import io.micronaut.http.server.exceptions.ExceptionHandler
import jakarta.inject.Singleton

/**
 * HTTP only: bind the query string, call [SillageService], write JSON.
 *
 * Every parameter is bound as a nullable String and validated by the service, so the
 * accepted spellings and error statuses match FastAPI's rather than Micronaut's
 * defaults (see Params.kt).
 */
@Controller("/api")
class ApiController(private val service: SillageService) {

    @Get("/trips")
    fun trips(
        @QueryValue start: String?,
        @QueryValue end: String?,
        @QueryValue hours: String?,
        @QueryValue layer: String?,
        @QueryValue route: String?,
        @QueryValue("min_points") minPoints: String?,
        @QueryValue("include_ground") includeGround: String?,
    ): HttpResponse<String> =
        json(200, service.trips(RawTripsParams(start, end, hours, layer, route, minPoints, includeGround)))

    @Get("/range")
    fun range(): HttpResponse<String> = json(200, service.range())

    @Get("/health")
    fun health(): HttpResponse<String> {
        val (status, body) = service.health()
        return json(status, body)
    }
}

/** Turns every [ApiException] into FastAPI's `{"detail": ...}` body with its status. */
@Produces
@Singleton
class ApiExceptionHandler : ExceptionHandler<ApiException, HttpResponse<String>> {
    override fun handle(request: HttpRequest<*>, exception: ApiException): HttpResponse<String> =
        json(exception.status, mapOf("detail" to exception.detail))
}

private fun json(status: Int, body: Any): HttpResponse<String> =
    HttpResponse.status<String>(HttpStatus.valueOf(status))
        .contentType(MediaType.APPLICATION_JSON_TYPE)
        .body(Json.write(body))
