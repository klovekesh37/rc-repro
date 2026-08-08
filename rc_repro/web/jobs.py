"""In-memory job manager for long-running service calls.

A job runs a service function on a worker thread; the function's `emit(Event)`
appends to the job's event buffer. The SSE endpoint polls that buffer (simple
and robust for a local single-user server) and streams new events until the job
finishes. No web-framework dependency lives here.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from rc_repro.errors import ConflictError, ReproError
# The audit trail lives in services/audit.py so the CLI can write to the same file
# without importing the web layer, and so destructive operations are recorded
# where they HAPPEN rather than only where jobs are submitted. Re-exported here:
# these names are what the web layer already imports.
from rc_repro.services.audit import AUDIT_FILE, CURRENT_ACTOR, audit  # noqa: F401
from rc_repro.services.events import Event


# `serve` is long-lived and every streamed docker line becomes an Event, so both
# the registry and each job's buffer need a ceiling or they grow for the life of
# the process.
MAX_JOBS = 100
MAX_EVENTS_PER_JOB = 2000

# Job RETENTION was bounded; concurrency was not. submit() started a bare thread
# per job, so ten teammates each starting a capacity search meant ten k6
# containers, ten metrics samplers and ten resource monitors against one Docker
# engine -- on a box the design already says will "hit RAM before anything else".
#
# Two pools, because the two problems are different:
#
#   measurement -- a load test running beside another load test is not a
#     measurement of anything. Serialised box-wide: the second waits rather than
#     quietly producing numbers shaped by the first.
#   heavy       -- creates, restores and upgrades are legitimately parallel; they
#     just should not all run at once on one engine.
#
# Everything else (reads, state changes, seeds) is unbounded, as before.
_MEASURE_KINDS = frozenset({"loadtest", "capacity", "benchmark"})
_HEAVY_KINDS = frozenset({"create", "restore", "upgrade", "backup", "scale"})
_measure_slots = threading.BoundedSemaphore(1)
_heavy_slots = threading.BoundedSemaphore(max(2, (os.cpu_count() or 4) // 2))


#: A job that has not reached a terminal state. "queued" belongs here: it has not
#: finished, it has not even started. Anything testing `status != "running"` to
#: mean "done" would close a queued job's event stream the moment it opened, and
#: would let eviction drop a job that is still waiting for its slot.
ACTIVE_STATUSES = frozenset({"running", "queued"})


def _slots_for(kind: str) -> "threading.BoundedSemaphore | None":
    if kind in _MEASURE_KINDS:
        return _measure_slots
    if kind in _HEAVY_KINDS:
        return _heavy_slots
    return None


@dataclass
class Job:
    id: str
    kind: str
    # What the job acts on (usually a repro name), for the activity list -- `kind`
    # alone cannot tell two concurrent seeds apart.
    label: str = ""
    # Who asked for this. Empty in token mode, where a shared secret genuinely
    # cannot say. With named accounts it is what answers "who tore down X?".
    actor: str = ""
    status: str = "running"          # queued|running|done|error
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: list[Event] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    error_kind: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Total events ever emitted. `events` is trimmed on a flood, so callers index
    # against this absolute counter rather than the live list's length.
    _emitted: int = 0

    def emit(self, ev: Event) -> None:
        with self._lock:
            self.events.append(ev)
            self._emitted += 1
            overflow = len(self.events) - MAX_EVENTS_PER_JOB
            if overflow > 0:
                del self.events[:overflow]

    @property
    def n_events(self) -> int:
        return self._emitted

    def summary(self) -> dict:
        """One row for the activity list: no event bodies and no `result`, which
        for a benchmark or capacity search is a large nested document."""
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "actor": self.actor,
                "status": self.status, "started_at": self.started_at,
                "finished_at": self.finished_at, "n_events": self.n_events,
                "error": self.error}

    def snapshot(self, since: int) -> tuple[list[dict], bool, int]:
        """Events with absolute index >= `since`, whether the job finished, and the
        next absolute index.

        The third element matters once trimming kicks in: the caller cannot derive
        its next index by counting, because the oldest events may be gone.
        """
        with self._lock:
            first = self._emitted - len(self.events)   # absolute index of events[0]
            start = max(0, since - first)
            return ([e.as_dict() for e in self.events[start:]],
                    self.status not in ACTIVE_STATUSES,
                    self._emitted)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # Job threads are daemons, so Ctrl-C / `systemctl restart` / an OOM kill
        # terminates them mid-operation and skips every `finally`. That restore
        # logic is careful and correct, and all of it was bypassed: a backup killed
        # inside _Quiesced leaves Rocket.Chat STOPPED with nothing to restart it; a
        # load test killed mid-run leaves the rate limiter disabled, the Mongo
        # profiler armed and container CPU/RAM caps applied. With the design's
        # `Restart=always` unit that is routine, not an edge case.
        self._threads: dict[str, threading.Thread] = {}
        self._draining = threading.Event()

    def drain(self, timeout: float = 25.0) -> list[str]:
        """Refuse new jobs and wait for running ones. Returns what was abandoned.

        Bounded, because a capacity search can run for an hour and a shutdown that
        never completes is its own failure -- systemd would SIGKILL us anyway, and
        then nothing has run its cleanup. Whatever is still going is NAMED, so the
        operator knows which repro to look at rather than discovering it later.
        """
        self._draining.set()
        deadline = time.monotonic() + timeout
        with self._lock:
            pending = [(jid, t) for jid, t in self._threads.items() if t.is_alive()]
        for jid, thread in pending:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            return [jid for jid, t in self._threads.items() if t.is_alive()]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        """Every retained job, newest first. dicts preserve insertion order, and
        submit() only ever appends, so reversing gives newest-first."""
        with self._lock:
            return [j.summary() for j in reversed(list(self._jobs.values()))]

    def _evict_locked(self) -> None:
        """Drop the oldest FINISHED jobs once past MAX_JOBS. dicts preserve
        insertion order, so the oldest come first; running jobs are never
        evicted."""
        if len(self._jobs) <= MAX_JOBS:
            return
        for jid, job in list(self._jobs.items()):
            if len(self._jobs) <= MAX_JOBS:
                return
            if job.status not in ACTIVE_STATUSES:
                del self._jobs[jid]

    def submit(self, kind: str, target: Callable[..., Any], *args,
               label: str = "", actor: str = "", **kwargs) -> Job:
        """Run `target(*args, emit=job.emit, **kwargs)` on a worker thread.

        `label` and `actor` are keyword-only and consumed here rather than
        forwarded; no job target takes either argument.
        """
        if self._draining.is_set():
            # A ReproError, so both front ends report it properly instead of a 500
            # during shutdown.
            raise ConflictError(
                "rc-repro is shutting down and is not accepting new work")
        actor = actor or CURRENT_ACTOR.get("")
        slots = _slots_for(kind)
        job = Job(id="job_" + uuid.uuid4().hex[:10], kind=kind, label=label,
                  actor=actor,
                  # Shown as "waiting for a slot" rather than looking hung. A job
                  # that is queued has not started, and saying so is the whole
                  # point of bounding this.
                  status="queued" if slots is not None else "running")
        audit(actor, kind, label)
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()

        def run() -> None:
            # Set status/result BEFORE emitting the terminal event: a client that
            # polls /api/jobs/<id> on seeing `terminal` would otherwise still read
            # status="running", result=null. finished_at goes with them, for the
            # same reason.
            if slots is not None:
                if not slots.acquire(blocking=False):
                    job.emit(Event(f"waiting for a free {kind} slot — something "
                                   "else is using the machine", phase="queued"))
                    slots.acquire()
                job.status = "running"
            try:
                result = target(*args, emit=job.emit, **kwargs)
                job.result = result
                job.finished_at = time.time()
                job.status = "done"
                job.emit(Event("done", phase="done", terminal=True,
                               data={"result": result}))
            except ReproError as exc:
                job.error, job.error_kind = str(exc), type(exc).__name__
                job.finished_at = time.time()
                job.status = "error"
                job.emit(Event(str(exc), phase="done", level="error", terminal=True,
                               data={"error": str(exc), "kind": type(exc).__name__}))
            except Exception as exc:  # noqa: BLE001 - unexpected; surface, don't crash server
                # The traceback goes to the server's own stderr, NOT to the
                # browser: it carries absolute paths and internal structure, and
                # the GUI renders whatever it is handed.
                traceback.print_exc()
                job.error, job.error_kind = str(exc) or repr(exc), "InternalError"
                job.finished_at = time.time()
                job.status = "error"
                job.emit(Event(f"internal error: {exc}", phase="done", level="error",
                               terminal=True, data={"error": str(exc)}))
            finally:
                # In `finally`, not after the try: a job that raises still holds a
                # slot, and one leaked slot on the measurement pool (size 1) wedges
                # every future load test for the life of the process.
                if slots is not None:
                    slots.release()

        thread = threading.Thread(target=run, name=f"job-{job.id}", daemon=True)
        with self._lock:
            self._threads[job.id] = thread
            # Bounded with the job registry it mirrors, or a long-lived server
            # accumulates one dead Thread object per job forever.
            for jid in [j for j in self._threads if j not in self._jobs]:
                del self._threads[jid]
        thread.start()
        return job
