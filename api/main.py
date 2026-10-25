"""
Sillage backend.

Serves the same trip JSON that `export/export_trips.py` writes to disk, so the browser
can change its time window without anyone re-running the export script. Both sides call
into `export/trips_query.py`; this module adds only HTTP concerns.

Run from the project root:

    uvicorn api.main:app --reload --port 8000

Then open http://localhost:8000/docs/ — the page is served by this same app, which also
retires the old "you must start the server from the project root" trap.
"""

import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
from psycopg2 import extensions as pgext
from psycopg2 import pool as pgpool
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "export"))
sys.path.insert(0, str(ROOT / "simulate"))

from trips_query import (  # noqa: E402
    NoDataError, OverBudgetError, VALID_LAYERS, build_trips, check_budget, get_range,
    use_montreal_time,
)
import gtfs_sim  # noqa: E402

# Recorded layers come from Postgres; simulated ones are materialised from archived
# static GTFS on the fly. They are mergeable: /api/trips?layer=all returns both kinds on
# one shared timeline, so the frontend cannot tell which is which.
DB_LAYERS = tuple(VALID_LAYERS)                  # bus, aircraft
SIM_LAYERS = tuple(gtfs_sim.MODES)               # metro, train, rem
ALL_LAYERS = DB_LAYERS + SIM_LAYERS

PG_DSN = os.environ.get("PG_DSN", "dbname=sillage")

# Point budget for a single /api/trips response. The frontend was benchmarked smooth at
# ~178k points, so the default is roughly 2x that: generous enough that no realistic
# window trips it, tight enough that an unbounded query fails fast with a clear message
# instead of streaming ~1M points into the browser. The CLI is deliberately NOT capped —
# writing a huge file to disk is a legitimate thing to want.
MAX_POINTS = int(os.environ.get("MTL_MAX_POINTS", 400_000))

# How wide a window the simulated layers will materialise. Unlike a database query, a
# schedule exists for any instant, so a year-wide request is not empty — it is thousands
# of service days, each scanning every trip in the feed. The point budget below bounds
# the output; this bounds the work done to discover it (F15). 48 h is well past any
# window the page asks for and still a fraction of a second of scanning.
MAX_SIM_HOURS = float(os.environ.get("MTL_MAX_SIM_HOURS", 48))

# Requests wait this long for a free database connection before a 503 (F14). The same
# 3 s as the Kotlin service's Hikari connectionTimeout, so both behave alike under load.
POOL_SIZE = 8
POOL_WAIT_SEC = float(os.environ.get("MTL_POOL_WAIT_SEC", 3))

app = FastAPI(title="Sillage API", version="1.0")

# A one-hour peak window is ~178k points; the JSON compresses roughly 5-10x, which is
# worth it even over localhost.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Localhost only — this is a local dev tool, not something to expose.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- database

def dsn_label(dsn):
    """The connection string reduced to what is safe to put in a response (F18).

    `PG_DSN` is free-form libpq and may carry `password=...` — or `user:password@` in the
    URI spelling — so it is never echoed as it stands. Only host, port and dbname survive,
    in that order; user, password and everything else are dropped. `parse_dsn` reads both
    spellings, and a string it cannot read is reported as such rather than guessed at.

    The Kotlin service builds the same label from the same three keys, so `/api/health`
    answers identically on both (`dsnLabel` in api-kotlin/.../Database.kt).
    """
    try:
        parsed = pgext.parse_dsn(dsn)
    except psycopg2.Error:
        return "(unparseable)"
    kept = [f"{key}={parsed[key]}" for key in ("host", "port", "dbname") if parsed.get(key)]
    return " ".join(kept) or "(default)"


# Built lazily so the app still boots (and still serves web/) when Postgres is down.
# The lock matters because FastAPI runs this sync code on ~40 threads: two first
# requests arriving together would otherwise each build a pool, and the one that lost
# the assignment would be left holding an open connection nobody ever closes.
_POOL = None
_POOL_LOCK = threading.Lock()

# psycopg2's pool raises PoolError the moment all its connections are out, and FastAPI
# runs this sync code on ~40 threads, so a 9th overlapping request used to be a 500
# (F14). One slot per connection makes the extra requests queue instead.
_SLOTS = threading.BoundedSemaphore(POOL_SIZE)


def _get_pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:          # another thread may have built it while we waited
                _POOL = pgpool.ThreadedConnectionPool(minconn=1, maxconn=POOL_SIZE,
                                                      dsn=PG_DSN)
    return _POOL


