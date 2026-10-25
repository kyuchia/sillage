# Testing log

What the test suite found, how it found it, and what was done about it. Each finding
has an ID; where a test encodes the intended behaviour it is marked
`xfail(strict=True, reason="F…")`, so fixing the code makes the suite fail until the
marker is removed — the fix and this log cannot drift apart.

Status is one of **open** (found, not yet decided), **fixed**, or **kept** (decided to
leave as is, with the reason).

---

## 2026-10-19 — first pass: API and data checks against the seed

Suite: 154 tests — 136 new (125 passing, 11 expected failures, findings below) plus the 18
existing simulator tests. Coverage of `api/`,
`export/trips_query.py`, `qa/`, `simulate/`: 82% overall; `api/main.py` 97%,
`trips_query.py` 99%. `export_trips.py` shows 0% only because it is exercised in a
subprocess (`test_cli_export_writes_the_same_json_as_the_api`).

| ID | Finding | Severity | Status |
|---|---|---|---|
| F1 | Unparseable start/end on a simulated layer → 500 | medium | fixed |
| F2 | NUL byte in start/end → 500 | low | fixed |
| F3 | start after end → 404 "no data" | low | fixed |
| F4 | hours ≤ 0 accepted, reads as an empty future window | low | fixed |
| F5 | 400 responses echo the generated SQL | low | fixed |
| F6 | Two different answers for "nothing to draw" | decision | kept — both answers are intended |
| F7 | Offset-less timestamps depend on the server's TimeZone | medium | fixed |
| F8 | Point budget is enforced only after every row is fetched | high | fixed |
| F9 | Duplicate fixes reach the payload | decision | kept — 0 in the archive |
| F10 | `route` documented as bus-only, also filters simulated layers | low | fixed |
| F11 | `null_island` check used `= 0` and missed every real null-island fix | — | fixed |
| F12 | Most bus "teleports" are stale-then-caught-up fixes, not GPS errors | data | kept — positions are right, timing is off |
| F13 | The "continuous" August run is missing 28 of 307 hours, on the host side | docs | kept — README makes no "continuous" claim |
| F14 | More than 8 concurrent requests → 500 "connection pool exhausted" (Python) | high | fixed |
| F15 | Simulated layers sample the whole window before the budget is checked | medium | fixed |
| F16 | API error text reaches the page through `innerHTML` unescaped | low | fixed |
| F17 | Postgres dying after startup answers 500, not 503 | medium | fixed |
| F18 | `/api/health` and the 503 details publish the whole connection string | medium | fixed |

### F1 — unparseable timestamp on a simulated layer is a 500

- **Found by**: `test_simulated_layer_with_unparseable_timestamp_is_400`, after a
  parameter sweep of `/api/trips`.
- **What happens**: `/api/trips?layer=metro&start=notadate&end=…` calls
  `gtfs_sim.parse_dt()`, which raises `SystemExit` (a CLI idiom). `api/main.py` only
  wraps `gtfs_sim.simulate()` in `except SystemExit`, not `_sim_window()`, so the
  exception escapes the handler. Under uvicorn the request is a 500 and the server
  survives (checked by hand); under Starlette's `TestClient` it aborts the client.
- **Same input on a recorded layer** is a correct 400, because Postgres parses it.
- **Likely fix**: catch `SystemExit` around `_sim_window()` too and answer 400 — or
  have `parse_dt` raise `ValueError` and let the CLI turn that into an exit.
- **Fixed**: `_sim_window()` now catches the `SystemExit` from `parse_dt` and answers
  `400 bad query parameter: cannot parse datetime: '…'`. `parse_dt` is unchanged, so
  the simulator CLI still exits the same way. The xfail marker is removed; the test
  passes for both inputs.

### F2 — NUL byte in a timestamp is a 500

- **Found by**: `test_nul_byte_in_timestamp_is_400`.
- psycopg2 refuses to send a string containing `\x00` and raises `ValueError`, which
  the handler's `except (psycopg2.DataError, …)` does not cover.
- **Fixed**: the handler catches `ValueError` alongside the psycopg2 errors and answers
  400. This also covers a NUL byte in `route`, which reaches the same query.

### F3 — start after end reports "no data"

- **Found by**: `test_start_after_end_is_400`.
- `start=08:10&end=08:00` builds `fetched_at >= 08:10 AND fetched_at <= 08:00`, finds
  nothing, and answers `404 no data for this query`. The window is invalid, not empty;
  the simulator's own CLI already rejects it (`--end must be after --start`).
- **Fixed**: `/api/trips` checks the order of `start` and `end` before querying and answers
  `400 start must be before end`, for recorded and simulated layers alike. Strings
  Python cannot parse are left for Postgres to reject as before. Equal start and end
  are still accepted.

### F4 — non-positive `hours` is accepted

- **Found by**: `test_non_positive_hours_is_rejected` (0, −1, −24).
- `min_points` is declared `ge=0`; `hours` has no bound. `hours=-1` becomes
  `fetched_at >= NOW() - INTERVAL '-1 hours'`, a window in the future → 404.
  `hours=nan` / `inf` are already rejected (Postgres cannot parse the interval).
