"""
Shared fixtures for the Sillage test suite.

The tests run against a throwaway PostGIS database, loaded from db/schema.sql and
tests/fixtures/seed.sql — never against the recorded archive. Point them at it with:

    SILLAGE_TEST_DSN="dbname=sillage_test" pytest

`seed.sql` TRUNCATEs both tables, so the guard below refuses to run against any
database called `sillage` or holding more rows than a seed ever could.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parent.parent
TEST_DSN = os.environ.get("SILLAGE_TEST_DSN", "dbname=sillage_test")

# api.main reads PG_DSN at import time, so it must be set before the import below.
os.environ["PG_DSN"] = TEST_DSN

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "export"))
sys.path.insert(0, str(ROOT / "simulate"))

# The largest table a seed may hold. The real archive has ~25.5M rows.
GUARD_MAX_ROWS = 10_000


# Every number below is derived from tests/fixtures/seed.sql; see its header.
class SEED:
    WINDOW_START = "2026-08-20T08:00:00-04:00"
    WINDOW_END = "2026-08-20T08:10:00-04:00"
    NIGHT_START = "2026-08-20T23:55:00-04:00"
    NIGHT_END = "2026-08-21T00:05:00-04:00"

    BUS_ROWS = 118
    AIRCRAFT_ROWS = 46

    # Window A, default filters (min_points=3, include_ground=false)
    BUS_TRIPS = {"B1": 31, "B2": 31, "B4": 9, "B5": 4, "B7": 4, "B8": 4}
    AIRCRAFT_TRIPS = {"c0ffee": 31, "def456": 5}
    SHORT_TRIP = "B3"          # 2 points
    GROUND_TRIP = "abc123"     # 10 points, on_ground = true

    # Planted defects, as the data-quality checks must report them
    DEFECTS_BUS = {"null_coords": 1, "null_ids": 0, "outside_plausible_box": 1,
                   "null_island": 1, "duplicate_fixes": 1, "geom_mismatch": 0,
                   "future_timestamps": 0, "teleports": 2, "collection_gaps": 1}
    DEFECTS_AIRCRAFT = {"null_coords": 0, "null_ids": 0, "outside_plausible_box": 0,
                        "null_island": 0, "duplicate_fixes": 0, "geom_mismatch": 0,
                        "future_timestamps": 0, "teleports": 0, "collection_gaps": 0}


def _guard(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        name = cur.fetchone()[0]
        if name == "sillage":
            pytest.exit(f"refusing to seed the recorded archive ({name!r}); "
                        f"set SILLAGE_TEST_DSN to a throwaway database", returncode=2)
        for table in ("vehicle_positions", "aircraft_positions"):
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                continue
            cur.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} LIMIT %s) s",
                        (GUARD_MAX_ROWS + 1,))
            if cur.fetchone()[0] > GUARD_MAX_ROWS:
                pytest.exit(f"{name}.{table} holds more than {GUARD_MAX_ROWS:,} rows — "
                            f"that is not a test database; refusing to TRUNCATE it",
                            returncode=2)


@pytest.fixture(scope="session")
def seeded_db():
    """Create the schema and load the seed once per session; yield the DSN."""
    try:
        conn = psycopg2.connect(TEST_DSN)
    except psycopg2.OperationalError as e:
        pytest.exit(f"cannot reach the test database ({TEST_DSN}): {e}", returncode=2)
    conn.autocommit = True
    try:
        _guard(conn)
        with conn.cursor() as cur:
            schema = (ROOT / "db" / "schema.sql").read_text()
            # psql meta-commands (\echo, \dt) are not SQL
            schema = "\n".join(l for l in schema.splitlines() if not l.startswith("\\"))
            cur.execute(schema)
            cur.execute((ROOT / "tests" / "fixtures" / "seed.sql").read_text())
    finally:
        conn.close()
    return TEST_DSN


@pytest.fixture
def db(seeded_db):
    """A read-only connection to the seeded database."""
    conn = psycopg2.connect(seeded_db)
    conn.set_session(readonly=True)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(scope="session")
def app_module(seeded_db):
    import api.main
    return api.main


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient
    import trips_query

    # busiest_hour() memoises across calls; a stale value would leak between tests.
    trips_query._BUSIEST_CACHE.update({"at": 0.0, "value": None, "computed": False})
    with TestClient(app_module.app) as c:
        yield c


def parse(ts):
    return datetime.fromisoformat(ts)

