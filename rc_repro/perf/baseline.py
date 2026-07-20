"""Saved load-test baselines: `--save LABEL` writes a run to
~/.rc-repro/loadtests/<label>.json, `--compare LABEL` diffs the current run
against it — the "did my fix/setting change actually help?" workflow.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from rc_repro import config

_LABEL_RE = re.compile(r"[^a-z0-9_-]+")

# How much worse a latency/error metric may get before the delta is flagged.
REGRESS_PCT = 25.0


def sanitize_label(label: str) -> str:
    """Filesystem-safe baseline name; raises ValueError if nothing survives."""
    clean = _LABEL_RE.sub("-", label.strip().lower()).strip("-")
    if not clean:
        raise ValueError(f"{label!r} is not a usable baseline name")
    return clean


def baselines_dir() -> Path:
    return config.home() / "loadtests"


def path(label: str) -> Path:
    return baselines_dir() / f"{sanitize_label(label)}.json"


def save(label: str, payload: dict) -> str:
    p = path(label)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(p)


def load(label: str) -> dict:
    p = path(label)
    if not p.exists():
        raise FileNotFoundError(f"no saved baseline {sanitize_label(label)!r} (looked in {p.parent})")
    return json.loads(p.read_text(encoding="utf-8"))


def _delta(metric: str, before: float, after: float, higher_is_better: bool) -> dict:
    pct = (after / before - 1) * 100 if before else 0.0
    worse = (pct < 0) if higher_is_better else (pct > 0)
    return {
        "metric": metric, "before": before, "after": after, "pct": pct,
        "worse": worse,
        # Regression flag: meaningfully worse, not noise. Errors use an absolute
        # bump too (0.1% -> 0.2% is +100% but rarely meaningful).
        "flag": worse and abs(pct) > REGRESS_PCT,
    }


def compare(current: dict, base: dict) -> list[dict]:
    """Delta rows between the current summary and a baseline's, most-important
    first: throughput, overall latency percentiles, error rate, then per-step
    p95 for every step both runs measured."""
    cur_s, base_s = current.get("summary") or {}, base.get("summary") or {}
    rows: list[dict] = []

    def add(metric: str, key: str, higher_is_better: bool = False) -> None:
        b, a = base_s.get(key), cur_s.get(key)
        if b is not None and a is not None:
            rows.append(_delta(metric, float(b), float(a), higher_is_better))

    add("throughput (rps)", "rps", higher_is_better=True)
    add("p50", "p50")
    add("p95", "p95")
    add("p99", "p99")
    add("error rate", "error_rate")

    cur_steps, base_steps = cur_s.get("steps") or {}, base_s.get("steps") or {}
    for name in [s for s in _step_order(cur_steps) if s in base_steps]:
        b, a = base_steps[name].get("p95"), cur_steps[name].get("p95")
        if b is not None and a is not None:
            rows.append(_delta(f"step {name} p95", float(b), float(a), False))
    return rows


_CANONICAL_STEPS = ("login", "rooms", "open", "post", "sync", "read")


def _step_order(steps: dict) -> list[str]:
    """Canonical step order for display (journey flow first, extras appended)."""
    known = [s for s in _CANONICAL_STEPS if s in steps]
    return known + sorted(s for s in steps if s not in _CANONICAL_STEPS)


step_order = _step_order  # public alias used by cli/report rendering
