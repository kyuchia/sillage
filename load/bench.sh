#!/usr/bin/env bash
# One full k6 round against the archive: start both services, run the two scenarios
# on each, stop them. Results land in load/results/<service>-<scenario>-<label>.json.
#
#   load/bench.sh before                # run from the repo root, venv active
#   load/bench.sh after
#   python load/compare.py before after
#
# Environment: PG_DSN (default "dbname=sillage"), MTL_MAX_POINTS (default 400000),
# and anything trips.js reads (ITER, WARMUP, DURATION, ...). SERVICES="python" or
# "kotlin" runs one side only; SCENARIOS="windows" one scenario only.
#
# Builds the Kotlin service first, so a branch switch between rounds is picked up.
set -euo pipefail

LABEL="${1:?usage: load/bench.sh <label>}"
export PG_DSN="${PG_DSN:-dbname=sillage}"
SERVICES="${SERVICES:-python kotlin}"
SCENARIOS="${SCENARIOS:-windows concurrency}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p load/results load/logs

command -v k6 >/dev/null || { echo "k6 not found (brew install k6)"; exit 1; }

PID=""
stop() {
  if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi
  PID=""
}
trap stop EXIT

wait_up() {
  for _ in $(seq 1 60); do
    curl -sf "$1/api/health" >/dev/null && return 0
    sleep 1
  done
  echo "$1 did not come up; see load/logs/" >&2
  exit 1
}

for svc in $SERVICES; do
  case "$svc" in
    python)
      url=http://localhost:8000
      uvicorn api.main:app --port 8000 --log-level warning >"load/logs/python-$LABEL.log" 2>&1 &
      ;;
    kotlin)
      url=http://localhost:8081
      (cd api-kotlin && ./gradlew -q installDist)
      api-kotlin/build/install/api-kotlin/bin/api-kotlin >"load/logs/kotlin-$LABEL.log" 2>&1 &
      ;;
    *) echo "unknown service $svc"; exit 1 ;;
  esac
  PID=$!
  wait_up "$url"
  for sc in $SCENARIOS; do
    echo "== $svc · $sc · $LABEL"
    k6 run -q -e SERVICE="$svc" -e BASE_URL="$url" -e SCENARIO="$sc" -e LABEL="$LABEL" load/trips.js
  done
  stop
done

echo "done. Compare with: python load/compare.py <label> <label>"
