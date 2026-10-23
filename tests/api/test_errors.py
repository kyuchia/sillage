"""
/api/trips — invalid input, empty results, the point budget, and missing dependencies.

Tests marked `xfail(strict=True)` describe the behaviour the API *should* have and
currently does not. Each one is a finding logged in qa/testing_log.md under the same
ID. They are strict, so fixing the bug makes the suite fail until the marker is removed
— the fix and the record of it cannot drift apart.
"""

import pytest
from fastapi import HTTPException

from conftest import SEED

WINDOW = {"start": SEED.WINDOW_START, "end": SEED.WINDOW_END}


def finding(fid, why):
    return pytest.mark.xfail(strict=True, reason=f"{fid}: {why} (qa/testing_log.md)")


# ---------------------------------------------------------------- 400 / 422

@pytest.mark.parametrize("start", [
    "notadate",
    "2026-13-45 08:00",          # month 13
    "2026-08-20T25:00:00-04:00",  # hour 25
    "2026-08-20T08:00:00-04:00" + "0" * 5000,
])
def test_unparseable_timestamp_is_400(client, start):
    r = client.get("/api/trips", params={"start": start, "end": SEED.WINDOW_END})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("bad query parameter")


@pytest.mark.parametrize("layer, message", [
    ("", "must not be empty"),
    (" , ", "must not be empty"),
    ("boat", "unknown layer 'boat'"),
    ("bus,boat", "unknown layer 'boat'"),
    ("BUS", "unknown layer 'BUS'"),
    ("bus;DROP TABLE vehicle_positions", "unknown layer"),
])
def test_bad_layer_is_400_with_the_valid_list(client, layer, message):
    r = client.get("/api/trips", params={"layer": layer, **WINDOW})
    assert r.status_code == 400
    assert message in r.json()["detail"]


@pytest.mark.parametrize("params, field", [
    ({"hours": "abc"}, "hours"),
    ({"min_points": -1}, "min_points"),
    ({"min_points": "three"}, "min_points"),
    ({"include_ground": "maybe"}, "include_ground"),
])
def test_wrongly_typed_parameter_is_422(client, params, field):
    r = client.get("/api/trips", params={**WINDOW, **params})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["query", field]


@pytest.mark.parametrize("hours", ["nan", "inf"])
def test_non_finite_hours_is_rejected(client, hours):
    # 422 since F4: rejected by validation, before Postgres sees the interval
    assert client.get("/api/trips", params={"hours": hours}).status_code == 422


def test_route_is_a_bound_parameter_not_sql(client, db):
    r = client.get("/api/trips", params={"layer": "bus", "route": "51' OR '1'='1", **WINDOW})
    assert r.status_code == 404
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM vehicle_positions")
        assert cur.fetchone()[0] == SEED.BUS_ROWS


# ---------------------------------------------------------------- empty results

def test_window_with_no_rows_is_404_naming_the_empty_layers(client):
    r = client.get("/api/trips", params={"start": "2026-08-25T08:00:00-04:00",
                                         "end": "2026-08-25T09:00:00-04:00"})
    assert r.status_code == 404
    assert "bus, aircraft" in r.json()["detail"]


def test_instant_between_fixes_is_404(client):
    t = "2026-08-20T08:00:10-04:00"     # fixes land on :00 / :20 / :40
    assert client.get("/api/trips", params={"start": t, "end": t}).status_code == 404


def test_rows_that_are_all_culled_give_200_with_no_trips(client):
    # Characterisation, see F6: rows existed, every trace was shorter than min_points.
    r = client.get("/api/trips", params={"start": "2026-08-20T08:00:00-04:00",
                                         "end": "2026-08-20T08:00:20-04:00",
                                         "min_points": 4})
    assert r.status_code == 200
    assert r.json()["trips"] == []


def test_hours_window_on_a_closed_archive_is_404(client):
    # The recording closed on 2026-09-18; "last N hours" is always empty now.
    assert client.get("/api/trips", params={"hours": 1}).status_code == 404


# ---------------------------------------------------------------- point budget

