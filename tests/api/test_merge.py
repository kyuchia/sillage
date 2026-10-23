"""
Recorded + simulated layers on one timeline, and CLI/API parity.

The simulated layers normally read archived GTFS from gtfs/ (gitignored, so absent in
CI). These tests build a one-trip synthetic metro feed with the simulator's own test
helper and point gtfs_sim at it.
"""

import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, SEED, parse

WINDOW = {"start": SEED.WINDOW_START, "end": SEED.WINDOW_END}


@pytest.fixture
def synthetic_metro(monkeypatch, tmp_path):
    import gtfs_sim
    from test_gtfs_sim import WEEKDAY_SERVICE, build_feed

    # One metro trip, 08:02 → 08:08 on a weekday service; 2026-08-20 is a Thursday.
    build_feed(str(tmp_path), calendar_rows=[WEEKDAY_SERVICE], calendar_dates_rows=[],
               trips_rows=["R1,WK,M1,S1"],
               stop_times_rows=["M1,08:02:00,08:02:00,ST0,1", "M1,08:08:00,08:08:00,ST4,2"])
    monkeypatch.setattr(gtfs_sim, "GTFS_DIR", str(tmp_path))
    return tmp_path


def test_metro_alone_comes_from_the_schedule(client, synthetic_metro):
    r = client.get("/api/trips", params={"layer": "metro", **WINDOW})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["modes"] == ["metro"]
    (trip,) = body["trips"]
    assert trip["timestamps"][0] == 0
    assert parse(body["start_time"]) == parse("2026-08-20T08:02:00-04:00")


def test_recorded_and_simulated_share_one_origin(client, synthetic_metro):
    body = client.get("/api/trips", params={"layer": "bus,metro", **WINDOW}).json()
    assert body["modes"] == ["bus", "metro"]
    assert parse(body["start_time"]) == parse(SEED.WINDOW_START)   # the earlier of the two

    trips = {t["id"]: t for t in body["trips"]}
    assert trips["B1"]["timestamps"][0] == 0
    metro = next(t for t in body["trips"] if t["mode"] == "metro")
    assert metro["timestamps"][0] == 120                             # 08:02 on the shared clock
    assert metro["timestamps"][-1] == 480


def test_simulated_layer_rescues_an_empty_recorded_window(client, synthetic_metro):
    # No bus runs route R1: alone that is a 404, with metro it is a partial result.
    params = {"route": "R1", **WINDOW}
    assert client.get("/api/trips", params={"layer": "bus", **params}).status_code == 404
    body = client.get("/api/trips", params={"layer": "bus,metro", **params}).json()
    assert body["modes"] == ["metro"]


def test_route_filter_also_applies_to_simulated_layers(client, synthetic_metro):
    # F10: `route` filters simulated layers as well as the bus layer, and the 404
    # names every layer it emptied.
    r = client.get("/api/trips", params={"layer": "bus,metro", "route": "nope", **WINDOW})
    assert r.status_code == 404
    assert r.json()["detail"].endswith("(empty layers: bus, metro)")


def test_route_description_names_the_simulated_layers(client):
    params = client.get("/openapi.json").json()["paths"]["/api/trips"]["get"]["parameters"]
    desc = next(p for p in params if p["name"] == "route")["description"]
    assert "bus layer only" not in desc
    assert all(mode in desc for mode in ("metro", "train", "rem"))


def test_all_layers_with_missing_train_and_rem_feeds_is_503(client, synthetic_metro):
    # Only stm was archived in the synthetic tree; exo and REM are missing.
    r = client.get("/api/trips", params={"layer": "all", **WINDOW})
    assert r.status_code == 503
    assert "no archived feed" in r.json()["detail"]


# ---------------------------------------------------------------- CLI parity

def test_cli_export_writes_the_same_json_as_the_api(client, seeded_db, tmp_path):
    """trips_query.py promises the CLI and the API agree "by construction"; check it."""
    out = tmp_path / "trips.json"
    env = {**os.environ, "PG_DSN": seeded_db}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "export" / "export_trips.py"),
         "--start", SEED.WINDOW_START, "--end", SEED.WINDOW_END, "--out", str(out)],
        env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "bus" in proc.stdout and "skipped 1 too short" in proc.stdout

    api = client.get("/api/trips", params=WINDOW).json()
    assert json.loads(out.read_text()) == api


def test_cli_exits_nonzero_on_an_empty_window(seeded_db, tmp_path):
    env = {**os.environ, "PG_DSN": seeded_db}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "export" / "export_trips.py"),
         "--start", "2026-08-25 08:00-04:00", "--end", "2026-08-25 09:00-04:00",
         "--out", str(tmp_path / "x.json")],
        env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1
    assert "no data in any layer" in proc.stdout
    assert not (tmp_path / "x.json").exists()
