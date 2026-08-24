"""A durable, redacted record of what rc-repro did — the file you read after the fact.

WHY THIS EXISTS. Everything else here is either state or a stream. `audit.log` records
who ATTEMPTED what, not what happened or why it failed. `journal/` holds side effects a
kill must not strand -- actionable state, a closed set of kinds. `web/jobs.py` buffers a
job's events beautifully and keeps them in MEMORY, so a `serve` restart loses even the
knowledge that a job existed. And on the CLI a failure prints to stderr and is gone when
the terminal scrolls. The richest record this tool produces was the one that never
survived the process.

WHY IT IS CHEAP. `Emit` is already the single funnel every long-running function goes
through, so one sink covers ~125 emit sites without touching any of them -- the same
property that got the GUI its progress bar for free.

FOUR RULES, each of which is a way this could have gone wrong:

  NEVER RAISE, NEVER PRINT. A log that cannot be written must not break the operation it
  was describing, and it must never touch stdout: `--json` promises exactly one envelope
  there and this would be the thing that broke it. Any OSError disables the sink for the
  rest of the process.

  REDACT AT THE SINK. Terminal events carry the whole result document, and the token,
  pat and env paths hold live credentials -- `MONGO_URL` carries a password in its VALUE,
  which is why `_URL_USERINFO` exists. Writing them to a file is an exposure that does
  not exist today, so this is the one place the feature can make things worse rather
  than better.

  IMPORT NOTHING AT MODULE LEVEL. `events.py` deliberately imports nothing from
  `rc_repro` and is imported by thirteen modules; the redaction helpers live in
  `lifecycle`, which imports `events`. A module-level import here is a circular import
  that breaks every command at startup. Everything is imported inside a function.

  BOUNDED. Two files, ~8 MiB. On a tool whose whole premise is that the box runs out of
  disk and RAM first, an unbounded log is a bug.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextvars import ContextVar

#: One id per process, so `grep '"run":"<id>"'` is one invocation start to finish --
#: across the CLI and across a GUI job, which is what makes this a timeline rather than
#: a pile of lines.
RUN = secrets.token_hex(4)

#: Current file plus one rotation. `RC_REPRO_LOG_MAX_MB=0` disables logging entirely.
DEFAULT_MAX_MB = 4
DIRNAME = "logs"
FILENAME = "rc-repro.log"

#: `data` keys dropped rather than walked. A result document is unbounded and holds
#: whatever the operation returned; the others are credentials by name. Dropping beats
#: redacting because a walk can miss a shape and this cannot.
_DROP_KEYS = ("result", "token", "auth", "password", "authtoken", "auth_token")

#: Set once the sink has failed. Checked first, so a broken log costs one boolean per
#: event rather than an exception per event.
_DISABLED = False
_SIZE: int | None = None          # cached, so there is no stat() per record

#: Filled in by whoever knows: the CLI sets the command line once at startup, and
#: `emit` cannot work it out for itself.
_CONTEXT: dict = {"cmd": "", "actor": "", "ws": ""}

#: PER-THREAD attribution, because one `serve` process is one `run` and it runs many
#: jobs at once. Without this, two concurrent creates interleave in the log with nothing
#: to tell them apart -- which would undermine the whole point of having it. `jobs.py`
#: already solves this shape for the actor and explains why: contextvars do not cross
#: into a bare `threading.Thread`, so the worker re-establishes them itself.
CURRENT_JOB: ContextVar[str] = ContextVar("rc_repro_log_job", default="")
CURRENT_WS: ContextVar[str] = ContextVar("rc_repro_log_ws", default="")


def configure(*, cmd: str = "", actor: str = "", ws: str = "") -> None:
    """Record what this process is, for every line it will write."""
    if cmd:
        _CONTEXT["cmd"] = cmd
    if actor:
        _CONTEXT["actor"] = actor
    if ws:
        _CONTEXT["ws"] = ws


def max_bytes() -> int:
    try:
        return int(float(os.environ.get("RC_REPRO_LOG_MAX_MB", DEFAULT_MAX_MB)) * 1_048_576)
    except (TypeError, ValueError):
        return DEFAULT_MAX_MB * 1_048_576


def log_path():
    """The log file. Imported lazily -- see the module docstring on import cycles."""
    from rc_repro import config
    return config.home() / DIRNAME / FILENAME


def _redact(value, _depth: int = 0):
    """Strip credentials from anything about to be written.

    Reuses `lifecycle`'s helpers rather than a second copy: a redaction rule that exists
    twice is one that will disagree with itself. `_depth` bounds a pathological
    structure rather than trusting the shape of a result document.
    """
    from rc_repro.services.lifecycle import _SECRET_KEY_HINTS, _URL_USERINFO, REDACTED

    if _depth > 6:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() in _DROP_KEYS:
                continue
            if any(h in key.lower() for h in _SECRET_KEY_HINTS):
                out[key] = REDACTED
            else:
                out[key] = _redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, _depth + 1) for v in value[:20]]
    if isinstance(value, str):
        # The credential the KEY NAME never reveals: `scheme://user:password@host`.
        return _URL_USERINFO.sub(r"\1:" + REDACTED + "@", value)
    return value


def _write(record: dict) -> None:
    """Append one line. Swallows everything; disables itself on the first failure."""
    global _DISABLED, _SIZE
    if _DISABLED:
        return
    cap = max_bytes()
    if cap <= 0:
        _DISABLED = True
        return
    try:
        path = log_path()
        # ONE write() to an O_APPEND file. `serve` runs continuously while somebody uses
        # the CLI -- two writers is the normal case here, which is why `update_config`
        # needed a real lock. For a regular file on a local filesystem, O_APPEND makes
        # the offset update and the write one operation, so a single write() per record
        # cannot interleave with another writer's. No lock and no read-modify-write.
        if _SIZE is None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _SIZE = path.stat().st_size if path.exists() else 0
        if _SIZE > cap:
            _rotate(path)
            _SIZE = 0
        line = (json.dumps(record, default=str, separators=(",", ":")) + "\n").encode()
        # 0600: this can carry a workspace name, an actor and an error message, and it
        # lives in a home that only `serve` tightens to 0700.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        _SIZE += len(line)
    except OSError:
        # A read-only home, a full disk, a path that is not a directory. Off for the
        # rest of the process, and silent: see the module docstring.
        _DISABLED = True


def _rotate(path) -> None:
    """Current -> .1, replacing whatever was there. Best-effort.

    The one non-atomic moment, so it takes a lock -- two processes both seeing the file
    over the cap would otherwise both rotate and one would lose the other's records. A
    failed rotation keeps appending rather than dropping anything.
    """
    try:
        import fcntl
        with open(path, "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                if path.stat().st_size > max_bytes():
                    os.replace(path, path.with_suffix(path.suffix + ".1"))
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (OSError, ImportError):
        pass


def _base(kind: str) -> dict:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "run": RUN, "pid": os.getpid(), "kind": kind,
           "actor": _CONTEXT["actor"], "cmd": _CONTEXT["cmd"],
           "ws": CURRENT_WS.get("") or _CONTEXT["ws"]}
    job = CURRENT_JOB.get("")
    if job:
        # The key that separates concurrent jobs inside one `serve`. `run` alone cannot:
        # it is per PROCESS, and a serve is one process for many jobs.
        rec["job"] = job
    return rec


def started(cmd: str, actor: str = "") -> None:
    configure(cmd=cmd, actor=actor)
    _write(_base("start"))


def ended(exit_code: int, secs: float) -> None:
    _write({**_base("end"), "exit": int(exit_code), "secs": round(secs, 2)})


def failed(code: str, message: str, *, traceback_on_stderr: bool = False) -> None:
    _write({**_base("error"), "code": code,
            "msg": _redact(str(message)), "trace": bool(traceback_on_stderr)})


def event(ev) -> None:
    """Record one `Event`. Called from the emit helpers, so every site is covered.

    DOCKER'S ECHO IS SKIPPED. `_up` turns every line of `docker compose pull` output
    into an event, so a GUI create emits several hundred `Downloading 45.09MB` records.
    That is echo, not narrative: logging it buries the four lines that matter and spends
    the whole size cap on one create. `RC_REPRO_LOG_ECHO=1` keeps it for when somebody
    is debugging a pull. A warning or an error is NEVER skipped, echo or not.
    """
    if _DISABLED:
        return
    level = getattr(ev, "level", "info")
    data = getattr(ev, "data", None) or {}
    if (level not in ("warn", "error")
            and data.get("echo")
            and os.environ.get("RC_REPRO_LOG_ECHO") != "1"):
        return
    rec = {**_base("event"), "phase": getattr(ev, "phase", ""), "level": level,
           "msg": _redact(str(getattr(ev, "message", "")))}
    clean = _redact(data)
    if clean:
        rec["data"] = clean
    _write(rec)


def job_started(job_id: str, kind: str, label: str) -> None:
    """A GUI job beginning. The record `web/jobs.py` could never keep: its registry is
    in memory, so a restart loses even the knowledge that a job existed."""
    _write({**_base("job-start"), "job_kind": kind, "label": label})


def job_ended(job_id: str, kind: str, label: str, status: str, error: str) -> None:
    rec = {**_base("job-end"), "job_kind": kind, "label": label, "status": status}
    if error:
        rec["msg"] = _redact(str(error))
    _write(rec)
