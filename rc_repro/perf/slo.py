"""Parse and evaluate an SLO gate against a k6 load-test summary.

Rule syntax (comma-separated): ``p95=300ms,error=1%,rps=100``

- latency metrics (``avg`` ``min`` ``max`` ``p50`` ``p90`` ``p95`` ``p99``) —
  the value is an **upper bound** in ms (``300ms``, ``1.5s`` or a bare number).
- ``error`` — max allowed error rate, a percent (``1%`` or ``1``).
- ``rps`` — a **lower bound** on requests/second (the one "at least" metric).

`evaluate` returns one result dict per rule; the load-test command fails
(non-zero exit) if any rule is not met — the CI-gate use case.
"""

from __future__ import annotations

LATENCY_KEYS = {"avg", "min", "max", "p50", "p90", "p95", "p99"}


def _parse_ms(v: str) -> float:
    v = v.strip().lower()
    if v.endswith("ms"):
        return float(v[:-2])
    if v.endswith("s"):
        return float(v[:-1]) * 1000
    return float(v)


def parse(spec: str) -> list[tuple[str, str, float, str]]:
    """Parse "p95=300ms,error=1%" into (key, op, threshold, raw) tuples."""
    rules: list[tuple[str, str, float, str]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"bad rule {part!r} (expected key=value)")
        key, val = part.split("=", 1)
        key, val = key.strip().lower(), val.strip()
        if key in LATENCY_KEYS:
            rules.append((key, "<=", _parse_ms(val), val))
        elif key == "error":
            rules.append((key, "<=", float(val.rstrip("%")), val))
        elif key == "rps":
            rules.append((key, ">=", float(val), val))
        else:
            raise ValueError(
                f"unknown metric {key!r} (use p50/p90/p95/p99/avg/min/max/error/rps)"
            )
    return rules


# The summary key each rule metric reads from (latency keys map to themselves).
_SUMMARY_KEY = {"error": "error_rate", "rps": "rps"}


def _summary_key(key: str) -> str:
    return _SUMMARY_KEY.get(key, key)


def _actual(key: str, summary: dict) -> float:
    raw = summary.get(_summary_key(key))
    raw = float(raw) if raw is not None else 0.0   # null value -> 0, don't crash
    return raw * 100 if key == "error" else raw


def evaluate(rules: list[tuple[str, str, float, str]], summary: dict) -> list[dict]:
    """Return [{key, op, threshold, raw, actual, ok, measured}] for each rule.

    A rule whose metric is absent from the summary is reported as not `measured`
    and fails — a metric that wasn't captured must never silently PASS at 0.0."""
    out = []
    for key, op, threshold, raw in rules:
        # A present-but-null value is not a real measurement — it must fail as
        # "not measured", never pass at 0.0 (and never crash float(None)).
        measured = summary.get(_summary_key(key)) is not None
        actual = _actual(key, summary)
        ok = measured and (actual <= threshold if op == "<=" else actual >= threshold)
        out.append({"key": key, "op": op, "threshold": threshold, "raw": raw,
                    "actual": actual, "ok": ok, "measured": measured})
    return out


def fmt_actual(key: str, actual: float) -> str:
    """Human string for an observed value, matching the metric's unit."""
    if key in LATENCY_KEYS:
        return f"{actual:.0f}ms"
    if key == "error":
        return f"{actual:.2f}%"
    return f"{actual:.1f}"
