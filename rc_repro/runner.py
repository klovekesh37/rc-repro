"""On-disk repro state and docker-compose invocations.

Each repro is a workspace dir under ~/.rc-repro/repros/<name>/ holding the
generated docker-compose.yml and a repro.json metadata file.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rc_repro import config


def _atomic_write(path: Path, content: str) -> None:
    """Write via a temp file in the same dir + os.replace, so readers never see a
    partially written file (rename is atomic on the same filesystem)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


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
    # Write atomically (temp + rename): an interruption mid-write must not leave a
    # half-written repro.json that read_meta would choke on, nor a compose file
    # out of sync with its metadata.
    _atomic_write(ws / "docker-compose.yml", compose_yaml)
    _atomic_write(ws / "repro.json", json.dumps(asdict(meta), indent=2))
    # Preset-generated files (e.g. a seeded LDIF that a service mounts).
    # `{{ROOT_URL}}` is substituted with the repro's URL — presets are built
    # before the host port is known, so a generated file that must reference the
    # workspace URL (e.g. the livechat widget embed snippet) uses the placeholder.
    for relpath, content in files or []:
        fp = ws / relpath
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content.replace("{{ROOT_URL}}", meta.root_url), encoding="utf-8")


def read_meta(name: str) -> Metadata:
    blob = json.loads((workspace(name) / "repro.json").read_text(encoding="utf-8"))
    return Metadata(**blob)


def read_compose(name: str) -> dict:
    """Load a repro's generated docker-compose.yml as a dict (for in-place edits
    like attaching/detaching the monitoring stack)."""
    import yaml
    return yaml.safe_load((workspace(name) / "docker-compose.yml").read_text(encoding="utf-8")) or {}


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
    ports: set[int] = set()
    for m in list_meta():
        ports.add(m.host_port)
        # A multi-instance repro also occupies host_port+1..+N (direct instance
        # access), so those are claimed too.
        n = m.extra.get("instances") if isinstance(m.extra, dict) else None
        if isinstance(n, int) and n > 1:
            ports.update(m.host_port + i for i in range(1, n + 1))
        # Preset side services (Keycloak/Mailpit/MinIO…) and the monitoring
        # add-on (Prometheus/Grafana) publish fixed host ports recorded at `up` —
        # claimed too, so RC port allocation avoids them.
        for key in ("sidecar_ports", "monitoring_ports"):
            claimed = m.extra.get(key) if isinstance(m.extra, dict) else None
            if isinstance(claimed, list):
                ports.update(int(p) for p in claimed if isinstance(p, int) or str(p).isdigit())
    return ports


PORT_MAX = 65535


def port_free(port: int) -> bool:
    """True if `port` is free to publish on the host right now."""
    # First: is something already LISTENING on loopback? A wildcard bind with
    # SO_REUSEADDR (below) can miss a docker publish on 127.0.0.1:<port> (repros
    # bind loopback), so probe it directly — connect success == in use.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return False
        except OSError:
            return False
    # Then: can we actually bind it? (catches reserved-but-not-listening ports.)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # On Unix, SO_REUSEADDR lets us probe a port that's only in TIME_WAIT.
        # On Windows it would let bind() succeed even for an active listener
        # (a false "free"), so skip it there.
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except (OSError, OverflowError):
            return False


def pick_port(start: int = 3000) -> int:
    """Lowest port >= start not claimed by another repro AND free on the host.

    Bounded: raises RuntimeError instead of scanning past 65535 (which happens
    when the host can't bind anything, e.g. sandboxed environments)."""
    used = used_ports()
    port = start
    while port in used or not port_free(port):
        port += 1
        if port > PORT_MAX:
            raise RuntimeError(
                f"no free host port found (scanned {start}-{PORT_MAX}) — "
                "can this environment bind TCP ports at all?"
            )
    return port


def pick_port_range(count: int, start: int = 3000) -> int:
    """Lowest base port with `count` consecutive ports all unclaimed AND free.

    Used by multi-instance repros, which need one port for the load balancer plus
    one per instance (the block [base, base+count)). Bounded like pick_port."""
    used = used_ports()
    base = start
    while not all((base + i) not in used and port_free(base + i) for i in range(count)):
        base += 1
        if base + count - 1 > PORT_MAX:
            raise RuntimeError(
                f"no free block of {count} consecutive host ports found "
                f"(scanned from {start}) — can this environment bind TCP ports at all?"
            )
    return base


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
        # A failed pull is deliberately non-fatal: cached images may satisfy
        # `up -d` anyway (e.g. registry hiccup), and `up` itself fails loudly
        # if an image is truly missing.
        _compose(name, "pull")
    # --remove-orphans: if the compose file changed shape (e.g. a different
    # preset after --force), containers of dropped services are cleaned up.
    return _compose(name, "up", "-d", "--remove-orphans").returncode


def down(name: str, *, volumes: bool = False) -> int:
    args = ["down", "--remove-orphans"]
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


def compose_exec_capture(name: str, service: str, args: list[str]) -> tuple[int, str]:
    """Like compose_exec, but captures stdout: (returncode, stdout)."""
    r = _compose(name, "exec", "-T", service, *args, capture=True)
    return r.returncode, r.stdout or ""


def rm_services(name: str, services: list[str]) -> int:
    """Stop and remove specific services (docker compose rm -s -f <services>)."""
    return _compose(name, "rm", "-s", "-f", *services).returncode


def service_container_ids(name: str, service: str) -> list[str]:
    """Container id(s) of one compose service in this repro (usually a single id)."""
    r = _compose(name, "ps", "-q", service, capture=True)
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def docker_capacity() -> tuple[float, int] | None:
    """(cpus, memory_bytes) available to the docker engine/VM, or None."""
    r = subprocess.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        ncpu, mem = r.stdout.split()
        return float(ncpu), int(mem)
    except ValueError:
        return None


def container_ids(name: str) -> list[str]:
    """Container ids of a repro's running services (docker compose ps -q)."""
    proc = _compose(name, "ps", "-q", capture=True)
    return [line for line in (proc.stdout or "").split() if line]


def docker_stats(container_ids: list[str]) -> str:
    """One `docker stats --no-stream` sample for the given containers, as
    tab-separated `name<TAB>cpu%<TAB>mem-usage` lines ('' on error/none)."""
    if not container_ids:
        return ""
    proc = subprocess.run(
        ["docker", "stats", "--no-stream", "--format",
         "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", *container_ids],
        capture_output=True, text=True,
    )
    return proc.stdout if proc.returncode == 0 else ""


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
        # "rocketchat" for a normal repro, "rocketchat-1"/"-2"/… for a
        # multi-instance one — the first instance found is enough for readiness.
        if parts and (parts[0] == "rocketchat" or parts[0].startswith("rocketchat-")):
            return parts[1] if len(parts) > 1 else "unknown"
    return "absent"


def project_states() -> dict[str, str] | None:
    """Map compose project name -> status string for ALL projects, in one call.

    Uses `docker compose ls` so listing N repros costs one subprocess, not N.
    Status looks like "running(3)" / "exited(2)"; a fully `down`ed repro (no
    containers) is absent from the output. Returns None if the query itself
    failed — callers that DELETE based on absence (prune) must not confuse
    "no projects" with "couldn't ask docker".
    """
    proc = subprocess.run(
        ["docker", "compose", "ls", "--all", "--format", "json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
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
