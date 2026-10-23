#!/usr/bin/env python3
"""
Parity test: the FastAPI service and the Kotlin port must answer alike.

Sends the same requests to both servers, pointed at the same database, and compares:

  * 200 responses — the whole JSON body, value for value (floats exactly);
  * 400 / 404 / 413 / 503 — the status and the `detail` string;
  * 422 — the status and each problem's (loc, type), since the message wording is
    pydantic's own.

Cases the port answers differently on purpose are listed with the reason and checked
on the Kotlin side only (simulated layers, the NUL-byte message).

Usage, from the project root:

    # both servers already running (uvicorn on 8000, ./gradlew run on 8081)
    python scripts/parity_kotlin.py

    # or let the script start both, against the database in PG_DSN
    (cd api-kotlin && ./gradlew installDist)
    PG_DSN="dbname=sillage_test" python scripts/parity_kotlin.py --launch

    # against the recorded archive: windows derived from /api/range, no seed needed
    PG_DSN="dbname=sillage" python scripts/parity_kotlin.py --launch --real

The default cases assume tests/fixtures/seed.sql is loaded (any pytest run loads it).
"""

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KOTLIN_BIN = ROOT / "api-kotlin" / "build" / "install" / "api-kotlin" / "bin" / "api-kotlin"

# tests/conftest.py SEED
A_START, A_END = "2026-08-20T08:00:00-04:00", "2026-08-20T08:10:00-04:00"
B_START, B_END = "2026-08-20T23:55:00-04:00", "2026-08-21T00:05:00-04:00"
A = {"start": A_START, "end": A_END}


def case(name, params, path="/api/trips", *, kotlin_only=None, status_only=False):
    """kotlin_only=(status, why): the port differs on purpose; check Kotlin alone."""
    return {"name": name, "path": path, "params": params,
            "kotlin_only": kotlin_only, "status_only": status_only}


