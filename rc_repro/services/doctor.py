"""Preflight environment checks, shared by `rc-repro doctor` and the web GUI.

Extracted from cli.py so both front-ends run the IDENTICAL checks: the CLI
colours and prints the rows, the API serves them as JSON. Checks never raise --
each one degrades to a warn row -- because the whole point is to run when the
environment is broken.
"""

from __future__ import annotations

import re
import shutil

import requests

from rc_repro import config, runner
from rc_repro.services import lifecycle as lc

# Kernel from which mongod 8.0 hard-exits (SERVER-121912).
MONGO8_BAD_KERNEL = (6, 19)


def _major_version(version: str | None) -> int | None:
    """Leading integer of a version string ('v2.29.1' -> 2, '5.3.1' -> 5), or None.

    Comparing only the first CHARACTER (`...[:1] == "2"`) reported every Compose
    release newer than v2 as unsupported — v5 is current — and would misread a
    future v10 as v1.
    """
    m = re.match(r"v?(\d+)", (version or "").strip())
    return int(m.group(1)) if m else None


def _kernel_major_minor(kv: str | None) -> tuple[int, int] | None:
    """(major, minor) from a kernel string like '6.19.7-200.fc43.aarch64', or None."""
    m = re.match(r"(\d+)\.(\d+)", kv or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def run_checks() -> dict:
    """Run every preflight check and return the findings.

    {"checks": [{"status": "ok"|"warn"|"fail", "message": str}, ...],
     "counts": {...}, "verdict": "ok"|"warn"|"fail",
     "repros": {"total": int, "running": int} | None}
    """
    rows: list[dict] = []

    def line(status: str, msg: str) -> None:
        rows.append({"status": status, "message": msg})

    # Docker daemon (everything else that needs Docker degrades gracefully).
    docker_up = runner.docker_available()
    if docker_up:
        line("ok", f"Docker daemon running ({runner.docker_server_version() or '?'})")
    else:
        line("fail", "Docker daemon not running — start Docker Desktop / dockerd")

    # docker compose v2 or newer
    cv = runner.compose_version()
    cv_major = _major_version(cv)
    if cv_major is not None and cv_major >= 2:
        line("ok", f"docker compose v{cv_major} ({cv})")
    elif cv:
        line("warn", f"docker compose {cv} — rc-repro needs Compose v2 or newer")
    else:
        line("warn", "couldn't detect `docker compose` — install Compose v2 or newer")

    # Engine/VM kernel vs Mongo 8 (SERVER-121912): mongod 8.0 hard-exits on
    # kernel >= 6.19, which recent RC versions require. Common on fresh Podman /
    # FCOS machines and easy to misread as a volume/permission failure.
    if docker_up:
        kv = runner.docker_kernel_version()
        mm = _kernel_major_minor(kv) if kv else None
        if mm and mm >= MONGO8_BAD_KERNEL:
            line("warn", f"engine kernel {kv} — MongoDB 8.0 will not start (SERVER-121912); "
                         "use an engine on kernel < 6.19 for RC versions that require Mongo 8")
        elif kv:
            line("ok", f"engine kernel {kv}")

    # Docker Hub auth: anonymous pulls hit Hub's rate limit (registry.rocket.chat
    # counts against Hub too), which shows up as a silent, container-less `down`.
    hub = runner.hub_logged_in()
    if hub is True:
        line("ok", "logged in to Docker Hub (avoids anonymous pull-rate limits)")
    elif hub is False:
        line("warn", "not logged in to Docker Hub — anonymous pulls can hit the rate "
                     "limit; run `docker login`. registry.rocket.chat counts against Hub too")

    # Disk headroom (RC images are ~1.5 GB each).
    try:
        free_gb = shutil.disk_usage(config.home().parent).free / 1e9
        if free_gb >= 10:
            line("ok", f"Disk: {free_gb:.0f} GB free")
        else:
            line("warn", f"Disk: only {free_gb:.0f} GB free — images are ~1.5 GB each")
    except OSError:
        line("warn", "couldn't check disk space")

    # Live version lookup reachability.
    try:
        r = requests.get("https://releases.rocket.chat/8.5.1/info", timeout=5)
        if r.status_code == 200:
            line("ok", "releases.rocket.chat reachable (live version lookup available)")
        else:
            line("warn", "releases.rocket.chat returned non-200 — use `--offline` if needed")
    except requests.RequestException:
        line("warn", "releases.rocket.chat unreachable — use `--offline` (falls back to shipped map)")

    # Ports.
    try:
        free = runner.pick_port()
        if runner.port_free(3000):
            line("ok", f"Port 3000 free (repros auto-pick from 3000; next free: {free})")
        else:
            line("warn", f"Port 3000 in use — `up` will auto-pick the next free port ({free})")
    except RuntimeError as exc:   # bounded scan found nothing bindable
        line("fail", str(exc))

    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("ok", "warn", "fail")}
    verdict = "fail" if counts["fail"] else ("warn" if counts["warn"] else "ok")

    # Repro tally. Counted through list_repros() so doctor's idea of "running"
    # is the dashboard's (see lc.repro_state), not a second opinion.
    repros = None
    if docker_up:
        try:
            listed = lc.list_repros()
            if listed:
                repros = {"total": len(listed),
                          "running": sum(1 for r in listed if r["state"] == "running")}
        except Exception:  # noqa: BLE001 - a tally must never break the report
            repros = None

    return {"checks": rows, "counts": counts, "verdict": verdict, "repros": repros}
