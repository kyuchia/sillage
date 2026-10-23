#!/usr/bin/env python3
"""Turn k6 summaries from load/results/ into Markdown tables.

    python load/compare.py before after       # before/after, both services
    python load/compare.py before             # one round

Reads load/results/<service>-<scenario>-<label>.json as written by load/trips.js.
"""

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
SERVICES = ["python", "kotlin"]
WINDOWS = ["1m", "10m", "1h", "1d"]
STAGES = [10, 50, 100]
STATUSES = [200, 413, 404, 500, 503, 0]


def load(service, scenario, label):
    path = RESULTS / f"{service}-{scenario}-{label}.json"
    return json.loads(path.read_text()) if path.exists() else None


def ms(v):
    if v is None:
        return "–"
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms"


def stats(run, tags):
    if run is None:
        return None
    m = run["metrics"]
    d = m.get(f"http_req_duration{{{tags}}}", {}).get("values", {})
    n = m.get(f"http_reqs{{{tags}}}", {}).get("values", {}).get("count", 0)
    codes = {s: m.get(f"responses{{{tags},status:{s}}}", {}).get("values", {}).get("count", 0)
             for s in STATUSES}
    return {"p50": d.get("med"), "p95": d.get("p(95)"), "n": n, "codes": codes}


def codes(s):
    if s is None:
        return "–"
    return " ".join(f"{k}×{v}" for k, v in s["codes"].items() if v) or "–"


def errors(s):
    if s is None or not s["n"]:
        return "–"
    bad = sum(v for k, v in s["codes"].items() if k not in (200, 413))
    return f"{100 * bad / s['n']:.1f}%"


def windows_table(labels):
    out = ["### Window size — 1 VU, bus layer", ""]
    head = "| Service | Window |"
    for lb in labels:
        head += f" p50 {lb} | p95 {lb} | status {lb} |"
    out += [head, "|" + "---|" * (2 + 3 * len(labels))]
    for svc in SERVICES:
        runs = [load(svc, "windows", lb) for lb in labels]
        if not any(runs):
            continue
        for w in WINDOWS:
            line = f"| {svc} | {w} |"
            for r in runs:
                s = stats(r, f"window:{w},phase:measure")
                line += f" {ms(s and s['p50'])} | {ms(s and s['p95'])} | {codes(s)} |"
            out.append(line)
    return out


def concurrency_table(labels):
    out = ["### Concurrency — 1-hour bus window", ""]
    head = "| Service | VUs |"
    for lb in labels:
        head += f" p50 {lb} | p95 {lb} | req/s {lb} | errors {lb} |"
    out += [head, "|" + "---|" * (2 + 4 * len(labels))]
    for svc in SERVICES:
        runs = [load(svc, "concurrency", lb) for lb in labels]
        if not any(runs):
            continue
        for vus in STAGES:
            line = f"| {svc} | {vus} |"
            for r in runs:
                s = stats(r, f"vus:{vus}")
                rate = "–"
                if s and r:
                    rate = f"{s['n'] / int(r['meta']['duration'].rstrip('s')):.1f}"
                line += f" {ms(s and s['p50'])} | {ms(s and s['p95'])} | {rate} | {errors(s)} |"
            out.append(line)
    return out


def main(labels):
    if not labels:
        sys.exit(__doc__)
    print("\n".join(windows_table(labels) + [""] + concurrency_table(labels)))
    print("\nerrors = responses other than 200 and 413 (500, 503, timeouts) as a share of all.")


if __name__ == "__main__":
    main(sys.argv[1:])
