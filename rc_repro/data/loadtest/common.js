// Shared helpers for rc-repro k6 load scenarios.
// Parameterized via env: RC_URL, RC_TOKEN, RC_UID, RC_USER, RC_PASS, VUS,
// DURATION, RAMP ("start:end"). handleSummary writes a compact JSON that
// rc-repro parses (it replaces k6's default end-of-test stdout summary; the
// live progress bar still shows during the run).

import { Counter } from "k6/metrics";

export const URL = __ENV.RC_URL;

// Response-status buckets, so rc-repro can tell rate-limits (429) from server
// errors (5xx) from client errors (4xx) — the first question in any perf ticket.
// A counter never incremented simply doesn't appear in the summary.
const _status = {
  "2xx": new Counter("status_2xx"),
  "429": new Counter("status_429"),
  "4xx": new Counter("status_4xx"),
  "5xx": new Counter("status_5xx"),
  "other": new Counter("status_other"),
};

// Call once per response: buckets it by status. Returns the response for chaining.
export function record(res) {
  const c = res.status;
  if (c >= 200 && c < 300) _status["2xx"].add(1);
  else if (c === 429) _status["429"].add(1);
  else if (c >= 400 && c < 500) _status["4xx"].add(1);
  else if (c >= 500) _status["5xx"].add(1);
  else _status["other"].add(1);
  return res;
}

export const authParams = {
  headers: {
    "X-Auth-Token": __ENV.RC_TOKEN,
    "X-User-Id": __ENV.RC_UID,
    "Content-Type": "application/json",
  },
};

// Pre-authenticated seeded users (alice, bob, …), written by rc-repro when
// --users is in effect: [{username, password, token, uid}, …]. Each VU sticks
// to one user (round-robin by VU number) so load carries real per-user
// permissions and subscriptions instead of one shared admin token.
const USERS = __ENV.RC_USERS_FILE ? JSON.parse(open(__ENV.RC_USERS_FILE)) : [];

export function vuUser() {
  return USERS.length ? USERS[(__VU - 1) % USERS.length] : null;
}

// Auth headers for this VU: its assigned seeded user, else the admin token.
export function vuAuth() {
  const u = vuUser();
  return {
    headers: {
      "X-Auth-Token": u ? u.token : __ENV.RC_TOKEN,
      "X-User-Id": u ? u.uid : __ENV.RC_UID,
      "Content-Type": "application/json",
    },
  };
}

// p(99) and count are not in k6's default summaryTrendStats — request them so
// the report has full percentiles and per-step call counts.
const TREND = ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)", "count"];

function seconds(s) {
  const m = String(s || "60s").match(/^(\d+(?:\.\d+)?)(s|m|h)?$/);
  if (!m) return 60;
  const v = parseFloat(m[1]);
  return m[2] === "m" ? v * 60 : m[2] === "h" ? v * 3600 : v;
}

export function buildOptions() {
  const ramp = __ENV.RAMP;
  const spike = __ENV.SPIKE;
  if (spike) {
    // "base:peak" — hold base for a third, jump to peak for a third, drop back
    // for a third (recovery window). 1s ramps keep the jumps sharp.
    const [base, peak] = spike.split(":").map(Number);
    const third = Math.max(2, Math.floor(seconds(__ENV.DURATION) / 3));
    return {
      startVUs: base, summaryTrendStats: TREND,
      stages: [
        { duration: `${third}s`, target: base },
        { duration: "1s", target: peak },
        { duration: `${third}s`, target: peak },
        { duration: "1s", target: base },
        { duration: `${third}s`, target: base },
      ],
    };
  }
  if (ramp) {
    const [start, end] = ramp.split(":").map(Number);
    return {
      startVUs: start, summaryTrendStats: TREND,
      stages: [{ duration: __ENV.DURATION || "60s", target: end }],
    };
  }
  return {
    vus: Number(__ENV.VUS) || 10, duration: __ENV.DURATION || "30s",
    summaryTrendStats: TREND,
  };
}

function _count(metrics, name) {
  return metrics[name] ? metrics[name].values.count : 0;
}

export function handleSummary(data) {
  // A run that issued zero requests emits no http_* metrics — guard so the
  // summary is still written (with zeros) instead of throwing.
  const d = data.metrics.http_req_duration ? data.metrics.http_req_duration.values : {};
  const reqs = data.metrics.http_reqs ? data.metrics.http_reqs.values : { rate: 0, count: 0 };
  const failed = data.metrics.http_req_failed ? data.metrics.http_req_failed.values : { rate: 0 };
  const checks = data.metrics.checks ? data.metrics.checks.values : { rate: 1 };
  const out = {
    rps: reqs.rate,
    count: reqs.count,
    avg: d.avg, min: d.min, max: d.max,
    p50: d.med, p90: d["p(90)"], p95: d["p(95)"], p99: d["p(99)"],
    error_rate: failed.rate,
    checks_rate: checks.rate,
    status: {
      "2xx": _count(data.metrics, "status_2xx"),
      "429": _count(data.metrics, "status_429"),
      "4xx": _count(data.metrics, "status_4xx"),
      "5xx": _count(data.metrics, "status_5xx"),
      "other": _count(data.metrics, "status_other"),
    },
  };
  // Per-step latency: any Trend named step_<name> (journey/mixed scenarios).
  const steps = {};
  for (const k of Object.keys(data.metrics)) {
    if (k.indexOf("step_") === 0) {
      const v = data.metrics[k].values;
      steps[k.slice(5)] = {
        count: v.count, avg: v.avg, p50: v.med,
        p90: v["p(90)"], p95: v["p(95)"], p99: v["p(99)"],
      };
    }
  }
  if (Object.keys(steps).length) out.steps = steps;
  return { "/k6/summary.json": JSON.stringify(out) };
}
