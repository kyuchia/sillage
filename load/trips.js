// k6 load test for /api/trips — test plan §D.
//
// Runs against a service backed by the real archive (not the seed, not in CI):
//
//   k6 run -e SERVICE=python -e BASE_URL=http://localhost:8000 -e SCENARIO=windows  -e LABEL=before load/trips.js
//   k6 run -e SERVICE=kotlin -e BASE_URL=http://localhost:8081 -e SCENARIO=concurrency -e LABEL=before load/trips.js
//
// or simply `load/run.sh <python|kotlin> <label>`.
//
// SCENARIO=windows      1 VU, the bus layer over a 1 min / 10 min / 1 h / 1 day window,
//                       WARMUP unmeasured + ITER measured requests each, in that order.
//                       The 1-day window exceeds MTL_MAX_POINTS on the archive, so it is
//                       the F8 case: a request that ends in 413.
// SCENARIO=concurrency  the 1-hour window (the 676 ms query in qa/testing_log.md) under
//                       10, then 50, then 100 constant VUs, DURATION each.
//
// Both services use a pool of 8 database connections, so the comparison is fair.
// Results: a short table on stdout and the full summary in load/results/.

import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const SERVICE = __ENV.SERVICE || 'python';
const BASE_URL = (__ENV.BASE_URL || 'http://localhost:8000').replace(/\/$/, '');
const SCENARIO = __ENV.SCENARIO || 'windows';
const LABEL = __ENV.LABEL || 'run';
const LAYER = __ENV.LAYER || 'bus';
const ITER = parseInt(__ENV.ITER || '10', 10);
const WARMUP = parseInt(__ENV.WARMUP || '2', 10);
const DURATION = __ENV.DURATION || '60s';
// Seconds a stage may run over while its last requests finish; the next stage waits for it.
const GRACE = parseInt(__ENV.GRACE || '60', 10);
const RESULTS_DIR = __ENV.RESULTS_DIR || 'load/results';
// The hour measured in qa/testing_log.md: 2026-08-20 08:00 → 09:00, Montréal time.
const ANCHOR = __ENV.ANCHOR || '2026-08-20T08:00:00-04:00';

const WINDOWS = [
  { name: '1m', minutes: 1 },
  { name: '10m', minutes: 10 },
  { name: '1h', minutes: 60 },
  { name: '1d', minutes: 1440 },
];
const STAGES = [10, 50, 100];
const STATUSES = [200, 413, 404, 500, 503, 0];

const responses = new Counter('responses');

function windowParams(minutes) {
  const start = new Date(ANCHOR);
  const end = new Date(start.getTime() + minutes * 60_000);
  // Sent in UTC; the services read it as an absolute instant.
  return `start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`;
}

function buildOptions() {
  const thresholds = {};
  // A threshold on a tagged sub-metric is what makes k6 report it; `max>=0` always passes.
  const track = (tags) => {
    thresholds[`http_req_duration{${tags}}`] = ['max>=0'];
    thresholds[`http_reqs{${tags}}`] = ['count>=0'];
    for (const s of STATUSES) thresholds[`responses{${tags},status:${s}}`] = ['count>=0'];
  };

  let scenarios;
  if (SCENARIO === 'windows') {
    scenarios = {
      windows: {
        executor: 'per-vu-iterations', vus: 1,
        iterations: WINDOWS.length * (WARMUP + ITER), maxDuration: '60m',
      },
    };
    for (const w of WINDOWS) track(`window:${w.name},phase:measure`);
  } else if (SCENARIO === 'concurrency') {
    scenarios = {};
    const seconds = parseInt(DURATION, 10);
    STAGES.forEach((vus, i) => {
      scenarios[`vus_${vus}`] = {
        executor: 'constant-vus', vus, duration: DURATION,
        // Each stage starts after the previous one has drained (gracefulStop + a pause).
        startTime: `${i * (seconds + GRACE + 10)}s`, gracefulStop: `${GRACE}s`,
        tags: { vus: String(vus) },
      };
      track(`vus:${vus}`);
    });
  } else {
    throw new Error(`unknown SCENARIO ${SCENARIO}; use windows or concurrency`);
  }

  return {
    scenarios,
    thresholds,
    discardResponseBodies: true,      // payloads are megabytes; only status and timing matter here
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
    // 413 is the answer we expect for an over-budget window, not a failure.
    // Everything else (500, 503, timeouts) counts in http_req_failed.
    setupTimeout: '30s',
  };
}

export const options = buildOptions();
http.setResponseCallback(http.expectedStatuses(200, 413));

export function setup() {
  const r = http.get(`${BASE_URL}/api/health`, { timeout: '10s' });
  if (r.status !== 200) throw new Error(`${BASE_URL}/api/health answered ${r.status}`);
}

function hit(minutes, tags) {
  const url = `${BASE_URL}/api/trips?layer=${LAYER}&${windowParams(minutes)}`;
  const r = http.get(url, { tags, timeout: '180s' });
  responses.add(1, { ...tags, status: String(r.status) });
  check(r, { 'status is 200 or 413': (res) => res.status === 200 || res.status === 413 });
}

export default function () {
  if (SCENARIO === 'windows') {
    const i = exec.scenario.iterationInTest;
    const w = WINDOWS[Math.floor(i / (WARMUP + ITER))];
    const phase = i % (WARMUP + ITER) < WARMUP ? 'warmup' : 'measure';
    hit(w.minutes, { window: w.name, phase });
  } else {
    hit(60, { window: '1h' });
  }
}

// ---------------------------------------------------------------- summary

function fmt(ms) {
  if (ms === undefined) return '-';
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms.toFixed(0)} ms`;
}

function row(data, tags, name, seconds) {
  const d = data.metrics[`http_req_duration{${tags}}`];
  const n = data.metrics[`http_reqs{${tags}}`];
  const counts = STATUSES
    .map((s) => [s, data.metrics[`responses{${tags},status:${s}}`]])
    .filter(([, m]) => m && m.values.count > 0)
    .map(([s, m]) => `${s}×${m.values.count}`)
    .join(' ');
  const v = d ? d.values : {};
  // Throughput only means something for a stage of known length.
  const rate = n && seconds ? (n.values.count / seconds).toFixed(2) : '-';
  return `${name.padEnd(6)} ${fmt(v.med).padStart(9)} ${fmt(v['p(95)']).padStart(9)} ${fmt(v.max).padStart(9)} ${rate.padStart(8)}  ${counts}`;
}

export function handleSummary(data) {
  const rows = SCENARIO === 'windows'
    ? WINDOWS.map((w) => row(data, `window:${w.name},phase:measure`, w.name))
    : STAGES.map((vus) => row(data, `vus:${vus}`, `${vus} VU`, parseInt(DURATION, 10)));
  const text = [
    '',
    `${SERVICE} · ${SCENARIO} · ${LABEL} · layer=${LAYER} · ${BASE_URL}`,
    `${''.padEnd(6)} ${'p50'.padStart(9)} ${'p95'.padStart(9)} ${'max'.padStart(9)} ${'req/s'.padStart(8)}  statuses`,
    ...rows,
    '',
  ].join('\n');

  const meta = {
    service: SERVICE, scenario: SCENARIO, label: LABEL, layer: LAYER, base_url: BASE_URL,
    anchor: ANCHOR, iter: ITER, warmup: WARMUP, duration: DURATION,
    finished_at: new Date().toISOString(),
  };
  return {
    stdout: text,
    [`${RESULTS_DIR}/${SERVICE}-${SCENARIO}-${LABEL}.json`]: JSON.stringify({ meta, metrics: data.metrics }, null, 1),
  };
}
