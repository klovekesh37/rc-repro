"""Installing the canonical agent skill into whichever host wants it.

The bundle ships as package data, so the skill in the package is by definition the
one that shipped with the running rc-repro. There is no second place to publish it
and no way for it to describe a version of rc-repro that is not installed. That is
what makes version matching structural rather than aspirational.

Installation copies and records a digest rather than symlinking. A symlink cannot
drift, which is tempting, but it breaks when pipx rebuilds or relocates the venv and
fails on Windows without developer mode: that trades a detectable problem for a
mysterious one. Drift is reported through `capabilities` instead, and a skill a
human has edited is never silently overwritten.

Host paths come from the published discovery rules. Cursor and Copilot need no
adapter of their own because they already read the Claude Code and Codex locations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from rc_repro import __version__
from rc_repro.errors import ConflictError, ValidationError

SKILL_NAME = "rc-repro"

#: Where each host looks. Cursor reads Claude Code's and Codex's directories, and
#: Copilot reads .claude/skills or .agents/skills, so neither needs its own copy.
HOSTS: dict[str, dict[str, str]] = {
    "claude": {"user": "~/.claude/skills", "project": ".claude/skills"},
    "codex": {"user": "~/.agents/skills", "project": ".agents/skills"},
}

#: Hosts that are satisfied by another host's install rather than their own.
COVERED_BY: dict[str, str] = {"cursor": "claude", "copilot": "codex"}

_SIDECAR = ".rc-repro-skill.json"


@dataclass
class Status:
    host: str
    scope: str
    path: Path
    state: str          # absent | current | stale | modified
    installed_version: str = ""
    digest: str = ""


def bundle_text() -> str:
    """The canonical SKILL.md, read from package data."""
    # Traversed from the rc_repro package rather than a data subpackage, so no
    # __init__.py is needed under data/.
    return (resources.files("rc_repro") / "data" / "skill" / "SKILL.md") \
        .read_text(encoding="utf-8")


def bundle_digest() -> str:
    return hashlib.sha256(bundle_text().encode("utf-8")).hexdigest()


def target_dir(host: str, scope: str = "user") -> Path:
    if host in COVERED_BY:
        raise ValidationError(
            f"{host} reads {COVERED_BY[host]}'s skill directory, so install for "
            f"{COVERED_BY[host]} instead")
    if host not in HOSTS:
        raise ValidationError(
            f"unknown host {host!r}; known: {', '.join(sorted(HOSTS))} "
            f"(covered for free: {', '.join(sorted(COVERED_BY))})")
    if scope not in ("user", "project"):
        raise ValidationError(f"scope must be 'user' or 'project', got {scope!r}")
    base = HOSTS[host][scope]
    return (Path(base).expanduser() if base.startswith("~") else Path(base)) / SKILL_NAME


def status(host: str, scope: str = "user") -> Status:
    """Whether the installed copy matches the running rc-repro."""
    path = target_dir(host, scope)
    skill = path / "SKILL.md"
    if not skill.exists():
        return Status(host, scope, path, "absent")
    side = path / _SIDECAR
    recorded: dict = {}
    if side.exists():
        try:
            recorded = json.loads(side.read_text(encoding="utf-8"))
        except ValueError:
            recorded = {}
    on_disk = hashlib.sha256(skill.read_bytes()).hexdigest()
    if recorded.get("digest") and on_disk != recorded["digest"]:
        # A human edited it. Never silently overwrite: clobbering someone's local
        # change costs more trust than the staleness costs.
        return Status(host, scope, path, "modified",
                      recorded.get("rc_repro_version", ""), on_disk)
    if recorded.get("rc_repro_version") != __version__:
        return Status(host, scope, path, "stale",
                      recorded.get("rc_repro_version", ""), on_disk)
    return Status(host, scope, path, "current", __version__, on_disk)


def install(host: str, scope: str = "user", *, force: bool = False) -> Status:
    """Install or repair the skill. Idempotent, so repairing is one command."""
    current = status(host, scope)
    if current.state == "modified" and not force:
        raise ConflictError(
            f"{current.path / 'SKILL.md'} has local edits; re-run with --force to "
            f"overwrite them")
    current.path.mkdir(parents=True, exist_ok=True)
    (current.path / "SKILL.md").write_text(bundle_text(), encoding="utf-8")
    (current.path / _SIDECAR).write_text(json.dumps({
        "rc_repro_version": __version__,
        "digest": bundle_digest(),
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": host,
        "scope": scope,
    }, indent=2), encoding="utf-8")
    return status(host, scope)


def install_all(scope: str = "user", *, force: bool = False) -> list[Status]:
    return [install(h, scope, force=force) for h in sorted(HOSTS)]


def state_for_capabilities() -> dict:
    """A compact summary for `capabilities`, so a skill learns it is stale through
    the contract it already reads rather than a second mechanism."""
    out: dict = {"bundled_version": __version__, "installs": []}
    for host in sorted(HOSTS):
        try:
            st = status(host, "user")
        except Exception:  # noqa: BLE001 - discovery must not fail on a bad path
            continue
        out["installs"].append({"host": host, "scope": st.scope, "state": st.state,
                                "installed_version": st.installed_version})
    out["repair"] = "rc-repro skill install --host all"
    return out