- **Fixed**: `hours` is declared `Query(None, gt=0)`, so 0 and negative values are
  rejected with 422 before any query, the same way an out-of-range `min_points` is.
  `allow_inf_nan=False` is set too, so `nan` and `inf` — previously a 400 from Postgres
  failing to parse the interval — are now also 422 from validation;
  `test_non_finite_hours_is_rejected` expects 422 accordingly.

### F5 — error details include the SQL

- **Found by**: `test_error_detail_does_not_leak_sql`.
- The 400 detail is `f"bad query parameter: {e}"`, and psycopg2's message includes the
  `LINE 5: … latitude BETWEEN 44.5 AND 46.5 AND fetched_at >= 'notadate'` context.
  CORS limits the API to localhost, so this is hygiene rather than exposure.
- **Fixed**: the 400 detail keeps only the first line of the database message, e.g.
  `bad query parameter: invalid input syntax for type timestamp with time zone: "notadate"`.
  The value the client sent is still named; the SQL context is not.

### F6 — "nothing to draw" has two answers

- **Found by**: `test_window_with_no_rows_is_404_naming_the_empty_layers` and
  `test_rows_that_are_all_culled_give_200_with_no_trips` (both characterisation).
- No rows in the window → **404**. Rows exist but every trace is shorter than
  `min_points` → **200** with `"trips": []`. A client has to handle both.
- The plan that started this pass expected `200 + []` for an empty window; the API
  deliberately documents 404, and the frontend reports non-200s in its status line.
  Changing either side is a contract decision, so nothing was changed.
- **Decision (2026-10-25): kept.** Both answers are intended: 404 says the window
  holds no rows, 200 with `"trips": []` says rows exist but none survive
  `min_points`. The two characterisation tests pin both behaviours, and the frontend
  handles both. No contract change.

### F7 — offset-less timestamps follow the server's TimeZone

- **Found by**: `test_naive_timestamps_mean_montreal_time_on_any_server`, run with the
  session TimeZone forced to UTC and to Asia/Taipei.
- For recorded layers Postgres interprets `start=2026-08-20 08:00` in the session
  TimeZone; for simulated layers `gtfs_sim.parse_dt` assumes Montréal. With
  `layer=all` the two halves of one payload can disagree by the server's offset.
- On the original machine the cluster was initialised in Montréal, so the two agree by
  accident. In CI (UTC) or on a cluster initialised in Taipei they do not.
- The frontend always sends offset-aware timestamps (from `/api/range`), so the page is
  unaffected; hand-written URLs and the CLI are.
- **Fixed**: `trips_query.use_montreal_time(conn)` runs `SET LOCAL TIME ZONE
  'America/Montreal'` at the start of each transaction — in the API for every borrowed
  connection, and in `export_trips.py` after it connects — so an offset-less start/end
  means Montréal time on any server, on both halves of a payload. `SET LOCAL` ends with
  the transaction, so pooled connections are not left changed.
  **Visible side effect**: timestamps in the payload and in `/api/range` now always
  carry Montréal offsets (−04:00 / −05:00) instead of the server's. On the original
  machine the cluster was already in Montréal time, so nothing changes there; in CI
  (UTC) the offsets now match the simulated layers. The full suite was also run with
  the server in UTC and in Asia/Taipei: 154 passed both times, including CLI/API parity.

### F8 — the point budget is checked after the full fetch

- **Found by**: `test_over_budget_request_is_refused_without_fetching_everything`.
- `MAX_POINTS` is meant to make "an unbounded query fail fast with a clear message".
  It is compared after `fetch_layer()` has `fetchall()`-ed every matching row and
  `build_trips` has assembled them. On the seed the test fetched 116 rows to refuse a
  10-point budget; against the archive, `/api/trips?layer=bus` with no window would
  pull ~23.7M rows into Python before answering 413.
- **Fixed** in both services: before the real query, `check_budget()` /
  `checkBudget()` counts each layer's matching rows with
  `SELECT count(*) FROM (SELECT 1 … LIMIT remaining + 1)`. The inner LIMIT lets
  Postgres stop as soon as the budget is exceeded, and without an ORDER BY nothing is
  sorted first. Over budget → 413 before a single row is fetched.
- **Behaviour change**: the budget now counts rows the query matches, before
  `min_points` drops short traces — the rows that would actually be held in memory.
  The 413 message reads "more than N points match this query". A request whose
  matched rows fit but whose simulated layers push it over is still caught by the
  original after-the-fact count (`test_simulated_points_can_still_push_the_payload_over_the_budget`).
- The strict xfail now passes; `test_payload_exactly_at_the_budget_is_allowed` moved
  its boundary from 119 kept points to 121 matched rows. Kotlin: `PointBudgetTest`,
  `PointBudgetBoundaryTest`.
- **Measured** (k6, 1-day bus window, 10 requests, see "Load tests" below): the 413
  went from p50 **30.04 s → 84 ms** in Python and **9.62 s → 94 ms** in Kotlin. The
  probe costs in-budget requests little: the 1-hour window moved 1.89 → 1.93 s
  (Python) and 636 → 681 ms (Kotlin).

### F9 — duplicate fixes reach the payload

