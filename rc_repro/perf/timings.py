"""Latency accumulator: collect per-call durations, report the distribution.

Dependency-free (pure Python) — percentiles via nearest-rank on a sorted copy,
which is fine for the thousands of samples a seed/load run produces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ASCII density ramp for the inline histogram — width-1 in every terminal (no
# ambiguous-width Unicode block glyphs, which misalign on some terminals).
_RAMP = " .:-=+*#"


@dataclass
class Timings:
    samples_ms: list[float] = field(default_factory=list)

    def add(self, ms: float) -> None:
        self.samples_ms.append(ms)

    def __len__(self) -> int:
        return len(self.samples_ms)

    def pct(self, p: float) -> float:
        """Nearest-rank percentile in ms (0 if empty)."""
        if not self.samples_ms:
            return 0.0
        s = sorted(self.samples_ms)
        k = max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))
        return s[k]

    def summary(self) -> dict:
        n = len(self.samples_ms)
        if not n:
            return {"count": 0}
        s = sorted(self.samples_ms)
        return {
            "count": n,
            "mean": sum(s) / n,
            "min": s[0],
            "max": s[-1],
            "p50": self.pct(50),
            "p90": self.pct(90),
            "p95": self.pct(95),
            "p99": self.pct(99),
        }

    def rate_per_s(self, wall_s: float) -> float:
        return len(self.samples_ms) / wall_s if wall_s > 0 else 0.0

    def histogram(self, buckets: int = 12) -> str:
        """A single-line ASCII density strip over `buckets` linear latency bins."""
        if len(self.samples_ms) < 2:
            return ""
        lo, hi = min(self.samples_ms), max(self.samples_ms)
        if hi <= lo:
            return ""
        counts = [0] * buckets
        span = hi - lo
        for v in self.samples_ms:
            i = min(buckets - 1, int((v - lo) / span * buckets))
            counts[i] += 1
        cmax = max(counts) or 1
        return "".join(_RAMP[min(len(_RAMP) - 1, round(c / cmax * (len(_RAMP) - 1)))] for c in counts)


def fmt_ms(ms: float) -> str:
    """Human latency: 42ms, 1.20s."""
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.2f}s"