SEED_CASES = [
    # ---- 200: payloads must be identical
    case("default layer (both)", A),
    case("layer=bus", {**A, "layer": "bus"}),
    case("layer=aircraft", {**A, "layer": "aircraft"}),
    case("layer=aircraft,bus (order ignored)", {**A, "layer": "aircraft,bus"}),
    case("layer with spaces and repeats", {**A, "layer": " bus , aircraft,bus "}),
    case("min_points=0", {**A, "min_points": "0"}),
    case("min_points=1", {**A, "min_points": "1"}),
    case("min_points=1_0 (pydantic spelling)", {**A, "min_points": "1_0"}),
    case("all traces culled -> 200 []", {"start": A_START, "end": "2026-08-20T08:00:20-04:00",
                                          "min_points": "4"}),
    case("include_ground=true", {**A, "include_ground": "true"}),
    case("include_ground=yes", {**A, "include_ground": "yes"}),
    case("route=51 on bus", {**A, "layer": "bus", "route": "51"}),
    case("route=51 leaves aircraft alone", {**A, "route": "51"}),
    case("window across local midnight", {"start": B_START, "end": B_END}),
    case("naive timestamps = Montreal time", {"start": "2026-08-20 08:00", "end": "2026-08-20 08:10"}),
    case("UTC timestamps", {"start": "2026-08-20T12:00:00Z", "end": "2026-08-20T12:10:00Z"}),
    case("+08:00 timestamps", {"start": "2026-08-20T20:00:00+08:00", "end": "2026-08-20T20:10:00+08:00"}),
    case("start == end on a fix", {"start": A_START, "end": A_START, "min_points": "1"}),
    case("open-ended start", {"start": B_START}),
    case("open-ended end", {"end": A_END}),
    case("no window at all", {}),
    case("hours=1e400 is not finite", {"hours": "1e400"}),
    # ---- 400
    case("layer empty", {**A, "layer": ""}),
    case("layer blank list", {**A, "layer": " , "}),
    case("layer boat", {**A, "layer": "boat"}),
    case("layer bus,boat", {**A, "layer": "bus,boat"}),
    case("layer BUS", {**A, "layer": "BUS"}),
    case("layer injection", {**A, "layer": "bus;DROP TABLE vehicle_positions"}),
    case("start notadate", {"start": "notadate", "end": A_END}),
    case("month 13", {"start": "2026-13-45 08:00", "end": A_END}),
    case("hour 25", {"start": "2026-08-20T25:00:00-04:00", "end": A_END}),
    case("5000 trailing zeros", {"start": A_START + "0" * 5000, "end": A_END}),
    case("start after end", {"start": A_END, "end": A_START}),
    case("start after end, naive", {"start": "2026-08-20 09:00", "end": "2026-08-20 08:00"}),
    case("NUL byte", {"start": "2026-08-20\x00", "end": A_END}, status_only=True),
    # ---- 422
    case("hours=abc", {**A, "hours": "abc"}),
    case("hours=0", {"hours": "0"}),
    case("hours=-1", {"hours": "-1"}),
    case("hours=nan", {"hours": "nan"}),
    case("hours=inf", {"hours": "inf"}),
    case("min_points=-1", {**A, "min_points": "-1"}),
    case("min_points=three", {**A, "min_points": "three"}),
    case("min_points=3.5", {**A, "min_points": "3.5"}),
    case("include_ground=maybe", {**A, "include_ground": "maybe"}),
    case("two bad parameters at once", {"hours": "abc", "min_points": "-1"}),
    case("422 comes before a bad layer", {"hours": "0", "layer": "boat"}),
    # ---- 404
    case("window with no rows", {"start": "2026-08-25T08:00:00-04:00", "end": "2026-08-25T09:00:00-04:00"}),
    case("instant between fixes", {"start": "2026-08-20T08:00:10-04:00", "end": "2026-08-20T08:00:10-04:00"}),
    case("unknown route", {**A, "layer": "bus", "route": "999"}),
    case("route injection is a bound value", {**A, "layer": "bus", "route": "51' OR '1'='1"}),
    case("hours=1 on a closed archive", {"hours": "1"}),
    # ---- the port refuses simulated layers on purpose
    case("layer=metro", {**A, "layer": "metro"},
         kotlin_only=(400, "the port serves recorded layers only")),
    case("layer=all", {**A, "layer": "all"},
         kotlin_only=(400, "the port serves recorded layers only")),
    # ---- other endpoints
    case("/api/range", {}, path="/api/range"),
    case("/api/health", {}, path="/api/health"),
]


