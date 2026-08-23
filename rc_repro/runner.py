"""On-disk repro state and docker-compose invocations.

Each repro is a workspace dir under ~/.rc-repro/repros/<name>/ holding the
generated docker-compose.yml and a repro.json metadata file.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from rc_repro import config
# ConflictError, not the builtin TimeoutError, for a lock that could not be taken:
# it is the FAILURE contract both front ends understand (errors.py) -- the CLI
# prints it and exits 1, the API returns 409. A builtin escaped both, so waiting
# out the timeout ended in a raw traceback or an opaque "internal error".
from rc_repro.errors import ConflictError


def atomic_write(path: Path, content: str, *, mode: int | None = None) -> None:
    """Write via a temp file in the same dir + os.replace, so readers never see a
    partially written file (rename is atomic on the same filesystem).

    The temp name is unique per call: a fixed `<name>.tmp` meant two concurrent
    writers (two web jobs touching the same repro) shared one temp path and
    clobbered each other, defeating the atomicity this exists to provide.

    `mode` is applied to the TEMP FILE, before the rename -- so the target never exists
    at the umask's permissions even for an instant. Callers writing a private key pass
    0o600: `edge.issue_local_cert` wrote `certs/<host>.key` through here and it landed
    at whatever the umask allowed, typically 0644, on a box where `~/.rc-repro` is only
    tightened to 0700 by `serve` or a config save -- neither of which runs on a
    CLI-only `up --https`.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)   # no-op after a successful replace


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
    # External https URL when `up --https` was used. Kept SEPARATE from root_url,
    # which stays the plain http://localhost:<port> that rc-repro's own API calls
    # (login, PAT, seeding, load tests) use. Pointing those at a locally-signed
    # https URL would fail certificate verification in 71 call sites; RC still
    # advertises this one as its ROOT_URL, which is what the browser needs.
    public_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def external_url(self) -> str:
        """What a human/browser should open — the https URL if there is one."""
        return self.public_url or self.root_url


def project_name(name: str) -> str:
    return config.PROJECT_PREFIX + name


def workspace(name: str) -> Path:
    return config.repros_dir() / name


def exists(name: str) -> bool:
    """Whether a workspace is here, whatever runtime it runs on.

    This used to be `(workspace/docker-compose.yml).exists()` -- a repro WAS a
    compose file, which was true of every workspace this tool had ever made. A
    Kubernetes workspace has no compose file, so it would have been invisible to
    `list`, `info`, `down` and the name-collision check, and `up` would have
    silently created a second one over the top of it.

    Either marker counts, which is strictly more permissive than before: no
    workspace that used to be found can stop being found. repro.json is written
    after the compose file and both go through `atomic_write`, so a half-written
    workspace cannot look complete.
    """
    ws = workspace(name)
    return (ws / "docker-compose.yml").exists() or (ws / "repro.json").exists()


def _restrict(root: Path) -> None:
    """Keep a workspace's contents away from other local users. Best-effort.

    The DIRECTORY is what does the work: 0700 means no other user can traverse
    into the workspace, so nothing inside it is reachable whatever its own mode.
    Files are left group/other readable, and that is not a compromise -- it is
    required. Half of what lives here is bind-mounted INTO a container, and those
    run as non-root (Prometheus as 65534, Grafana, Loki, the OTel collector), so
    0600 made them unreadable and Prometheus crash-looped on
    "open /etc/prometheus/prometheus.yml: permission denied".

    Only the ROOT is 0700. Nested directories are 0755, because some volumes
    mount a DIRECTORY rather than a file and the container then has to list it --
    Prometheus globs `/etc/prometheus/file_sd_configs/*.yml`, and with that
    directory at 0700 it started cleanly and scraped nothing at all, which is
    worse than crashing.

    Both rules verified rather than reasoned about:

      mounted FILE       0644 inside a 0700 dir  -> readable  (the host path's
                         0600 inside a 0700 dir  -> refused    directories never
                                                                apply)
      mounted DIRECTORY  0700                    -> cannot list
                         0755 under a 0700 root  -> lists and reads

    The 0700 root is what keeps other local users out, and it keeps them out of
    every subdirectory too, whatever those are set to -- so the privacy goal and
    the containers do not actually conflict.

    Never fatal: a workspace on a filesystem that cannot represent these modes
    (some network shares) must still be usable -- refusing to create a repro over
    a permission bit would trade a small exposure for a broken tool.
    """
    try:
        root.chmod(0o700)
        for path in root.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
    except OSError:
        pass