def test_payload_over_the_point_budget_is_413(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_POINTS", 50)
    r = client.get("/api/trips", params=WINDOW)
    assert r.status_code == 413
    detail = r.json()["detail"]
    assert "exceeds the 50 budget" in detail and "Narrow the window" in detail


def test_payload_exactly_at_the_budget_is_allowed(client, app_module, monkeypatch):
    total = sum(SEED.BUS_TRIPS.values()) + sum(SEED.AIRCRAFT_TRIPS.values())
    monkeypatch.setattr(app_module, "MAX_POINTS", total)
    assert client.get("/api/trips", params=WINDOW).status_code == 200
    monkeypatch.setattr(app_module, "MAX_POINTS", total - 1)
    assert client.get("/api/trips", params=WINDOW).status_code == 413


@finding("F8", "an over-budget request materialises every row before it is refused")
def test_over_budget_request_is_refused_without_fetching_everything(
        client, app_module, monkeypatch):
    import trips_query
    fetched = []
    real = trips_query.fetch_layer

    def spy(conn, layer, **filters):
        rows = real(conn, layer, **filters)
        fetched.append(len(rows))
        return rows

    monkeypatch.setattr(trips_query, "fetch_layer", spy)
    monkeypatch.setattr(app_module, "MAX_POINTS", 10)
    r = client.get("/api/trips", params={"layer": "bus"})      # no window at all
    assert r.status_code == 413
    assert sum(fetched) <= 10 + 1, f"fetched {sum(fetched)} rows to refuse a 10-point budget"


# ---------------------------------------------------------------- simulated layers

def test_simulated_layer_without_archived_gtfs_is_503(client, monkeypatch, tmp_path):
    import gtfs_sim
    monkeypatch.setattr(gtfs_sim, "GTFS_DIR", str(tmp_path))
    r = client.get("/api/trips", params={"layer": "metro", **WINDOW})
    assert r.status_code == 503
    assert "no archived feed" in r.json()["detail"]


@pytest.mark.parametrize("params", [{"start": SEED.WINDOW_START}, {"end": SEED.WINDOW_END}, {}])
def test_simulated_layer_without_a_full_window_is_400(client, params):
    r = client.get("/api/trips", params={"layer": "rem", **params})
    assert r.status_code == 400
    assert "explicit window" in r.json()["detail"]


# ---------------------------------------------------------------- findings

@pytest.mark.parametrize("start", ["notadate", "2026-13-45"])
def test_simulated_layer_with_unparseable_timestamp_is_400(app_module, start):
    with pytest.raises(HTTPException) as e:
        app_module._sim_window(start, SEED.WINDOW_END, None)
    assert e.value.status_code == 400


def test_nul_byte_in_timestamp_is_400(client):
    from fastapi.testclient import TestClient
    with TestClient(client.app, raise_server_exceptions=False) as c:
        r = c.get("/api/trips", params={"start": "2026-08-20\x00", "end": SEED.WINDOW_END})
    assert r.status_code == 400


def test_start_after_end_is_400(client):
    r = client.get("/api/trips", params={"start": SEED.WINDOW_END, "end": SEED.WINDOW_START})
    assert r.status_code in (400, 422)
    assert "before" in r.json()["detail"] or "after" in str(r.json()["detail"])


@pytest.mark.parametrize("hours", [0, -1, -24])
def test_non_positive_hours_is_rejected(client, hours):
    assert client.get("/api/trips", params={"hours": hours}).status_code in (400, 422)


def test_error_detail_does_not_leak_sql(client):
    detail = client.get("/api/trips", params={"start": "notadate"}).json()["detail"]
    assert "LINE" not in detail and "BETWEEN" not in detail


@pytest.mark.parametrize("server_tz", ["UTC", "Asia/Taipei"])
def test_naive_timestamps_mean_montreal_time_on_any_server(app_module, monkeypatch, server_tz):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app_module, "PG_DSN", f"{app_module.PG_DSN} options='-c TimeZone={server_tz}'")
    monkeypatch.setattr(app_module, "_POOL", None)
    try:
        with TestClient(app_module.app) as c:
            r = c.get("/api/trips", params={"layer": "bus",
                                            "start": "2026-08-20 08:00", "end": "2026-08-20 08:10"})
        assert r.status_code == 200, r.text
        assert {t["id"] for t in r.json()["trips"]} == set(SEED.BUS_TRIPS)
    finally:
        if app_module._POOL is not None:
            app_module._POOL.closeall()
