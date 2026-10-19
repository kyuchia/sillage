"""
db/schema.sql — the contract the fetchers and the query layer rely on.

These run against the seed database, which is built from schema.sql itself, so a change
to the schema that would break the query layer fails here before it reaches the archive.
"""

import pytest

from trips_query import LAYERS

TABLES = [cfg["table"] for cfg in LAYERS.values()]


def columns(db, table):
    with db.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, is_nullable, is_generated
            FROM information_schema.columns WHERE table_name = %s
        """, (table,))
        return {r[0]: r[1:] for r in cur.fetchall()}


@pytest.mark.parametrize("layer", list(LAYERS))
def test_query_layer_columns_exist(db, layer):
    """Every column trips_query.py names must exist in its table."""
    cfg = LAYERS[layer]
    cols = columns(db, cfg["table"])
    for col in ("fetched_at", "latitude", "longitude", cfg["id_col"], cfg["label_col"]):
        assert col in cols, f"{cfg['table']}.{col} missing"
    if layer == "aircraft":
        assert "on_ground" in cols          # build_where filters on it


@pytest.mark.parametrize("table", TABLES)
def test_fetched_at_is_timestamptz_and_required(db, table):
    data_type, nullable, _ = columns(db, table)["fetched_at"]
    assert data_type == "timestamp with time zone"
    assert nullable == "NO"


@pytest.mark.parametrize("table", TABLES)
def test_coordinates_are_double_precision(db, table):
    cols = columns(db, table)
    assert cols["latitude"][0] == cols["longitude"][0] == "double precision"


@pytest.mark.parametrize("table", TABLES)
def test_geom_is_generated_wgs84_geography(db, table):
    data_type, _, generated = columns(db, table)["geom"]
    assert generated == "ALWAYS"
    with db.cursor() as cur:
        cur.execute("""
            SELECT type, srid FROM geography_columns
            WHERE f_table_name = %s AND f_geography_column = 'geom'
        """, (table,))
        assert cur.fetchone() == ("Point", 4326)


@pytest.mark.parametrize("table", TABLES)
def test_geom_follows_lon_lat_and_is_null_without_a_position(db, table):
    with db.cursor() as cur:
        cur.execute(f"""
            SELECT
              COUNT(*) FILTER (WHERE latitude IS NOT NULL AND
                               (ST_X(geom::geometry) <> longitude OR ST_Y(geom::geometry) <> latitude)),
              COUNT(*) FILTER (WHERE latitude IS NULL AND geom IS NOT NULL)
            FROM {table}
        """)
        assert cur.fetchone() == (0, 0)


@pytest.mark.parametrize("table, prefix", [("vehicle_positions", "idx_vp"),
                                           ("aircraft_positions", "idx_ap")])
def test_time_and_space_indexes_exist(db, table, prefix):
    with db.cursor() as cur:
        cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", (table,))
        idx = dict(cur.fetchall())
    assert f"{prefix}_fetched_at" in idx and "(fetched_at)" in idx[f"{prefix}_fetched_at"]
    assert "USING gist (geom)" in idx[f"{prefix}_geom"]


@pytest.mark.parametrize("layer", list(LAYERS))
def test_id_column_is_indexed(db, layer):
    """fetch_layer() orders by (id, fetched_at) — the id must at least be indexed."""
    cfg = LAYERS[layer]
    with db.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename = %s", (cfg["table"],))
        defs = [r[0] for r in cur.fetchall()]
    assert any(f"({cfg['id_col']})" in d for d in defs)
