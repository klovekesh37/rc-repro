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
from rc_repro.services.audit import (AUDIT_FILE, CURRENT_ACTOR,  # noqa: F401
                                     CURRENT_ORIGIN, audit)
from rc_repro.services.events import Event


# `serve` is long-lived and every streamed docker line becomes an Event, so both
# the registry and each job's buffer need a ceiling or they grow for the life of
# the process.
MAX_JOBS = 100
MAX_EVENTS_PER_JOB = 2000

# How many FINISHED jobs keep their full result document. The registry above is a
# hundred entries; the results hanging off them are not the same size at all.
#
# A capacity search returns `steps` -- one complete loadtest result per VU level,
# each with its summary, SLO table, verdict, resource samples and (with --diag) a
# Mongo slow-query dump and a timeline. Measured: ~118 KB each, so a hundred of
# them is 68.8 MB held for the life of the process, on a box this file's own
# comments describe as hitting RAM before anything else.
#
# The Activity LIST only ever renders summary(), which excludes the result. The
# result is needed when somebody reopens a job, and the job people reopen is a
# recent one. So keep a hundred summaries and ten results.
KEEP_RESULTS = 10

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
#
# THE POOL IS CHOSEN BY THE KIND STRING, AND THE KIND STRING IS NOT THE WORK. Two
# routes submitted engine-heavy work under a label nobody had added here: "up" calls
# `lc.create_repro` -- the same function as "create" -- and "rollback" restores a
# bundle exactly as "restore" does. Both therefore resolved to no pool at all, so N
# concurrent `POST /api/repros/{name}/up` calls ran N compose-ups at once, past a
# `check_capacity` each of them passes independently because none has finished
# allocating yet. A GUI that retries a failed start reaches that by accident.
# `test_every_engine_heavy_job_kind_lands_in_a_pool` walks app.py and fails if a new
# one is added without a pool, which is the only thing that keeps this honest.
_MEASURE_KINDS = frozenset({"loadtest", "capacity", "benchmark"})
_HEAVY_KINDS = frozenset({"create", "up", "restore", "rollback", "upgrade",
                          "backup", "scale", "teardown", "prune",
                          # `attach` does a PULLING `runner.up` of six containers, and
                          # `detach` removes them and their volumes. Both were unpooled
                          # because the route chose the kind with a ternary, which the
                          # walk test could not read.
                          "monitor", "monitor-off",
                          # A seed drives hundreds of REST writes and is the one
                          # data-mutating operation that had neither a pool nor a lock.
                          "seed"})
