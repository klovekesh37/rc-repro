"""Drive HTTP load at a repro with k6, run as a throwaway container.

k6 joins the repro's own compose network (``rcrepro-<name>_default``) and targets
the **internal** service address (``rocketchat:3000`` / ``traefik:80``) — so there
is no host-port round trip and it works even when the repro binds to loopback
only. Scenario scripts ship in ``data/loadtest/`` and are copied into the repro's
workspace, mounted at ``/k6``; the script's ``handleSummary`` writes
``/k6/summary.json``, which we read back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import resources

from rc_repro import config, runner

# arm64/amd64 multi-arch (native on Apple Silicon) — pinned for reproducibility.
K6_IMAGE = "grafana/k6:0.55.0"

SCENARIOS = ("messages", "login", "read", "mixed", "journey", "webhook", "badbot", "custom")

# In-network address of the monitor stack's Prometheus (loadtest --live).
PROM_RW_URL = "http://prometheus:9090/api/v1/write"


def run(
    name: str,
    scenario: str,
    *,
    vus: int,
    duration: str,
    ramp: str | None,
    token: str,
    uid: str,
    target: str,
    user: str = config.ADMIN_USERNAME,
    password: str = config.ADMIN_PASSWORD,
    extra_env: dict | None = None,
    users: list[dict] | None = None,
    quiet: bool = False,
    timeline: bool = False,
    spike: str | None = None,
    live: bool = False,
) -> dict:
    """Run `scenario` against `target` (an in-network URL) and return the summary.

    `users` is an optional list of pre-authenticated seeded users
    ({username, password, token, uid}) — VUs round-robin over them so load
    carries real per-user identity instead of one shared admin token.

    Streams k6's live progress to the terminal. Raises RuntimeError if k6
    produced no summary (e.g. it couldn't reach the target)."""
    ws = runner.workspace(name)
    dest = ws / "loadtest"
    dest.mkdir(parents=True, exist_ok=True)

    src = resources.files("rc_repro").joinpath("data", "loadtest")
    for fn in (f"{scenario}.js", "common.js"):
        (dest / fn).write_text(src.joinpath(fn).read_text(encoding="utf-8"), encoding="utf-8")
    summary = dest / "summary.json"
    if summary.exists():
        summary.unlink()  # stale result from a prior run must not be mistaken for this one
    points = dest / "points.json"
    if points.exists():
        points.unlink()   # ditto for the timeline point stream

    env = {
        "RC_URL": target, "RC_TOKEN": token, "RC_UID": uid,
        "RC_USER": user, "RC_PASS": password, "DURATION": duration,
    }
    if spike:
        env["SPIKE"] = spike
    elif ramp:
        env["RAMP"] = ramp
    else:
        env["VUS"] = str(vus)
    if live:
        env["K6_PROMETHEUS_RW_SERVER_URL"] = PROM_RW_URL
        env["K6_PROMETHEUS_RW_TREND_STATS"] = "p(95),p(99),avg"
    ufile = dest / "users.json"
    if users:
        ufile.write_text(json.dumps(users), encoding="utf-8")
        env["RC_USERS_FILE"] = "/k6/users.json"
    elif ufile.exists():
        ufile.unlink()   # stale user list from a prior run — this run is admin-only
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if v is not None})

    network = runner.project_name(name) + "_default"
    cmd = ["docker", "run", "--rm", "--network", network, "-v", f"{dest}:/k6"]
    # Run as the host user so k6 can write /k6/summary.json into the bind-mounted
    # dir. On Linux, bind-mount permissions are enforced and the image's non-root
    # user otherwise can't write there ("permission denied"); Docker Desktop
    # (mac/win) ignores this but the flag is harmless there. (POSIX only.)
    if hasattr(os, "getuid"):
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [K6_IMAGE, "run"]
    if timeline:
        # Stream every measurement so rc-repro can build latency-over-time buckets.
        cmd += ["--out", "json=/k6/points.json"]
    if live:
        # Push metrics into the monitor stack's Prometheus as the test runs.
        cmd += ["--out", "experimental-prometheus-rw"]
    cmd += [f"/k6/{scenario}.js"]

    # k6 writes its banner/progress to stdout. `quiet` (--json mode) reroutes it
    # to stderr so stdout stays a single parseable JSON object for CI/scripts.
    subprocess.run(cmd, stdout=sys.stderr if quiet else None)
    if not summary.exists():
        raise RuntimeError(
            "k6 produced no summary (see output above) — could it reach the target, "
            "and is the image available? (`docker pull " + K6_IMAGE + "`)"
        )
    try:
        return json.loads(summary.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:  # truncated/corrupt summary (killed mid-write, disk full)
        raise RuntimeError(f"k6 summary was unreadable ({exc}) — the run likely did not finish") from exc