def write(name: str, compose_yaml: str, meta: Metadata,
          files: list[tuple[str, str]] | None = None) -> None:
    ws = workspace(name)
    ws.mkdir(parents=True, exist_ok=True)
    # The generated compose file carries ADMIN_PASS and, when set, REG_TOKEN, and
    # preset files carry LDAP and MinIO credentials -- all of it written at the
    # process umask, which on a normal box leaves them readable by every local
    # user. That is tolerable on a laptop and not on the shared server this branch
    # is for, where "your own workspace" is the whole point. Applied on every
    # write, so workspaces created before this are tightened when next touched.
    _restrict(ws)
    # Write atomically (temp + rename): an interruption mid-write must not leave a
    # half-written repro.json that read_meta would choke on, nor a compose file
    # out of sync with its metadata.
    atomic_write(ws / "docker-compose.yml", compose_yaml)
    atomic_write(ws / "repro.json", json.dumps(asdict(meta), indent=2))
    # Preset-generated files (e.g. a seeded LDIF that a service mounts).
    # `{{ROOT_URL}}` is substituted with the repro's URL — presets are built
    # before the host port is known, so a generated file that must reference the
    # workspace URL (e.g. the livechat widget embed snippet) uses the placeholder.
    for relpath, content in files or []:
        fp = ws / relpath
        fp.parent.mkdir(parents=True, exist_ok=True)
        # The ADVERTISED url, falling back to the loopback one. A generated file
        # carrying this placeholder is read by a BROWSER, and `meta.root_url` is the
        # address rc-repro uses for its own API calls -- always loopback, by contract.
        # Substituting that shipped a livechat demo page that only worked on the
        # machine docker runs on.
        advertised = (meta.extra.get("advertised_url")
                      if isinstance(meta.extra, dict) else "") or meta.root_url
        fp.write_text(content.replace("{{ROOT_URL}}", advertised), encoding="utf-8")
    _restrict(ws)          # again: covers the files just written, and their dirs


_META_FIELDS = frozenset(f.name for f in fields(Metadata))


def read_meta(name: str) -> Metadata:
    """Load a repro's metadata.

    Unknown keys are dropped rather than raising: a repro.json written by a NEWER
    rc-repro carrying an added field would otherwise TypeError, and `list_meta`
    swallows that — silently making the repro vanish from `rc-repro list`.
    """
    blob = json.loads((workspace(name) / "repro.json").read_text(encoding="utf-8"))
    if not isinstance(blob, dict):
        raise TypeError("repro.json is not a JSON object")
    return Metadata(**{k: v for k, v in blob.items() if k in _META_FIELDS})


def update_meta(name: str, mutate) -> "Metadata":
    """Read-modify-write repro.json under the repro lock.

    Every other mutator holds this lock while it rewrites the workspace, so a
    metadata edit that skipped it could be lost by a concurrent `up --force`
    rewriting the same file from the spec. `mutate(meta)` edits in place.
    """
    with repro_lock(name, timeout=60.0):
        meta = read_meta(name)
        mutate(meta)
        atomic_write(workspace(name) / "repro.json", json.dumps(asdict(meta), indent=2))
        return meta


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
        for key in ("sidecar_ports", "monitoring_ports", "tls_ports"):
            claimed = m.extra.get(key) if isinstance(m.extra, dict) else None
            if isinstance(claimed, list):
                ports.update(int(p) for p in claimed if isinstance(p, int) or str(p).isdigit())
    return ports


PORT_MAX = 65535


