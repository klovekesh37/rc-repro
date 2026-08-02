"""Repro lifecycle orchestration shared by the CLI and the web API.

Extracted from cli.py so both front-ends run the identical flow. Functions raise
`rc_repro.errors` and report progress through `emit` (see services.events); they
never call typer / sys.exit / typer.confirm.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rc_repro import compose, config, presets, rcapi, runner, versions
from rc_repro import seed as seeder
from rc_repro.errors import (ConflictError, DockerError, NotFoundError,
                             NotReadyError, ValidationError)
from rc_repro.services import diagnose, postready
from rc_repro.services.events import Emit, Event, info, null_emit, warn

_NAME_RE = re.compile(r"[^a-z0-9-]+")


# --- naming (pure) ------------------------------------------------------------

def sanitize(name: str) -> str:
    name = name.lower().replace(".", "-")
    return _NAME_RE.sub("-", name).strip("-")


def derive_name(version: str, preset: str) -> str:
    base = "rc" + version
    if preset and preset != "default":
        base += "-" + preset
    return sanitize(base)


# --- preconditions ------------------------------------------------------------

def require_docker() -> None:
    # DockerError, not NotReadyError: an absent engine is a preflight problem the
    # caller must fix (exit 3), not a "still starting, poll again" state (exit 5).
    if not runner.docker_available():
        raise DockerError("Docker isn't running. Start Docker Desktop and try again.")


def resolve_name(name: str | None) -> str:
    """Explicit name (must exist) else the configured default (must exist)."""
    if name:
        if not runner.exists(name):
            raise NotFoundError(f"no repro named {name!r} (run `rc-repro list`)")
        return name
    default = config.load_config().get("default_repro")
    if not default:
        raise ValidationError("no name given and no default repro set (use `rc-repro use <name>`)")
    if not runner.exists(default):
        raise NotFoundError(f"default repro {default!r} no longer exists; set another with `rc-repro use`")
    return default


# --- port accounting ----------------------------------------------------------

def own_ports(name: str) -> set[int]:
    if not name or not runner.exists(name):
        return set()
    try:
        m = runner.read_meta(name)
    except Exception:  # noqa: BLE001 - half-written record
        return set()
    own = {m.host_port}
    n = m.extra.get("instances") if isinstance(m.extra, dict) else None
    if isinstance(n, int) and n > 1:
        own.update(m.host_port + i for i in range(1, n + 1))
    for key in ("sidecar_ports", "monitoring_ports"):
        claimed = m.extra.get(key) if isinstance(m.extra, dict) else None
        if isinstance(claimed, list):
            own.update(int(p) for p in claimed if isinstance(p, int) or str(p).isdigit())
    return own


def check_sidecar_ports(pre: presets.Preset, exclude: str = "") -> None:
    if not pre.ports:
        return
    wanted = set(pre.ports)
    own: set[int] = set()
    for m in runner.list_meta():
        claimed = set(m.extra.get("sidecar_ports") or []) if isinstance(m.extra, dict) else set()
        if m.name == exclude:
            own = claimed
            continue
        overlap = sorted(claimed & wanted)
        if overlap:
            raise ConflictError(
                f"preset {pre.name!r} publishes port(s) {overlap}, already claimed by "
                f"repro {m.name!r} - delete it first: rc-repro down --name {m.name} --volumes")
    for p in sorted(wanted - own):
        if not runner.port_free(p):
            raise ConflictError(f"preset {pre.name!r} needs host port {p}, already in use on this machine")


def check_monitor_ports(exclude: str = "") -> None:
    wanted = set(config.MONITOR_PORTS)
    own: set[int] = set()
    for m in runner.list_meta():
        claimed = set(m.extra.get("monitoring_ports") or []) if isinstance(m.extra, dict) else set()
        if m.name == exclude:
            own = claimed
            continue
        overlap = sorted(claimed & wanted)
        if overlap:
            raise ConflictError(f"monitoring needs port(s) {overlap}, already used by repro {m.name!r} "
                                f"(its monitoring) - stop it first: rc-repro monitor --name {m.name} --off")
    for p in sorted(wanted - own):
        if not runner.port_free(p):
            raise ConflictError(f"monitoring needs host port {p}, already in use on this machine")


def pick_host_port(port: int, pre: presets.Preset, exclude: str = "") -> int:
    span = pre.instances + 1 if pre.instances > 1 else 1
    if port:
        if port + span - 1 > runner.PORT_MAX:
            raise ValidationError(f"--port {port}: a {pre.instances}-instance repro needs ports "
                                  f"up to {port + span - 1} (past 65535)")
        own = own_ports(exclude)
        used = runner.used_ports() - own
        for p in range(port, port + span):
            if p in used:
                raise ConflictError(f"port {p} is already claimed by another repro (see `rc-repro list`)")
            if p not in own and not runner.port_free(p):
                raise ConflictError(f"port {p} is already in use on this machine")
        return port
    try:
        return runner.pick_port_range(span) if span > 1 else runner.pick_port()
    except RuntimeError as exc:
        raise ConflictError(str(exc)) from exc


# --- create -------------------------------------------------------------------

@dataclass
class CreateReq:
    version: str
    preset: str = "default"
    name: str = ""
    port: int = 0
    root_url: str = ""
    bind: str = ""
    rc_image: str = ""
    mongo: str = ""
    reg_token: str = ""
    params: dict = field(default_factory=dict)
    seed: bool = False
    seed_profile: str = "small"
    pin: bool = False
    wait: bool = False
    offline: bool = False
    no_pull: bool = False
    fresh: bool = False
    force: bool = False
    monitor: bool = False
    stats: bool = False


def _unknown_params(params: dict, pre: presets.Preset) -> list[str]:
    return sorted(set(params) - set(pre.params_help))


def _guard_project_collision(name: str) -> None:
    """Refuse to create when a docker compose project of the same derived name
    already exists but belongs to a DIFFERENT workspace.

    The project name is `rcrepro-<name>` regardless of RC_REPRO_HOME, so a repro
    named after an existing one in another home would make `docker compose up`
    reconcile (hijack) the other workspace's containers/volume. Best-effort: if
    docker can't be queried, skip the check rather than block."""
    existing = runner.project_config_files()
    if not existing:
        return
    proj = runner.project_name(name)
    if proj not in existing:
        return
    ours = str(runner.workspace(name) / "docker-compose.yml")
    files = existing[proj]
    if ours not in files:
        raise ConflictError(
            f"a docker compose project {proj!r} already exists, owned by a different "
            f"workspace ({files or 'unknown'}). Creating {name!r} here would hijack its "
            "containers and data volume - choose a different --name.")


