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


#: How the identity on a line was established. Four values rather than a
#: verified/asserted boolean, because the code already distinguishes them and the
#: difference is what makes a line evidence or not:
#:
#:   session   a signed-in GUI request. The server checked a credential.
#:   local     the CLI, where os.getlogin() matched a known account.
#:   asserted  the CLI with RC_REPRO_USER set. Taken at face value -- cli.py:132
#:             honours it even for an unknown name, so this is a CLAIM.
#:   system    rc-repro acting on its own behalf (rotation, startup).
#:
#: A log that cannot say which of its lines are trustworthy is not much better
#: than one with no names at all.
ORIGINS = ("session", "local", "asserted", "system")

#: Who is making the current request, and HOW that was established. Set by the
#: web guard per request and by the CLI at startup, beside CURRENT_ACTOR.
CURRENT_ORIGIN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "rc_repro_origin", default="")

#: Rotate at 32 MB, keeping five. Checked cheaply rather than on every write.
MAX_BYTES = 32 * 1024 * 1024
KEEP = 5

_FIELDS = 6


def origin() -> str:
    return CURRENT_ORIGIN.get("") or ""


def set_origin(value: str) -> None:
    CURRENT_ORIGIN.set(value if value in ORIGINS else "")


def _rotate_if_needed(path) -> None:
    """Keep audit.log bounded. Best-effort, like everything else here."""
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    try:
        for n in range(KEEP - 1, 0, -1):
            src, dst = path.with_suffix(f".log.{n}"), path.with_suffix(f".log.{n + 1}")
            if src.exists():
                os.replace(src, dst)
        os.replace(path, path.with_suffix(".log.1"))
    except OSError:
        pass


def audit(actor_name: str, kind: str, label: str, *,
          origin_: str = "", outcome: str = "ok") -> None:
    """Append one line: timestamp, actor, kind, label, origin, outcome.

    The two new fields are APPENDED, never inserted, so every existing
    `cut -f2`/`awk '{print $3}'` keeps working and a four-field line is
    unambiguously one written before they existed.

    Best-effort -- an unwritable log must never stop the work. Created 0600: on
    the shared box this exists for, the trail names people and what they touched.
    """
    try:
        line = (f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t"
                f"{actor_name or '-'}\t{kind}\t{label or '-'}\t"
                f"{origin_ or origin() or '-'}\t{outcome or 'ok'}\n")
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        # Open through os.open so the mode applies at CREATION; a later chmod
        # leaves a window where the file is world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def record(kind: str, label: str = "", *, outcome: str = "ok") -> None:
    """Audit an action by the CURRENT actor. What service code calls."""
    audit(actor(), kind, label, origin_=origin(), outcome=outcome)


def parse_line(line: str) -> dict | None:
    """One stored line -> a record, or None if it is not one.

    A four-field line (written before origin/outcome existed) parses with
    `origin="-"` and `outcome="ok"`, so old logs stay readable rather than being
    silently skipped by the reader that was supposed to make them useful.
    """
    line = line.rstrip("\n")
    if not line:
        return None
    parts = line.split("\t")
    if len(parts) < 4:
        return None
    parts += ["-"] * (_FIELDS - len(parts))
    return {"ts": parts[0], "actor": parts[1], "kind": parts[2], "label": parts[3],
            "origin": parts[4] if parts[4] != "-" else "",
            "outcome": parts[5] if parts[5] in ("ok", "denied") else "ok"}


def read(*, limit: int = 200, actor_name: str = "", kind: str = "", q: str = "",
         since: str = "", max_bytes: int = 8 * 1024 * 1024) -> dict:
    """The most recent matching lines, newest first.

    Read BACKWARDS from the end in chunks: the interesting lines are the recent
    ones, and a year-old log on a busy box should not be walked from the start to
    show today. `max_bytes` bounds the scan and the result says when it stopped,
    because an unindexed file has an honest limit and pretending otherwise is how
    a filter silently lies about what it found.
    """
    path = audit_path()
    out: list[dict] = []
    scanned = 0
    truncated = False
    try:
        size = path.stat().st_size
    except OSError:
        return {"lines": [], "truncated": False, "path": str(path)}
    chunk = 64 * 1024
    tail = b""
    try:
        with open(path, "rb") as fh:
            pos = size
            while pos > 0 and len(out) < limit and scanned < max_bytes:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                block = fh.read(step) + tail
                scanned += step
                lines = block.split(b"\n")
                tail = lines.pop(0) if pos > 0 else b""
                for raw in reversed(lines):
                    rec = parse_line(raw.decode("utf-8", "replace"))
                    if not rec:
                        continue
                    if actor_name and rec["actor"] != actor_name:
                        continue
                    if kind and rec["kind"] != kind:
                        continue
                    if since and rec["ts"] < since:
                        continue
                    if q and q.lower() not in (rec["label"] + rec["kind"]).lower():
                        continue
                    out.append(rec)
                    if len(out) >= limit:
                        break
            truncated = pos > 0 and (len(out) >= limit or scanned >= max_bytes)
    except OSError:
        pass
    return {"lines": out, "truncated": truncated, "path": str(path)}