def port_free(port: int, host: str = "") -> bool:
    """True if `port` is free to publish on the host right now.

    `host` narrows the question to one interface. It matters because the wildcard
    bind below reports busy for EVERY address as soon as anything holds the port
    on 0.0.0.0 — correct for "can I publish this everywhere", wrong for "is
    203.0.113.10:443 free", which is the dedicated-IP escape hatch: two processes
    CAN hold the same port on different IPs (verified on 172.16.0.2 and .3 at
    once). Without this the front door holding 443 on 0.0.0.0 made every other
    address look taken.
    """
    probe_host = host or "127.0.0.1"
    # First: is something already LISTENING there? A wildcard bind with
    # SO_REUSEADDR (below) can miss a docker publish on 127.0.0.1:<port> (repros
    # bind loopback), so probe it directly — connect success == in use.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            if probe.connect_ex((probe_host, port)) == 0:
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
            s.bind((host or "0.0.0.0", port))
            return True
        except PermissionError:
            # A privileged port (<1024) cannot be bound by an unprivileged user,
            # but the DOCKER DAEMON can — it runs as root, so `ports: 443:443`
            # publishes fine. Treating EACCES as "in use" made `up --https
            # --domain ...` refuse 443 on every non-root machine, with nothing
            # actually listening on it. Nobody is holding it; we just can't probe
            # this way. The connect() check above already caught a real listener.
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