_measure_slots = threading.BoundedSemaphore(1)
_heavy_slots = threading.BoundedSemaphore(max(2, (os.cpu_count() or 4) // 2))

# ...and the QUEUE needs a ceiling too, which is a different thing from the pools
# above. Those bound how many run at once; they are acquired INSIDE the worker
# thread, so the thread already exists by the time it waits. Measured: 40 capacity
# submissions against a measurement pool of size 1 produced 40 live OS threads and
# 40 retained Job objects, and _evict_locked() will not drop them because a queued
# job is correctly counted as active. So the bound on concurrency was not a bound
# on resources at all -- any member+ could hold as many threads as they cared to
# click for, on a box this file's own comments describe as hitting RAM first.
#
# Refusing at SUBMIT is the fix: a person who queues a 33rd load test wants a
# message, not a silent thread. Generous enough that no honest workflow reaches it.
#
# PER POOL, not per kind -- see the count in submit(). The first spelling of this was
# per kind, which multiplied the bound by the number of kinds sharing each pool and
# let the exact thing it was measured against happen anyway.
MAX_QUEUED_PER_POOL = 32
#: Old name, kept so anything reading it still resolves. The bound it described was
#: never the one that mattered.
MAX_QUEUED_PER_KIND = MAX_QUEUED_PER_POOL


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

    #: Whether the result document was released to reclaim memory. Reported by
    #: GET /api/jobs/{id}, because a panel that renders nothing is indistinguishable
    #: from a job that produced nothing.
    result_dropped: bool = False

    def forget_result(self) -> None:
        """Release the result document, keeping everything the list view shows.

        BOTH references have to go. `job.result` and the terminal event's
        `data["result"]` are the SAME object -- not a copy, which is easy to
        misread -- so clearing one of them frees nothing at all.
        """
        if self.result is None and not self.result_dropped:
            return
        with self._lock:
            self.result = None
            self.result_dropped = True
            for ev in self.events:
                if ev.terminal and isinstance(ev.data, dict):
                    ev.data.pop("result", None)

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

    def _trim_results_locked(self) -> None:
        """Keep the full result only for the newest KEEP_RESULTS finished jobs.

        dicts preserve insertion order and submit() only appends, so `finished` is
        oldest-first. Running jobs are never touched -- theirs is not written yet.
        """
        finished = [j for j in self._jobs.values()
                    if j.status not in ACTIVE_STATUSES]
        for job in finished[:-KEEP_RESULTS] if KEEP_RESULTS else finished:
            job.forget_result()

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
        # Captured HERE, on the request thread, because contextvars do not cross
        # into a bare threading.Thread -- measured: the worker sees "". Latent
        # until something inside a job audits; services/perf.py mints a PAT inside
        # the loadtest and capacity jobs, so the moment that is audited every one
        # of those lines would have been written with no actor at all.
        origin = CURRENT_ORIGIN.get("")
        slots = _slots_for(kind)
        if slots is not None:
            # COUNTED PER POOL, not per kind. The ceiling protects a resource, and the
            # resource is the pool -- which three kinds share on the measurement side
            # and nine on the heavy side. Counting per kind made the reachable total
            # 3x32 threads against a pool of 1 and 9x32 against a pool of 2, so the
            # bound written to stop 40 threads permitted 384. And `_evict_locked`
            # cannot claw any of it back: a queued job is correctly counted active, so
            # MAX_JOBS is unenforceable against exactly the jobs this refuses.
            with self._lock:
                waiting = sum(1 for j in self._jobs.values()
                              if j.status == "queued" and _slots_for(j.kind) is slots)
            if waiting >= MAX_QUEUED_PER_POOL:
                raise ConflictError(
                    f"{waiting} job(s) are already waiting for a free slot. "
                    "Let those finish first — see the Activity list.")
        job = Job(id="job_" + uuid.uuid4().hex[:10], kind=kind, label=label,
                  actor=actor,
                  # Shown as "waiting for a slot" rather than looking hung. A job
                  # that is queued has not started, and saying so is the whole
                  # point of bounding this.
                  status="queued" if slots is not None else "running")
        audit(actor, kind, label, origin_=origin)
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()
            # Here rather than on completion: submit() is what grows the registry,
            # so trimming here bounds the peak. A hundred jobs that all finish
            # without another submit hold their results, but they are also not
            # adding any.
            self._trim_results_locked()

        def run() -> None:
            # Re-establish the request's identity on this thread (see above).
            CURRENT_ACTOR.set(actor)
            CURRENT_ORIGIN.set(origin)
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
            except BaseException as exc:  # noqa: BLE001 - see below
                # NOT AN ACADEMIC BRANCH. Only ReproError and Exception were caught, so
                # anything else -- a KeyboardInterrupt delivered to this thread, a
                # SystemExit from a library, a MemoryError -- left `status` at "running"
                # forever. That is in ACTIVE_STATUSES, so `_evict_locked` can never drop
                # the job, `_trim_results` never releases its result, and the SSE stream
                # never terminates: a browser tab holds the connection open waiting for
                # an event that cannot arrive. Re-raised after being recorded, because
                # swallowing a BaseException is its own bug.
                traceback.print_exc()
                job.error = f"{type(exc).__name__}: {exc}" or repr(exc)
                job.error_kind = type(exc).__name__
                job.finished_at = time.time()
                job.status = "error"
                job.emit(Event(f"job ended abnormally: {type(exc).__name__}",
                               phase="done", level="error", terminal=True,
                               data={"error": str(exc)}))
                raise
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
