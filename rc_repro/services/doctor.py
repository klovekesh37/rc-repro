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


def mongo_kernel_conflict(mongo_tag: str, kernel: str | None = None) -> str:
    """Why this MongoDB cannot start on this engine, or "" if it can.

    SERVER-121912: mongod 8.0 hard-EXITS on kernel >= 6.19. Not degrades -- exits,
    every time, with a message that reads like a volume or permission problem. So
    there is no case where continuing works, which is what makes this a refusal
    rather than a warning (the same reasoning as `check_capacity`).

    Version-aware on purpose. `run_checks()` below can only warn generically,
    because `doctor` does not know which release you are about to boot; by the
    time a create has resolved its pairing, the answer is exact -- RC 8.6.1 needs
    Mongo 8.0 and therefore cannot start here, while RC 7.4.1 needs 7.0 and can.

    One implementation, three callers: the create preflight refuses with it, the
    GUI's version lookup shows it before you press the button, and the CLI gets
    it for free by going through the service. It was two copies with different
    behaviour -- a generic warn here and a version-aware warn inlined in
    web/app.py -- and neither of them stopped the pull.
    """
    mm = _kernel_major_minor(kernel if kernel is not None else runner.docker_kernel_version())
    if not mm or mm < MONGO8_BAD_KERNEL:
        return ""
    try:
        mongo_major = int(str(mongo_tag).split(".")[0])
    except (ValueError, AttributeError):
        return ""
    if mongo_major < 8:
        return ""
    kv = ".".join(str(n) for n in mm)
    return (f"MongoDB {mongo_tag} cannot start on this engine's kernel ({kv}) — "
            "mongod 8.0 exits on kernel 6.19 and newer (SERVER-121912). Use an "
            "engine on an older kernel, or a Rocket.Chat version that pairs with "
            "MongoDB 7.0.")


