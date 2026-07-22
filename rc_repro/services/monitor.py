"""Attach/detach the Prometheus+Grafana monitoring add-on — shared by CLI and API.

Extracted from cli.py's `monitor` command. Raises rc_repro.errors and reports via
`emit`; also used by the web GUI (and needed by loadtest --live).
"""

from __future__ import annotations

from rc_repro import compose, config, monitoring, rcapi, runner
from rc_repro.errors import DockerError, NotReadyError
from rc_repro.services import lifecycle
from rc_repro.services.events import Emit, info, null_emit, warn


def _detect_bind(doc: dict) -> str:
    """Host bind interface from an existing published port (ip:host:container)."""
    for svc in doc.get("services", {}).values():
        for p in svc.get("ports", []):
            parts = str(p).split(":")
            if len(parts) == 3:
                return parts[0]
    return config.DEFAULT_BIND_HOST


def _rc_services_in(doc: dict) -> list[str]:
    return [s for s in doc.get("services", {}) if s == "rocketchat" or s.startswith("rocketchat-")]


def attach(name: str, emit: Emit = null_emit) -> dict:
    lifecycle.require_docker()
    m = runner.read_meta(lifecycle.resolve_name(name))
    doc = runner.read_compose(m.name)
    lifecycle.check_monitor_ports(exclude=m.name)   # raises ConflictError on a taken port
    try:
        auth = lifecycle.login(m)
        if not rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                 monitoring.RC_METRICS_SETTING, True):
            warn(emit, "could not enable RC metrics via the API (is it ready?)", phase="config")
    except Exception as exc:  # noqa: BLE001
        raise NotReadyError(f"repro not reachable to enable metrics (ready first): {exc}") from exc

    doc.setdefault("services", {}).update(monitoring.bind_ports(monitoring.services(), _detect_bind(doc)))
    doc.setdefault("volumes", {}).update(monitoring.volumes())
    m.extra["monitoring"] = True
    m.extra["monitoring_ports"] = list(config.MONITOR_PORTS)
    m.extra["notes"] = [n for n in m.extra.get("notes", []) if n not in monitoring.notes()] + monitoring.notes()
    targets = _rc_services_in(doc) or ["rocketchat"]
    runner.write(m.name, compose.to_yaml(doc), m, files=monitoring.files(targets))

    info(emit, "attaching monitoring (pulling images if needed)...", phase="boot")
    if runner.up(m.name, pull=True) != 0:
        raise DockerError("`docker compose up` failed bringing up monitoring")
    grafana = f"http://localhost:{config.MONITOR_PORTS[1]}"
    info(emit, f"monitoring attached to {m.name!r}", phase="done",
         data={"grafana_url": grafana, "notes": monitoring.notes()})
    return {"name": m.name, "monitoring": True, "grafana_url": grafana, "notes": monitoring.notes()}


def detach(name: str, emit: Emit = null_emit) -> dict:
    lifecycle.require_docker()
    m = runner.read_meta(lifecycle.resolve_name(name))
    doc = runner.read_compose(m.name)
    rc_ok = False
    try:
        auth = lifecycle.login(m)
        rc_ok = rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  monitoring.RC_METRICS_SETTING, False)
    except Exception:  # noqa: BLE001 - best-effort; the repro may be stopped
        pass
    runner.rm_services(m.name, list(monitoring.SERVICES))
    for s in monitoring.SERVICES:
        doc.get("services", {}).pop(s, None)
    for v in monitoring.VOLUMES:
        doc.get("volumes", {}).pop(v, None)
    m.extra.pop("monitoring", None)
    m.extra.pop("monitoring_ports", None)
    m.extra["notes"] = [n for n in m.extra.get("notes", []) if n not in monitoring.notes()]
    runner.write(m.name, compose.to_yaml(doc), m)
    info(emit, f"monitoring detached from {m.name!r}", phase="done", data={"rc_setting_reset": rc_ok})
    return {"name": m.name, "monitoring": False, "rc_setting_reset": rc_ok}