@contextmanager
def db_conn():
    """Borrow a pooled connection, always returning it in a clean state.

    Every query here is read-only, so the transaction is rolled back on the way out
    either way — otherwise a connection returns to the pool holding an idle
    transaction, or a failed one that poisons the next request that borrows it.

    A database that dies *after* the pool was built fails here rather than at connect
    time, so those errors are converted too: the pool hands back a connection whose
    server is gone and the first statement on it raises (F17). Every such failure is a
    503, never a 500, and the dead connection is closed rather than pooled again.
    """
    try:
        pool = _get_pool()
    except psycopg2.OperationalError as e:
        raise HTTPException(
            status_code=503,
            detail=f"cannot connect to PostgreSQL ({dsn_label(PG_DSN)}): {e}")

    if not _SLOTS.acquire(timeout=POOL_WAIT_SEC):
        raise HTTPException(
            status_code=503,
            detail=(f"database busy: no connection became free within "
                    f"{POOL_WAIT_SEC:g} s. Try again shortly."),
        )
    try:
        try:
            conn = pool.getconn()
        except psycopg2.OperationalError as e:
            # The pool had nothing idle and opening a new connection failed.
            raise HTTPException(
                status_code=503,
                detail=f"cannot connect to PostgreSQL ({dsn_label(PG_DSN)}): {e}") from e

        broken = False
        try:
            use_montreal_time(conn)
            yield conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            # The server went away mid-request: this connection is unusable, and the
            # answer is 503 (unavailable), not 500 (a bug here).
            broken = True
            raise HTTPException(
                status_code=503,
                detail=(f"database unavailable ({dsn_label(PG_DSN)}): "
                        f"{str(e).strip().splitlines()[0]}"),
            ) from e
        finally:
            # One rollback for both paths. On a connection whose server is gone it
            # raises in its own right, and that must never replace the error that
            # brought us here — nor leave the connection to be handed out again.
            try:
                conn.rollback()
            except psycopg2.Error:
                broken = True
            pool.putconn(conn, close=broken or bool(conn.closed))
    finally:
        _SLOTS.release()


# ---------------------------------------------------------------- layer parsing

def _resolve_layers(layer):
    """Turn the `layer` parameter into an ordered, de-duplicated list of layer names.

    Accepts a single name, one of the `both`/`all` aliases, or a comma-separated list
    mixing recorded and simulated layers (e.g. `metro,rem,train`). Aliases may appear
    inside a list too, so `both,metro` is the recorded pair plus metro.

    Order follows ALL_LAYERS rather than the order given, so the same set always produces
    the same payload regardless of how it was spelled.
    """
    raw = [part.strip() for part in (layer or "").split(",")]
    raw = [part for part in raw if part]
    if not raw:
        raise HTTPException(status_code=400, detail="layer must not be empty")

    want = set()
    for part in raw:
        if part == "both":
            want.update(DB_LAYERS)
        elif part == "all":
            want.update(ALL_LAYERS)
        elif part in ALL_LAYERS:
            want.add(part)
        else:
            raise HTTPException(
                status_code=400,
                detail=(f"unknown layer {part!r}. Valid: {', '.join(ALL_LAYERS)}, "
                        f"both, all — or a comma-separated list of them"),
            )
    return [l for l in ALL_LAYERS if l in want]


def _check_window_order(start, end):
    """Reject a window whose start is after its end (F3).

    Such a window is invalid, not empty, and would otherwise reach the database and
    come back as "no data". Strings Python cannot parse are left alone here: Postgres
    accepts more formats and reports the bad ones itself.
    """
    if not (start and end):
        return
    try:
        lo, hi = gtfs_sim.parse_dt(start), gtfs_sim.parse_dt(end)
    except SystemExit:
        return
    if lo > hi:
        raise HTTPException(status_code=400, detail="start must be before end")


# ---------------------------------------------------------------- simulated layers

def _sim_window(start, end, hours):
    """Resolve the absolute window the simulator needs.

    Unlike a DB query, a schedule can be materialised for any instant in history, so an
    unbounded request has no natural answer — it is rejected rather than guessed at, and
    so is one wider than MAX_SIM_HOURS (F15).
    """
    if start and end:
        try:
            lo, hi = gtfs_sim.parse_dt(start), gtfs_sim.parse_dt(end)
        except SystemExit as e:
            # parse_dt is shared with the CLI, where SystemExit is the right exit;
            # here it is a bad parameter, not a server error (F1)
            raise HTTPException(status_code=400,
                                detail=f"bad query parameter: {str(e).lstrip('❌ ')}")
    elif hours is not None:
        now = datetime.now(gtfs_sim.TZ)
        lo, hi = now - timedelta(hours=hours), now
    else:
        raise HTTPException(
            status_code=400,
            detail=("simulated layers (metro, train, rem) need an explicit window: "
                    "pass start and end, or hours"),
        )

    span_hours = (hi - lo).total_seconds() / 3600
    if span_hours > MAX_SIM_HOURS:
        raise HTTPException(
            status_code=400,
            detail=(f"simulated layers (metro, train, rem) accept at most "
                    f"{MAX_SIM_HOURS:g} hours per request; this window is "
                    f"{round(span_hours, 1):g} hours. Narrow start/end or hours."),
        )
    return lo, hi