def run_checks() -> dict:
    """Run every preflight check and return the findings.

    {"checks": [{"status": "ok"|"warn"|"fail", "message": str}, ...],
     "counts": {...}, "verdict": "ok"|"warn"|"fail",
     "repros": {"total": int, "running": int} | None}
    """
    rows: list[dict] = []

    def line(status: str, msg: str, elsewhere: str = "") -> None:
        """`elsewhere` names a place the GUI shows this fact PERMANENTLY.

        The web report drops those rows (see the /api/doctor endpoint). A terminal
        has no header chip, no user list and no workspace rail, so `rc-repro
        doctor` is the only place a CLI user learns them and they stay; in the
        browser they were a report restating what was already on the screen.
        Only ever set on an `ok` row -- a WARNING about the same subject is not
        something the chip beside it says, and those all still appear.
        """
        row = {"status": status, "message": msg}
        if elsewhere:
            row["elsewhere"] = elsewhere
        rows.append(row)

    # Docker daemon (everything else that needs Docker degrades gracefully).
    # max_age=0: the dashboard poll memoises this, but somebody running `doctor`
    # has explicitly asked whether Docker is up RIGHT NOW -- answering that from a
    # cache is how a diagnostic tells you the opposite of the truth.
    docker_up = runner.docker_available(max_age=0)
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

    # Memory headroom, in workspaces rather than megabytes -- "3.2 GB available"
    # does not tell you whether you can start another one, and that is the only
    # question anybody is actually asking. Added after seven concurrent stacks
    # OOM-killed a 10 GB host: every `up` had succeeded, and nothing anywhere
    # reported that the machine was nearly full.
    cap = lc.capacity()
    if cap["known"]:
        total_mb, avail_mb, swap_mb = cap["total_mb"], cap["available_mb"], cap["swap_mb"]
        room = cap["room"]
        detail = f"{avail_mb} MB available of {total_mb} MB"
        if room >= 2:
            # No count when there is plenty. The count exists to answer "can I
            # start another one", and while the answer is an unqualified yes it is
            # a number to read past; the two rows below still say it the moment it
            # stops being one.
            line("ok", f"Memory: {detail}")
        elif room == 1:
            line("warn", f"Memory: {detail} — room for about 1 more workspace")
        else:
            line("warn", f"Memory: {detail} — NOT enough for another workspace. "
                         "`rc-repro stop --name <it>` frees memory and keeps the data")
        if swap_mb == 0:
            line("warn", "No swap: memory pressure becomes an OOM kill rather than "
                         "slowdown, and the kernel picks its own victim")

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

    # The shared edge. Silent unless one is set up, so a single-user install
    # sees no rows about a thing it does not have -- but once one exists it is
    # reported, because everything on the box depends on it (§8, shared fate):
    # while it is down the GUI and EVERY registered workspace are unreachable,
    # and nothing else in this report would say so.
    try:
        from rc_repro.services import edge as edgesvc

        if edgesvc.installed():
            served = edgesvc.served_domain()
            where = f" ({served})" if served else ""
            routes = edgesvc.registered()
            if not docker_up:
                line("warn", f"Edge{where} installed — cannot check it, Docker is down")
            elif edgesvc.running():
                line("ok", f"Edge running{where} — holds :80/:443, "
                           f"{len(routes)} workspace route(s)",
                     elsewhere="the edge chip in the header")
            else:
                # Name what is holding the port. "the edge is not running" sends
                # you looking at the edge, when the cause is usually something
                # else on :443 -- a Traefik left by an older rc-repro, or an
                # unrelated web server.
                holder = edgesvc.port_holder(443) if docker_up else ""
                because = f" — {holder} is holding :443" if holder else ""
                line("fail", f"Edge{where} is NOT running{because}. Every https "
                             f"name on this box ({len(routes)} route(s)) is "
                             "unreachable. `rc-repro edge start`")
            if docker_up:
                # A route whose workspace is gone points the edge at nothing, so
                # that hostname 502s instead of 404ing and the name cannot be
                # reused.
                known = {m.name for m in runner.list_meta()}
                stale = [r for r in routes if r not in known]
                if stale:
                    line("warn", f"{len(stale)} edge route(s) with no workspace: "
                                 f"{', '.join(sorted(stale)[:5])} — remove with "
                                 f"`rc-repro down --name <it>`")
                # A route the edge cannot REACH is a 502 rather than an error, and
                # nothing else in the tool would ever say so. Happens after the
                # edge is recreated, since attachments are runtime state.
                if edgesvc.running():
                    attached = set(edgesvc.attached_networks())
                    orphan = [r for r in routes if r in known
                              and edgesvc.workspace_network(r) not in attached]
                    if orphan:
                        line("warn",
                             f"{len(orphan)} route(s) the edge cannot reach: "
                             f"{', '.join(sorted(orphan)[:5])} — they answer 502. "
                             "`rc-repro edge restart` re-attaches them")
    except Exception:  # noqa: BLE001 - a check must never break the report
        line("warn", "Edge status could not be determined")

    # Identity: who can sign in, and whether the files that decide it are sound.
    # Nothing else reports this, and an install with no admin cannot make one from
    # the GUI -- the repair is hand-editing a file most people would not think to
    # look for.
    try:
        from rc_repro.services import sessions as sessionsvc
        from rc_repro.services import users as usersvc

        if not usersvc.any_users():
            line("warn", "No GUI accounts — `rc-repro serve` will refuse to start on "
                         "anything but loopback (`rc-repro users add <name>`)")
        else:
            admins = usersvc.admins()
            if admins:
                line("ok", f"{len(usersvc.list_users())} GUI account(s), "
                           f"admin: {', '.join(admins)}",
                     elsewhere="the People page")
            else:
                line("fail", "No admin account — nobody can manage people from the "
                             "GUI. `rc-repro users role <name> admin`")
            implicit = usersvc.implicit_admins()
            if implicit:
                line("warn", f"admin by default (blank role column): "
                             f"{', '.join(implicit)} — that is the migration for "
                             "accounts made before roles existed, not a choice. "
                             f"`rc-repro users role {implicit[0]} member`")
            # Reported rather than warned about: this is the default and it is a
            # deliberate one, but "what may a member actually do on this box?" should
            # be answerable without reading source. Same reason `serve` names the
            # implicit admins -- a state that is visible is a state somebody can
            # disagree with.
            from rc_repro.services import lifecycle as lcsvc
            if config.load_config().get(lcsvc.CREATE_POLICY_KEY) == "admin":
                line("ok", "members may not set --rc-image/--reg-token/--bind "
                           "(gui.create_policy admin)",
                     elsewhere="which fields the New workspace form offers")
            else:
                line("ok", "members may set --rc-image/--reg-token/--bind — narrow "
                           "it with `rc-repro config set gui.create_policy admin`",
                     elsewhere="which fields the New workspace form offers")
        for path in (usersvc.users_file(), sessionsvc.sessions_file(),
                     config.home() / "audit.log"):
            if path.exists() and (path.stat().st_mode & 0o077):
                line("warn", f"{path} is readable by other local users "
                             f"(mode {oct(path.stat().st_mode)[-3:]}); it should be 0600")
        home = config.home()
        if home.exists() and (home.stat().st_mode & 0o077):
            line("warn", f"{home} is 0{oct(home.stat().st_mode)[-3:]}, so another "
                         "local user can read the accounts and sessions inside it "
                         "— `serve` tightens it to 0700 at startup")
        # A state file written by a NEWER rc-repro must not be half-understood.
        marker = "# rc-repro-state: "
        try:
            for ln in sessionsvc.sessions_file().read_text().splitlines():
                if ln.startswith(marker):
                    seen = int(ln[len(marker):].strip())
                    if seen > sessionsvc.STATE_VERSION:
                        line("fail", f"{sessionsvc.sessions_file()} was written by a "
                                     f"newer rc-repro (state v{seen}, this reads "
                                     f"v{sessionsvc.STATE_VERSION}) — upgrade rather "
                                     "than run against it")
                    break
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - a check must never break the report
        line("warn", "Identity status could not be determined")

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
