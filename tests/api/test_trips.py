"""
/api/trips — happy paths, payload contract, filters and window boundaries.

Counts come from tests/fixtures/seed.sql via conftest.SEED.
"""

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from conftest import SEED, parse
from trips_query import PLAUSIBLE_BOX

MONTREAL = ZoneInfo("America/Montreal")
WINDOW = {"start": SEED.WINDOW_START, "end": SEED.WINDOW_END}


def get_trips(client, **params):
    r = client.get("/api/trips", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def by_id(payload, mode=None):
    return {t["id"]: t for t in payload["trips"] if mode is None or t["mode"] == mode}


# ---------------------------------------------------------------- happy path

def test_default_layer_returns_both_recorded_modes(client):
    body = get_trips(client, **WINDOW)
    assert body["modes"] == ["aircraft", "bus"]
    assert {i: len(t["path"]) for i, t in by_id(body, "bus").items()} == SEED.BUS_TRIPS
    assert {i: len(t["path"]) for i, t in by_id(body, "aircraft").items()} == SEED.AIRCRAFT_TRIPS


@pytest.mark.parametrize("layer, expected", [
    ("bus", SEED.BUS_TRIPS),
    ("aircraft", SEED.AIRCRAFT_TRIPS),
])
def test_single_layer_returns_only_that_mode(client, layer, expected):
    body = get_trips(client, layer=layer, **WINDOW)
    assert body["modes"] == [layer]
    assert {t["mode"] for t in body["trips"]} == {layer}
    assert {i: len(t["path"]) for i, t in by_id(body).items()} == expected


@pytest.mark.parametrize("spelling", ["bus,aircraft", "aircraft,bus", "both",
                                      "bus,bus,aircraft", " bus , aircraft "])
def test_layer_spellings_of_the_same_set_give_identical_payloads(client, spelling):
    reference = get_trips(client, layer="both", **WINDOW)
    assert get_trips(client, layer=spelling, **WINDOW) == reference


def test_response_is_gzipped_when_asked(client):
    r = client.get("/api/trips", params=WINDOW, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


# ---------------------------------------------------------------- payload contract

def test_payload_top_level_shape(client):
    body = get_trips(client, **WINDOW)
    assert set(body) == {"start_time", "end_time", "duration_sec", "modes", "trips"}
    assert parse(body["start_time"]) == parse(SEED.WINDOW_START)
    assert parse(body["end_time"]) == parse(SEED.WINDOW_END)
    assert body["duration_sec"] == 600


def test_every_trip_honours_the_documented_shape(client):
    body = get_trips(client, **WINDOW)
    for t in body["trips"]:
        assert set(t) == {"mode", "id", "label", "path", "timestamps"}, t["id"]
        assert isinstance(t["id"], str)
        assert t["label"] is None or (isinstance(t["label"], str) and t["label"])
        assert len(t["path"]) == len(t["timestamps"]) >= 3
        for lon, lat in t["path"]:
            assert isinstance(lon, float) and isinstance(lat, float)
        ts = t["timestamps"]
        assert all(isinstance(s, (int, float)) for s in ts)
        assert ts == sorted(ts), f"{t['id']} timestamps go backwards"
        assert 0 <= ts[0] and ts[-1] <= body["duration_sec"]


def test_timestamps_are_relative_to_the_shared_start(client):
    body = get_trips(client, **WINDOW)
    assert min(t["timestamps"][0] for t in body["trips"]) == 0
    a3 = by_id(body)["def456"]          # A3 starts at 08:05:00
    assert a3["timestamps"][0] == 300
    assert a3["timestamps"][1] - a3["timestamps"][0] == 20


def test_labels_are_stripped_and_missing_labels_are_null(client):
    body = get_trips(client, layer="aircraft", **WINDOW)
    assert by_id(body)["c0ffee"]["label"] == "ACA123"     # stored as 'ACA123  '
    assert by_id(body)["def456"]["label"] is None          # stored as NULL


def test_path_is_lon_lat_order(client):
    b1 = by_id(get_trips(client, layer="bus", **WINDOW))["B1"]
    assert b1["path"][0] == [-73.60, 45.50]


# ---------------------------------------------------------------- data filters

def test_null_island_fix_never_reaches_the_payload(client):
    body = get_trips(client, layer="bus", **WINDOW)
    b4 = by_id(body)["B4"]
    assert len(b4["path"]) == 9                       # 10 recorded, null island dropped
    assert all(abs(lon) > 1 and abs(lat) > 1 for lon, lat in b4["path"])
    assert 100 not in b4["timestamps"]                # the 08:01:40 slot is a gap now


def test_every_point_is_inside_the_plausible_box(client):
    body = get_trips(client, layer="both", include_ground=True, min_points=0, **WINDOW)
    for t in body["trips"]:
        for lon, lat in t["path"]:
            assert PLAUSIBLE_BOX["min_lon"] <= lon <= PLAUSIBLE_BOX["max_lon"]
            assert PLAUSIBLE_BOX["min_lat"] <= lat <= PLAUSIBLE_BOX["max_lat"]


def test_fix_without_position_is_dropped(client):
    b5 = by_id(get_trips(client, layer="bus", **WINDOW))["B5"]
    assert len(b5["path"]) == 4
    assert all(None not in p for p in b5["path"])


def test_duplicate_fix_passes_through_with_a_repeated_timestamp(client):
    # Characterisation, see F9: B7's 08:00:20 fix was recorded twice and both copies
    # reach the payload — a zero-length, zero-duration segment.
    b7 = by_id(get_trips(client, layer="bus", **WINDOW))["B7"]
    assert b7["timestamps"] == [0, 20, 20, 40]
    assert b7["path"][1] == b7["path"][2]


def test_short_traces_are_culled_by_default(client):
    assert SEED.SHORT_TRIP not in by_id(get_trips(client, layer="bus", **WINDOW))


@pytest.mark.parametrize("min_points", [0, 1, 2])
def test_min_points_below_three_keeps_the_short_trace(client, min_points):
    body = get_trips(client, layer="bus", min_points=min_points, **WINDOW)
    assert len(by_id(body)[SEED.SHORT_TRIP]["path"]) == 2


def test_min_points_above_every_trace_leaves_no_trips(client):
    body = get_trips(client, layer="bus", min_points=1000, **WINDOW)
    assert body["trips"] == []


def test_parked_aircraft_hidden_by_default(client):
    assert SEED.GROUND_TRIP not in by_id(get_trips(client, layer="aircraft", **WINDOW))


def test_include_ground_shows_parked_aircraft(client):
    body = get_trips(client, layer="aircraft", include_ground=True, **WINDOW)
    assert len(by_id(body)[SEED.GROUND_TRIP]["path"]) == 10


def test_route_filter_on_bus_layer(client):
    body = get_trips(client, layer="bus", route="51", **WINDOW)
    assert set(by_id(body)) == {"B1", "B8"}               # B3 is route 51 but too short
    assert {t["label"] for t in body["trips"]} == {"51"}


def test_route_filter_does_not_touch_aircraft(client):
    # Documented as "bus layer only": with both layers, aircraft come back unfiltered.
    body = get_trips(client, layer="both", route="51", **WINDOW)
    assert set(by_id(body, "aircraft")) == set(SEED.AIRCRAFT_TRIPS)
    assert set(by_id(body, "bus")) == {"B1", "B8"}


def test_unknown_route_on_bus_layer_is_404(client):
    r = client.get("/api/trips", params={"layer": "bus", "route": "nope", **WINDOW})
    assert r.status_code == 404


# ---------------------------------------------------------------- window boundaries

def test_window_bounds_are_inclusive_at_both_ends(client):
    # 08:00:20 → 08:00:40 holds exactly two B1 fixes only if both ends are inclusive
    body = get_trips(client, layer="bus", min_points=1,
                     start="2026-08-20T08:00:20-04:00", end="2026-08-20T08:00:40-04:00")
    assert len(by_id(body)["B1"]["path"]) == 2


def test_start_equal_to_end_on_a_fix_returns_that_instant(client):
    body = get_trips(client, layer="bus", min_points=1,
                     start="2026-08-20T08:05:00-04:00", end="2026-08-20T08:05:00-04:00")
    assert body["duration_sec"] == 0
    assert {i: len(t["path"]) for i, t in by_id(body).items()} == {"B1": 1, "B2": 1}


def test_window_that_crosses_local_midnight(client):
    body = get_trips(client, layer="bus",
                     start="2026-08-20T23:58:00-04:00", end="2026-08-21T00:02:00-04:00")
    b6 = by_id(body)["B6"]
    assert len(b6["path"]) == 13                          # 23:58:00 … 00:02:00 every 20 s
    assert b6["timestamps"][-1] == 240


def test_whole_night_window_spans_two_calendar_days(client):
    body = get_trips(client, layer="bus", start=SEED.NIGHT_START, end=SEED.NIGHT_END)
    assert set(by_id(body)) == {"B6"}
    # The payload carries the server session's offset, so compare in Montréal time.
    local = lambda ts: parse(ts).astimezone(MONTREAL).date()
    assert (local(body["start_time"]), local(body["end_time"])) == (
        date(2026, 8, 20), date(2026, 8, 21))


@pytest.mark.parametrize("start, end", [
    ("2026-08-20T08:00:00-04:00", "2026-08-20T08:10:00-04:00"),
    ("2026-08-20T12:00:00Z", "2026-08-20T12:10:00Z"),             # same instant in UTC
    ("2026-08-20T20:00:00+08:00", "2026-08-20T20:10:00+08:00"),   # same instant in Taipei
    ("2026-08-20 08:00-04:00", "2026-08-20 08:10-04:00"),         # the documented example form
])
def test_offset_aware_timestamps_in_any_zone_select_the_same_rows(client, start, end):
    reference = get_trips(client, **WINDOW)
    body = get_trips(client, start=start, end=end)
    assert by_id(body).keys() == by_id(reference).keys()
    assert parse(body["start_time"]) == parse(reference["start_time"])


def test_open_ended_start_runs_to_the_end_of_the_archive(client):
    body = get_trips(client, layer="bus", start="2026-08-20T23:00:00-04:00")
    assert set(by_id(body)) == {"B6"}


def test_open_ended_end_runs_from_the_start_of_the_archive(client):
    body = get_trips(client, layer="bus", end="2026-08-20T08:00:40-04:00", min_points=1)
    assert "B6" not in by_id(body)
    assert len(by_id(body)["B1"]["path"]) == 3