def _add_simulated(payload, sim_layers, start, end, hours, route):
    """Materialise simulated modes and merge them onto one shared timeline.

    Both sides are re-based onto a single origin so `timestamps` stay comparable across
    recorded and simulated trips — deck.gl interpolates every layer against one
    `currentTime`, so a per-source origin would desynchronise them.

    The re-basing is a scalar shift per trip, not a per-point datetime conversion: a
    peak window carries ~178k points and rebuilding a datetime for each costs more than
    the query that produced them.
    """
    lo, hi = _sim_window(start, end, hours)

    # Whatever the recorded layers already spent is not available to the schedules: the
    # budget is for the merged payload, so each mode is given only what is left (F15).
    remaining = MAX_POINTS
    if payload is not None:
        remaining -= sum(len(t["path"]) for t in payload["trips"])

    sim_trips = []
    for mode in sim_layers:
        try:
            trips, _stats = gtfs_sim.simulate(mode, lo, hi, route=route, verbose=False,
                                              max_points=remaining)
        except gtfs_sim.OverBudget:
            # Refused before sampling, so the message names what the query matches
            # rather than what was built — the same wording as the recorded layers.
            raise HTTPException(status_code=413, detail=_over_budget_detail(
                f"more than {MAX_POINTS:,} points match this query, which"))
        except SystemExit as e:
            # _latest_zip raises SystemExit when a feed was never archived
            raise HTTPException(status_code=503, detail=str(e))
        remaining -= sum(len(tr["path"]) for tr in trips)
        sim_trips.extend(trips)

    if not sim_trips and payload is None:
        return None

    # epoch seconds throughout — cheap to compare and to shift
    sim_epochs = [[t.timestamp() for t in tr["times"]] for tr in sim_trips]
    candidates = [e[0] for e in sim_epochs if e]
    db_start_epoch = None
    if payload is not None:
        db_start_epoch = datetime.fromisoformat(payload["start_time"]).timestamp()
        candidates.append(db_start_epoch)
    if not candidates:
        return payload
    origin = min(candidates)

    trips_out = []
    if payload is not None:
        shift = db_start_epoch - origin        # one scalar for every recorded trip
        for t in payload["trips"]:
            if shift:
                t["timestamps"] = [ts + shift for ts in t["timestamps"]]
            trips_out.append(t)

    for tr, epochs in zip(sim_trips, sim_epochs):
        trips_out.append({
            "mode": tr["mode"],
            "id": tr["id"],
            "label": tr["label"],
            "path": tr["path"],
            "timestamps": [e - origin for e in epochs],
        })

    ends = [max(e) for e in sim_epochs if e]
    if payload is not None:
        ends.append(datetime.fromisoformat(payload["end_time"]).timestamp())
    end_epoch = max(ends)

    return {
        "start_time": datetime.fromtimestamp(origin, gtfs_sim.TZ).isoformat(),
        "end_time": datetime.fromtimestamp(end_epoch, gtfs_sim.TZ).isoformat(),
        "duration_sec": end_epoch - origin,
        "modes": sorted({t["mode"] for t in trips_out}),
        "trips": trips_out,
    }


# ---------------------------------------------------------------- API

