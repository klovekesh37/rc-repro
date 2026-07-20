"""Latency-over-time from a k6 run.

k6's `--out json` streams every measurement as a JSON line; we downsample
`http_req_duration` (+ `http_req_failed`) into <=30 time buckets, which is what
turns "p95 was 900ms" into "p95 was fine for 40s, then degraded as RAM filled" —
and pins WHEN errors started.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

MAX_BYTES = 200 * 1024 * 1024   # a soak test can produce a huge stream — bail politely
TARGET_BUCKETS = 30

_FRAC_RE = re.compile(r"\.(\d{6})\d+")   # k6 emits ns precision; fromisoformat wants <= 6


def _ts(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(_FRAC_RE.sub(r".\1", iso)).timestamp()


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, math.ceil(p / 100 * len(sorted_vals)) - 1)]


def parse(path: Path) -> dict | None:
    """{"width_s", "span_s", "buckets": [{t0, reqs, p50, p95, max, errors}],
    "first_error_s"} — or None (no file / too big / nothing usable)."""
    try:
        if not path.exists() or path.stat().st_size > MAX_BYTES:
            return None
        durations: list[tuple[float, float]] = []   # (epoch, ms)
        failures: list[float] = []                  # epochs of failed requests
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                # Cheap prefilter before json.loads (the stream can be millions of
                # lines). Metric-definition lines never contain "Point".
                if '"Point"' not in line:
                    continue
                if '"http_req_duration"' in line:
                    d = json.loads(line)["data"]
                    durations.append((_ts(d["time"]), float(d["value"])))
                elif '"http_req_failed"' in line:
                    d = json.loads(line)["data"]
                    if d["value"]:
                        failures.append(_ts(d["time"]))
    except (OSError, ValueError, KeyError):
        return None
    if len(durations) < 2:
        return None

    t0 = min(t for t, _ in durations)
    span = max(t for t, _ in durations) - t0
    width = max(1, math.ceil(span / TARGET_BUCKETS))
    n = math.floor(span / width) + 1

    per: list[list[float]] = [[] for _ in range(n)]
    errs = [0] * n
    for t, ms in durations:
        per[min(n - 1, int((t - t0) / width))].append(ms)
    for t in failures:
        i = int((t - t0) / width)
        if 0 <= i < n:
            errs[i] += 1

    buckets = []
    for i, vals in enumerate(per):
        vals.sort()
        buckets.append({
            "t0": i * width, "reqs": len(vals),
            "p50": _pct(vals, 50), "p95": _pct(vals, 95),
            "max": vals[-1] if vals else 0.0, "errors": errs[i],
        })
    first_err = next((b["t0"] for b in buckets if b["errors"]), None)
    return {"width_s": width, "span_s": span, "buckets": buckets,
            "first_error_s": first_err}


def spike_recovery(tl: dict) -> dict | None:
    """For a --spike run (base third / peak third / recovery third): how long
    after the spike ended did p95 return to <= 1.5x the pre-spike baseline?
    {"baseline_p95", "spike_p95", "recovered_after_s" (None = not within run)}."""
    buckets = tl.get("buckets") or []
    if len(buckets) < 6:
        return None
    span = buckets[-1]["t0"] + tl.get("width_s", 1)
    third = span / 3
    pre = sorted(b["p95"] for b in buckets if b["t0"] < third and b["reqs"])
    mid = [b["p95"] for b in buckets if third <= b["t0"] < 2 * third and b["reqs"]]
    post = [b for b in buckets if b["t0"] >= 2 * third and b["reqs"]]
    if not pre or not post:
        return None
    baseline = pre[len(pre) // 2]   # median of the pre-spike third
    threshold = baseline * 1.5
    recovered = next((b["t0"] - 2 * third for b in post if b["p95"] <= threshold), None)
    return {"baseline_p95": baseline, "spike_p95": max(mid) if mid else 0.0,
            "recovered_after_s": round(recovered) if recovered is not None else None}


_RAMP = " .:-=+*#"   # ASCII only — same ramp as timings.histogram


def render_ascii(tl: dict) -> list[str]:
    """Two compact lines: p95 trend over the run, plus error positions if any."""
    buckets = tl["buckets"]
    p95s = [b["p95"] for b in buckets]
    lo, hi = min(p95s), max(p95s)
    rng = (hi - lo) or 1.0
    chart = "".join(_RAMP[min(len(_RAMP) - 1, int((v - lo) / rng * (len(_RAMP) - 1)))]
                    for v in p95s)
    from rc_repro.perf.timings import fmt_ms
    lines = [f"p95 over time   {fmt_ms(lo)} |{chart}| {fmt_ms(hi)}   "
             f"({len(buckets)} buckets of {tl['width_s']}s)"]
    if any(b["errors"] for b in buckets):
        marks = "".join("x" if b["errors"] else "." for b in buckets)
        lines.append(f"errors          {' ' * len(fmt_ms(lo))} |{marks}|   "
                     f"(first at ~{tl['first_error_s']}s)")
    return lines
