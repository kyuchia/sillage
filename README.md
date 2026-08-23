# Sillage

**Montréal's transit network, drawn as the trails its vehicles leave behind.**

[![Sillage morning peak](docs/screenshot.png)](https://kyuchia.github.io/sillage/)

<sub>Morning peak, 08:00–09:00, replayed at 60×. Métro lines use STM's official colours, REM its signature lime, exo trains indigo, and aircraft crimson. Buses are deliberately desaturated so the dense surface network reads as texture rather than noise.</sub>

### → **[Live demo](https://kyuchia.github.io/sillage/)**

Named after *sillage*, the French word for the wake a boat leaves on water, Sillage records live positions from STM buses and OpenSky aircraft in a PostGIS spatiotemporal database, simulates métro, commuter rail, and REM movement from static GTFS schedules, and replays them together as animated trails using deck.gl and MapLibre.

The published demo uses curated, precomputed scenes and requires no backend. Run the project locally to query arbitrary time windows directly from the database.

---

## What it does

Two Python fetchers poll public APIs and write each position fix to PostgreSQL. A FastAPI backend retrieves a requested time window, combines the recorded positions with schedule-simulated modes on a shared timeline, and returns the paths and timestamps expected by deck.gl's `TripsLayer`.

The browser then replays that window interactively: scrub through time, change playback speed, adjust trail persistence, and toggle individual modes.

Because positions are stored rather than only streamed, any collected period can be replayed later. Morning rush hour can be revisited repeatedly, while quieter overnight windows reveal a very different network.

Five modes can render together:

- **Bus** and **aircraft** use recorded positions.
- **Métro**, **commuter rail**, and **REM** are interpolated from published GTFS schedules.

---

## Stack

| Layer      | Choice                                                            |
| ---------- | ----------------------------------------------------------------- |
| Ingestion  | Python, `gtfs-realtime-bindings`, `opensky-api`                   |
| Storage    | PostgreSQL 17, PostGIS 3.6                                        |
| API        | FastAPI, uvicorn                                                  |
| Simulation | Static GTFS interpolation using `shapes.txt` and `stop_times.txt` |
| Map        | MapLibre GL JS                                                    |
| Layers     | deck.gl `TripsLayer` and `ScatterplotLayer`                       |
| Basemap    | CARTO Dark Matter                                                 |

MapLibre and CARTO keep the map stack open and usable without requiring a Mapbox account or credit card.

---

## Setup

### Prerequisites

- PostgreSQL 17 with PostGIS (`brew install postgresql@17 postgis`)
- Python 3.12
- An STM API key from the [STM developer portal](https://portail.developpeurs.stm.info/apihub)
- An OpenSky OAuth2 client. Anonymous OpenSky access is capped at 400 requests per day, which is insufficient for sustained 20-second polling.

### Install

```bash
git clone https://github.com/kyuchia/sillage.git
cd sillage

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

createdb sillage
psql sillage < db/schema.sql
```

### Credentials

Secrets are stored in the macOS Keychain rather than in files or launchd property lists:

```bash
security add-generic-password -a "$USER" -s sillage-stm -T /usr/bin/security -U -w
./scripts/store_opensky_credentials.sh ~/Downloads/credentials.json
```

### Collect data

```bash
python fetchers/stm_fetcher.py       # buses, every 20s
python fetchers/opensky_fetcher.py   # aircraft, every 20s
```

An hour of morning rush-hour collection typically yields around 1,200 vehicles and 180,000 position fixes.

For unattended collection, launchd agents can run the fetchers independently of a terminal session and keep the machine awake for the lifetime of each process:

```bash
sudo pmset -c sleep 0 disksleep 0    # AC only; battery behaviour is unchanged
cp launchd/ca.sillage.*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ca.sillage.stm.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ca.sillage.opensky.plist
```

> Use `caffeinate -i <command>`, not a standalone `caffeinate -i &`. The launchd agents tie the sleep assertion to the fetcher process so unattended collection survives terminal closure.

The fetchers include basic health monitoring for sleep and wake events, stale writes, unusually low collection yield, per-run logging under `fetchers/logs/`, and degraded-run detection in the exit summary.

### Static GTFS

Fetch and archive the feeds used by the simulated modes:

```bash
python scripts/fetch_gtfs.py
```

Feeds are stored under:

```text
gtfs/<agency>/<date>/
```

### Visualize

```bash
uvicorn api.main:app --reload --port 8000
```

Open **[http://localhost:8000/](http://localhost:8000/)**. FastAPI serves the visualization directly.

URL parameters are forwarded to the API:

```text
/?layer=all&start=2026-08-20%2008:00-04:00&end=2026-08-20%2009:00-04:00
```

`layer` accepts `bus`, `aircraft`, `metro`, `train`, `rem`, the aliases `both` and `all`, or a comma-separated list such as `metro,rem`.

---

## The published demo

GitHub Pages serves `docs/` statically, so the published site cannot query the PostgreSQL database or FastAPI backend directly. Instead, selected time windows are exported into `docs/scenes/` and made available through a scene picker.

To rebuild them:

```bash
uvicorn api.main:app --port 8000 &
./scripts/bake_scenes.sh
```

When running locally, the visualization prefers the live API. Baked scenes act as a fallback, and the interface indicates when fallback data is being shown.

---

## Data model

Recorded positions are stored in two tables. Each includes a `GENERATED` PostGIS geography column derived from latitude and longitude, a GiST index for spatial queries, and a B-tree index on `fetched_at` for time-range scans.

See [`db/schema.sql`](db/schema.sql) for the full schema.

For example, buses within 500 metres of Place-des-Arts during the last hour can be grouped by route with:

```sql
SELECT route_id, COUNT(DISTINCT vehicle_id)
FROM vehicle_positions
WHERE fetched_at > NOW() - INTERVAL '1 hour'
  AND ST_DWithin(
        geom,
        ST_MakePoint(-73.5772, 45.5048)::geography,
        500
      )
GROUP BY route_id
ORDER BY 2 DESC;
```

---

## Rendering

`TripsLayer` represents each vehicle as one record containing parallel `path` and `timestamps` arrays. Sillage uses timestamps in seconds relative to the beginning of the requested window rather than Unix epoch values, avoiding unnecessary precision loss during deck.gl interpolation.

`TripsLayer` renders the trail but not a moving vehicle head. To show current positions, the client binary-searches each vehicle's timestamp array, finds the two fixes surrounding the current playback time, and interpolates between their coordinates. The resulting positions are rendered separately with `ScatterplotLayer`.

### Colour

**Hue identifies mode; brightness encodes speed.**

Routes are deliberately not assigned individual colours. With more than 200 bus routes, route-level hashing quickly becomes rainbow soup and obscures the larger structure of the network.

Where an operator publishes an official colour, Sillage uses it directly. This includes STM's four métro line colours and REM's `#73A400`. The much denser bus layer remains largely achromatic so it reads as a moving texture without competing visually with the rail network.

Colour ramps are checked by `scripts/check_colours.js`, which compares them using CIEDE2000 and fails when the minimum perceptual distance falls below the configured threshold.

---

## Schedule simulation

Simulating transit from static GTFS involves a few details that are easy to miss.

STM does not publish `shape_dist_traveled`, so stops must be projected onto route geometry. A simple nearest-point search can place a later stop earlier along a shape where the route passes near itself, so projections are constrained to move forward.

GTFS times can also extend past midnight. A departure at `25:30:00`, for example, belongs to the previous service day even though it occurs on the next calendar day.

Finally, static GTFS feeds cover limited service periods. Historical recordings are therefore matched to a compatible service date when the original date falls outside the available feed.

---

## Data sources

| Mode              | Source                              | Rendering          |
| ----------------- | ----------------------------------- | ------------------ |
| STM bus           | GTFS-Realtime                       | Recorded           |
| Aircraft          | OpenSky OAuth2 API                  | Recorded           |
| STM métro         | Static GTFS                         | Schedule-simulated |
| REM               | Static GTFS                         | Schedule-simulated |
| exo commuter rail | Static GTFS                         | Schedule-simulated |
| RTL / STL         | GTFS-Realtime, application required | Not implemented    |

STM does not publish realtime métro vehicle positions. REM's public GTFS-Realtime feed provides service alerts but not vehicle positions. These modes are therefore simulated from static schedules by interpolating each vehicle's expected position along its published trip geometry.

exo commuter rail currently uses the same schedule-based approach; realtime access requires a separate application.

---

## Roadmap

- [x] Colour encoding by mode and speed
- [x] Aircraft layer
- [x] FastAPI time-window API
- [x] Métro, REM, and commuter rail simulation from static GTFS
- [x] Unattended collection with health monitoring
- [x] Static GitHub Pages demo
- [ ] 3D extrusion and hover tooltips
- [ ] exo commuter rail realtime integration
- [ ] WebSocket live mode alongside replay

---

## License

MIT

Transit data © STM and © exo/ARTM, used under their respective open-data terms. REM GTFS © CDPQ Infra under CC BY 4.0. Aircraft data © [The OpenSky Network](https://opensky-network.org). Basemap © CARTO and © OpenStreetMap contributors.
