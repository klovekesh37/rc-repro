"""The machine-readable output contract: one envelope every `--json` reply uses.

Why an envelope rather than a bare payload per command: a caller should find
success, data, warnings, and errors in the same place regardless of which verb it
ran, so one parser handles all of them. Without that, every consumer needs
per-command shape knowledge, which is how a "stable" interface quietly becomes 25
unstable ones.

    {
      "schema": "rc-repro.info.v1",
      "contract": 1,
      "rc_repro_version": "0.9.3",
      "generated_at": "2026-07-31T09:14:02+00:00",
      "ok": true,
      "data": {...},
      "warnings": [{"code": "...", "message": "..."}],
      "error": null
    }

Two independent version numbers, because they answer different questions:

- `contract` is the wire generation. It bumps only on a breaking change to the
  envelope, the error-code set, or the exit-code map. A caller that does not
  recognise it should refuse to run rather than guess.
- `schema` is `rc-repro.<kind>.v<n>`, versioning each payload independently, so
  adding a field to one command does not churn every other command's version.

Additive changes are NOT breaking: new optional `data` keys, new `warnings`
entries. Callers must ignore unknown keys. Removing or retyping a key, or
emitting an error code outside the published set, IS breaking.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import typer

from rc_repro import __version__, errors, presets

#: Wire contract generation. See the module docstring before changing this.
CONTRACT = 1


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
        # Always a list, never null, so callers can iterate without a None check.
        "warnings": list(warnings or []),
        "error": None,
    }


def error_envelope(exc: errors.ReproError) -> dict:
    """Build a failure envelope from a domain error.

    `code` comes from the errors taxonomy so the CLI exit code, the web API
    status, and this payload all derive from one definition. `message` is prose
    and may be reworded between releases; only `code` is stable.
    """
    err: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    gate = getattr(exc, "as_gate", None)
    if callable(gate):
        # An authority gate carries what to ask a human to run. Never something
        # the caller may run itself.
        err["gate"] = gate()
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


#: The closed progress vocabulary. Deliberately the informal names already used by
#: services/events.py (pull, boot, wait, post_ready, seed, restore, done) plus the
#: phases that only exist once preflight and non-Compose topologies are real, so
#: the diff against current behaviour stays small and the web GUI's stream keeps
#: working.
#:
#: A caller may branch on any name here. A name NOT here is normalised to "info"
#: (with the original preserved in detail.phase_raw) so the published set really is
#: closed: callers never have to handle a phase that appeared without a contract
#: bump. Load-test phases such as "k6" go through that path on purpose; those verbs
#: have their own JSON output and are outside the lifecycle contract.
PHASES: tuple[str, ...] = (
    "preflight",    # engine, versions, disk, ports, connectivity checks
    "resolve",      # version / preset / image resolution
    "provision",    # create or verify the execution target itself
    "pull",         # fetching images
    "boot",         # starting components
    "wait",         # waiting for readiness or health
    "post_ready",   # settings applied after Rocket.Chat serves
    "seed",         # seeding users, channels, messages
    "restore",      # restoring data into a repro
    "teardown",     # stopping and removing
    "done",         # finished successfully
    "failed",       # terminal failure; carries detail.code
    "info",         # uncategorised progress; never branch on it
)


class EventWriter:
    """Serialises progress events as NDJSON, one object per line on stdout.

    Holds the small amount of state the contract's guarantees need: `pct` must be
    monotonic non-decreasing within a run, because a bar that goes backwards makes
    a caller think a new attempt started.
    """

    def __init__(self) -> None:
        self._max_pct: float | None = None

    def event(self, ev: Any) -> dict:
        """Build the wire object for one services.events.Event."""
        phase = ev.phase if ev.phase in PHASES else "info"
        detail = dict(ev.data or {})
        if phase != ev.phase:
            # Keep the original rather than discard it: useful for debugging, and
            # it means normalising to "info" loses nothing.
            detail["phase_raw"] = ev.phase
        pct = ev.pct
        if pct is not None:
            # Clamp rather than drop: a caller can still see progress, and the
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
        # Terminal events are dropped here: the command wrapper emits the final
        # envelope itself, and the contract promises exactly one envelope, last.
        if getattr(ev, "terminal", False):
            return
        emit(self.event(ev))


def emit(payload: dict) -> None:
    """Print one envelope on stdout as a single line.

    One object per line so a caller can read a stream of them without a
    incremental JSON parser. Human-facing prose goes to stderr, never here.
    """
    typer.echo(json.dumps(payload, sort_keys=True))


def fail(exc: errors.ReproError) -> None:
    """Emit an error envelope and exit with the code its class defines."""
    emit(error_envelope(exc))
    raise typer.Exit(exc.exit_code)


def _error_codes() -> list[str]:
    """Every stable error code this build can emit.

    Walked from the exception hierarchy rather than listed by hand, so a new
    ReproError subclass cannot be forgotten here.
    """
    seen: set[str] = set()

    def walk(cls: type) -> None:
        code = getattr(cls, "code", None)
        if code:
            seen.add(code)
        for sub in cls.__subclasses__():
            walk(sub)

    walk(errors.ReproError)
    # AuthorityGateError carries its specific code as an argument, so the class walk
    # only finds the base "GATE". The declared registry supplies the rest, otherwise
    # a caller could not anticipate a gate it is required to stop on.
    seen.update(errors.GATE_CODES)
    return sorted(seen)


def _commands(app: Any) -> list[dict]:
    """Describe the CLI surface by introspecting the registered commands.

    Derived, not hardcoded: a hand-written list would silently drift from the real
    flags, and the whole point of this call is that an agent can trust it. Only
    option flags are reported, since those are what a caller composes.
    """
    import inspect

    out: list[dict] = []
    for cmd in getattr(app, "registered_commands", []):
        cb = cmd.callback
        if cb is None:
            continue
        # typer defaults a command's name to the function name with underscores
        # turned into dashes; an explicit name= wins.
        name = cmd.name or cb.__name__.replace("_", "-")
        flags: list[str] = []
        streams = False
        supports_json = False
        for param in inspect.signature(cb).parameters.values():
            decls = getattr(param.default, "param_decls", None)
            if not decls:
                continue
            flags.extend(d for d in decls if d.startswith("--"))
            if param.name in ("json_out", "json_output"):
                supports_json = True
                # A verb that takes an emit-driven service call streams events.
                streams = name in ("up", "ready", "down")
        entry: dict[str, Any] = {"name": name, "flags": sorted(set(flags)),
                                 "json": supports_json}
        if supports_json:
            entry["schema"] = f"rc-repro.{name}.v1"
            entry["streams"] = streams
        out.append(entry)
    return sorted(out, key=lambda e: e["name"])


def _onboarding_state() -> dict:
    try:
        from rc_repro.services import onboarding
        st = onboarding.state()
        return {"completed": st["completed"], "grants": st["grants"],
                "preferences": st["preferences"],
                "onboard_with": onboarding.ONBOARD_COMMAND}
    except Exception:  # noqa: BLE001 - discovery must not fail on a bad config
        return {"completed": False, "grants": {}, "preferences": {},
                "onboard_with": "rc-repro onboard"}


def _skill_state() -> dict:
    try:
        from rc_repro.services import skill
        return skill.state_for_capabilities()
    except Exception:  # noqa: BLE001
        return {}


def capabilities(app: Any) -> dict:
    """What this build can do, for a version-matched agent skill.

    Must answer offline and without a working container engine: a caller asks this
    *before* it knows whether the environment works, so anything requiring the
    engine belongs in `doctor` instead.
    """
    try:
        catalog = list(presets.list_presets())
    except Exception:  # noqa: BLE001 - discovery must not fail on a bad user preset
        catalog = []
    preset_names = sorted(p.name for p in catalog)
    # Derived from the catalog, so a new topology becomes discoverable the moment a
    # preset uses it. "compose" is always available as the default path.
    topologies = sorted({getattr(p, "topology", "compose") or "compose"
                         for p in catalog} | {"compose"})
    return {
        "contract_versions": [CONTRACT],
        "rc_repro_version": __version__,
        "commands": _commands(app),
        "phases": list(PHASES),
        "error_codes": _error_codes(),
        "exit_codes": {str(k): v for k, v in sorted(errors.EXIT_CODES.items())},
        "presets": preset_names,
        # Whether a human has onboarded this machine, and which authority was
        # handed over. A skill reads this instead of parsing config.yaml, which
        # keeps the config file an implementation detail rather than a second
        # contract.
        "onboarding": _onboarding_state(),
        # Whether the installed agent skill matches this build, so a skill learns
        # it is stale through the contract it already reads.
        "skill": _skill_state(),
        "topologies": topologies,
        # Presets whose topology is not compose, so a skill can tell which ones
        # need a cluster before it tries.
        "presets_by_topology": {
            t: sorted(p.name for p in catalog
                      if (getattr(p, "topology", "compose") or "compose") == t)
            for t in topologies
        },
        # Public selector vocabulary. The compatibility matrix is kept out of
        # this first additive surface; unsupported requests still return the
        # structured VALIDATION_FAILED envelope before mutation.
        "selection": {
            "deployment_flag": "--deployment",
            "scenario_flag": "--scenario",
            "scenario_repeatable": True,
            "deployment_presets": list(presets.deployment_names()),
            "scenario_names": list(presets.scenario_names()),
            "saved_config_keys": ["default_deployment", "default_scenarios"],
            "legacy_preset_alias": True,
        },
        # What each topology supports, so an agent or GUI can hide unsupported
        # choices before submission rather than learning only after failure.
        "topology_features": {
            "compose": {
                "seed": True,
                "seed_stats": True,
                "scale": True,
                "clear_scale": True,
            },
            "kubernetes": {
                "seed": True,
                "seed_stats": False,
                "scale": False,
                "clear_scale": False,
            },
        },
    }
