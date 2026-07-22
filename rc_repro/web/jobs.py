"""In-memory job manager for long-running service calls.

A job runs a service function on a worker thread; the function's `emit(Event)`
appends to the job's event buffer. The SSE endpoint polls that buffer (simple
and robust for a local single-user server) and streams new events until the job
finishes. No web-framework dependency lives here.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from rc_repro.errors import ReproError
from rc_repro.services.events import Event


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"          # running|done|error
    events: list[Event] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    error_kind: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, ev: Event) -> None:
        with self._lock:
            self.events.append(ev)

    def snapshot(self, since: int) -> tuple[list[dict], bool]:
        """Events after index `since`, plus whether the job has finished."""
        with self._lock:
            evs = [e.as_dict() for e in self.events[since:]]
            return evs, self.status != "running"


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def submit(self, kind: str, target: Callable[..., Any], *args, **kwargs) -> Job:
        """Run `target(*args, emit=job.emit, **kwargs)` on a worker thread."""
        job = Job(id="job_" + uuid.uuid4().hex[:10], kind=kind)
        with self._lock:
            self._jobs[job.id] = job

        def run() -> None:
            try:
                result = target(*args, emit=job.emit, **kwargs)
                job.emit(Event("done", phase="done", terminal=True,
                               data={"result": result}))
                job.result = result
                job.status = "done"
            except ReproError as exc:
                job.error, job.error_kind = str(exc), type(exc).__name__
                job.emit(Event(str(exc), phase="done", level="error", terminal=True,
                               data={"error": str(exc), "kind": type(exc).__name__}))
                job.status = "error"
            except Exception as exc:  # noqa: BLE001 - unexpected; surface, don't crash server
                tb = traceback.format_exc()
                job.error, job.error_kind = str(exc) or repr(exc), "InternalError"
                job.emit(Event(f"internal error: {exc}", phase="done", level="error",
                               terminal=True, data={"error": str(exc), "trace": tb}))
                job.status = "error"

        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job