- **Found by**: the `duplicate_fixes` check (planted in seed bus B7) and
  `test_duplicate_fix_passes_through_with_a_repeated_timestamp`.
- Both copies of a repeated `(vehicle_id, fetched_at)` are returned, giving a
  zero-length, zero-duration segment. `buildSpeeds()` guards `dt > 0`, so the map shows
  a one-point speed dip rather than an error; a duplicate also counts twice toward
  `min_points`.
- **Kept**: the archive run found **0** duplicate fixes in either table, so there is
  nothing to filter. The test stays as a characterisation of what would happen.

### F10 — `route` is documented as bus-only but also filters simulated layers

- **Found by**: `test_route_filter_also_applies_to_simulated_layers`.
- The `route` query description says "bus layer only". Aircraft ignore it, but
  `_add_simulated()` passes it to `gtfs_sim.simulate(route=…)`, so `layer=bus,metro`
  with a bus route number also empties the metro layer. The resulting 404 then lists
  only `bus` as the empty layer.
- Filtering metro by route may well be wanted (`--route` exists on the simulator CLI);
  the mismatch is between the description and the behaviour.

---

### F14 — the Python service answers 500 once more than 8 requests overlap

- **Found by**: the k6 `concurrency` scenario (`load/trips.js`), first dry run on the
  seed: 10 VUs → 14% of requests 500, 50 VUs → 59%, 100 VUs → 60%.
- **Cause**: `db_conn()` borrows from psycopg2's `ThreadedConnectionPool(maxconn=8)`,
  which raises `PoolError("connection pool exhausted")` instead of waiting. FastAPI runs
  the sync handler on a 40-thread pool, so the 9th overlapping request fails at once.
- **On the archive** (k6, 1-hour bus window, 60 s per stage): **95–98% of requests
  fail with 500** at 10, 50 and 100 VUs. A refused request returns in ~3 ms and its
  VU retries at once, so failures dominate the count; only 0.7–1.1 successful
  responses per second get through, against 0.53/s for one VU on its own.
- The Kotlin service's Hikari pool (also 8) queues instead: **0 errors** at 10 and
  100 VUs (5 × 503 at 50 VUs in one run, its 3 s `connectionTimeout`), ~7–8 req/s
  at every level, and latency growing with the queue (p50 1.3 s → 11–13 s).
