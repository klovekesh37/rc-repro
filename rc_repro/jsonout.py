"""The machine-readable output contract: one envelope every `--json` reply uses.

Why an envelope rather than a bare payload per command: a caller should find
success, data, warnings and errors in the same place whichever verb it ran, so one
parser handles all of them. Without that, every consumer needs per-command shape
knowledge, which is how a "stable" interface quietly becomes twenty-five unstable
ones -- `loadtest --json` and `capacity --json` had already become two of them here,
each printing a bare payload with no way to tell success from failure except the
exit code.

    {
      "schema": "rc-repro.info.v1",
      "contract": 1,
      "rc_repro_version": "0.61.0",
      "generated_at": "2026-08-19T09:14:02+00:00",
      "ok": true,
      "data": {...},
      "warnings": [{"code": "...", "message": "..."}],
      "error": null
    }

Two independent version numbers, because they answer different questions:

- `contract` is the wire generation. It bumps only on a breaking change to the
  envelope, the error-code set or the exit-code map. A caller that does not
  recognise it should refuse to run rather than guess.
- `schema` is `rc-repro.<kind>.v<n>`, versioning each payload independently, so
  adding a field to one command does not churn every other command's version.

Additive changes are NOT breaking: new optional `data` keys, new `warnings`
entries. Callers must ignore unknown keys. Removing or retyping a key, or emitting
an error code outside the published set, IS breaking.

**stdout carries JSON and nothing else.** Every human-facing line moves to stderr
for the life of the process (see `ui.json_mode`), so `rc-repro info --json > case.json`
is a valid document and the person running it still sees what happened.

**Exactly one envelope, and it is last.** A streaming command writes NDJSON
`rc-repro.event.v1` progress lines first; the final line is the envelope, success or
failure. A caller can therefore read to end-of-stream and parse the last line
without buffering an incremental parser.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import typer

from rc_repro import __version__, errors

#: Wire contract generation. See the module docstring before changing this.
CONTRACT = 1

#: The closed progress vocabulary -- every `phase` a caller may branch on.
#:
#: Taken from the phases this codebase ACTUALLY emits (tests/test_core.py walks the
#: source and fails if a new one appears), plus four that the lifecycle will grow
#: into. That distinction matters: the obvious move is to adopt a short, tidy list
#: and normalise the rest to "info", and a short list borrowed from elsewhere would
#: have swallowed `tls`, `monitor`, `upgrade`, `create`, `backup`, `config`, `plan`
#: and `ready` -- eight of the sixteen we emit, and most of the interesting ones.
#: A vocabulary is closed because it is DOCUMENTED and enforced, not because it is
#: short.
#:
#: A phase not in here normalises to "info", with the original preserved in
#: `detail.phase_raw`, so a caller never has to handle a phase that appeared without
#: a contract bump.
PHASES: tuple[str, ...] = (
    "preflight",    # engine, versions, disk, ports, connectivity checks
    "resolve",      # version / image / chart resolution
    "provision",    # create or verify the execution target itself
    "plan",         # work out what will be done, before doing any of it
    "pull",         # fetching images
    "create",       # building the workspace's own definition
    "boot",         # starting components
    "wait",         # waiting for readiness or health
    "post_ready",   # settings applied after Rocket.Chat serves
    "config",       # changing a running workspace's configuration
    "tls",          # certificates and the shared edge
    "seed",         # seeding users, channels, messages
    "backup",       # dumping data out
    "restore",      # loading data back in
    "upgrade",      # moving a workspace between Rocket.Chat versions
    "monitor",      # attaching or detaching Prometheus + Grafana
    "teardown",     # stopping and removing
    "ready",        # the workspace is serving
    "done",         # finished successfully
    "failed",       # terminal failure; carries detail.code
    "queued",       # accepted, waiting for a slot rather than working yet
    "info",         # uncategorised progress; never branch on it
)

#: Whether this process is answering in JSON. Set once, by the command that was
#: given `--json`, and read by `_fail`/`_err` in cli.py so that EVERY error path
#: produces an envelope -- including the forty-eight call sites that predate this
#: module and know nothing about it. Without that, a `--json` caller would get a
#: clean envelope on the paths somebody remembered and an empty stdout with exit 1
#: on the rest, which is the worst of both: a contract that holds until it matters.
_ACTIVE = False


def activate() -> None:
    """Enter JSON mode for the rest of the process. Idempotent."""
    global _ACTIVE
    _ACTIVE = True
    from rc_repro import ui
    ui.json_mode(True)


def active() -> bool:
    return _ACTIVE


def json_mode_reset() -> None:
    """Leave JSON mode. For tests only: one real process runs one command, but a
    test process runs many, and a leaked flag would move the next command's prose
    to stderr where nothing is looking for it."""
    global _ACTIVE
    _ACTIVE = False
    from rc_repro import ui
    ui.json_mode(False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def envelope(kind: str, data: Any = None, *, schema_version: int = 1,
             warnings: list[dict] | None = None) -> dict:
    """Build a success envelope for `kind` (the command name, e.g. "info")."""
    return {
        "schema": f"rc-repro.{kind}.v{schema_version}",
        "contract": CONTRACT,
        "rc_repro_version": __version__,
        "generated_at": _now(),
        "ok": True,
        "data": data,
        # Always a list, never null, so a caller can iterate without a None check.
        "warnings": list(warnings or []),
        "error": None,
    }


def error_envelope(exc: BaseException) -> dict:
    """Build a failure envelope from an exception.

    `code` comes from the errors taxonomy, so the CLI exit code, the web API status
    and this payload all derive from one definition. `message` is prose and may be
    reworded between releases; only `code` is stable.

    A non-ReproError is reported as the base `REPRO_ERROR`/exit 1 rather than being
    dressed up as something more specific. Saying "unclassified" is honest; picking
    a plausible code would tell a caller to retry, or not to, on no evidence.
    """
    code = getattr(exc, "code", None) or errors.ReproError.code
    err: dict[str, Any] = {"code": code, "message": str(exc)}
    gate = getattr(exc, "as_gate", None)
    if callable(gate):
        # An authority gate carries what to ask a HUMAN to run -- never something
        # the caller may run itself.
        try:
            err["gate"] = gate()
        except Exception:  # noqa: BLE001 - the refusal matters more than its detail
            pass
    return {
        "schema": "rc-repro.error.v1",
        "contract": CONTRACT,
        "rc_repro_version": __version__,
        "generated_at": _now(),
        "ok": False,
        "data": None,
        "warnings": [],
        "error": err,
    }


def emit(payload: dict) -> None:
    """Print one object on stdout as a single line.

    One object per line so a caller can read a stream of them without an
    incremental JSON parser. `sort_keys` so two runs of the same command diff
    cleanly -- this output gets pasted into tickets.
    """
    typer.echo(json.dumps(payload, sort_keys=True))


def reply(kind: str, data: Any = None, *, schema_version: int = 1,
          warnings: list[dict] | None = None) -> None:
    """The success envelope, which must be the last line the command writes."""
    emit(envelope(kind, data, schema_version=schema_version, warnings=warnings))


def fail(exc: BaseException) -> None:
    """Emit an error envelope and exit with the code the exception's class defines.

    The human line goes to stderr as well. `rc-repro up --json > out.json` that
    fails should say so on the terminal rather than only inside a file the person
    then has to open -- and stdout still carries the envelope and nothing else.
    """
    from rc_repro import ui
    emit(error_envelope(exc))
    ui.fail(f"error: {exc}")
    raise typer.Exit(getattr(exc, "exit_code", 1))


class EventWriter:
    """Serialises progress events as NDJSON, one object per line on stdout.

    Holds the small amount of state the contract's guarantees need: `pct` must be
    monotonic non-decreasing within a run, because a bar that goes backwards makes a
    caller think a new attempt started.
    """

    def __init__(self) -> None:
        self._max_pct: float | None = None

    def event(self, ev: Any) -> dict:
        """The wire object for one services.events.Event."""
        phase = ev.phase if ev.phase in PHASES else "info"
        detail = dict(ev.data or {})
        if phase != ev.phase:
            # Kept rather than discarded: useful for debugging, and it means
            # normalising to "info" loses nothing.
            detail["phase_raw"] = ev.phase
        pct = ev.pct
        if pct is not None:
            # Clamped rather than dropped: a caller still sees progress, and the
            # monotonic promise holds even if a service reports out of order.
            if self._max_pct is not None and pct < self._max_pct:
                pct = self._max_pct
            self._max_pct = pct
        return {
            "schema": "rc-repro.event.v1",
            "contract": CONTRACT,
            "phase": phase,
            "level": ev.level,
            "pct": pct,
            "message": ev.message,   # prose for humans; never branch on it
            "detail": detail,
        }

    def emit(self, ev: Any) -> None:
        # Terminal events are dropped here: the command emits the final envelope
        # itself, and the contract promises exactly one envelope, last.
        if getattr(ev, "terminal", False):
            return
        emit(self.event(ev))
