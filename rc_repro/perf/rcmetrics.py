"""Sample Rocket.Chat's own Prometheus metrics during a load test.

RC (a Node app) exposes prom-client metrics on :9458 inside the container when
`Prometheus_Enabled` is on — including **event-loop lag**, THE saturation signal
for a Node process: once the event loop lags, every request queues behind it.
The port isn't published to the host, so each sample runs `node -e "fetch(…)"`
inside the RC container (Node ships with RC by definition) — no extra images,
no Grafana needed.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from rc_repro import runner

# Unlabeled gauges worth tracking (names verified against a live RC /metrics
# dump and the official dashboard in data/monitoring/rocketchat-metrics.json).
# The lag histogram gauges (_max/_p99, reset per scrape) matter more than the
# instantaneous nodejs_eventloop_lag_seconds, which samples BETWEEN stalls and
# reads near-zero even while requests queue for seconds.
METRICS = {
    "nodejs_eventloop_lag_seconds": "eventloop_lag_s",
    "nodejs_eventloop_lag_max_seconds": "eventloop_lag_max_s",
    "nodejs_eventloop_lag_p99_seconds": "eventloop_lag_p99_s",
    "nodejs_heap_size_used_bytes": "heap_used_bytes",
    "rocketchat_ddp_connected_users": "ddp_users",
    "rocketchat_oplog_queue": "oplog_queue",
}

_LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eE+-]+)\s*$")

# require('http') instead of fetch(): works on every Node version RC ever shipped.
_FETCH_JS = (
    "require('http').get('http://localhost:9458/metrics',function(r){"
    "var d='';r.on('data',function(c){d+=c});r.on('end',function(){console.log(d)})"
    "}).on('error',function(){process.exit(1)})"
)


def parse_prom(text: str) -> dict[str, float]:
    """Prometheus exposition text -> {metric_name: value}. Labeled series of the
    same name are summed (our tracked metrics are unlabeled gauges anyway)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        try:
            out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(3))
        except ValueError:
            continue
    return out


@dataclass
class ServiceSeries:
    samples: dict[str, list[float]] = field(default_factory=dict)   # key -> values

    def add(self, parsed: dict[str, float]) -> None:
        for prom_name, key in METRICS.items():
            if prom_name in parsed:
                self.samples.setdefault(key, []).append(parsed[prom_name])

    def summary(self) -> dict:
        out = {}
        for key, vals in self.samples.items():
            if vals:
                out[key] = {"mean": sum(vals) / len(vals), "max": max(vals),
                            "last": vals[-1], "n": len(vals)}
        return out


@dataclass
class RCMetricsSampler:
    """Background thread polling every RC instance's /metrics while a test runs.
    Mirrors ResourceMonitor's lifecycle: start() -> workload -> stop() -> report."""

    name: str
    services: list[str]
    interval: float = 2.0
    _series: dict[str, ServiceSeries] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> "RCMetricsSampler":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            for svc in self.services:
                rc, text = runner.compose_exec_capture(self.name, svc, ["node", "-e", _FETCH_JS])
                if rc == 0 and text:
                    self._series.setdefault(svc, ServiceSeries()).add(parse_prom(text))
            self._stop.wait(self.interval)

    def stop(self) -> dict:
        """{service: {eventloop_lag_s: {mean,max,last,n}, …}} — empty if the
        endpoint never answered (metrics off / old RC)."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        return {svc: s.summary() for svc, s in self._series.items() if s.summary()}
