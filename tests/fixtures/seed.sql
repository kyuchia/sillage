-- ============================================================
-- Sillage test seed — 164 rows, deterministic, with planted defects
-- ============================================================
-- Loaded by tests/conftest.py into a throwaway database (never `sillage`).
-- Every count asserted in tests/ is derived from the rows below; change a row here
-- and the matching constant in tests/conftest.py (SEED) must change with it.
--
-- Window A: 2026-08-20 08:00:00 → 08:10:00 EDT, 20-second cadence like the fetchers.
-- Window B: 2026-08-20 23:55:00 → 08-21 00:05:00 EDT, crossing local midnight.
--
-- Planted defects (the data-quality checks must find exactly these):
--   B4  one null-island fix near (0, 0)       → outside_plausible_box = 1, null_island = 1
--       (1e-7, -1e-7), not exactly 0: the archive's 409 are like this, and an
--       equality test on 0 missed every one of them
--   B5  one fix with NULL latitude/longitude  → null_coords = 1
--   B7  one repeated (vehicle_id, fetched_at) → duplicate_fixes = 1
--   B8  one ~39 km jump inside the box        → teleports = 2 (the jump out and back)
--   bus table has no rows 09:00 → 23:00       → collection_gaps = 1
-- ============================================================

TRUNCATE vehicle_positions, aircraft_positions RESTART IDENTITY;

-- ---------------------------------------------------------------- bus, window A

-- B1, route 51: 31 clean fixes, drifting east ~39 m per poll
INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, latitude, longitude)
SELECT TIMESTAMPTZ '2026-08-20 08:00:00-04' + i * INTERVAL '20 seconds',
       'B1', 'T51-1', '51', 45.50, -73.60 + i * 0.0005
FROM generate_series(0, 30) AS i;

-- B2, route 24: 31 clean fixes on a parallel line
INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, latitude, longitude)
SELECT TIMESTAMPTZ '2026-08-20 08:00:00-04' + i * INTERVAL '20 seconds',
       'B2', 'T24-1', '24', 45.52, -73.60 + i * 0.0005
FROM generate_series(0, 30) AS i;

-- B3, route 51: only 2 fixes — below the default min_points=3, so culled from payloads
INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, latitude, longitude)
SELECT TIMESTAMPTZ '2026-08-20 08:00:00-04' + i * INTERVAL '20 seconds',
       'B3', 'T51-2', '51', 45.54, -73.58 + i * 0.0005
FROM generate_series(0, 1) AS i;

-- B4, route 80: 10 fixes, the 6th (i = 5) is null island
INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, latitude, longitude)
SELECT TIMESTAMPTZ '2026-08-20 08:00:00-04' + i * INTERVAL '20 seconds',
       'B4', 'T80-1', '80',
       CASE WHEN i = 5 THEN 1e-7 ELSE 45.48 END,
       CASE WHEN i = 5 THEN -1e-7 ELSE -73.62 + i * 0.0005 END
FROM generate_series(0, 9) AS i;

-- B5, route 80: 5 fixes, the 3rd (i = 2) has no position at all
INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, latitude, longitude)
SELECT TIMESTAMPTZ '2026-08-20 08:00:00-04' + i * INTERVAL '20 seconds',
       'B5', 'T80-2', '80',
       CASE WHEN i = 2 THEN NULL ELSE 45.46 END,
       CASE WHEN i = 2 THEN NULL ELSE -73.64 + i * 0.0005 END
FROM generate_series(0, 4) AS i;

-- B7, route 24: 4 rows, the 08:00:20 fix recorded twice
INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, latitude, longitude) VALUES
    ('2026-08-20 08:00:00-04', 'B7', 'T24-2', '24', 45.56, -73.570),
    ('2026-08-20 08:00:20-04', 'B7', 'T24-2', '24', 45.56, -73.5695),
    ('2026-08-20 08:00:20-04', 'B7', 'T24-2', '24', 45.56, -73.5695),
    ('2026-08-20 08:00:40-04', 'B7', 'T24-2', '24', 45.56, -73.569);

-- B8, route 51: 4 fixes, the 3rd jumps ~39 km east (still inside PLAUSIBLE_BOX)
INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, latitude, longitude) VALUES
    ('2026-08-20 08:00:00-04', 'B8', 'T51-3', '51', 45.58, -73.6000),
    ('2026-08-20 08:00:20-04', 'B8', 'T51-3', '51', 45.58, -73.5995),
    ('2026-08-20 08:00:40-04', 'B8', 'T51-3', '51', 45.58, -73.1000),
    ('2026-08-20 08:01:00-04', 'B8', 'T51-3', '51', 45.58, -73.5985);

-- ---------------------------------------------------------------- bus, window B

-- B6, night route 361: 31 fixes from 23:55 to 00:05, across local midnight
INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, latitude, longitude)
SELECT TIMESTAMPTZ '2026-08-20 23:55:00-04' + i * INTERVAL '20 seconds',
       'B6', 'T361-1', '361', 45.50, -73.58 + i * 0.0005
FROM generate_series(0, 30) AS i;

-- ---------------------------------------------------------------- aircraft, window A

-- A1: airborne, callsign padded with spaces as OpenSky sends it
INSERT INTO aircraft_positions (fetched_at, icao24, callsign, origin_country,
                                latitude, longitude, baro_altitude, on_ground, velocity)
SELECT TIMESTAMPTZ '2026-08-20 08:00:00-04' + i * INTERVAL '20 seconds',
       'c0ffee', 'ACA123  ', 'Canada', 45.45, -73.75 + i * 0.002, 1500 + i * 30, false, 120
FROM generate_series(0, 30) AS i;

-- A2: parked on the apron — hidden unless include_ground=true
INSERT INTO aircraft_positions (fetched_at, icao24, callsign, origin_country,
                                latitude, longitude, on_ground, velocity)
SELECT TIMESTAMPTZ '2026-08-20 08:00:00-04' + i * INTERVAL '20 seconds',
       'abc123', 'JZA456', 'Canada', 45.4706, -73.7408, true, 0
FROM generate_series(0, 9) AS i;

-- A3: on_ground unknown (NULL) and no callsign — kept, with a null label
INSERT INTO aircraft_positions (fetched_at, icao24, callsign, origin_country,
                                latitude, longitude, on_ground, velocity)
SELECT TIMESTAMPTZ '2026-08-20 08:05:00-04' + i * INTERVAL '20 seconds',
       'def456', NULL, 'United States', 45.60, -73.90 + i * 0.002, NULL, 110
FROM generate_series(0, 4) AS i;
