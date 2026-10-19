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
| F1 | Unparseable start/end on a simulated layer → 500 | medium | open |
| F2 | NUL byte in start/end → 500 | low | open |
| F3 | start after end → 404 "no data" | low | open |
| F4 | hours ≤ 0 accepted, reads as an empty future window | low | open |
| F5 | 400 responses echo the generated SQL | low | open |
| F6 | Two different answers for "nothing to draw" | decision | open |
| F7 | Offset-less timestamps depend on the server's TimeZone | medium | open |
| F8 | Point budget is enforced only after every row is fetched | high | open |
| F9 | Duplicate fixes reach the payload | decision | kept — 0 in the archive |
| F10 | `route` documented as bus-only, also filters simulated layers | low | open |
| F11 | `null_island` check used `= 0` and missed every real null-island fix | — | fixed |
| F12 | Most bus "teleports" are stale-then-caught-up fixes, not GPS errors | data | open |
| F13 | The "continuous" August run is missing 28 of 307 hours, on the host side | docs | open |

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

### F2 — NUL byte in a timestamp is a 500

- **Found by**: `test_nul_byte_in_timestamp_is_400`.
- psycopg2 refuses to send a string containing `\x00` and raises `ValueError`, which
  the handler's `except (psycopg2.DataError, …)` does not cover.

### F3 — start after end reports "no data"

- **Found by**: `test_start_after_end_is_400`.
- `start=08:10&end=08:00` builds `fetched_at >= 08:10 AND fetched_at <= 08:00`, finds
  nothing, and answers `404 no data for this query`. The window is invalid, not empty;
  the simulator's own CLI already rejects it (`--end must be after --start`).

### F4 — non-positive `hours` is accepted

- **Found by**: `test_non_positive_hours_is_rejected` (0, −1, −24).
- `min_points` is declared `ge=0`; `hours` has no bound. `hours=-1` becomes
  `fetched_at >= NOW() - INTERVAL '-1 hours'`, a window in the future → 404.
  `hours=nan` / `inf` are already rejected (Postgres cannot parse the interval).

### F5 — error details include the SQL

- **Found by**: `test_error_detail_does_not_leak_sql`.
- The 400 detail is `f"bad query parameter: {e}"`, and psycopg2's message includes the
  `LINE 5: … latitude BETWEEN 44.5 AND 46.5 AND fetched_at >= 'notadate'` context.
  CORS limits the API to localhost, so this is hygiene rather than exposure.

### F6 — "nothing to draw" has two answers

- **Found by**: `test_window_with_no_rows_is_404_naming_the_empty_layers` and
  `test_rows_that_are_all_culled_give_200_with_no_trips` (both characterisation).
- No rows in the window → **404**. Rows exist but every trace is shorter than
  `min_points` → **200** with `"trips": []`. A client has to handle both.
- The plan that started this pass expected `200 + []` for an empty window; the API
  deliberately documents 404, and the frontend reports non-200s in its status line.
  Changing either side is a contract decision, so nothing was changed.

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

### F8 — the point budget is checked after the full fetch

- **Found by**: `test_over_budget_request_is_refused_without_fetching_everything`.
- `MAX_POINTS` is meant to make "an unbounded query fail fast with a clear message".
  It is compared after `fetch_layer()` has `fetchall()`-ed every matching row and
  `build_trips` has assembled them. On the seed the test fetched 116 rows to refuse a
  10-point budget; against the archive, `/api/trips?layer=bus` with no window would
  pull ~23.7M rows into Python before answering 413.
- **Next**: measure it with k6 against the real database before choosing a fix
  (e.g. a `COUNT(*)` or `LIMIT MAX_POINTS + 1` probe before the real query).

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

## Data-quality checks

`qa/data_quality.py` holds nine SQL checks. Before they are trusted on the archive,
`tests/db/test_quality_checks.py` runs them against the seed, where each defect is
planted a known number of times (null-island fix, missing position, duplicate fix,
~39 km jump, overnight collection gap), and asserts the exact counts.

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
- **Docs**: the README and v14 call the August run "continuous". It is continuous in
  the sense of one run with no dormancy, but not hour by hour. Wording is the
  owner's call; nothing in the README was changed.

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
- **Open**: whether to store the per-vehicle GPS timestamp if recording ever
  restarts; whether to drop only spikes.
- Aircraft teleports (2,067) not yet classified; OpenSky has the same poll-time
  vs. position-time split (`state_time` is the snapshot time).

Rerun:

    source venv/bin/activate
    python qa/data_quality.py --dsn "dbname=sillage" --out qa/data_quality_report.md
    psql sillage < qa/teleport_kinds.sql
