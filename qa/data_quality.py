"""
Data-quality audit for the recorded archive (vehicle_positions, aircraft_positions).

One set of SQL checks, two uses:

  * tests/db/test_quality_checks.py runs them against the seed, where every defect is
    planted on purpose, and asserts the exact counts — so the checks themselves are
    tested before anyone trusts what they say about the real data.
  * this CLI runs the same checks against the real archive and writes a report:

        source venv/bin/activate
        python qa/data_quality.py --dsn "dbname=sillage" --out qa/data_quality_report.md

The connection is opened read-only (SET SESSION CHARACTERISTICS ... READ ONLY), so a
mistake in this file cannot write to the archive. Expect several minutes on ~25M rows:
the teleport check walks every vehicle's fixes in order with a window function.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "export"))

from trips_query import LAYERS, PLAUSIBLE_BOX  # noqa: E402

# Fastest believable ground speed between two consecutive fixes. Above this the pair is
# a GPS jump, not movement. Buses: STM runs on autoroutes at ~100 km/h, so 150 leaves
# room for timestamp jitter. Aircraft: a jet with a jet-stream tailwind can touch
# ~1,200 km/h over the ground; 1,500 is past anything a civil aircraft does.
MAX_KMH = {"bus": 150, "aircraft": 1500}

# A window the README already uses as its fixed example, inside the archive.
EXPLAIN_WINDOW = ("2026-08-20 08:00-04:00", "2026-08-20 09:00-04:00")

PLAUSIBLE = ("latitude IS NOT NULL AND longitude IS NOT NULL "
             "AND longitude BETWEEN %(min_lon)s AND %(max_lon)s "
             "AND latitude BETWEEN %(min_lat)s AND %(max_lat)s")

# name -> (what it means, SQL returning one integer). {table}/{id} are filled per layer.
CHECKS = {
    "null_coords": (
        "Fix with no latitude or longitude",
        "SELECT COUNT(*) FROM {table} WHERE latitude IS NULL OR longitude IS NULL",
    ),
    "null_ids": (
        "Fix with no vehicle id / icao24, so it cannot join a trace",
        "SELECT COUNT(*) FROM {table} WHERE {id} IS NULL OR btrim({id}) = ''",
    ),
    "outside_plausible_box": (
        "Position outside greater Montréal ±100 km (PLAUSIBLE_BOX)",
        "SELECT COUNT(*) FROM {table} "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND NOT ("
        "longitude BETWEEN %(min_lon)s AND %(max_lon)s "
        "AND latitude BETWEEN %(min_lat)s AND %(max_lat)s)",
    ),
    "null_island": (
        "Position within 1° of (0, 0) — a subset of the row above",
        # Not `= 0`: the archive's null-island fixes are tiny non-zero values that
        # round to (0.0, 0.0), and an equality test counted none of the 409.
        "SELECT COUNT(*) FROM {table} WHERE abs(latitude) < 1 AND abs(longitude) < 1",
    ),
    "duplicate_fixes": (
        "Extra copies of the same (id, fetched_at)",
        "SELECT COALESCE(SUM(n - 1), 0) FROM ("
        "  SELECT COUNT(*) AS n FROM {table} GROUP BY {id}, fetched_at HAVING COUNT(*) > 1"
        ") d",
    ),
    "geom_mismatch": (
        "PostGIS geom missing, not SRID 4326, or not equal to lon/lat",
        "SELECT COUNT(*) FROM {table} "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND ("
        "  geom IS NULL OR ST_SRID(geom::geometry) <> 4326"
        "  OR abs(ST_X(geom::geometry) - longitude) > 1e-9"
        "  OR abs(ST_Y(geom::geometry) - latitude) > 1e-9)",
    ),
    "future_timestamps": (
        "fetched_at later than now",
        "SELECT COUNT(*) FROM {table} WHERE fetched_at > now()",
    ),
    "teleports": (
        "Consecutive plausible fixes implying more than MAX_KMH",
        "WITH p AS ("
        "  SELECT {id} AS uid, fetched_at, geom FROM {table} WHERE " + PLAUSIBLE +
        "), s AS ("
        "  SELECT EXTRACT(EPOCH FROM fetched_at - LAG(fetched_at) OVER w) AS dt,"
        "         ST_Distance(geom, LAG(geom) OVER w) AS metres"
        "  FROM p WINDOW w AS (PARTITION BY uid ORDER BY fetched_at)"
        ") SELECT COUNT(*) FROM s WHERE dt > 0 AND metres / dt * 3.6 > %(max_kmh)s",
    ),
    "collection_gaps": (
        "Breaks of more than one hour between hours that hold data",
        "WITH h AS (SELECT DISTINCT date_trunc('hour', fetched_at) AS hr FROM {table}),"
        " g AS (SELECT hr - LAG(hr) OVER (ORDER BY hr) AS step FROM h)"
        " SELECT COUNT(*) FROM g WHERE step > INTERVAL '1 hour'",
    ),
}

# What the archive does about each defect today. Status, not plans.
HANDLING = {
    "null_coords": "Dropped by the query layer (`latitude/longitude IS NOT NULL`).",
    "null_ids": "Not filtered; such rows group under a NULL id.",
    "outside_plausible_box": "Dropped by the query layer since 8b0da87 (`PLAUSIBLE_BOX`).",
    "null_island": "Dropped with the row above.",
    "duplicate_fixes": "Not filtered; both copies reach the payload (testing log F9).",
    "geom_mismatch": "`geom` is a generated column; any count here is a schema problem.",
    "future_timestamps": "Not filtered.",
    "teleports": "Not filtered in SQL; the frontend's `buildSpeeds()` clamps them on screen.",
    "collection_gaps": ("The 125-day dormancy and OpenSky quota resets are expected; gaps "
                        "shared by both tables are host outages (testing log F13)."),
}


def _params(layer):
    return {**PLAUSIBLE_BOX, "max_kmh": MAX_KMH[layer]}


def run_checks(conn, layers=tuple(LAYERS), checks=tuple(CHECKS), progress=None):
    """Return {layer: {check: count}} for the requested layers and checks."""
    out = {}
    with conn.cursor() as cur:
        for layer in layers:
            cfg = LAYERS[layer]
            out[layer] = {}
            for name in checks:
                sql = CHECKS[name][1].format(table=cfg["table"], id=cfg["id_col"])
                t0 = time.monotonic()
                cur.execute(sql, _params(layer))
                out[layer][name] = int(cur.fetchone()[0])
                if progress:
                    progress(f"  {layer:9} {name:22} {out[layer][name]:>12,}"
                             f"   {time.monotonic() - t0:6.1f}s")
    return out


def gap_list(conn, layer):
    """Every collection gap as (last hour with data, next hour with data, hours missing)."""
    table = LAYERS[layer]["table"]
    with conn.cursor() as cur:
        cur.execute(f"""
            WITH h AS (SELECT DISTINCT date_trunc('hour', fetched_at) AS hr FROM {table}),
                 g AS (SELECT LAG(hr) OVER (ORDER BY hr) AS prev, hr FROM h)
            SELECT prev, hr, EXTRACT(EPOCH FROM hr - prev) / 3600 - 1 AS missing
            FROM g WHERE hr - prev > INTERVAL '1 hour' ORDER BY prev
        """)
        return cur.fetchall()


def overview(conn, layer):
    table = LAYERS[layer]["table"]
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM {table}")
        return cur.fetchone()


def fetched_at_indexes(conn, layer):
    with conn.cursor() as cur:
        cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s",
                    (LAYERS[layer]["table"],))
        return [(n, d) for n, d in cur.fetchall() if "(fetched_at)" in d]


def explain_window(conn, layer, start, end):
    """EXPLAIN ANALYZE the exact query /api/trips runs for a one-hour window."""
    import trips_query
    cfg = LAYERS[layer]
    where, params = trips_query.build_where(layer, start=start, end=end)
    sql = (f"SELECT fetched_at, {cfg['id_col']} AS uid, {cfg['label_col']} AS label, "
           f"latitude, longitude FROM {cfg['table']} WHERE {' AND '.join(where)} "
           f"ORDER BY {cfg['id_col']}, fetched_at")
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        plan = cur.fetchone()[0][0]

    nodes = []

    def walk(n):
        nodes.append((n["Node Type"], n.get("Index Name")))
        for child in n.get("Plans", []):
            walk(child)

    walk(plan["Plan"])
    return {"execution_ms": plan["Execution Time"], "planning_ms": plan["Planning Time"],
            "rows": plan["Plan"].get("Actual Rows"), "nodes": nodes}


# ---------------------------------------------------------------- report

def _pct(n, total):
    return f"{n / total * 100:.4f}%" if total else "—"


def write_report(conn, path, progress=print):
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'America/Montreal'")
    totals = {layer: overview(conn, layer) for layer in LAYERS}
    progress("running checks…")
    results = run_checks(conn, progress=progress)

    L = ["# Data quality report", "",
         f"Generated {started:%Y-%m-%d %H:%M} UTC by `qa/data_quality.py` against "
         f"`{conn.get_dsn_parameters().get('dbname')}`. Times below are Montréal local.", "",
         "## Archive", "", "| Layer | Rows | First fix | Last fix |", "|---|---:|---|---|"]
    for layer, (n, lo, hi) in totals.items():
        L.append(f"| {layer} | {n:,} | {lo:%Y-%m-%d %H:%M} | {hi:%Y-%m-%d %H:%M} |")

    L += ["", "## Checks", "",
          "Each check is verified against planted defects in `tests/fixtures/seed.sql` "
          "before it is run here.", "",
          "| Check | Bus | % | Aircraft | % | Handling today |",
          "|---|---:|---:|---:|---:|---|"]
    for name, (desc, _) in CHECKS.items():
        b, a = results["bus"][name], results["aircraft"][name]
        # A gap count is not a share of rows
        pb, pa = (("—", "—") if name == "collection_gaps" else
                  (_pct(b, totals["bus"][0]), _pct(a, totals["aircraft"][0])))
        L.append(f"| **{name}** — {desc} | {b:,} | {pb} | {a:,} | {pa} | {HANDLING[name]} |")
    L += ["", f"Teleport thresholds: bus {MAX_KMH['bus']} km/h, "
              f"aircraft {MAX_KMH['aircraft']} km/h."]

    L += ["", "## Collection gaps", ""]
    for layer in LAYERS:
        gaps = gap_list(conn, layer)
        L += [f"### {layer} ({len(gaps)})", "",
              "| Last hour with data | Next hour with data | Hours missing |", "|---|---|---:|"]
        for prev, nxt, missing in gaps:
            L.append(f"| {prev:%Y-%m-%d %H:00} | {nxt:%Y-%m-%d %H:00} | {float(missing):,.0f} |")
        L.append("")

    L += ["## Time-window query plan", "",
          f"`EXPLAIN ANALYZE` of the exact `/api/trips` query for "
          f"{EXPLAIN_WINDOW[0]} → {EXPLAIN_WINDOW[1]}.", "",
          "| Layer | fetched_at index | Rows | Execution | Plan nodes |", "|---|---|---:|---:|---|"]
    for layer in LAYERS:
        idx = ", ".join(n for n, _ in fetched_at_indexes(conn, layer)) or "**none**"
        ex = explain_window(conn, layer, *EXPLAIN_WINDOW)
        nodes = " → ".join(f"{t}" + (f" ({i})" if i else "") for t, i in ex["nodes"])
        L.append(f"| {layer} | {idx} | {ex['rows']:,} | {ex['execution_ms']:,.0f} ms | {nodes} |")

    L += ["", "## Raw counts", "", "```json", json.dumps(results, indent=2), "```", ""]
    Path(path).write_text("\n".join(L))
    progress(f"wrote {path}")
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dsn", default=os.environ.get("PG_DSN", "dbname=sillage"))
    ap.add_argument("--out", default=str(ROOT / "qa" / "data_quality_report.md"))
    args = ap.parse_args()

    conn = psycopg2.connect(args.dsn)
    conn.set_session(readonly=True)
    try:
        write_report(conn, args.out)
    finally:
        conn.rollback()
        conn.close()


if __name__ == "__main__":
    main()