def _compose(name: str, *args: str, capture: bool = False,
             timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run `docker compose <args>` in a repro workspace.

    `timeout` is opt-in and left unset by default: `up`/`pull`/`logs -f` are
    legitimately long-running. Bounded callers (samplers, one-shot queries) pass
    one so a wedged daemon can't block a worker thread forever.
    """
    cmd = ["docker", "compose", *args]
    return subprocess.run(
        cmd,
        cwd=workspace(name),
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def compose_stream(name: str, *args: str, on_line=None) -> int:
    """Run `docker compose <args>` streaming combined stdout+stderr line-by-line
    to `on_line` (for live progress in the web UI). Returns the exit code.

    Uses Popen (not subprocess.run) so callers get output as it happens; the CLI
    keeps the blocking `_compose` path, which inherits the terminal for docker's
    own progress rendering."""
    cmd = ["docker", "compose", *args]
    proc = subprocess.Popen(
        cmd, cwd=workspace(name), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
    )
    try:
        for line in proc.stdout or []:
            line = line.rstrip("\n")
            if on_line and line:
                on_line(line)
    finally:
        if proc.stdout:
            proc.stdout.close()
    return proc.wait()


def up(name: str, *, pull: bool = True) -> int:
    if pull:
        # A failed pull is deliberately non-fatal: cached images may satisfy
        # `up -d` anyway (e.g. registry hiccup), and `up` itself fails loudly
        # if an image is truly missing.
        _compose(name, "pull")
    # --remove-orphans: if the compose file changed shape (e.g. a different
    # preset after --force), containers of dropped services are cleaned up.
    return _mutating(name, "up", "-d", "--remove-orphans")


def down(name: str, *, volumes: bool = False) -> int:
    args = ["down", "--remove-orphans"]
    if volumes:
        args.append("-v")
    return _mutating(name, *args)


def start(name: str) -> int:
    return _mutating(name, "start")


def stop(name: str) -> int:
    return _mutating(name, "stop")


def restart(name: str) -> int:
    return _mutating(name, "restart")


def _mutating(name: str, *args: str) -> int:
    """A compose command that CHANGES container state.

    Drops the memoised query results afterwards: a dashboard that still showed
    "running" for two seconds after a Stop would be a cache the user can notice,
    which is worse than the process spawns the memo saves.
    """
    try:
        return _compose(name, *args).returncode
    finally:
        invalidate_docker_queries()


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


def compose_exec_capture(name: str, service: str, args: list[str],
                         timeout: float | None = None) -> tuple[int, str]:
    """Like compose_exec, but captures stdout: (returncode, stdout).

    Returns (1, "") instead of raising when the exec can't run or `timeout`
    expires, so best-effort samplers degrade to "no sample" rather than
    stranding their thread."""
    try:
        r = _compose(name, "exec", "-T", service, *args, capture=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return r.returncode, r.stdout or ""


def compose_exec_to_file(name: str, service: str, args: list[str], dest: "Path",
                         timeout: float | None = None) -> tuple[int, str]:
    """Run a command in a service, writing its RAW stdout to `dest`.

    Binary-safe on purpose: `mongodump --archive` emits BSON, and the text-mode
    decode/newline translation that compose_exec_capture applies would corrupt it
    silently -- the dump would restore with errors nobody could trace back here.
    Returns (returncode, stderr-as-text).
    """
    with open(dest, "wb") as fh:
        try:
            proc = subprocess.run(
                ["docker", "compose", "exec", "-T", service, *args],
                cwd=workspace(name), stdout=fh, stderr=subprocess.PIPE,
                timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
    return proc.returncode, (proc.stderr or b"").decode("utf-8", "replace")


def compose_exec_from_file(name: str, service: str, args: list[str], src: "Path",
                           timeout: float | None = None) -> tuple[int, str]:
    """Run a command in a service with `src` piped to its stdin.

    The counterpart of compose_exec_to_file, for `mongorestore --archive`. Output
    is merged so a caller has one blob to show when a restore fails.
    """
    with open(src, "rb") as fh:
        try:
            proc = subprocess.run(
                ["docker", "compose", "exec", "-T", service, *args],
                cwd=workspace(name), stdin=fh, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
    return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace")


def stop_services(name: str, services: list[str]) -> int:
    """Stop specific services, leaving the rest of the project running.

    `stop()` stops everything including Mongo -- useless for a dump, which needs
    Mongo up and only the writers quiesced.
    """
    return _compose(name, "stop", *services).returncode


def start_services(name: str, services: list[str]) -> int:
    return _compose(name, "start", *services).returncode


def rc_services(name: str) -> list[str]:
    """The repro's Rocket.Chat service names, read from its compose file.

    A multi-instance repro has rocketchat-1..N rather than one `rocketchat`, and
    quiescing only the first would leave the others writing during a dump.
    """
    try:
        doc = read_compose(name)
    except (OSError, ValueError):
        return []
    return sorted(s for s in (doc.get("services") or {})
                  if s == "rocketchat" or s.startswith("rocketchat-"))


#: Per-repro reentrant thread locks. `serve` runs every job on its own thread, so
#: thread-level exclusion is needed as well as the cross-process file lock -- flock
#: is held per file DESCRIPTION, and two threads opening the lock file separately
#: would each be granted it.
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
#: name -> reentrancy depth, per thread.
_HELD = threading.local()


def _held_names() -> dict[str, int]:
    names = getattr(_HELD, "names", None)
    if names is None:
        names = _HELD.names = {}
    return names


def _thread_lock_for(name: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(name)
        if lock is None:
            lock = _THREAD_LOCKS[name] = threading.RLock()
        return lock


@contextlib.contextmanager
def repro_lock(name: str, *, timeout: float = 900.0, poll: float = 0.2):
    """Exclusive lock for mutating one repro, across threads AND processes.

    Every mutating operation does read-compose -> write-compose -> `docker compose
    up`. Two interleaving on one repro corrupts it: compose races itself and leaves
    an orphaned container behind, which is how a repro ends up with Rocket.Chat gone
    and a stray `<hash>_rcrepro-...` container in its place.

    Two layers, because one is not enough:
      * threading.RLock  -- `serve` runs jobs on separate threads, and flock is per
        open file description, so two threads that each open the file would both be
        granted it. RLock is also what makes the lock REENTRANT, which
        `restore --new` needs: it holds the lock and then calls create_repro, which
        takes it again on the same thread.
      * flock            -- the CLI and `serve` are different processes acting on
        the same repros, and a thread lock says nothing about that.

    Degrades to thread-only where flock is unavailable (Windows): a hard dependency
    would be worse than the race it prevents.
    """
    tlock = _thread_lock_for(name)
    if not tlock.acquire(timeout=timeout):
        raise ConflictError(
            f"another rc-repro operation is still working on {name!r} "
            f"(waited {int(timeout)}s). Check `rc-repro list` and the Activity "
            "list, then retry.")
    # Reentrancy depth is tracked per THREAD, in step with the RLock's own
    # semantics. It cannot live on the lock object: threading.RLock is a C type
    # with no __dict__, so assigning an attribute to it raises.
    held = _held_names()
    reentrant = held.get(name, 0)
    held[name] = reentrant + 1
    try:
        # The outermost frame on this thread already owns the file lock, and
        # re-flocking the same path from a second descriptor would deadlock
        # against ourselves -- which is what `restore --new` would do when it
        # calls create_repro from inside its own lock.
        if reentrant:
            yield
            return
        try:
            import fcntl
        except ImportError:                              # pragma: no cover - Windows
            yield
            return
        lock_dir = config.home() / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        path = lock_dir / f"{name}.lock"
        deadline = time.monotonic() + timeout
        with open(path, "w", encoding="utf-8") as fh:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ConflictError(
                            f"another rc-repro process is still working on {name!r} "
                            f"(waited {int(timeout)}s). Check `rc-repro list` and "
                            "the Activity list, then retry.") from None
                    time.sleep(poll)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        if held.get(name, 0) <= 1:
            held.pop(name, None)
        else:
            held[name] -= 1
        tlock.release()


def rm_services(name: str, services: list[str]) -> int:
    """Stop and remove specific services (docker compose rm -s -f <services>)."""
    return _compose(name, "rm", "-s", "-f", *services).returncode


def remove_volumes(name: str, volumes: list[str]) -> list[str]:
    """Delete named volumes belonging to a repro's compose project.

    `docker compose down -v` only removes volumes DECLARED in the compose file, so
    a volume whose declaration is dropped (detaching monitoring, switching preset
    with --force) becomes unreachable and survives forever. Containers must be
    gone first. Returns the volume names that could not be removed.
    """
    proj = project_name(name)
    failed: list[str] = []
    for vol in volumes:
        full = f"{proj}_{vol}"
        try:
            r = subprocess.run(["docker", "volume", "rm", full],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            failed.append(full)
            continue
        # Already gone is success, not a failure.
        if r.returncode != 0 and "no such volume" not in (r.stderr or "").lower():
            failed.append(full)
    return failed


def service_container_ids(name: str, service: str) -> list[str]:
    """Container id(s) of one compose service in this repro (usually a single id)."""
    r = _compose(name, "ps", "-q", service, capture=True)
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def host_memory() -> tuple[int, int, int] | None:
    """(total_mb, available_mb, swap_total_mb) for the machine, or None.

    MemAvailable, not MemFree: free memory excludes the page cache, which the
    kernel reclaims on demand, so `free` looks alarming on a healthy box and
    reassuring on a doomed one. MemAvailable is the kernel's own estimate of what
    a new workload can actually get, which is exactly the question here.

    Linux only. Everywhere else this returns None and the caller skips the check
    rather than guessing -- a wrong refusal is worse than no refusal.
    """
    try:
        fields = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                fields[key] = int(rest.strip().split()[0])   # kB
    except (OSError, ValueError, IndexError):
        return None
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        return None
    return (fields["MemTotal"] // 1024, fields["MemAvailable"] // 1024,
            fields.get("SwapTotal", 0) // 1024)


def docker_capacity() -> tuple[float, int] | None:
    """(cpus, memory_bytes) available to the docker engine/VM, or None.

    Never raises: callers use this during best-effort restore paths, where an
    OSError (docker gone, fork failure) must not escape."""
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
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
    tab-separated `name<TAB>cpu%<TAB>mem-usage` lines ('' on error/none).

    Timed out: this is polled on a sampler thread once a second, so a wedged
    daemon would otherwise block that thread (and its child) forever — the
    thread is a daemon and its `join` gives up, so it would leak silently."""
    if not container_ids:
        return ""
    try:
        proc = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", *container_ids],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
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


def _compose_ls() -> list[dict] | None:
    """Parsed `docker compose ls --all --format json`, or None if the query failed.

    Newer compose emits a JSON array; older versions emit NDJSON (one object per
    line). Both are handled so callers work across compose versions.

    Bounded: the GUI polls this every 4s on a threadpool worker. A daemon that
    answers `docker info` (so docker_available() says yes) but then wedges on
    `compose ls` would otherwise park a worker per poll until the server stops
    answering at all. Failing to None is already the "couldn't ask docker" path.
    """
    try:
        proc = subprocess.run(
            ["docker", "compose", "ls", "--all", "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [data] if isinstance(data, dict) else data
    except json.JSONDecodeError:
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


def project_states() -> dict[str, str] | None:
    """Map compose project name -> status string for ALL projects, in one call.

    Status looks like "running(3)" / "exited(2)"; a fully `down`ed repro (no
    containers) is absent from the output. Returns None if the query itself
    failed — callers that DELETE based on absence (prune) must not confuse
    "no projects" with "couldn't ask docker".
    """
    def query():
        data = _compose_ls()
        if data is None:
            return None
        return {item.get("Name", ""): item.get("Status", "") for item in data}
    return _cached_query("project_states", query)


def project_config_files() -> dict[str, str] | None:
    """Map compose project name -> its ConfigFiles string (comma-joined paths).

    Used to detect a project-name COLLISION: two repro workspaces (e.g. a real
    one and a throwaway in a different RC_REPRO_HOME) derive the same project
    name `rcrepro-<name>`, and `docker compose up` would then reconcile the wrong
    workspace's containers. None if docker couldn't be queried.
    """
    data = _compose_ls()
    if data is None:
        return None
    return {item.get("Name", ""): (item.get("ConfigFiles") or "") for item in data}


def rc_restart_count(name: str) -> int:
    """How many times the repro's rocketchat container has restarted — a nonzero
    and climbing value signals a crash-loop (usually resource pressure). 0 if
    unknown."""
    ids = service_container_ids(name, "rocketchat") or service_container_ids(name, "rocketchat-1")
    if not ids:
        return 0
    # Bounded for the same reason as _compose_ls: the detail panel refreshes this
    # on a timer, and "unknown" (0) is already the documented fallback.
    try:
        r = subprocess.run(["docker", "inspect", "--format", "{{.RestartCount}}", ids[0]],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return 0
    try:
        return int((r.stdout or "0").strip())
    except ValueError:
        return 0


def rc_status_by_project() -> dict[str, str]:
    """Map compose project -> its rocketchat container `Status` string
    ("Up 2 hours (healthy)"), in ONE `docker ps` call (cheap enough for the whole
    dashboard). Used to show uptime/health per repro without an N-call fan-out."""
    return {proj: svcs.get("rocketchat") or svcs.get("rocketchat-1") or ""
            for proj, svcs in services_by_project().items()}


def services_by_project() -> dict[str, dict[str, str]]:
    """Map compose project -> {service: `Status` string} for EVERY container.

    The same single `docker ps` this always ran; it just stopped discarding the rest
    of the output. `rc_status_by_project` used to keep the rocketchat line and throw
    the others away, which is why a repro whose MONGODB had exited reported `running`
    and `healthy` -- both derived from the Rocket.Chat container alone, and Rocket.Chat
    serves /api/info perfectly well with no database behind it. The truth was one field
    away in a call already being made.
    """
    return _cached_query("services_by_project", _services_by_project_uncached)


def _services_by_project_uncached() -> dict[str, dict[str, str]]:
    try:
        proc = subprocess.run(
            ["docker", "ps", "--all", "--format",
             '{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}\t{{.Status}}'],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}          # same 4s-poll bound as _compose_ls; {} = "couldn't ask"
    if proc.returncode != 0:
        return {}
    out: dict[str, dict[str, str]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0]:
            out.setdefault(parts[0], {})[parts[1]] = parts[2]
    return out


def container_details(name: str) -> list[dict]:
    """Per-container [{service, state, status, health}] for a repro (incl. stopped)."""
    r = _compose(name, "ps", "--all", "--format", "json", capture=True)
    if r.returncode != 0:
        return []
    raw = (r.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        data = [data] if isinstance(data, dict) else data
    except json.JSONDecodeError:
        data = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return [{"service": it.get("Service", ""), "state": it.get("State", ""),
             "status": it.get("Status", ""), "health": it.get("Health", "")}
            for it in data]


#: `docker info` forks a client that talks to the daemon: ~35 ms, and /api/health
#: calls it on every poll. One open dashboard is 4 per 4 s; a ten-person team with
#: a few tabs each turns the health check -- the cheap, unauthenticated one -- into
#: the most expensive thing the server does. Short enough that `doctor` and the
#: badge still react to Docker stopping within a poll or two.
_DOCKER_TTL = 3.0
_docker_seen: tuple[float, bool] | None = None
_docker_lock = threading.Lock()


#: Read-only docker QUERIES that the dashboard poll repeats. Each open tab asks
#: for /api/repros every 4s, which is `docker compose ls` + `docker ps` + `docker
#: info`; ten teammates with a tab open is ~40 process spawns every 4 seconds on
#: the shared box. Memoised just long enough to collapse concurrent tabs into one
#: call, and INVALIDATED by every mutation so the UI never shows a stale state
#: after an action -- a cache the user can notice would be worse than the spawns.
_QUERY_TTL = 2.5
_query_cache: dict[str, tuple[float, object]] = {}
_query_lock = threading.Lock()


def invalidate_docker_queries() -> None:
    """Drop memoised docker state. Called after anything that changes it."""
    with _query_lock:
        _query_cache.clear()
    global _docker_seen
    _docker_seen = None


def _cached_query(key: str, fn):
    now = time.monotonic()
    with _query_lock:
        hit = _query_cache.get(key)
        if hit is not None and now - hit[0] < _QUERY_TTL:
            return hit[1]
    value = fn()
    with _query_lock:
        _query_cache[key] = (now, value)
    return value


def docker_available(*, max_age: float = _DOCKER_TTL) -> bool:
    """Whether the daemon answers. Cached briefly; `max_age=0` forces a probe."""
    global _docker_seen
    now = time.monotonic()
    if max_age > 0:
        seen = _docker_seen
        if seen is not None and now - seen[0] < max_age:
            return seen[1]
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
        ok = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    with _docker_lock:
        _docker_seen = (now, ok)
    return ok


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


def compose_standalone_version() -> str | None:
    """The version of a STANDALONE `docker-compose` binary, or None.

    Asked only when `docker compose` has already failed, and it changes the answer
    completely. Compose v2 ships as a docker CLI PLUGIN, and the same binary also works
    when invoked directly -- so a box can have `docker-compose` on PATH printing a
    perfectly good v2 help text while `docker compose` does not exist, because the
    binary was never put where the docker CLI looks for plugins. Reported from an EC2
    instance where `docker-compose` worked and every rc-repro command did not.

    rc-repro deliberately does NOT fall back to it: `docker compose -p` and the plugin's
    own project handling are what every path here is written against, and quietly using a
    second entry point would mean two code paths for one operation. Telling someone to
    register the binary they already have is a smaller ask than a fallback nobody tests.
    """
    return _first_line(["docker-compose", "version", "--short"])


def docker_kernel_version() -> str | None:
    """Kernel of the engine host/VM (e.g. Podman machine), or None. This is the
    kernel MongoDB actually runs on - not the macOS/Windows host kernel."""
    return _first_line(["docker", "info", "--format", "{{.KernelVersion}}"])


def compose_logs_capture(name: str, *, tail: int = 200) -> str:
    """Captured (not streamed) combined logs for a repro - for post-mortem
    diagnosis of a failed `up`. '' on error."""
    r = _compose(name, "logs", "--no-color", "--tail", str(tail), capture=True)
    return (r.stdout or "") + (r.stderr or "")


def compose_pull_capture(name: str) -> str:
    """Captured output of `docker compose pull` (stdout+stderr) - used to surface
    registry errors when a repro never started any container. '' on error."""
    r = _compose(name, "pull", capture=True)
    return (r.stdout or "") + (r.stderr or "")


def hub_logged_in() -> bool | None:
    """Best-effort: is the client authenticated to Docker Hub? True/False, or None
    when no auth file is readable (can't tell). Checks Docker and Podman auth
    files, honouring REGISTRY_AUTH_FILE / DOCKER_CONFIG."""
    candidates: list[Path] = []
    for env in ("REGISTRY_AUTH_FILE", "DOCKER_CONFIG"):
        val = os.environ.get(env)
        if val:
            p = Path(val)
            candidates.append(p if p.suffix == ".json" else p / "config.json")
    candidates += [Path.home() / ".docker" / "config.json",
                   Path.home() / ".config" / "containers" / "auth.json"]
    hub_keys = ("https://index.docker.io/v1/", "index.docker.io",
                "registry-1.docker.io", "docker.io")
    seen = False
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        seen = True
        auths = data.get("auths") or {}
        if any(k in auths for k in hub_keys):
            return True
    return False if seen else None
