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

// p(99) is not in k6's default summaryTrendStats — request it so the report has it.
const TREND = ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"];

export function buildOptions() {
  const ramp = __ENV.RAMP;
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
  return { "/k6/summary.json": JSON.stringify(out) };
}
