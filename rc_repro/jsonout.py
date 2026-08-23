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

#: Warnings collected for THIS command's envelope.
#:
#: The field has been in the envelope since it was defined and nothing ever wrote to
#: it: `grep -n 'warnings=' cli.py` returned nothing and all ten `reply()` sites
#: omitted it. So a caller reading the documented field for the documented purpose got
#: [] on every command, every time -- including the two cases that most need it.
#:
#: `doctor --json` returned `{"ok": true, "warnings": [], "data": {"verdict": "warn",
#: "counts": {"warn": 6, ...}}}`: six warnings, none of them in `warnings`. And `up
#: --version 7.0.0` against an existing 8.5.1 workspace reuses it and IGNORES the
#: version -- which it says loudly in prose and, in JSON, only as an NDJSON progress
#: event, leaving the envelope `ok: true, warnings: []`. For a tool whose single
#: purpose is version-matching, a caller following the documented read-the-last-line
#: strategy saw success.
_WARNINGS: list[dict] = []


def warn_once(code: str, message: str, **detail) -> None:
    """Add a warning to this command's envelope. Deduplicated by code.

    `code` is the stable half and `message` is prose, exactly as in the error payload,
    so a caller can branch on the code without matching English.
    """
    if any(w.get("code") == code for w in _WARNINGS):
        return
    entry = {"code": code, "message": message}
    if detail:
        entry["detail"] = dict(detail)
    _WARNINGS.append(entry)


def warnings_so_far() -> list[dict]:
    return list(_WARNINGS)


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
    _WARNINGS.clear()
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
    """The success envelope, which must be the last line the command writes.

    Collected warnings are folded in by DEFAULT, so a call site cannot forget them --
    which is how the field came to be inert on all ten of them.
    """
    collected = warnings_so_far()
    if warnings:
        collected += [w for w in warnings
                      if not any(c.get("code") == w.get("code") for c in collected)]
    emit(envelope(kind, data, schema_version=schema_version,
                  warnings=collected or None))


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


# --- capabilities ---------------------------------------------------------------
#
# What this build can do, described by the build itself. It exists so an agent (or a
# CI step, or a colleague's script) can ask rather than assume: which commands are
# here, which speak JSON, which stream, what the codes mean. The alternative is a
# caller that hardcodes a flag list and breaks the first time one moves -- and a
# hardcoded list on OUR side would drift from the real flags just as fast, which is
# why every field below is derived from the registered app rather than written out.
#
# No `--json` flag of its own, deliberately: this command IS JSON. A flag that is
# accepted and then ignored is worse than no flag.


def _error_codes() -> list[str]:
    """Every stable error code this build can actually emit.

    Walked from the exception hierarchy rather than listed by hand, so a new
    ReproError subclass cannot be forgotten.

    `errors.GATE_CODES` is deliberately NOT merged in. Those name authority gates --
    onboarding, an unapproved cluster, public exposure -- and this build raises none
    of them: `AuthorityGateError` is defined and never used. Advertising a code
    nothing can produce is the exact failure this document exists to prevent, and it
    would teach a caller to write a branch that never runs. If a gate ever gets
    raised, it joins the hierarchy walk on its own.
    """
    seen: set[str] = set()

    def walk(cls: type) -> None:
        code = getattr(cls, "code", None)
        # `advertised = False` marks a class the hierarchy knows about and this build
        # never raises. The exclusion above was applied to GATE_CODES and not to the
        # class that carries them, so the base "GATE" was published regardless -- the
        # very thing the paragraph above forbids, by the mechanism it describes.
        if code and getattr(cls, "advertised", True):
            seen.add(code)
        for sub in cls.__subclasses__():
            walk(sub)

    walk(errors.ReproError)
    return sorted(seen)


def _commands(app: Any) -> list[dict]:
    """The CLI surface, introspected from the registered commands.

    Groups are walked as well as top-level commands. rc-repro has no groups today,
    and the version of this borrowed from elsewhere read only
    `app.registered_commands` -- so the moment one is added, its subcommands would be
    missing from the document that claims to list everything, silently.
    """
    import inspect

    out: list[dict] = []

    def describe(cmd: Any, prefix: str = "") -> None:
        callback = getattr(cmd, "callback", None)
        if callback is None:
            return
        name = cmd.name or callback.__name__.replace("_", "-")
        name = name.removesuffix("-cmd")
        flags: list[str] = []
        speaks_json = False
        for param in inspect.signature(callback).parameters.values():
            decls = getattr(param.default, "param_decls", None) or ()
            flags.extend(d for d in decls if d.startswith("--"))
            if param.name in ("json_out", "json_output"):
                speaks_json = True
        # `capabilities` ITSELF. It answers in an envelope and always has -- it has no
        # `--json` flag because it has no other mode -- so deriving the flag from a
        # parameter made the one document whose purpose is saying which commands speak
        # JSON report that this one does not, while speaking it.
        if name == "capabilities":
            speaks_json = True
        entry: dict[str, Any] = {"name": (prefix + name).strip(),
                                 "flags": sorted(set(flags)), "json": speaks_json}
        if speaks_json:
            entry["schema"] = f"rc-repro.{entry['name']}.v1"
            # DERIVED, not a hardcoded list of verb names: a command streams if its
            # body builds an EventWriter. A list would be right on the day it was
            # written and wrong the first time a command gained progress.
            try:
                entry["streams"] = "EventWriter" in inspect.getsource(callback)
            except OSError:                      # no source (frozen, exec'd)
                entry["streams"] = False
        out.append(entry)

    for cmd in getattr(app, "registered_commands", []):
        describe(cmd)
    for group in getattr(app, "registered_groups", []):
        sub = getattr(group, "typer_instance", None)
        if sub is None:
            continue
        for cmd in getattr(sub, "registered_commands", []):
            describe(cmd, prefix=f"{group.name} ")
    return sorted(out, key=lambda e: e["name"])


def capabilities(app: Any) -> dict:
    """What this build can do, for a version-matched caller.

    Answers OFFLINE and with no container engine: a caller asks this *before* it
    knows whether the environment works, so anything needing docker belongs in
    `doctor` instead.
    """
    from rc_repro import config, presets
    from rc_repro.services import topology

    try:
        catalog = sorted(p.name for p in presets.list_presets())
    except Exception:  # noqa: BLE001 - discovery must not fail on a bad user preset
        catalog = []
    try:
        from rc_repro import seed as seeder
        profiles = sorted(seeder.PROFILES)
    except Exception:  # noqa: BLE001
        profiles = []
    return {
        "contract_versions": [CONTRACT],
        "rc_repro_version": __version__,
        "commands": _commands(app),
        "phases": list(PHASES),
        "error_codes": _error_codes(),
        "exit_codes": {str(k): v for k, v in sorted(errors.EXIT_CODES.items())},
        "presets": catalog,
        "seed_profiles": profiles,
        # The three axes, from the one table that defines them -- the same source
        # /api/settings serves the browser, so the CLI and the GUI cannot disagree
        # about what exists.
        "runtimes": [{"name": rt, "deployments": list(deps),
                      "default_deployment": deps[0],
                      "creatable": rt in topology.REGISTERED}
                     for rt, deps in topology.DEPLOYMENTS.items()],
        "default_runtime": topology.DOCKER,
        "default_bind_host": config.DEFAULT_BIND_HOST,
        "skill": _skill_state(),
    }


def _skill_state() -> dict:
    try:
        from rc_repro.services import skill
        return skill.state()
    except Exception:  # noqa: BLE001 - discovery must not fail on an odd home
        return {}
