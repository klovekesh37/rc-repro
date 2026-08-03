"""In-memory job manager for long-running service calls.

A job runs a service function on a worker thread; the function's `emit(Event)`
appends to the job's event buffer. The SSE endpoint polls that buffer (simple
and robust for a local single-user server) and streams new events until the job
finishes. No web-framework dependency lives here.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from rc_repro.errors import ReproError
from rc_repro.services.events import Event


# `serve` is long-lived and every streamed docker line becomes an Event, so both
# the registry and each job's buffer need a ceiling or they grow for the life of
# the process.
MAX_JOBS = 100
MAX_EVENTS_PER_JOB = 2000


@dataclass
class Job:
    id: str
    kind: str
    # What the job acts on (usually a repro name), for the activity list -- `kind`
    # alone cannot tell two concurrent seeds apart.
    label: str = ""
    status: str = "running"          # running|done|error
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
                    self.status != "running",
                    self._emitted)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

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
            if job.status != "running":
                del self._jobs[jid]

    def submit(self, kind: str, target: Callable[..., Any], *args,
               label: str = "", **kwargs) -> Job:
        """Run `target(*args, emit=job.emit, **kwargs)` on a worker thread.

        `label` is keyword-only and consumed here rather than forwarded; no job
        target takes a `label` argument.
        """
        job = Job(id="job_" + uuid.uuid4().hex[:10], kind=kind, label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()

        def run() -> None:
            # Set status/result BEFORE emitting the terminal event: a client that
            # polls /api/jobs/<id> on seeing `terminal` would otherwise still read
            # status="running", result=null. finished_at goes with them, for the
            # same reason.
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

        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job
