"""Background resource sampler: poll `docker stats` for a repro's containers on
a thread, so any operation can be wrapped and its CPU/RAM cost reported.

Portability: `docker stats` works on Docker Desktop and Linux. On Docker Desktop
CPU% is relative to the VM's allocated CPUs and memory is the VM's view
(consistent with node-exporter). The idle->peak deltas are the trustworthy
signal; absolute numbers are host-specific.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from rc_repro import runner

_MEM_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}


def _parse_cpu(tok: str) -> float:
    try:
        return float(tok.strip().rstrip("%"))
    except ValueError:
        return 0.0


def _to_bytes(tok: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", tok)
    if not m:
        return 0.0
    return float(m.group(1)) * _MEM_UNITS.get(m.group(2).lower(), 1)


def _parse_mem(tok: str) -> tuple[float, float]:
    # "540MiB / 2GiB" -> (used, limit)
    parts = tok.split("/")
    used = _to_bytes(parts[0]) if parts else 0.0
    limit = _to_bytes(parts[1]) if len(parts) > 1 else 0.0
    return used, limit


@dataclass
class ContainerStats:
    idle_cpu: float
    peak_cpu: float
    idle_mem: float
    peak_mem: float
    limit_mem: float
    peak_cpu_t: float   # seconds since monitor start


@dataclass
class ResourceMonitor:
    """Usage:  with ResourceMonitor(name) as mon: <do work>;  rep = mon.report()"""
    repro_name: str
    interval: float = 1.0
    _series: dict = field(default_factory=dict)   # container -> [(t, cpu, mem_used, mem_limit)]
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _t0: float = 0.0

    def start(self) -> "ResourceMonitor":
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        ids = runner.container_ids(self.repro_name)
        while not self._stop.is_set():
            if not ids:
                ids = runner.container_ids(self.repro_name)
            t = time.monotonic() - self._t0
            for line in runner.docker_stats(ids).splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    used, limit = _parse_mem(parts[2])
                    self._series.setdefault(parts[0], []).append((t, _parse_cpu(parts[1]), used, limit))
            self._stop.wait(self.interval)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self.report()

    def __enter__(self) -> "ResourceMonitor":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def mem_slopes(self, min_span_s: float = 600.0) -> dict[str, float]:
        """Per-container RAM growth in bytes/hour over the sampled span (simple
        endpoint slope) — the soak-test leak signal. Empty unless the monitor ran
        for at least `min_span_s` (short runs say nothing about leaks)."""
        out: dict[str, float] = {}
        for name, series in self._series.items():
            if len(series) < 2:
                continue
            span = series[-1][0] - series[0][0]
            if span < min_span_s:
                continue
            out[name] = (series[-1][2] - series[0][2]) / span * 3600
        return out

    def report(self, window: tuple[float, float] | None = None) -> dict:
        """Per-container ContainerStats, optionally restricted to a (t0,t1) window."""
        out: dict[str, ContainerStats] = {}
        for name, series in self._series.items():
            sel = [s for s in series if window is None or window[0] <= s[0] <= window[1]]
            if not sel:
                continue
            peak = max(sel, key=lambda s: s[1])
            out[name] = ContainerStats(
                idle_cpu=sel[0][1],
                peak_cpu=peak[1],
                idle_mem=sel[0][2],
                peak_mem=max(s[2] for s in sel),
                limit_mem=sel[-1][3],
                peak_cpu_t=peak[0],
            )
        return out