- **Fixed**: `db_conn()` now takes one of 8 slots (`threading.BoundedSemaphore`, one
  per pooled connection) before `getconn()`, waiting up to `MTL_POOL_WAIT_SEC`
  (default 3 s, Hikari's `connectionTimeout` in Kotlin). A request that cannot get one
  in time is a **503 "database busy: no connection became free within 3 s. Try again
  shortly."** — a temporary condition a client may retry, not a server fault.
- Kotlin said "cannot connect to PostgreSQL" for the same situation. A Hikari timeout
  with no cause (every connection in use) now gives the same "database busy" 503;
  one caused by a failed connect keeps "cannot connect".
- Tests: `test_requests_beyond_the_pool_wait_for_a_free_connection`,
  `test_a_request_that_waits_too_long_is_503_not_500`; Kotlin `PoolBusyTest`. On the
  seed, k6 at 10/50/100 VUs went from 14–60% errors to none.
- **Measured on the archive** (`SERVICES=python SCENARIOS=concurrency load/bench.sh f14`,
  1-hour bus window, 60 s per stage; "after" is the F8-fixed run):

  | VUs | 200s after | 500s after | 200s f14 | 503s f14 | p50 f14 |
  |---:|---:|---:|---:|---:|---:|
  | 10 | 43 | 1,093 | 40 | 18 | 11.80 s |
  | 50 | 65 | 1,255 | 56 | 467 | 5.20 s |
  | 100 | 48 | 2,576 | 57 | 531 | 10.85 s |

- **What changed**: no 500s; a request that cannot get a connection waits 3 s and gets
  a 503 it can retry, instead of an instant 500 that its client retries at once. The
  error *rate* fell (96% → 31% at 10 VUs) mostly because refused requests now take 3 s
  instead of 3 ms, so there are fewer of them — it is not a fair headline.
- **What did not change**: successful responses, ~40–60 a minute, as before. The fix
  is about answering correctly under load, not about speed.
- **Why Python is slow under load**: one 1-hour request takes 1.9 s alone but ~12 s
  with 8 in flight. Assembling and serialising 155k points is CPU-bound Python, and the
  GIL runs one thread at a time, so concurrent requests mostly slow each other down.
  Kotlin, on the JVM, serves ~7 req/s at the same load. Scaling Python would take
  several worker processes (`uvicorn --workers N`); not done here.

---

## 2026-10-23 — load tests (k6) against the archive

`load/bench.sh before` on `fe0990e` (scripts only), `load/bench.sh after` on `e19d85e`
(F8 fixed); `python load/compare.py before after`. Owner's MacBook Pro (Apple silicon),
local Postgres with the 25.5M-row `sillage` archive, both services and k6 2.3.0 on the
same machine, `MTL_MAX_POINTS` 400,000, both pools 8 connections. Summaries are in
`load/results/`.

**Window size** — 1 VU, bus layer from 2026-08-20 08:00 EDT, 2 warm-up + 10 measured
requests each. The 1-day window matches more than 400,000 rows, so it is a 413.

| Service | Window | p50 before | p95 before | p50 after | p95 after | Status |
|---|---|---:|---:|---:|---:|---|
| Python | 1 min | 39 ms | 54 ms | 51 ms | 65 ms | 200 |
| Python | 10 min | 315 ms | 331 ms | 359 ms | 572 ms | 200 |
| Python | 1 h | 1.89 s | 2.06 s | 1.93 s | 2.10 s | 200 |
| Python | 1 day | **30.04 s** | 42.41 s | **84 ms** | 93 ms | 413 |
| Kotlin | 1 min | 20 ms | 39 ms | 20 ms | 34 ms | 200 |
| Kotlin | 10 min | 105 ms | 111 ms | 109 ms | 113 ms | 200 |
| Kotlin | 1 h | 636 ms | 682 ms | 681 ms | 698 ms | 200 |
| Kotlin | 1 day | **9.62 s** | 10.16 s | **94 ms** | 96 ms | 413 |

**Concurrency** — the 1-hour window, 60 s at each level. "Errors" are responses other
than 200/413.

| Service | VUs | p50 before | p95 before | req/s before | errors before | p50 after | p95 after | req/s after | errors after |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Python | 10 | 3 ms | 3.77 s | 16.2 | 95.5% | 3 ms | 2.86 s | 18.9 | 96.2% |
| Python | 50 | 93 ms | 48.53 s | 8.0 | 87.2% | 69 ms | 24.57 s | 22.0 | 95.1% |
| Python | 100 | 165 ms | 24.78 s | 32.6 | 96.2% | 151 ms | 11.81 s | 43.7 | 98.2% |
| Kotlin | 10 | 1.34 s | 1.86 s | 7.3 | 0.0% | 1.38 s | 1.71 s | 7.2 | 0.0% |
| Kotlin | 50 | 7.48 s | 13.39 s | 7.1 | 1.2% | 6.59 s | 11.10 s | 7.8 | 0.0% |
| Kotlin | 100 | 13.07 s | 36.54 s | 7.2 | 0.0% | 11.13 s | 34.77 s | 8.1 | 0.0% |

**Reading it**

- **F8**: refusing an over-budget request is now ~360× faster in Python and ~100× in
  Kotlin, and no longer scales with the size of the request. In-budget windows are
  unchanged within noise.
- **Python vs Kotlin, one request**: Kotlin is ~2–3× faster at every window size
  (1 h: 636 ms vs 1.89 s). The SQL alone takes ~676 ms (above), so Kotlin's time is
  almost all query, while Python spends ~1.2 s building and serialising 155k points.
- **Under load**: Python's numbers are dominated by F14 — its p50 of 3 ms is the
  pool refusing. Kotlin serves everything but its throughput is flat at ~7–8 req/s
  from 10 VUs up, so added users only add queueing (p50 ≈ VUs ÷ throughput, as
  Little's law predicts). A 1-hour window is a heavy request: ~155k points each.
- The req/s and errors in the Python rows count the fast 500s; they are not useful
  throughput.

---

## 2026-10-24 — end-to-end tests (Playwright) on the static demo

`tests/e2e/` (pytest-playwright 0.10, Playwright 1.63): the demo in `docs/` as GitHub
Pages serves it — no API, so `/api/range` is a 404 and the baked scenes play. Each
test runs at desktop (1280×800) and phone (390×844) size: 11 tests × 2 viewports = 22
per browser, 3 of them skipped by design (no clock or slider on a phone; the layout
check is phone-only). Skipped entirely without `--base-url`, so the pytest job does not
need a browser.

What is checked: MapLibre's and deck.gl's canvases exist; the first scene loads with
no warning and one legend row per mode; no uncaught exception or console error; every
request succeeds; the Controls panel opens and closes (full width on a phone); a scene
switch loads that scene's JSON and updates the URL; a layer toggle hides and shows a
mode; About opens and closes; on desktop the clock runs, pauses and follows the slider;
on a phone nothing scrolls sideways and the lockup and its links are on screen. No
pixel comparison.

One request is expected to fail: the `/api/range` 404 is how the page decides it is
static. Chromium and WebKit log it as a console error, so it is the only exception in
both the console and the request checks. Requests the browser cancels itself (MapLibre
dropping tiles it no longer needs) are not counted as failures.

| Run | Where | Target | Result |
|---|---|---|---|
| Chromium | cloud sandbox | `docs/` via `http.server`; deck.gl and MapLibre served from their npm packages and the Carto style stubbed, as the sandbox cannot reach unpkg or Carto | **19 passed, 3 skipped** |
| Chromium, WebKit | GitHub Actions (`e2e` job, one per browser) | this checkout's `docs/` | **passed** (19 passed, 3 skipped each) |
| Firefox | GitHub Actions | this checkout's `docs/` | cancelled at the 15-min job limit with no output (cause below); removed from the CI matrix |
| Firefox | Docker, `mcr.microsoft.com/playwright/python:v1.63.0-noble` on the Mac (no GPU) | this checkout's `docs/` | reproduced: no WebGL context, 22 errors in 30 s once the suite failed fast |
| Chromium, Firefox, WebKit | owner's Mac (Apple silicon), Playwright browsers Chromium 153, Firefox 155, WebKit 26.6 | `https://kyuchia.github.io/sillage/` | **57 passed, 9 skipped** (66 = 22 × 3; 5 min 8 s) |

**Observations (not findings)**

- **Without a GPU the replay is very slow.** In the sandbox (2 cores, software WebGL)
  the default scene (morning peak, 174k points) drew about 0.3 frames per second and
  the night scene about 2. Playwright waits for an element to be stable across frames
  before clicking, so clicks on the default scene timed out. The clicking tests
  therefore load the lightest scene in the manifest; the loading, console and request
  tests keep the default one. On a machine with a GPU this does not arise. Whether a
  visitor without hardware acceleration matters is a product question; it was not
  measured on real hardware.
- **Headless Firefox has no WebGL on a Linux machine without a GPU.** MapLibre cannot
  start, the page shows its error line, and the suite's "ready" condition never held,
  so every test waited out its 90 s: 22 × 90 s is past the 15-minute job limit. The CI
  log stayed empty because pytest buffers its progress line when not on a terminal.
  An earlier version of this entry said the browser hung, which was wrong.
  Reproduced in the Playwright Docker image and probed directly
  (`document.createElement("canvas").getContext("webgl")`), 12 combinations, all
  failing with `tryNativeGL () … Exhausted GL driver options`:
  - default prefs, `webgl.force-enabled`, and that plus `webgl.out-of-process=false`;
  - each with and without `LIBGL_ALWAYS_SOFTWARE=1`;
  - before and after installing Mesa (it was already present: `swrast_dri.so`,
    `libEGL_mesa.so.0`).
  Chromium and WebKit create a context on the same machine (Chromium reports
  SwiftShader). Headed Firefox under Xvfb hung in the same container and gave no
  result; not pursued. **Outcome**: CI runs Chromium and WebKit, Firefox runs on the
  Mac (57/57 non-skipped passed against the live demo). Kept from this round: a
  warn/error in the status line now fails the test at once with the page's message,
  and CI runs pytest with `-v`.
- WebKit on the runner first failed the overlay check: deck.gl's canvas is attached on
  MapLibre's `load`, which waits for the first basemap tiles to be drawn, and without a
  GPU that came after the scene was ready. The check now waits up to 60 s for the
  canvas instead of counting once (`fb9ebe5`).
- The bottom strip fades after a few idle seconds and returns on pointer movement (by
  design). The strip tests move the mouse over it before clicking, as a visitor would.
- deck.gl logs five deprecation warnings per load (`rounded` → `jointRounded` /
  `capRounded` on the trips layers). Warnings, not errors; not changed.

---

## 2026-10-25 — code review

A five-axis read of the whole project (correctness, readability, architecture, security,
performance), not driven by a failing test. Four findings worth fixing, below; the rest
were notes, and the two smallest are at the end of this section.

### F15 — simulated layers sample the whole window before the budget is checked

- **Found by**: code review, reading `_add_simulated()` against the F8 fix.
- The same class of bug as F8, on the other side of the merge. F8 moved the recorded
  layers' budget check ahead of the fetch; the simulated ones still did exactly what it
  fixed. `gtfs_sim.simulate()` sampled every trip in the window into memory and only
  then did `api_trips()` count the points against `MAX_POINTS`.
- Worse than F8, because nothing bounded the window either: `hours` was only validated
  `> 0`, and `start`/`end` could be years apart. `?layer=metro&hours=100000` expands to
  ~4,170 service days, each scanning every trip in the feed, on one synchronous worker
  thread, before any 413 could be returned.
- **Fixed**, in two parts:
  - `MAX_SIM_HOURS` (`MTL_MAX_SIM_HOURS`, default 48) caps the span a simulated layer
    will materialise. `_sim_window()` rejects anything wider with a **400** naming both
    the cap and the span asked for. Recorded layers are untouched: the cap is a property
    of materialising a schedule, not of reading rows.
  - `simulate()` takes `max_points`. In the per-trip loop, right after the cheap
    time-overlap reject and **before** any projection, it adds up the grid samples each
    trip could contribute and raises `gtfs_sim.OverBudget` as soon as the total exceeds
    the budget. `_add_simulated()` passes what the recorded payload has not already
    spent, and answers **413** with the same wording the recorded layers use.
  - Like F8's row count these are **candidate** points: a trip later dropped for having
    no shape or profile, or for yielding fewer than two samples, still counts. So the
    total is an upper bound on the payload, which is what makes it safe to refuse on.
  - `max_points=None` means no cap, so the CLI is unchanged — writing a huge file to
    disk is a legitimate thing to ask for, exactly as with the database export.
- The after-merge check in `api_trips()` stays, now as a final safety net rather than
  the thing that catches simulated layers. Both sides refuse before building anything,
  and both counts are upper bounds, so nothing is expected to reach it.
- Tests: the window cap via `start`/`end` and via `hours`, both asserting `simulate()`
  is never called; a recorded-only window proving the cap does not apply to it; the
  budget refusal asserting `trip_stop_profile()` is never called, which is what proves
  the check runs before sampling; the exact boundary (19 candidate points pass a budget
  of 19 and fail one of 18); and recorded points counting against the simulated budget.
  `simulate/test_gtfs_sim.py` adds `TestPointBudget`, including that `max_points=None`
  returns exactly what omitting it returns.
- `test_simulated_points_can_still_push_the_payload_over_the_budget` asserted the old
  after-the-fact message and became
  `test_recorded_points_count_against_the_simulated_budget`: the same scenario, now
  settled before any sample is generated.

### F16 — API error text reaches the page through `innerHTML` unescaped

- **Found by**: code review, then confirmed at runtime —
  `_resolve_layers("<img src=x onerror=alert(1)>")` answers
  `400 unknown layer '<img src=x onerror=alert(1)>'. Valid: bus, aircraft, ...`, echoing
  the parameter verbatim.
- `describeFailure()` in `docs/index.html` concatenated `body.detail` into an HTML
  string, which reaches the page as `status.notice` through `banner()` →
  `renderStatus()` → `el.innerHTML`. `layer` is in the page's `PASSTHROUGH` list, so
  `/docs/?layer=<img src=x onerror=alert(1)>` forwarded it, the 400 came back, and the
  fallback banner wrote the tag into the DOM. There is no CSP on the page, so the
  handler fired.
- Low, not high: a localhost dev tool with no cookies, tokens or session to steal, and
  unreachable on the Pages demo, where there is no API and the 404s are HTML rather than
  JSON with a `detail`. But it is a real injection, and the file already had `esc()` and
  already used it on the adjacent network-error path — these were missed calls, not a
  missing design.
- **Fixed**: escaped where the data enters rather than where it is displayed.
  `describeFailure()` escapes `body.detail`, `url` and `res.status`; `attachOverlay()`
  and the map `error` handler escape the exception text; the last-resort `.catch`
  escapes `String(err.message || err)`; `buildScenePicker()` escapes both the scene
  label and `describeScene()`, which returns plain text.
- The two `banner("API unavailable: ...")` calls pass `apiErr.message` through
  **unescaped on purpose**. By that point it is composed HTML whose data parts are each
  already escaped, and whose `<code>` / `<b>` are deliberate; escaping it again would
  print the tags literally and double-escape the payload.
- Audited every other `innerHTML` in the file: `list.innerHTML = ""` (twice) and the
  reset-view button are constants, and `buildLegend()` interpolates only `MODES`
  constants and a trip count. Nothing else takes external data.
- No test. There is no JS unit-test setup, and the e2e suite runs against the static
  demo where this path cannot be reached, so no framework was added for it. Verified by
  hand instead: with the API running, `/docs/?layer=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E`
  shows the tag as literal text in the banner and raises no alert.

### F17 — Postgres dying after startup answers 500, not 503

- **Found by**: code review, then reproduced by terminating the pooled backend.
- `db_conn()` converted `psycopg2.OperationalError` to a 503 only around `_get_pool()`.
  Once the pool existed, a server that went away surfaced later — at `getconn()`, or on
  the first statement of a connection the pool had handed back — as a bare
  `OperationalError`, and `/api/health`, whose only job is to say `database: down`,
  answered **500**.
- Two failures, not one: the `except Exception:` branch called `conn.rollback()` on the
  dead connection, which raised `InterfaceError: connection already closed` and
  **replaced** the original error, so the 500 did not even name the real cause. The
  reproduction showed exactly that — the traceback was the `InterfaceError`, not the
  server-died error.
- **Fixed**: `getconn()` failures become the existing "cannot connect" 503. Anything in
  the borrow-prepare-yield-rollback block raising `OperationalError` or `InterfaceError`
  becomes **503 "database unavailable (<label>): <first line>"**, chained with `from e`;
  `HTTPException` and every other exception from the caller's body pass through
  unchanged. The single cleanup rollback is wrapped so a failure there can never replace
  the error that caused it, and a connection known to be broken goes back with
  `putconn(conn, close=True)` so the next request gets a fresh one. The semaphore
  release is untouched.
- `/api/trips` and `/api/range` benefit too: a database failure mid-request is now a 503
  on those as well, where it used to be a 500.
- Tests (`tests/api/test_endpoints.py`): warm the pool, collect the backend pids the
  pool is holding idle, `pg_terminate_backend` **only** those from a separate
  connection, then assert health is 503 with `database: down` and that the detail
  mentions neither `InterfaceError` nor "connection already closed"; that the next call
  is 200 again, so the dead connection was discarded rather than reused; and the same
  for `/api/range` and `/api/trips`. `pg_terminate_backend` worked for the test role, so
  the monkeypatch fallback was not needed. The observed detail is
  `database unavailable (dbname=sillage_test): server closed the connection unexpectedly`.
- **Kotlin: the same bug, fixed the same way.** Checked rather than assumed: a probe gave
  the app a distinctive `application_name` and terminated only its own sessions. Hikari's
  borrow-time validation does **not** save it — it has a bypass window for a connection
  used moments ago, so the dead one is handed back and fails inside `readOnly()`. The
  port answered **500** `Internal Server Error: Connection is closed`, Hikari's
  closed-proxy error, because `readOnly()`'s `finally { c.rollback() }` masked the real
  exception exactly as Python's did.
- **Fixed**: the cleanup rollback is wrapped, so the real error survives — `FATAL:
  terminating connection due to administrator command`, **SQLState 57P01**. A
  `SQLException` whose state is class **08** (the standard's "connection exception") or
  exactly **57P01 / 57P02 / 57P03** (admin shutdown, crash shutdown, cannot connect now)
  becomes the same 503 "database unavailable (<label>)". Deliberately *not* all of class
  57: 57014 is `query_canceled`, a statement outcome rather than an outage. Nor 53300
  `too_many_connections`, which fails at connect time and already answers 503.
- Test: `DatabaseDiesMidLifeTest` — warm the pool, terminate this app's backends,
  assert 503 with `database: down`, then 200 on the next call. Emptying the 57Pxx set
  makes it fail with `expected: <503> but was: <500>`, so it is the mapping it tests.

### F18 — `/api/health` and the 503 details publish the whole connection string

- **Found by**: code review.
- Python returned `{"status": "ok", "database": "up", "dsn": PG_DSN}` and put `PG_DSN`
  into the "cannot connect" 503 detail; Kotlin did the same, `SillageFactory` passing the
  raw DSN as `dsnLabel` to both `Database` and `SillageService`. `PG_DSN` is free-form
  libpq and may carry `password=...`, or `user:password@` in the URI spelling. The
  default has no password, so nothing had leaked — but nothing prevented it, and CORS
  restricts browsers, not `curl`.
- **Fixed**: one label format, identical in both services — `host`, `port`, `dbname` in
  that order as space-separated `key=value`, only those present; `(default)` when none
  are, `(unparseable)` when the string cannot be read. User, password, options and
  everything else are dropped. Python's `dsn_label()` uses
  `psycopg2.extensions.parse_dsn`, which reads both spellings; Kotlin's `dsnLabel()`
  reuses the existing `parseUri` / `parseKeywords` branches. The JSON key stays `dsn`,
  so the response shape is unchanged.
- Tests: the same five label cases with the **same expected strings** on both sides, two
  password-bearing DSNs asserting neither the user nor the password survives, and an
  endpoint test that warms the pool, appends `password=hunter2` to `PG_DSN` and checks
  `/api/health` still reports the database up without it. The existing
  `test_data_endpoints_answer_503_not_500_when_postgres_is_down` compared only the
  message prefix, so it needed no change; its label is now
  `host=127.0.0.1 port=1 dbname=nowhere`.

### Two cleanups

`_get_pool()` had no lock, so two first requests arriving together could each build a
`ThreadedConnectionPool` and the one that lost the assignment would be left holding an
open connection nobody ever closed; creation is now double-checked under a module-level
`threading.Lock`. And two docstrings had gone stale now that collection has ended —
`get_range()` in `export/trips_query.py` claimed "both fetchers run continuously" and
`api_range()` in `api/main.py` claimed the tables "currently intersect for only ~1.7
hours". Both now explain why `overlap` exists without asserting anything about the
current state of the recording. Neither needed a new test.

---

## Data-quality checks

`qa/data_quality.py` holds nine SQL checks. Before they are trusted on the archive,
`tests/db/test_quality_checks.py` runs them against the seed, where each defect is
planted a known number of times (null-island fix, missing position, duplicate fix,
~39 km jump, overnight collection gap), and asserts the exact counts.
- **Fixed**: the behaviour was kept — filtering a simulated layer by route_id is useful,
  and the simulator CLI already offers `--route`. The description now reads "the bus
  route number, or the GTFS route_id on metro, train and rem. Aircraft ignore it."
  The 404 for a query that empties every layer now names the simulated layers too
  (`empty layers: bus, metro`). `test_route_description_names_the_simulated_layers`
  checks the OpenAPI description. `export_trips.py` keeps "bus layer only": the CLI has
  no simulated layers.

### Run against the archive — 2026-10-20

`python qa/data_quality.py` on the local `sillage` database (read-only session).
Bus ≈ 23.7M rows, aircraft ≈ 1.84M rows; exact totals are in
`qa/data_quality_report.md`.

| Check | Bus | Aircraft | Time (bus) |
|---|---:|---:|---:|
| null_coords | 0 | 0 | 3.8 s |
| null_ids | 0 | 0 | 5.5 s |
| outside_plausible_box | **409** (≈0.0017%) | 0 | 3.4 s |
| null_island | 0 → **409** after F11 | 0 | 2.0 s |
| duplicate_fixes | 0 | 0 | 63 s |
| geom_mismatch | 0 | 0 | 12 s |
| future_timestamps | 0 | 0 | 0 s |
| teleports | **16,262** jumps (≈0.07%) | **2,067** (≈0.11%) | 848 s |
| collection_gaps | 14 | 32 | 2.3 s |

**Clean**: no missing positions or ids, no duplicate fixes, `geom` agrees with
lon/lat everywhere, nothing timestamped in the future — on both tables.

**Implausible positions**: every one of the 409 out-of-box bus fixes rounds to
(0.0, 0.0) — 128 vehicles, 2026-04-14 → 08-31. They are all null island, as the v14
brief said; the first run reported `null_island = 0` only because of F11. All 409
are already removed by `PLAUSIBLE_BOX` in the query layer (8b0da87).

**Collection gaps**: every gap is accounted for (F13). Bus 14 = 1 in the April
night + the dormancy + 12 host outages. Aircraft 32 = the dormancy + 8 outages shared
with the bus table + 15 quota-shaped evening/afternoon gaps + 4 while the machine
travelled (08-31 → 09-01) + 4 others (08-19 start-up, 09-12/13, 09-18 before shutdown).

**Time-window query** (the exact `/api/trips` query, 2026-08-20 08:00 → 09:00):
bus 155,172 rows in **676 ms**, aircraft 1,432 rows in 11 ms. Both use the
`fetched_at` index, then sort by (id, fetched_at) — no sequential scan. This is the
baseline for the load tests and for F8.

### F13 — the "continuous" August run has 28 missing hours, all on the host side

- **Found by**: the `collection_gaps` tables in the report, cross-checked hour by hour
  between the two tables.
- During the bus run (08-18 23:00 → 08-31 18:00 EDT, 307 hours) **28 hours have no bus
  data at all (9%)**: 8 on the first morning (08-19 03:00–15:00, start-up), then
  08-22, 08-23 ×3, 08-27, 08-28 and 08-31 (2–7 h each).
- **Every one of those hours is also empty in the aircraft table** (one 08-31 hour
  is half-shared). The two fetchers hit different APIs on different schedules and
  fail together only when the machine running both is asleep, offline or off — so
  these are host outages, not STM or OpenSky problems.
- Aircraft-only gaps: 15 one- to three-hour evening or afternoon gaps (the quota
  shape), 4 around 08-31 → 09-01 as the machine travelled, 09-12/13 (7 h + 5 h), and
  one before shutdown. In September the quota gap sits at 19:00 EDT, as the README
  says; in late August it sits at 15:00 EDT on four days, which the README's
  midnight-UTC explanation does not cover. Not investigated further.
- A gap is only visible here if a whole clock hour is empty, so shorter outages are
  not counted — 28 hours is a floor.
- **Decision (2026-10-25): kept.** The README no longer calls the run continuous; it
  says the archive was collected "with gaps between recording periods". The
  hour-by-hour gaps are recorded here, which is enough. No README change.

### F11 — the null-island check used `= 0` (fixed)

- **Found by**: following up the report's `outside_plausible_box = 409` with a
  grouping query; all 409 rounded to (0.0, 0.0), yet `null_island` said 0.
- The fixes are tiny non-zero values, not exact zeros, so `latitude = 0 AND
  longitude = 0` matched none. The check now counts fixes within 1° of (0, 0).
- **Why the seed missed it**: the planted null-island fix was an exact 0. It is now
  (1e-7, −1e-7), and with the old equality check the seed test fails — so the test
  would have caught this had the seed matched reality.
- The query layer was never affected: it tests a box, not equality.

### F12 — most bus "teleports" are stale fixes catching up, not GPS errors

- **Found by**: `qa/teleport_kinds.sql`, one day (2026-08-20), 2,216 jumps over
  150 km/h:

  | Kind | Jumps | Share | Median distance | Median Δt |
  |---|---:|---:|---:|---:|
  | catch-up after frozen fix | 1,473 | 66% | 996 m | 20 s |
  | one-way jump | 637 | 29% | 991 m | 20 s |
  | spike: jump out | 49 | 2% | 2,142 m | 20 s |
  | spike: jump back | 57 | 3% | 2,344 m | 20 s |

- **Why**: `fetched_at` is the poll time, not the bus's GPS time (the feed's
  per-vehicle timestamp is not stored; `feed_timestamp` is the feed header). When
  the feed repeats a bus's last position for a few polls and then updates, ~1 km
  appears to happen in 20 s. ~1 km is about three polls' worth of travel at urban
  speed, which fits.
- Two thirds are confirmed catch-ups (the two fixes before the jump are identical).
  The one-way jumps have the same ~1 km profile and are probably the same thing, but
  this query does not prove it. Only the spikes (~5%) look like real GPS errors.
- **So**: the positions are mostly right and the timing is what is off. Deleting
  "teleports" in SQL would throw away good positions. The map already copes —
  `buildSpeeds()` replaces implausible segment speeds. For sonification, where
  timing is the point, this is worth knowing before scheduling arrivals.
- **Decision (2026-10-25): kept.** No rows are deleted, spikes included: the map
  already replaces implausible segment speeds, and the spikes are ~5% of jumps.
  Recording has ended, so storing the per-vehicle GPS timestamp is a note for any
  future recording, not a change now.
- Aircraft teleports (2,067) not yet classified; OpenSky has the same poll-time
  vs. position-time split (`state_time` is the snapshot time).

Rerun:

    source venv/bin/activate
    python qa/data_quality.py --dsn "dbname=sillage" --out qa/data_quality_report.md
    psql sillage < qa/teleport_kinds.sql
