"""On-disk repro state and docker-compose invocations.

Each repro is a workspace dir under ~/.rc-repro/repros/<name>/ holding the
generated docker-compose.yml and a repro.json metadata file.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rc_repro import config


@dataclass
class Metadata:
    name: str
    project: str
    rc_version: str
    rc_image: str
    mongo_tag: str
    mongo_flavor: str
    preset: str
    root_url: str
    host_port: int
    version_source: str
    pinned: bool = False
    created_at: str = ""
    extra: dict = field(default_factory=dict)


def project_name(name: str) -> str:
    return config.PROJECT_PREFIX + name


def workspace(name: str) -> Path:
    return config.repros_dir() / name


def exists(name: str) -> bool:
    return (workspace(name) / "docker-compose.yml").exists()


def write(name: str, compose_yaml: str, meta: Metadata,
          files: list[tuple[str, str]] | None = None) -> None:
    ws = workspace(name)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "docker-compose.yml").write_text(compose_yaml, encoding="utf-8")
    (ws / "repro.json").write_text(json.dumps(asdict(meta), indent=2), encoding="utf-8")
    # Preset-generated files (e.g. a seeded LDIF that a service mounts).
    for relpath, content in files or []:
        fp = ws / relpath
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")


def read_meta(name: str) -> Metadata:
    blob = json.loads((workspace(name) / "repro.json").read_text(encoding="utf-8"))
    return Metadata(**blob)


def list_meta() -> list[Metadata]:
    root = config.repros_dir()
    if not root.exists():
        return []
    out: list[Metadata] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            out.append(read_meta(d.name))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            continue  # skip half-written / foreign dirs
    return out


def used_ports() -> set[int]:
    return {m.host_port for m in list_meta()}


def port_free(port: int) -> bool:
    """True if `port` can be bound on the host right now (nothing listening)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # On Unix, SO_REUSEADDR lets us probe a port that's only in TIME_WAIT.
        # On Windows it would let bind() succeed even for an active listener
        # (a false "free"), so skip it there.
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def pick_port(start: int = 3000) -> int:
    """Lowest port >= start not claimed by another repro AND free on the host."""
    used = used_ports()
    port = start
    while port in used or not port_free(port):
        port += 1
    return port


def remove(name: str) -> None:
    shutil.rmtree(workspace(name), ignore_errors=True)


# --- docker compose -----------------------------------------------------------


def _compose(name: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    """Run `docker compose <args>` in a repro workspace."""
    cmd = ["docker", "compose", *args]
    return subprocess.run(
        cmd,
        cwd=workspace(name),
        text=True,
        capture_output=capture,
    )


def up(name: str, *, pull: bool = True) -> int:
    if pull:
        _compose(name, "pull")
    return _compose(name, "up", "-d").returncode


def down(name: str, *, volumes: bool = False) -> int:
    args = ["down"]
    if volumes:
        args.append("-v")
    return _compose(name, *args).returncode


def start(name: str) -> int:
    return _compose(name, "start").returncode


def stop(name: str) -> int:
    return _compose(name, "stop").returncode


def restart(name: str) -> int:
    return _compose(name, "restart").returncode


def logs(name: str, *, follow: bool = False, tail: int | None = None) -> int:
    args = ["logs"]
    if follow:
        args.append("-f")
    if tail is not None:
        args += ["--tail", str(tail)]
    return _compose(name, *args).returncode


def compose_exec(name: str, service: str, args: list[str]) -> int:
    """Run a command inside a running compose service (docker compose exec -T)."""
    return _compose(name, "exec", "-T", service, *args).returncode


def ps(name: str) -> str:
    """Return `docker compose ps` for a repro (service/state/status lines)."""
    proc = _compose(
        name, "ps", "--format", "{{.Service}}\t{{.State}}\t{{.Status}}", capture=True
    )
    return proc.stdout or ""


def rc_state(name: str) -> str:
    """Coarse state of the rocketchat service: running | exited | created | absent.

    One `docker compose ps` per call — fine for a single repro. For listing many
    repros use project_states() instead (a single docker call for all of them).
    """
    for line in ps(name).splitlines():
        parts = line.split("\t")
        if parts and parts[0] == "rocketchat":
            return parts[1] if len(parts) > 1 else "unknown"
    return "absent"


def project_states() -> dict[str, str]:
    """Map compose project name -> status string for ALL projects, in one call.

    Uses `docker compose ls` so listing N repros costs one subprocess, not N.
    Status looks like "running(3)" / "exited(2)"; a fully `down`ed repro (no
    containers) is absent from the output.
    """
    proc = subprocess.run(
        ["docker", "compose", "ls", "--all", "--format", "json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {}
    raw = (proc.stdout or "").strip()
    if not raw:
        return {}
    # Newer compose emits a JSON array; older versions emit NDJSON (one object
    # per line). Handle both so `list` works across compose versions.
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
    except json.JSONDecodeError:
        data = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return {item.get("Name", ""): item.get("Status", "") for item in data}


def docker_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _first_line(cmd: list[str]) -> str | None:
    """Run a command and return its trimmed stdout, or None on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def docker_server_version() -> str | None:
    return _first_line(["docker", "version", "--format", "{{.Server.Version}}"])


def compose_version() -> str | None:
    return _first_line(["docker", "compose", "version", "--short"])