def real_cases(base):
    """Windows cut from the recorded archive's own busiest hour."""
    status, rng, _ = fetch(base, "/api/range", {})
    if status != 200 or not rng.get("busiest_hour"):
        sys.exit(f"--real: /api/range gave {status}; is PG_DSN the recorded archive?")
    t0 = datetime.fromisoformat(rng["busiest_hour"]["start"])
    iso = lambda dt: dt.isoformat()
    w1 = {"start": iso(t0), "end": iso(t0 + timedelta(minutes=1))}
    w10 = {"start": iso(t0), "end": iso(t0 + timedelta(minutes=10))}
    return [
        case("/api/range", {}, path="/api/range"),
        case("busiest hour, 1 min, both", w1),
        case("busiest hour, 10 min, both", w10),
        case("busiest hour, 10 min, bus", {**w10, "layer": "bus"}),
        case("busiest hour, 10 min, aircraft", {**w10, "layer": "aircraft"}),
        case("busiest hour, 10 min, route 51", {**w10, "layer": "bus", "route": "51"}),
        case("busiest hour, 10 min, include_ground", {**w10, "include_ground": "true"}),
        case("busiest hour, naive timestamps",
             {"start": t0.strftime("%Y-%m-%d %H:%M"),
              "end": (t0 + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")}),
    ]


# ---------------------------------------------------------------- HTTP

def fetch(base, path, params):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, raw, enc = r.status, r.read(), r.headers.get("Content-Encoding")
    except urllib.error.HTTPError as e:
        status, raw, enc = e.code, e.read(), e.headers.get("Content-Encoding")
    ms = (time.perf_counter() - t) * 1000
    if enc == "gzip":
        raw = gzip.decompress(raw)
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw.decode(errors="replace")
    return status, body, ms


def first_difference(a, b, path="$"):
    """Where two JSON values first differ, or None. Floats compare exactly."""
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        return f"{path}: {type(a).__name__} {a!r:.80} vs {type(b).__name__} {b!r:.80}"
    if isinstance(a, dict):
        if a.keys() != b.keys():
            return f"{path}: keys {sorted(a)} vs {sorted(b)}"
        for k in a:
            d = first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r:.120} vs {b!r:.120}"


def compare(c, py, kt):
    """None if the two answers agree for this case, else a one-line reason."""
    (ps, pb, _), (ks, kb, _) = py, kt
    if ps != ks:
        return f"status {ps} vs {ks}"
    if c["status_only"]:
        return None
    if c["path"] == "/api/health":
        pick = lambda b: {k: b.get(k) for k in ("status", "database")}
        return first_difference(pick(pb), pick(kb))
    if ps == 422:
        pick = lambda b: [(d["loc"], d["type"]) for d in b["detail"]]
        return first_difference(pick(pb), pick(kb))
    return first_difference(pb, kb)


# ---------------------------------------------------------------- servers

def wait_healthy(base, proc, name, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"{name} exited with {proc.returncode} before it was ready")
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2):
                return
        except urllib.error.HTTPError:
            return          # 503 still means the server is up
        except OSError:
            time.sleep(0.5)
    sys.exit(f"{name} did not answer on {base} within {timeout}s")


def launch(kotlin_cmd):
    if "PG_DSN" not in os.environ:
        sys.exit("--launch needs PG_DSN (the database both servers will read)")
    env = dict(os.environ)
    py = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--port", "8000",
                           "--log-level", "warning"], cwd=ROOT, env=env)
    cmd = kotlin_cmd.split() if kotlin_cmd else [str(KOTLIN_BIN)]
    if not kotlin_cmd and not KOTLIN_BIN.exists():
        py.terminate()
        sys.exit(f"{KOTLIN_BIN} not found: run `./gradlew installDist` in api-kotlin/ first")
    kt = subprocess.Popen(cmd, cwd=ROOT / "api-kotlin", env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return [py, kt]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--python", default="http://127.0.0.1:8000")
    ap.add_argument("--kotlin", default="http://127.0.0.1:8081")
    ap.add_argument("--launch", action="store_true", help="start both servers with PG_DSN")
    ap.add_argument("--kotlin-cmd", help="command that starts the Kotlin server (with --launch)")
    ap.add_argument("--real", action="store_true", help="cases from the recorded archive")
    args = ap.parse_args()

    procs = launch(args.kotlin_cmd) if args.launch else []
    try:
        if procs:
            wait_healthy(args.python, procs[0], "uvicorn")
            wait_healthy(args.kotlin, procs[1], "Kotlin server")

        cases = real_cases(args.python) if args.real else SEED_CASES
        failures = 0
        for c in cases:
            kt = fetch(args.kotlin, c["path"], c["params"])
            if c["kotlin_only"]:
                want, why = c["kotlin_only"]
                ok = kt[0] == want
                verdict = "ok  " if ok else "FAIL"
                print(f"{verdict} {c['name']:<42} kotlin {kt[0]} (differs on purpose: {why})")
                failures += not ok
                continue
            py = fetch(args.python, c["path"], c["params"])
            diff = compare(c, py, kt)
            timing = f"{py[2]:7.0f} ms / {kt[2]:5.0f} ms" if args.real else f"{py[0]}"
            print(f"{'ok  ' if diff is None else 'DIFF'} {c['name']:<42} {timing}"
                  + (f"\n     {diff}" if diff else ""))
            failures += diff is not None

        print(f"\n{len(cases) - failures}/{len(cases)} cases agree")
        return 1 if failures else 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