@app.get("/api/trips")
def api_trips(
    start: str | None = Query(None, description='e.g. "2026-04-14 23:15-04:00"'),
    end: str | None = Query(None),
    hours: float | None = Query(None, gt=0, allow_inf_nan=False,
                               description="last N hours; needs a live fetcher"),
    layer: str = Query("both", description=("bus | aircraft | metro | train | rem | both | all, "
                                        "or a comma-separated list e.g. metro,rem")),
    route: str | None = Query(None, description=(
        "filter to one route: the bus route number, or the GTFS route_id on "
        "metro, train and rem. Aircraft ignore it.")),
    # ge=0 rather than ge=1 so the CLI's `--min-points 0` has an exact HTTP equivalent;
    # 0 and 1 both keep everything, since a trace group is never empty.
    min_points: int = Query(3, ge=0, description="drop traces shorter than this"),
    include_ground: bool = Query(False, description="keep parked aircraft"),
):
    """Return the same JSON shape `export/export_trips.py` writes to data/trips.json.

    `layer` accepts the recorded layers (bus, aircraft), the simulated ones
    (metro, train, rem), `both` (the two recorded layers — unchanged default), or `all`.
    """
    want = _resolve_layers(layer)
    _check_window_order(start, end)

    db_layers = [l for l in want if l in DB_LAYERS]
    sim_layers = [l for l in want if l in SIM_LAYERS]

    payload = None
    db_empty = []
    if db_layers:
        with db_conn() as conn:
            try:
                check_budget(conn, db_layers, MAX_POINTS, start=start, end=end,
                             hours=hours, route=route, include_ground=include_ground)
                payload = build_trips(
                    conn, db_layers,
                    start=start, end=end, hours=hours, route=route,
                    min_points=min_points, include_ground=include_ground,
                )
            except OverBudgetError as e:
                raise HTTPException(status_code=413, detail=_over_budget_detail(
                    f"more than {e.budget:,} points match this query, which"))
            except NoDataError as e:
                # Only fatal when nothing else was asked for; with simulated layers in
                # play an empty DB window is a partial result, not a failure.
                if not sim_layers:
                    raise HTTPException(
                        status_code=404,
                        detail=f"no data for this query (empty layers: {', '.join(e.empty)})",
                    )
                db_empty = e.empty
            except (psycopg2.DataError, psycopg2.errors.InvalidDatetimeFormat,
                    ValueError) as e:
                # Almost always an unparseable start/end string; Postgres does the
                # parsing, so this is where a bad timestamp surfaces. ValueError is
                # psycopg2 refusing to send a string containing a NUL byte (F2).
                # First line only: psycopg2 appends the offending SQL ("LINE 5: …"),
                # which is not the client's business (F5).
                first = str(e).strip().splitlines()[0]
                raise HTTPException(status_code=400, detail=f"bad query parameter: {first}")

    if sim_layers:
        payload = _add_simulated(payload, sim_layers, start, end, hours, route)

    if payload is None:
        # Nothing came back from any layer: name the recorded ones the database found
        # empty and every simulated one, since `route` filters those too (F10).
        empty = db_empty + sim_layers
        raise HTTPException(
            status_code=404,
            detail=f"no data for this query (empty layers: {', '.join(empty)})",
        )

    # A final safety net. Both sides now refuse before building anything — recorded rows
    # are counted before the fetch (F8), simulated samples before the sampling (F15) —
    # and both counts are upper bounds, so nothing is expected to reach this. It stays
    # because it is the one check that sees the payload itself.
    total_points = sum(len(t["path"]) for t in payload["trips"])
    if total_points > MAX_POINTS:
        raise HTTPException(status_code=413,
                            detail=_over_budget_detail(f"{total_points:,} points"))

    # Returned as an explicit Response so FastAPI skips jsonable_encoder — recursing a
    # 178k-point payload field by field costs more than the query itself.
    return JSONResponse(content=payload)


def _over_budget_detail(what):
    return (f"payload too large: {what} exceeds the {MAX_POINTS:,} budget. "
            f"Narrow the window (start/end or hours), pick a single layer, "
            f"or raise MTL_MAX_POINTS.")


@app.get("/api/range")
def api_range():
    """Report what data actually exists, so the client can pick a sane default window.

    Includes `overlap`, the window where every layer has data at once. The two recorded
    tables are filled by separate fetchers and need not span the same period, so a client
    that defaulted to the bus range alone could render an empty aircraft layer and look
    like a bug. How wide the overlap actually is depends on the archive in the database;
    the client decides what to do with it (see the MAX_DERIVED_HOURS note in
    docs/index.html).
    """
    with db_conn() as conn:
        return get_range(conn)


@app.get("/api/health")
def api_health():
    """Cheap liveness probe that also reports whether Postgres is actually reachable."""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"status": "ok", "database": "up", "dsn": dsn_label(PG_DSN)}
    except HTTPException as e:
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "database": "down",
                                     "detail": e.detail})


# ---------------------------------------------------------------- static

# `docs/index.html` falls back to fetching ../data/trips.json, which resolves to /data/
# when the page is served from /docs/ — so both directories are mounted, and the static
# workflow keeps working through this server too.
DATA_DIR = ROOT / "data"
if DATA_DIR.is_dir():
    app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

# Served as /docs/ because GitHub Pages publishes from main:/docs — one directory feeds
# both the local API mode and the static Pages deployment, so they cannot drift.
app.mount("/docs", StaticFiles(directory=str(ROOT / "docs"), html=True), name="docs")


@app.get("/")
def root():
    return RedirectResponse(url="/docs/")
