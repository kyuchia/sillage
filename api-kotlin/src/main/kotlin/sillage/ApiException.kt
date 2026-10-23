package sillage

/**
 * An error with an HTTP status, raised anywhere below the controller.
 *
 * The body is `{"detail": ...}`, the shape FastAPI's HTTPException produces, so a
 * client cannot tell the two services apart by their errors. `detail` is a string,
 * except for 422 where it is FastAPI's list of per-parameter problems.
 */
class ApiException(val status: Int, val detail: Any) : RuntimeException(detail.toString())

/** One entry of a 422 `detail` list, in FastAPI's field order. */
data class ParamProblem(
    val type: String,
    val loc: List<String>,
    val msg: String,
    val input: String,
)

/** Every requested layer came back empty. Carries their names for the 404 message. */
class NoDataException(val empty: List<Layer>) :
    RuntimeException("no rows in any requested layer: ${empty.joinToString(", ") { it.key }}")

/**
 * The query matches more rows than the point budget; raised before anything is fetched
 * (F8). Matched rows bound the payload from above, since `min_points` only drops some.
 */
class OverBudgetException(val budget: Int) :
    RuntimeException("more than $budget rows match this query")