def login(meta: runner.Metadata) -> rcapi.Auth:
    return rcapi.login(meta.root_url, mailpit_url=meta.extra.get(config.EXTRA_MAILPIT_URL))


#: Compose-only create flags and why each has no Kubernetes equivalent. Refused
#: rather than silently ignored: a flag accepted and then doing nothing is the exact
#: failure the contract exists to remove, and each of these could only be honoured by
#: guessing at a mapping that is not the same object. Refusing names the reason and
#: leaves the door open to implement a real equivalent later.
_COMPOSE_ONLY_FLAGS: dict[str, str] = {
    "fresh": "discards the compose data volume; the Kubernetes data lives in a PVC, "
             "which is a different object. Use `down --volumes` then recreate.",
    "force": "recreates over a compose project; a Kubernetes namespace collision is a "
             "different failure. Pick another --name, or `down` the existing repro.",
    "monitor": "attaches the Prometheus/Grafana compose sidecars on fixed host ports; "
               "nothing renders them into a cluster yet.",
}


def _reject_compose_only_flags(req: CreateReq) -> None:
    set_flags = [f for f in _COMPOSE_ONLY_FLAGS if getattr(req, f, False)]
    if not set_flags:
        return
    reasons = "; ".join(f"--{f} {_COMPOSE_ONLY_FLAGS[f]}" for f in set_flags)
    raise ValidationError(
        f"{', '.join('--' + f for f in set_flags)} "
        f"{'is' if len(set_flags) == 1 else 'are'} not supported on the Kubernetes "
        f"topology: {reasons}")


def warn_if_unlicensed(req: CreateReq, emit: Emit = null_emit) -> bool:
    """Warn when an enterprise preset is created without a licence.

    Returns whether the warning fired, so a caller (and a test) can tell. The code
    LICENSE_ABSENT_EE_PRESET is stable; the message is not. A registration token may
    arrive on the request or from the RC_REPRO_REG_TOKEN env override, so both count
    as a licence being supplied.
    """
    try:
        pre = presets.load(req.preset)
    except Exception:  # noqa: BLE001 - a bad preset is reported later, not here
        return False
    if not getattr(pre, "requires_license", False):
        return False
    supplied = bool(req.reg_token or config.load_config().get("reg_token"))
    if supplied:
        return False
    warn(emit, f"{req.preset!r} is an enterprise feature and no licence was supplied; "
               "it will run but may not function as licensed "
               "(pass --reg-token, or see cloud.rocket.chat)",
         phase="preflight", code="LICENSE_ABSENT_EE_PRESET")
    return True


