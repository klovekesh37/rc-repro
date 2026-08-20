"""The agent skill: one canonical file, installed per host, and told when it's stale.

rc-repro is increasingly driven by something that is not a person -- an agent asked
"does this reproduce on 8.6.1?", a CI step, a colleague's script. That caller needs
to know how to drive it, and the failure mode if nobody tells it is specific: it
scrapes the human output, which breaks the first time a line is reworded.

`data/skill/SKILL.md` is the canonical copy and ships as package data, so the
instructions travel with the build rather than with the checkout. `install()` places
it where an agent looks -- `~/.claude/skills/rc-repro/` and `~/.agents/skills/` --
alongside a small sidecar recording the version and the file's sha256.

The sidecar is what makes `state()` able to say `current` / `stale` / `modified` /
`absent`, and that distinction is the point: a skill written for an older build
describes flags this one may not have, and the caller it is instructing has no way
to notice on its own. `capabilities` reports the answer, so a stale skill is
discovered through the contract the caller already reads.

MODIFIED is never overwritten without `--force`. Somebody who edited their local
copy did it on purpose, and clobbering that to install an identical-in-spirit file
is the kind of helpfulness nobody asks for twice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from datetime import datetime, timezone

from rc_repro import __version__
from rc_repro.errors import ConflictError, NotFoundError

#: Where an agent looks. Two conventions, both real: Claude Code reads
#: `~/.claude/skills`, and the wider agent convention is `~/.agents/skills`.
HOSTS: dict[str, str] = {
    "claude": ".claude/skills",
    "agents": ".agents/skills",
}

SKILL_DIR = "rc-repro"
SKILL_FILE = "SKILL.md"
SIDECAR = ".rc-repro-skill.json"


def packaged() -> Path:
    """The canonical file shipped inside the package."""
    return Path(__file__).resolve().parent.parent / "data" / "skill" / SKILL_FILE


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _home() -> Path:
    """The user's home, not RC_REPRO_HOME.

    A skill is read by an agent that knows nothing about rc-repro's state directory,
    so it goes where that agent looks. `RC_REPRO_SKILL_HOME` exists only so a test
    can point this somewhere disposable -- without it a test run would write into the
    developer's real `~/.claude`.
    """
    import os
    return Path(os.environ.get("RC_REPRO_SKILL_HOME") or Path.home())


def target(host: str) -> Path:
    if host not in HOSTS:
        raise NotFoundError(
            f"unknown skill host {host!r} (want {' | '.join(sorted(HOSTS))} or all)")
    return _home() / HOSTS[host] / SKILL_DIR / SKILL_FILE


@dataclass
class HostState:
    host: str
    status: str          # current | stale | modified | absent
    path: str
    version: str = ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def host_state(host: str) -> HostState:
    """What is installed for one host, and whether it matches this build."""
    path = target(host)
    installed = _read(path)
    if not installed:
        return HostState(host=host, status="absent", path=str(path))
    want = _read(packaged())
    side = {}
    try:
        side = json.loads(_read(path.parent / SIDECAR) or "{}")
    except ValueError:
        side = {}
    recorded = str(side.get("sha256") or "")
    version = str(side.get("rc_repro_version") or "")
    if installed == want:
        return HostState(host=host, status="current", path=str(path), version=version)
    # The sidecar separates the two ways a file can differ, and they need opposite
    # answers: if it still matches what WE wrote, the package moved on and this is
    # stale (reinstall it). If it does not, a human edited it, and that is theirs.
    if recorded and recorded != sha256(installed):
        return HostState(host=host, status="modified", path=str(path), version=version)
    return HostState(host=host, status="stale", path=str(path), version=version)


def project_copy(start: Path | None = None) -> Path | None:
    """The CHECKOUT's own copy, if the caller is working inside one.

    An agent reads `.claude/skills/` relative to the project before it reads the
    one in a home directory, so in a checkout that is the file actually in play --
    and reporting only the home copy meant `capabilities` said `current: false` to
    a developer who was, at that moment, reading a perfectly current skill. It then
    told them to run `skill install`, which writes a DIFFERENT copy somewhere the
    agent was not reading. Found by driving the skill and watching it say that.

    Walked upward from the working directory, the same way the tooling finds it.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".claude" / HOSTS["claude"].split("/", 1)[1] / SKILL_DIR / SKILL_FILE
        if candidate.exists():
            return candidate
    return None


def state() -> dict:
    """Every copy that exists, for `capabilities`. Never raises: discovery must not
    fail on an odd home or an unreadable directory.

    `current` is true when every copy that EXISTS matches this build, and at least
    one does. Absent is not current -- there is nothing to have read -- and a stale
    copy anywhere is worth reporting even if another one is fine, because nobody
    knows which of them the caller loaded.
    """
    out: dict = {"version": __version__, "hosts": {}}
    for host in sorted(HOSTS):
        try:
            st = host_state(host)
            out["hosts"][host] = {"status": st.status, "path": st.path,
                                  "version": st.version, "scope": "user"}
        except Exception:  # noqa: BLE001
            out["hosts"][host] = {"status": "unknown", "path": "", "version": "",
                                  "scope": "user"}
    try:
        local = project_copy()
    except Exception:  # noqa: BLE001
        local = None
    if local is not None:
        same = _read(local) == _read(packaged())
        out["hosts"]["project"] = {"status": "current" if same else "stale",
                                   "path": str(local), "version": __version__ if same else "",
                                   "scope": "project"}
    present = [h for h in out["hosts"].values() if h["status"] != "absent"]
    out["current"] = bool(present) and all(h["status"] == "current" for h in present)
    return out


def install(host: str = "all", *, force: bool = False) -> dict:
    """Copy the packaged skill into each host's directory. Idempotent.

    Refuses a locally-modified file without `force`, because overwriting somebody's
    edit to install the version they had already chosen to change is not a service.
    """
    hosts = sorted(HOSTS) if host == "all" else [host]
    want = _read(packaged())
    if not want:
        raise NotFoundError(
            f"the packaged skill is missing from this install ({packaged()}) — "
            "reinstall rc-repro, or run from a checkout")
    results = []
    for name in hosts:
        st = host_state(name)
        path = target(name)
        if st.status == "modified" and not force:
            raise ConflictError(
                f"{path} was edited locally; installing would discard that. Pass "
                "--force to overwrite it, or move it aside first.")
        if st.status == "current":
            results.append({"host": name, "path": str(path), "action": "unchanged"})
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(want, encoding="utf-8")
        (path.parent / SIDECAR).write_text(json.dumps({
            "rc_repro_version": __version__,
            "sha256": sha256(want),
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=1) + "\n", encoding="utf-8")
        results.append({"host": name, "path": str(path),
                        "action": "installed" if st.status == "absent" else "updated"})
    return {"version": __version__, "hosts": results}
