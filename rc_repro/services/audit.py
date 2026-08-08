"""Who did what, in a file that survives a restart.

Lives in services/, not web/, for two reasons the first version got wrong:

* **The CLI has to write to it too.** `audit()` used to live in web/jobs.py, so
  `rc-repro down --volumes` on a shared box left no trace at all -- importing the
  web layer from the CLI to reach it was not an option (the core CLI deliberately
  does not depend on FastAPI).
* **Auditing at the front end misses whatever the front end does not route
  through.** The single call site was JobManager.submit(), so every endpoint that
  worked SYNCHRONOUSLY wrote nothing -- and that set was teardown and prune, the
  two most destructive operations in the product. The log filled with creates and
  seeds and stayed silent about deletions, which is worse than no log: it looks
  complete.

So the calls belong in the service layer, where both front ends already meet.
"""

from __future__ import annotations

import contextvars
import os
from datetime import datetime, timezone

from rc_repro import config

#: Appended to on every audited action. Tab-separated so `cut`/`awk` work on it.
AUDIT_FILE = "audit.log"

#: Who is making the current request/invocation. Set once -- by the web guard per
#: request, by the CLI at startup -- rather than threaded through fifteen
#: signatures. Verified to propagate into Starlette's threadpool, where every
#: `def` handler runs.
CURRENT_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "rc_repro_actor", default="")


def actor() -> str:
    return CURRENT_ACTOR.get("") or ""


def set_actor(name: str) -> None:
    CURRENT_ACTOR.set(name or "")


def audit_path():
    return config.home() / AUDIT_FILE


def audit(actor_name: str, kind: str, label: str) -> None:
    """Append one line: timestamp, actor, kind, label.

    Best-effort -- an unwritable log must never stop the work. Created 0600: on
    the shared box this exists for, the trail names people and what they touched.
    """
    try:
        line = (f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t"
                f"{actor_name or '-'}\t{kind}\t{label or '-'}\n")
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Open through os.open so the mode applies at CREATION; a later chmod
        # leaves a window where the file is world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def record(kind: str, label: str = "") -> None:
    """Audit an action by the CURRENT actor. What service code calls."""
    audit(actor(), kind, label)
