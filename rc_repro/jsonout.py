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

from rc_repro import __version__, errors

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
