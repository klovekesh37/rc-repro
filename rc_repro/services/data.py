"""Data-population services shared by the CLI and web API: bulk `--scale`
prefill, clear-scale, and config-import (dry-run plan + apply).

Extracted from cli.py so the web GUI runs the identical logic. Functions raise
`rc_repro.errors` and report progress via `emit`.
"""

from __future__ import annotations

import json
from pathlib import Path

from rc_repro import configimport, runner, scaleseed
from rc_repro.errors import DockerError, NotReadyError, ValidationError
from rc_repro.services import lifecycle
from rc_repro.services.events import Emit, info, null_emit, warn


# --- scale --------------------------------------------------------------------

def _scale_result(out: str) -> dict | None:
    """Last JSON line from a scaleseed mongosh run (banners may precede it)."""
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _scale_ok(rc: int, out: str, what: str, *, hint: str = "") -> dict:
    """Validate a scaleseed result: non-zero exit / no JSON => hard fail (Docker);
    a JS-level {error} payload (e.g. room-not-found) => a user error."""
    res = _scale_result(out)
    if rc != 0 or not res:
        raise DockerError(f"{what} failed (is mongodb up?): {(out or '').strip()[:200]}")
    if res.get("error"):
        raise ValidationError(f"{what} failed: {res['error']}" + (f" - {hint}" if hint else ""))
    return res


def run_scale(name: str, spec_str: str, emit: Emit = null_emit) -> dict:
    """Bulk-insert users/messages per a `--scale` spec (users=N,messages=N@room)."""
    target = lifecycle.resolve_name(name)
    try:
        spec = scaleseed.parse_scale(spec_str)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not spec:
        raise ValidationError("scale had nothing to do (want users=N and/or messages=N@room)")
    warn(emit, "bulk Mongo prefill: users are credential-less and messages fire no "
               "app hooks - for scale/perf repros, not feature testing.", phase="seed")
    result: dict = {}
    if "users" in spec:
        info(emit, f"inserting {spec['users']:,} users", phase="seed")
        res = _scale_ok(*scaleseed.bulk_users(target, spec["users"]), "user prefill")
        result["users"] = res.get("inserted", 0)
        info(emit, f"inserted {result['users']:,} users", phase="seed")
    if "messages" in spec:
        n, room = spec["messages"]
        info(emit, f"inserting {n:,} messages into {room!r}", phase="seed")
        res = _scale_ok(*scaleseed.bulk_messages(target, n, room), "message prefill",
                        hint="create the room first (REST seed, or use `general`)")
        result["messages"] = res.get("inserted", 0)
        result["room"] = room
        info(emit, f"inserted {result['messages']:,} messages into {room!r}", phase="seed")
    return result


def clear_scale(name: str, emit: Emit = null_emit) -> dict:
    """Remove everything a prior --scale added and restore affected rooms."""
    target = lifecycle.resolve_name(name)
    res = _scale_ok(*scaleseed.clear(target), "clear")
    out = {"users": res.get("users", 0), "messages": res.get("messages", 0),
           "rooms": res.get("rooms", 0)}
    info(emit, f"removed {out['users']:,} scale users and {out['messages']:,} scale messages",
         phase="done")
    return out


# --- config-import ------------------------------------------------------------

def _build_plan(settings_path: str, only: set[str] | None):
    try:
        return configimport.build_plan(Path(settings_path), only=only)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"couldn't read settings file: {exc}") from exc


def _repr(value) -> str:
    s = repr(value)
    return s if len(s) <= 80 else s[:80] + "..."


def import_plan(name: str, settings_path: str, only: set[str] | None = None) -> dict:
    """Dry-run: parse a support-dump *-settings.json into an import plan summary."""
    lifecycle.resolve_name(name)
    plan = _build_plan(settings_path, only)
    return {
        "apply": [{"id": sid, "value": _repr(v)} for sid, v in plan.apply],
        "redacted": plan.redacted,
        "denied": plan.denied,
        "oauth_services": plan.oauth_services,
        "counts": {"apply": len(plan.apply), "redacted": len(plan.redacted),
                   "denied": len(plan.denied)},
    }


def import_apply(name: str, settings_path: str, only: set[str] | None = None,
                 emit: Emit = null_emit) -> dict:
    """Apply the plan built from `settings_path` to the repro's live settings."""
    m = runner.read_meta(lifecycle.resolve_name(name))
    plan = _build_plan(settings_path, only)
    try:
        auth = lifecycle.login(m)
    except Exception as exc:  # noqa: BLE001
        raise NotReadyError(f"can't import - repro not ready: {exc}") from exc
    res = configimport.apply(m.root_url, auth, plan,
                             log=lambda s: info(emit, s.strip(), phase="config"))
    info(emit, f"imported {res['applied']} setting(s), skipped {res['skipped']}"
               + (f"; {res['failed']} rejected" if res["failed"] else ""), phase="done")
    return res
