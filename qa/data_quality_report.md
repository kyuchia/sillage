# Data quality report

Generated 2026-10-20 00:02 UTC by `qa/data_quality.py` against `sillage`. Times below are Montréal local.

## Archive

| Layer | Rows | First fix | Last fix |
|---|---:|---|---|
| bus | 23,681,278 | 2026-04-14 22:28 | 2026-08-31 18:18 |
| aircraft | 1,839,598 | 2026-04-14 23:15 | 2026-09-18 04:04 |

## Checks

Each check is verified against planted defects in `tests/fixtures/seed.sql` before it is run here.

| Check | Bus | % | Aircraft | % | Handling today |
|---|---:|---:|---:|---:|---|
| **null_coords** — Fix with no latitude or longitude | 0 | 0.0000% | 0 | 0.0000% | Dropped by the query layer (`latitude/longitude IS NOT NULL`). |
| **null_ids** — Fix with no vehicle id / icao24, so it cannot join a trace | 0 | 0.0000% | 0 | 0.0000% | Not filtered; such rows group under a NULL id. |
| **outside_plausible_box** — Position outside greater Montréal ±100 km (PLAUSIBLE_BOX) | 409 | 0.0017% | 0 | 0.0000% | Dropped by the query layer since 8b0da87 (`PLAUSIBLE_BOX`). |
| **null_island** — Position within 1° of (0, 0) — a subset of the row above | 409 | 0.0017% | 0 | 0.0000% | Dropped with the row above. |
| **duplicate_fixes** — Extra copies of the same (id, fetched_at) | 0 | 0.0000% | 0 | 0.0000% | Not filtered; both copies reach the payload (testing log F9). |
| **geom_mismatch** — PostGIS geom missing, not SRID 4326, or not equal to lon/lat | 0 | 0.0000% | 0 | 0.0000% | `geom` is a generated column; any count here is a schema problem. |
| **future_timestamps** — fetched_at later than now | 0 | 0.0000% | 0 | 0.0000% | Not filtered. |
| **teleports** — Consecutive plausible fixes implying more than MAX_KMH | 16,262 | 0.0687% | 2,067 | 0.1124% | Not filtered in SQL; the frontend's `buildSpeeds()` clamps them on screen. |
| **collection_gaps** — Breaks of more than one hour between hours that hold data | 14 | — | 32 | — | The 125-day dormancy and OpenSky quota resets are expected; gaps shared by both tables are host outages (testing log F13). |

Teleport thresholds: bus 150 km/h, aircraft 1500 km/h.

## Collection gaps

### bus (14)

| Last hour with data | Next hour with data | Hours missing |
|---|---|---:|
| 2026-04-15 01:00 | 2026-04-15 03:00 | 1 |
| 2026-04-15 11:00 | 2026-08-18 23:00 | 3,011 |
| 2026-08-19 03:00 | 2026-08-19 05:00 | 1 |
| 2026-08-19 05:00 | 2026-08-19 07:00 | 1 |
| 2026-08-19 07:00 | 2026-08-19 09:00 | 1 |
| 2026-08-19 10:00 | 2026-08-19 16:00 | 5 |
| 2026-08-22 06:00 | 2026-08-22 09:00 | 2 |
| 2026-08-23 04:00 | 2026-08-23 07:00 | 2 |
| 2026-08-23 09:00 | 2026-08-23 11:00 | 1 |
| 2026-08-23 11:00 | 2026-08-23 13:00 | 1 |
| 2026-08-27 08:00 | 2026-08-27 11:00 | 2 |
| 2026-08-28 12:00 | 2026-08-28 16:00 | 3 |
| 2026-08-31 02:00 | 2026-08-31 05:00 | 2 |
| 2026-08-31 05:00 | 2026-08-31 13:00 | 7 |

### aircraft (32)

| Last hour with data | Next hour with data | Hours missing |
|---|---|---:|
| 2026-04-15 00:00 | 2026-08-18 23:00 | 3,022 |
| 2026-08-19 01:00 | 2026-08-19 16:00 | 14 |
| 2026-08-20 14:00 | 2026-08-20 16:00 | 1 |
| 2026-08-22 06:00 | 2026-08-22 09:00 | 2 |
| 2026-08-23 04:00 | 2026-08-23 07:00 | 2 |
| 2026-08-23 09:00 | 2026-08-23 11:00 | 1 |
| 2026-08-23 11:00 | 2026-08-23 13:00 | 1 |
| 2026-08-24 14:00 | 2026-08-24 16:00 | 1 |
| 2026-08-25 14:00 | 2026-08-25 16:00 | 1 |
| 2026-08-26 14:00 | 2026-08-26 16:00 | 1 |
| 2026-08-27 08:00 | 2026-08-27 11:00 | 2 |
| 2026-08-28 12:00 | 2026-08-28 16:00 | 3 |
| 2026-08-29 18:00 | 2026-08-29 20:00 | 1 |
| 2026-08-31 02:00 | 2026-08-31 04:00 | 1 |
| 2026-08-31 05:00 | 2026-08-31 13:00 | 7 |
| 2026-08-31 18:00 | 2026-09-01 05:00 | 10 |
| 2026-09-01 07:00 | 2026-09-01 12:00 | 4 |
| 2026-09-01 15:00 | 2026-09-01 17:00 | 1 |
| 2026-09-01 17:00 | 2026-09-01 19:00 | 1 |
| 2026-09-04 18:00 | 2026-09-04 20:00 | 1 |
| 2026-09-05 18:00 | 2026-09-05 20:00 | 1 |
| 2026-09-06 18:00 | 2026-09-06 20:00 | 1 |
| 2026-09-07 18:00 | 2026-09-07 20:00 | 1 |
| 2026-09-09 18:00 | 2026-09-09 20:00 | 1 |
| 2026-09-10 18:00 | 2026-09-10 20:00 | 1 |
| 2026-09-11 18:00 | 2026-09-11 20:00 | 1 |
| 2026-09-12 18:00 | 2026-09-13 02:00 | 7 |
| 2026-09-13 03:00 | 2026-09-13 09:00 | 5 |
| 2026-09-14 18:00 | 2026-09-14 20:00 | 1 |
| 2026-09-16 18:00 | 2026-09-16 20:00 | 1 |
| 2026-09-17 18:00 | 2026-09-17 20:00 | 1 |
| 2026-09-18 02:00 | 2026-09-18 04:00 | 1 |

## Time-window query plan

`EXPLAIN ANALYZE` of the exact `/api/trips` query for 2026-08-20 08:00-04:00 → 2026-08-20 09:00-04:00.

| Layer | fetched_at index | Rows | Execution | Plan nodes |
|---|---|---:|---:|---|
| bus | idx_vp_fetched_at | 155,172 | 613 ms | Sort → Index Scan (idx_vp_fetched_at) |
| aircraft | idx_ap_fetched_at | 1,432 | 3 ms | Sort → Index Scan (idx_ap_fetched_at) |

## Raw counts

```json
{
  "bus": {
    "null_coords": 0,
    "null_ids": 0,
    "outside_plausible_box": 409,
    "null_island": 409,
    "duplicate_fixes": 0,
    "geom_mismatch": 0,
    "future_timestamps": 0,
    "teleports": 16262,
    "collection_gaps": 14
  },
  "aircraft": {
    "null_coords": 0,
    "null_ids": 0,
    "outside_plausible_box": 0,
    "null_island": 0,
    "duplicate_fixes": 0,
    "geom_mismatch": 0,
    "future_timestamps": 0,
    "teleports": 2067,
    "collection_gaps": 32
  }
}
```
