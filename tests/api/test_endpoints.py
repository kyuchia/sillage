"""
/api/range, /api/health, the static mounts and CORS.
"""

import pytest

from conftest import SEED, parse


# ---------------------------------------------------------------- /api/range

def test_range_reports_rows_and_extent_per_layer(client):
    body = client.get("/api/range").json()
    bus, air = body["layers"]["bus"], body["layers"]["aircraft"]
    assert (bus["table"], bus["rows"]) == ("vehicle_positions", SEED.BUS_ROWS)
    assert (air["table"], air["rows"]) == ("aircraft_positions", SEED.AIRCRAFT_ROWS)
    assert parse(bus["min_time"]) == parse(SEED.WINDOW_START)
    assert parse(bus["max_time"]) == parse(SEED.NIGHT_END)
    assert parse(air["max_time"]) == parse(SEED.WINDOW_END)


def test_range_overlap_is_the_intersection_of_the_layers(client):
    overlap = client.get("/api/range").json()["overlap"]
    assert parse(overlap["start"]) == parse(SEED.WINDOW_START)
    assert parse(overlap["end"]) == parse(SEED.WINDOW_END)


def test_busiest_hour_is_the_densest_bucket_across_layers(client):
    busiest = client.get("/api/range").json()["busiest_hour"]
    # Everything except night route B6 (31 rows) lands in the 08:00 hour.
    assert busiest["rows"] == SEED.BUS_ROWS - 31 + SEED.AIRCRAFT_ROWS
    assert parse(busiest["start"]) == parse(SEED.WINDOW_START)
    assert (parse(busiest["end"]) - parse(busiest["start"])).total_seconds() == 3600


def test_range_window_round_trips_into_trips(client):
    """What the page does on a parameter-less load: ask /api/range, then /api/trips."""
    overlap = client.get("/api/range").json()["overlap"]
    r = client.get("/api/trips", params=overlap)
    assert r.status_code == 200
    assert r.json()["modes"] == ["aircraft", "bus"]


# ---------------------------------------------------------------- /api/health

def test_health_reports_database_up(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["database"] == "up"


@pytest.fixture
def database_down(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "PG_DSN", "host=127.0.0.1 port=1 dbname=nowhere connect_timeout=1")
    monkeypatch.setattr(app_module, "_POOL", None)


def test_health_degrades_to_503_when_postgres_is_unreachable(client, database_down):
    r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded" and r.json()["database"] == "down"


@pytest.mark.parametrize("path", ["/api/range", "/api/trips?layer=bus"])
def test_data_endpoints_answer_503_not_500_when_postgres_is_down(client, database_down, path):
    r = client.get(path)
    assert r.status_code == 503
    assert "cannot connect to PostgreSQL" in r.json()["detail"]


def test_page_is_still_served_when_postgres_is_down(client, database_down):
    assert client.get("/docs/").status_code == 200


# ---------------------------------------------------------------- static + CORS

def test_root_redirects_to_the_page(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/docs/"


def test_page_and_scene_manifest_are_served(client):
    page = client.get("/docs/")
    assert page.status_code == 200 and "<title>Sillage</title>" in page.text
    manifest = client.get("/docs/scenes/manifest.json")
    assert manifest.status_code == 200


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:8000"])
def test_cors_allows_localhost(client, origin):
    r = client.get("/api/health", headers={"Origin": origin})
    assert r.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost.evil.example"])
def test_cors_refuses_other_origins(client, origin):
    r = client.get("/api/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in r.headers


def test_api_is_read_only(client):
    assert client.post("/api/trips").status_code == 405
