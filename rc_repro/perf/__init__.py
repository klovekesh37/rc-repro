"""Performance tooling for rc-repro (Phase 1: timing + resource sampling).

See docs/performance-design.md for the full plan. Phase 1 ships:
  - `timings.Timings`  — latency distribution (p50/p90/p95/p99 + a sparkline)
  - `resources.ResourceMonitor` — background docker-stats sampler (CPU/RAM peaks)
These power the `seed` timing breakdown, the `--stats` resource cost, and the
`rc-repro stats` command. Later phases (loadtest / benchmark / capacity) build on
the same primitives.
"""

from rc_repro.perf.resources import ResourceMonitor
from rc_repro.perf.timings import Timings

__all__ = ["Timings", "ResourceMonitor"]