def create_repro(req: CreateReq, emit: Emit = null_emit, *, stream_output: bool = False) -> dict:
    """Create-or-reuse a repro. Returns a result dict (meta + boot/seed info).

    `stream_output=True` streams docker's line output through `emit` (for the web
    job log); False leaves docker's own progress on the terminal (CLI default).
    """
    # Licence signal, before dispatch so it fires for every topology and every EE
    # preset. The chart does not validate a licence, so an unlicensed microservices
    # run comes up present but not necessarily functioning as licensed; a warn event
    # with a stable code lets an agent branch on it without reading prose, and it is
    # a warning rather than a refusal because the chart itself installs without one.
    warn_if_unlicensed(req, emit)

    # Topology dispatch. One line, delegating wholesale, so the Compose body below
    # stays exactly as it was and the web GUI gets the same routing as the CLI.
    if _topology_of(req.preset) == "kubernetes":
        from rc_repro.services import k8s, onboarding
        # The gate lives on the Kubernetes path, not on every command: the Docker
        # default has always worked with zero config and must keep doing so (the map
        # makes Docker the default), while the microservices path can resize the
        # engine and provision a cluster, which is exactly the authority onboarding
        # exists to have a human grant once. An un-onboarded agent gets exit 6 here
        # with the command to ask a human to run, rather than inventing a baseline.
        onboarding.require_onboarded()
        _reject_compose_only_flags(req)
        if req.offline:
            # --offline promises no network, but the Kubernetes path must pull the
            # chart and the images, so it cannot honour that. Saying so is better
            # than half-running: version resolution would use the shipped map while
            # helm and the pulls still hit the network, which is a confusing lie.
            raise ValidationError(
                "--offline cannot work on the Kubernetes topology: it must pull the "
                "Helm chart and the container images. Drop --offline, or use a "
                "Compose preset for a fully offline repro.")
        name = req.name or derive_name(req.version, req.preset)
        result = k8s.create_repro(name, req.version, offline=req.offline,
                                  rc_image=req.rc_image or "", mongo=req.mongo or "",
                                  port=req.port, emit=emit)
        if req.wait:
            # --wait must mean the same thing on both topologies, or a caller that
            # asked to block gets an unready repro and no error.
            result.update(k8s.wait_ready(name, emit=emit))
            result["waited"] = True
        return result
    require_docker()
    cfg = config.load_config()

    try:
        resolved = versions.resolve(req.version, offline=req.offline)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if req.rc_image or cfg.get("rc_image"):
        resolved.rc_image = req.rc_image or cfg["rc_image"]
    if req.mongo:
        versions.apply_mongo_override(resolved, req.mongo)

    try:
        pre = presets.load(req.preset, req.params)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    unknown = _unknown_params(req.params, pre)
    if unknown:
        valid = ", ".join(sorted(pre.params_help)) or "(this preset takes no --set params)"
        raise ValidationError(
            f"unknown --set param(s) for preset {req.preset!r}: {', '.join(unknown)} - valid: {valid}")

    wait = req.wait or bool(pre.post_ready) or req.seed
    repro_name = sanitize(req.name) if req.name else derive_name(req.version, req.preset)
    if not repro_name:
        raise ValidationError(f"name {req.name!r} contains no usable characters (want a-z, 0-9, '-')")
    if req.port and not (1024 <= req.port <= 65535):
        raise ValidationError(f"--port {req.port} is out of range (want 1024-65535)")

    if runner.exists(repro_name) and not req.force and not req.fresh:
        return _reuse(repro_name, wait, req, emit, stream_output=stream_output)

    _guard_project_collision(repro_name)
    check_sidecar_ports(pre, exclude=repro_name)
    if req.monitor:
        check_monitor_ports(exclude=repro_name)
    host_port = pick_host_port(req.port, pre, exclude=repro_name)
    root = req.root_url or f"http://localhost:{host_port}"
    token = req.reg_token or cfg.get("reg_token") or ""
    bind_host = req.bind or cfg.get("bind_host") or config.DEFAULT_BIND_HOST

    spec = compose.Spec.from_resolved(
        resolved, project_name=runner.project_name(repro_name), root_url=root,
        host_port=host_port, reg_token=token or None, preset=pre,
        bind_host=bind_host, monitoring=req.monitor)
    doc = compose.build(spec)

    meta = runner.Metadata(
        name=repro_name, project=spec.project_name, rc_version=resolved.rc_version,
        rc_image=resolved.rc_image, mongo_tag=resolved.mongo_tag,
        mongo_flavor=resolved.mongo_flavor, preset=pre.name, root_url=root,
        host_port=host_port, version_source=resolved.source, pinned=req.pin,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if pre.post_ready:
        meta.extra["post_ready"] = pre.post_ready
    if pre.notes:
        meta.extra["notes"] = pre.notes
    if pre.instances > 1:
        meta.extra["instances"] = pre.instances
    if pre.extra:
        meta.extra.update(pre.extra)
    if pre.ports:
        meta.extra["sidecar_ports"] = pre.ports
    files = list(pre.files)
    if req.monitor:
        from rc_repro import monitoring
        targets = compose.rc_service_names(pre.instances)
        files += monitoring.files(targets, project=spec.project_name)
        meta.extra["monitoring"] = True
        meta.extra["monitoring_ports"] = list(config.MONITOR_PORTS)
        meta.extra.setdefault("notes", [])
        meta.extra["notes"] = list(meta.extra["notes"]) + monitoring.notes()

    if runner.exists(repro_name):
        if runner.down(repro_name, volumes=req.fresh) != 0:
            raise DockerError(f"could not tear down the existing {repro_name!r}; not overwriting it")

    runner.write(repro_name, compose.to_yaml(doc), meta, files=files)
    if req.pin:
        raw = config.load_config(with_env=False)
        raw["default_repro"] = repro_name
        config.save_config(raw)

    info(emit, f"creating {repro_name!r} - RC {resolved.rc_version}, "
               f"Mongo {resolved.mongo_tag} ({resolved.mongo_flavor}), preset {pre.name}",
         phase="create", data={"name": repro_name})

    rc = _up(repro_name, pull=not req.no_pull, emit=emit, stream_output=stream_output)
    if rc != 0:
        cause = diagnose.diagnose_failure(repro_name)
        head = f"`docker compose up` failed - {cause}" if cause else "`docker compose up` failed."
        raise DockerError(
            f"{head} Workspace kept for inspection - retry with --force, or discard: "
            f"rc-repro down --name {repro_name} --volumes")

    result = _summary(meta)
    result["reused"] = False
    result["waited"] = wait
    if wait:
        result.update(wait_and_finalize(meta, emit))
    if req.seed:
        result["seed"] = run_seed_inline(meta, req.seed_profile, req.stats, emit)
    return result


def _reuse(name: str, wait: bool, req: CreateReq, emit: Emit, *, stream_output: bool) -> dict:
    state = runner.rc_state(name)
    if state == "running":
        info(emit, f"{name!r} is already running.", phase="create")
    else:
        info(emit, f"{name!r} already exists - bringing it back up.", phase="create")
        if _up(name, pull=False, emit=emit, stream_output=stream_output) != 0:
            raise DockerError("`docker compose up` failed")
    meta = runner.read_meta(name)
    result = _summary(meta)
    result["reused"] = True
    result["waited"] = wait
    if wait:
        result.update(wait_and_finalize(meta, emit))
    if req.seed:
        result["seed"] = run_seed_inline(meta, req.seed_profile, req.stats, emit)
    return result


def _up(name: str, *, pull: bool, emit: Emit, stream_output: bool) -> int:
    if not stream_output:
        return runner.up(name, pull=pull)   # docker draws its own progress on the terminal
    on_line = lambda ln: info(emit, ln, phase="boot")  # noqa: E731
    if pull:
        runner.compose_stream(name, "pull", on_line=on_line)   # non-fatal, like runner.up
    return runner.compose_stream(name, "up", "-d", "--remove-orphans", on_line=on_line)


# --- readiness / finalize -----------------------------------------------------

def wait_serving(meta: runner.Metadata, emit: Emit, timeout: float) -> dict:
    seen = {"restarts": 0}

    def is_alive() -> bool:
        return runner.rc_state(meta.name) in ("running", "restarting", "created")

    def tick(elapsed: float) -> None:
        # Surface a crash-loop: if RC keeps restarting, boot is slow for a reason
        # (usually CPU/RAM pressure or a boot error), not just "taking a while".
        rc = runner.rc_restart_count(meta.name)
        if rc >= 2 and rc > seen["restarts"]:
            warn(emit, f"Rocket.Chat has restarted {rc}x - likely resource pressure "
                       "(free some repros / raise Docker's CPU+RAM) or a boot error; "
                       "check Logs.", phase="wait")
        seen["restarts"] = max(seen["restarts"], rc)
        pct = max(0.0, min(99.0, elapsed / timeout * 100)) if timeout else None
        info(emit, f"still booting ({int(elapsed)}s)", phase="wait", pct=pct)

    try:
        return rcapi.wait_ready(meta.root_url, timeout=timeout, is_alive=is_alive, on_tick=tick)
    except rcapi.NotReady as exc:
        hint = ""
        if seen["restarts"] >= 2:
            hint = (f" - Rocket.Chat restarted {seen['restarts']}x; likely resource pressure "
                    f"(free repros / raise Docker CPU+RAM), then `rc-repro ready --name {meta.name}`")
        raise NotReadyError(str(exc) + hint) from exc


def finalize(meta: runner.Metadata, emit: Emit):
    try:
        auth = login(meta)
        if rcapi.complete_setup_wizard(meta.root_url, auth, config.ADMIN_PASSWORD):
            info(emit, "setup wizard skipped - no registration needed.", phase="post_ready")
        return auth
    except Exception:  # noqa: BLE001 - finalize is best-effort
        return None


def wait_and_finalize(meta: runner.Metadata, emit: Emit = null_emit, timeout: float = 300.0) -> dict:
    """Wait until the repro serves, then run the post-ready steps.

    Dispatches here rather than at each call site: the CLI's `ready`, its `--json`
    variant, and the web GUI all call this, and guarding three callers separately is
    how one of them gets missed. On Kubernetes the URL is a port-forward that may
    have died, so it is revived before waiting rather than timed out against.
    """
    if isinstance(meta.extra, dict) and meta.extra.get("topology") == "kubernetes":
        # Dispatch fully to the Kubernetes wait, not just revive-then-wait_serving.
        # wait_serving's is_alive/tick read compose state (runner.rc_state), which is
        # empty for a Kubernetes repro, and it has no terminal-pod detection, so a
        # stuck pull would sit out the timeout instead of aborting (exit 7). k8s.
        # wait_ready owns both. This is what the non-json `ready` and the GUI use, so
        # they must get the same behaviour as the --json path, not a compose wait.
        from rc_repro.services import k8s
        result = k8s.wait_ready(meta.name, timeout=timeout, emit=emit)
        return {"booted_s": result.get("booted_s", 0),
                "running_version": result.get("version", "?")}
    started = time.monotonic()
    served = wait_serving(meta, emit, timeout)
    elapsed = int(time.monotonic() - started)
    auth = finalize(meta, emit)
    postready.run_post_ready(meta, auth, emit)
    running = served.get("version", "?")
    if running != "?" and not meta.rc_version.startswith(running):
        warn(emit, f"running version {running} != requested {meta.rc_version}", phase="wait")
    info(emit, "ready", phase="done", pct=100.0)
    return {"booted_s": elapsed, "running_version": running}


# --- seed (inline, used by create --seed) -------------------------------------

def run_seed_inline(meta: runner.Metadata, profile: str, stats: bool, emit: Emit) -> dict:
    from rc_repro import perf
    try:
        auth = login(meta)
    except Exception as exc:  # noqa: BLE001
        raise NotReadyError(f"can't seed - repro not ready: {exc}") from exc
    try:
        plan = seeder.plan_from(profile)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    info(emit, f"seeding (profile {profile})", phase="seed")
    mon = perf.ResourceMonitor(meta.name).start() if stats else None
    t0 = time.monotonic()
    try:
        s = seeder.seed(meta.root_url, auth, plan, log=lambda m: info(emit, m.strip(), phase="seed"))
    finally:
        resources = mon.stop() if mon else None
    s["total_s"] = time.monotonic() - t0
    if resources is not None:
        s["resources_keys"] = sorted(resources)
    return s


# --- read / state -------------------------------------------------------------

def _summary(meta: runner.Metadata) -> dict:
    d = {
        "name": meta.name, "rc_version": meta.rc_version, "mongo_tag": meta.mongo_tag,
        "mongo_flavor": meta.mongo_flavor, "preset": meta.preset, "root_url": meta.root_url,
        "host_port": meta.host_port, "login": {"user": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        "pinned": meta.pinned, "notes": list(meta.extra.get("notes", []) if isinstance(meta.extra, dict) else []),
    }
    n = meta.extra.get("instances") if isinstance(meta.extra, dict) else None
    if n:
        d["instances"] = int(n)
        d["instance_urls"] = [f"http://localhost:{meta.host_port + i}" for i in range(1, int(n) + 1)]
    if isinstance(meta.extra, dict) and meta.extra.get("monitoring"):
        d["monitoring"] = True
        d["grafana_url"] = f"http://localhost:{config.MONITOR_PORTS[1]}"
    return d


# Preset sidecar links to surface on the dashboard: preset -> [(label, index into
# config.PRESET_PORTS[preset])]. Ports come from config so they never drift.
_PRESET_LINKS = {
    "email": [("Mailpit", 0)],
    "s3_minio": [("MinIO console", 1), ("MinIO API", 0)],
    "saml": [("Keycloak", 0)],
    "oidc": [("Keycloak", 0)],
    "livechat": [("Widget site", 0)],
}


def repro_links(m: runner.Metadata) -> list[dict]:
    """Clickable URLs for a repro: RC, extra instances, preset sidecars (S3,
    Keycloak, Mailpit, widget), and monitoring — [{label, url, kind}]."""
    links = [{"label": "Rocket.Chat", "url": m.root_url, "kind": "rc"}]
    extra = m.extra if isinstance(m.extra, dict) else {}
    n = extra.get("instances")
    if isinstance(n, int) and n > 1:
        for i in range(1, n + 1):
            links.append({"label": f"instance {i}", "url": f"http://localhost:{m.host_port + i}", "kind": "rc"})
    ports = config.PRESET_PORTS.get(m.preset, ())
    for label, idx in _PRESET_LINKS.get(m.preset, []):
        if idx < len(ports):
            links.append({"label": label, "url": f"http://localhost:{ports[idx]}", "kind": "sidecar"})
    if extra.get("monitoring"):
        links.append({"label": "Grafana", "url": f"http://localhost:{config.MONITOR_PORTS[1]}", "kind": "monitor"})
        links.append({"label": "Prometheus", "url": f"http://localhost:{config.MONITOR_PORTS[0]}", "kind": "monitor"})
    return links


def _pretty_state(status: str) -> str:
    if not status:
        return "down"
    if "running(" in status:
        return "running"
    if "exited(" in status:
        return "stopped"
    return status.split("(")[0]


def _uptime_health(status: str) -> tuple[str, str]:
    """Parse a docker `Status` string -> (uptime, health).
    "Up 2 hours (healthy)" -> ("2 hours", "healthy"); "Exited (0) ..." -> ("", "")."""
    if not status:
        return "", ""
    mm = re.search(r"\(([^)]+)\)", status)
    health = mm.group(1) if (mm and status.startswith("Up ")) else ""
    up = status[3:].split(" (")[0].strip() if status.startswith("Up ") else ""
    return up, health


def list_repros() -> list[dict]:
    metas = runner.list_meta()
    default = config.load_config().get("default_repro")
    docker_up = runner.docker_available()
    states = (runner.project_states() or {}) if docker_up else {}
    status_map = runner.rc_status_by_project() if docker_up else {}
    out = []
    for m in metas:
        if (m.extra or {}).get("topology") == "kubernetes" if isinstance(m.extra, dict) else False:
            # Ask Kubernetes, not compose: a compose lookup returns nothing for these
            # and `list` would show every Kubernetes repro as unknown forever.
            from rc_repro.services import k8s
            try:
                state = k8s.aggregate_state(k8s.pods(m.name))
            except Exception:  # noqa: BLE001 - cluster gone or unreachable
                state = "?"
            uptime, health = "", ""
            out.append({"name": m.name, "rc_version": m.rc_version, "mongo_tag": m.mongo_tag,
                        "host_port": m.host_port, "root_url": m.root_url, "state": state,
                        "preset": m.preset, "pinned": m.pinned, "default": m.name == default,
                        "monitoring": False, "created_at": m.created_at,
                        "uptime": uptime, "health": health, "grafana_url": None,
                        "links": [{"label": "Rocket.Chat", "url": m.root_url}]})
            continue
        state = "?" if not docker_up else _pretty_state(states.get(m.project, ""))
        uptime, health = _uptime_health(status_map.get(m.project, ""))
        monitored = bool(isinstance(m.extra, dict) and m.extra.get("monitoring"))
        out.append({"name": m.name, "rc_version": m.rc_version, "mongo_tag": m.mongo_tag,
                    "host_port": m.host_port, "root_url": m.root_url, "state": state,
                    "preset": m.preset, "pinned": m.pinned, "default": m.name == default,
                    "monitoring": monitored, "created_at": m.created_at,
                    "uptime": uptime, "health": health or (state if state == "running" else ""),
                    "grafana_url": f"http://localhost:{config.MONITOR_PORTS[1]}" if monitored else None,
                    "links": repro_links(m)})
    return out


def describe(name: str) -> dict:
    return _summary(runner.read_meta(resolve_name(name)))


def detail(name: str) -> dict:
    """Rich detail for the GUI panel: summary + state/uptime/health + links +
    containers + the RC service's env vars."""
    target = resolve_name(name)
    m = runner.read_meta(target)
    # Topology dispatch, same one-line pattern as create_repro. The Kubernetes
    # record uses the identical {service, state, status} container shape, so a
    # caller reads it without knowing which topology produced it.
    if isinstance(m.extra, dict) and m.extra.get("topology") == "kubernetes":
        from rc_repro.services import k8s
        return k8s.detail(target)
    d = _summary(m)
    containers = runner.container_details(target)
    rc = [c for c in containers if c["service"] == "rocketchat" or c["service"].startswith("rocketchat-")]
    running = any(c["state"] == "running" for c in rc)
    d["state"] = "running" if running else ("stopped" if containers else "down")
    up, health = _uptime_health(next((c["status"] for c in rc), ""))
    d["uptime"] = up
    d["health"] = health or (d["state"] if running else ("exited" if containers else ""))
    d["links"] = repro_links(m)
    d["containers"] = containers
    doc = runner.read_compose(target)
    svcs = doc.get("services", {})
    rc_svc = svcs.get("rocketchat") or svcs.get("rocketchat-1") or {}
    env = rc_svc.get("environment", {})
    if isinstance(env, dict):
        d["env"] = [{"key": k, "value": str(v)} for k, v in sorted(env.items())]
    elif isinstance(env, list):  # compose list form "K=V"
        d["env"] = [{"key": (e.split("=", 1) + [""])[0], "value": (e.split("=", 1) + [""])[1]}
                    for e in env]
    else:
        d["env"] = []
    return d


def set_state(name: str, action: str) -> None:
    target = resolve_name(name)
    if topology_of_repro(target) == "kubernetes":
        from rc_repro.services import k8s
        if action != "restart":
            # start/stop have no clean Kubernetes analogue: scaling to zero and back
            # is not the same as stopping a container, and silently doing something
            # different is worse than saying so.
            raise ValidationError(
                f"{action!r} is not supported on the Kubernetes topology; use "
                f"`rc-repro down --name {target}` and recreate, or `restart`")
        if k8s.restart(target, emit=null_emit) != 0:
            raise DockerError(f"rollout restart failed for {target!r}")
        return
    fn = {"start": runner.start, "stop": runner.stop, "restart": runner.restart}.get(action)
    if fn is None:
        raise ValidationError(f"unknown action {action!r} (want start|stop|restart)")
    if fn(target) != 0:
        raise DockerError(f"`docker compose {action}` failed for {target!r}")


def _clear_default_if(name: str) -> None:
    cfg = config.load_config(with_env=False)
    if cfg.get("default_repro") == name:
        cfg.pop("default_repro", None)
        config.save_config(cfg)


def topology_of_repro(name: str) -> str:
    """An existing repro's topology, read from its record.

    Separate from _topology_of, which answers for a preset before a repro exists.
    Every verb that touches a live repro dispatches on this, because a Kubernetes
    repro has no compose project and running `docker compose` against it either
    fails or, worse, silently does nothing.
    """
    try:
        meta = runner.read_meta(name)
    except Exception:  # noqa: BLE001 - half-written or absent record
        return "compose"
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    return extra.get("topology", "compose") or "compose"


def _topology_of(preset_name: str) -> str:
    """The preset's topology, defaulting to compose if it cannot be loaded.

    A failure to load is not this function's problem to report: the Compose path
    raises a proper ValidationError for an unknown preset a few lines later, and
    guessing "kubernetes" here would route a typo into the wrong lifecycle.
    """
    try:
        return getattr(presets.load(preset_name), "topology", "compose") or "compose"
    except Exception:  # noqa: BLE001
        return "compose"


def teardown(name: str, *, volumes: bool = False, confirm: bool = False, emit: Emit = null_emit) -> dict:
    target = resolve_name(name)
    if topology_of_repro(target) == "kubernetes":
        from rc_repro.services import k8s
        if volumes and not confirm:
            raise ValidationError(f"deleting {target!r}'s data and record is irreversible - "
                                  "pass confirm=true")
        result = k8s.teardown(target, volumes=volumes, emit=emit)
        if volumes:
            _clear_default_if(target)
        # residual is authoritative: a partial teardown must not report success.
        result["removed_ok"] = not result.get("residual")
        return result
    require_docker()
    if volumes and not confirm:
        raise ValidationError(f"deleting {target!r}'s data volume and record is irreversible - "
                              "pass confirm=true")
    if runner.down(target, volumes=volumes) != 0:
        raise DockerError(f"`docker compose down` failed for {target!r}")
    if volumes:
        runner.remove(target)
        _clear_default_if(target)
    info(emit, f"{target!r} {'removed' if volumes else 'down (data kept)'}", phase="done")
    return {"name": target, "removed": volumes}


def _is_kubernetes(meta) -> bool:
    return isinstance(meta.extra, dict) and meta.extra.get("topology") == "kubernetes"


def prunable() -> list[str]:
    """Names of repros that are safe to prune: not pinned and with no containers
    (a plain `down`). Raises DockerError if docker can't be queried — deleting on
    that ambiguity would be destructive."""
    require_docker()
    states = runner.project_states()
    if states is None:
        raise DockerError("couldn't query docker compose projects - not pruning (is Docker healthy?)")
    out = []
    for m in runner.list_meta():
        if m.pinned:
            continue
        if _is_kubernetes(m):
            # A Kubernetes repro's `project` is its namespace, which is never in the
            # compose project list, so the compose rule below would classify a
            # RUNNING repro as prunable and delete it. Ask Kubernetes instead, and
            # treat any uncertainty as "not prunable": deleting on ambiguity is the
            # one mistake prune must never make.
            try:
                from rc_repro.services import k8s
                if k8s.pods(m.name):
                    continue          # still has pods: live, do not prune
            except Exception:  # noqa: BLE001 - cluster unreachable: cannot tell
                continue
            out.append(m.name)
            continue
        if m.project not in states:
            out.append(m.name)
    return out


def prune_plan() -> dict:
    """Return the records and shared cluster that an explicit prune may remove."""
    from rc_repro.services import k8s
    return {"targets": prunable(), "cluster": k8s.cluster_prune_status()}


def prune(*, confirm: bool = False, emit: Emit = null_emit) -> dict:
    from rc_repro.services import k8s
    plan = prune_plan()
    targets = plan["targets"]
    cluster_target = bool(plan["cluster"].get("prunable"))
    if not targets and not cluster_target:
        return {"targets": [], "removed": [], "cluster": plan["cluster"]}
    if not confirm:
        detail = f"{len(targets)} down repro(s) incl. data"
        if plan["cluster"].get("exists"):
            detail += " and the owned Kind cluster once it is empty"
        raise ValidationError(f"prune deletes {detail} - pass confirm=true")
    removed = []
    for name in targets:
        # Dispatch: a Kubernetes repro has no compose project, so runner.down would
        # no-op and runner.remove would delete the record while leaking the recorded
        # port-forward and lingering namespace. k8s.teardown kills the forward (with
        # the identity check, so never a stranger) and deletes the namespace, which
        # is the orphan-forward reclaim for a pruned repro.
        if topology_of_repro(name) == "kubernetes":
            from rc_repro.services import k8s
            k8s.teardown(name, volumes=True, emit=emit)
            _clear_default_if(name)
            removed.append(name)
            info(emit, f"pruned {name!r}", phase="done")
            continue
        if runner.down(name, volumes=True) != 0:
            warn(emit, f"could not clean up {name!r} - skipping", phase="done")
            continue
        runner.remove(name)
        _clear_default_if(name)
        removed.append(name)
        info(emit, f"pruned {name!r}", phase="done")
    # This must run even when there were no down records. `down --volumes` removes
    # the final record before a later `prune`, which is exactly when the shared empty
    # cluster is the only remaining target. The helper rechecks labels and refuses on
    # ambiguity, so a race cannot turn this into deletion of a live repro.
    cluster = k8s.prune_cluster(emit=emit)
    return {"targets": targets, "removed": removed, "cluster": cluster}


def stale_forwards() -> list[dict]:
    """Kubernetes repros whose recorded port-forward is no longer alive-and-ours.

    The truly-orphaned case (a forward whose repro record was deleted without killing
    it) cannot be found from here: once the record is gone the pid is lost, and the
    #19 identity check means we will not go hunting arbitrary pids to kill. So this
    reports the recoverable case, a live repro whose tunnel died, which `ready` or
    any HTTP verb re-establishes on demand. `doctor` surfaces it so a stuck repro has
    a visible cause rather than a silent one.
    """
    out = []
    for m in runner.list_meta():
        if not (isinstance(m.extra, dict) and m.extra.get("topology") == "kubernetes"):
            continue
        from rc_repro.services import k8s
        if k8s.forward_state(m) == "down":
            out.append({"name": m.name, "host_port": m.host_port})
    return out


# --- cross-topology preconditions ----------------------------------------------
#
# The parity table in the design enumerated eleven verbs, but the CLI has
# twenty-five. The fourteen it omitted still touch a repro, so each needs one of two
# things: reachability fixed up before it talks HTTP, or an honest refusal. Silently
# running a compose-shaped command against a Kubernetes repro is the failure mode
# these two helpers exist to prevent.

def ensure_reachable(name: str, emit: Emit = null_emit) -> None:
    """Make a repro's URL usable before something talks HTTP to it.

    On Compose the published port is always there. On Kubernetes it is a port-forward
    that dies with whatever started it, so every HTTP-using verb has to revive it
    first or it fails for a reason that has nothing to do with what was asked.
    """
    if topology_of_repro(name) != "kubernetes":
        return
    from rc_repro.services import k8s
    meta = runner.read_meta(name)
    pid = k8s.ensure_port_forward(meta, emit)
    if pid and pid != (meta.extra or {}).get("k8s_forward_pid"):
        meta.extra = {**(meta.extra or {}), "k8s_forward_pid": pid}
        runner.write_meta(name, meta)


def require_compose_topology(name: str, verb: str, why: str = "") -> None:
    """Refuse a Compose-only verb on a non-Compose repro, naming the reason.

    Per the contract: a flag or command that is accepted and then does nothing is
    the afternoon-wasting failure rc-repro exists to remove. Refusing with exit 2 is
    the honest answer until a Kubernetes equivalent exists.
    """
    topology = topology_of_repro(name)
    if topology == "compose":
        return
    detail = f" {why}" if why else ""
    raise ValidationError(
        f"`{verb}` is not supported on the {topology} topology yet.{detail} "
        f"Use a Compose preset for this, or `rc-repro info --name {name} --json` "
        f"to inspect the repro instead.")
