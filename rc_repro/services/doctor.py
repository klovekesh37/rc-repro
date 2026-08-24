"""Preflight environment checks, shared by `rc-repro doctor` and the web GUI.

Extracted from cli.py so both front-ends run the IDENTICAL checks: the CLI
colours and prints the rows, the API serves them as JSON. Checks never raise --
each one degrades to a warn row -- because the whole point is to run when the
environment is broken.
"""

from __future__ import annotations

import os
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


#: Every check id `run_checks` can report, and the published half of
#: `doctor --json`. A caller watches an ID; the message beside it is prose and may
#: be reworded in any release.
#:
#: One id per SUBJECT, not per outcome -- `ports` covers "3000 is free" and "3000 is
#: in use" alike, because those are two states of one question and a caller watching
#: for the second must not have to learn a different key for it. Deriving the id
#: from the message instead, which is the obvious shortcut, gives `port-3000-free`
#: when it passes and `port-3000-in` when it does not: two ids for one check, and a
#: watcher that never sees the state it was watching for.
CHECKS: tuple[str, ...] = (
    "preflight",                    # the report itself could not be assembled
    "docker", "compose", "engine-kernel", "hub-auth",
    "disk", "memory", "swap",
    "version-lookup", "ports",
    "edge", "edge-routes-stale", "edge-routes-unreachable",
    "gui-accounts", "gui-admin", "gui-roles", "gui-create-policy",
    "state-file-perms", "home-perms", "home-writable", "sessions-file", "identity",
    "kubernetes", "kubernetes-tools", "kubernetes-cluster",
    "kubernetes-storage", "kubernetes-ingress", "kubernetes-loadbalancer",
    "kubernetes-metrics", "kubernetes-other-clusters", "inotify",
    "kubernetes-cert-manager", "kubernetes-edge-port", "kubernetes-chart-repo",
    "install-fresh", "edge-config-drift", "preset-browser-host",
    "interrupted-work", "event-log",
)


def run_checks() -> dict:
    """Run every preflight check and return the findings.

    {"checks": [{"status": "ok"|"warn"|"fail", "message": str}, ...],
     "counts": {...}, "verdict": "ok"|"warn"|"fail",
     "repros": {"total": int, "running": int} | None}
    """
    rows: list[dict] = []
    # The check a row is ABOUT, carried per block rather than repeated at each of
    # the fifty-two call sites. Two properties follow, and both are the point: a
    # `line()` added inside a block cannot forget its id, and every status of one
    # check shares an id, so an agent watching `ports` sees the ok and the warn on
    # one key. Reassigned as the report moves from subject to subject; `check=`
    # overrides it where one block asks more than one question.
    subject = "preflight"

    def line(status: str, msg: str, elsewhere: str = "", check: str = "") -> None:
        """`elsewhere` names a place the GUI shows this fact PERMANENTLY.

        The web report drops those rows (see the /api/doctor endpoint). A terminal
        has no header chip, no user list and no workspace rail, so `rc-repro
        doctor` is the only place a CLI user learns them and they stay; in the
        browser they were a report restating what was already on the screen.
        Only ever set on an `ok` row -- a WARNING about the same subject is not
        something the chip beside it says, and those all still appear.
        """
        cid = check or subject
        # Declared, not free-form: these are published by `doctor --json` as a
        # contract, and a typo would be a check that silently stops being watchable.
        #
        # NOT AN `assert`. Asserts are stripped under `python -O`, so the guard would
        # vanish exactly where a mis-declared id becomes unnoticeable -- and unstripped
        # it raised AssertionError out of `GET /api/doctor`, which is a 500 and directly
        # contradicts this function's own rule that a check must never break the report.
        # Falls back to the `preflight` id, which exists for "the report itself could
        # not be assembled", and says so in the row rather than in a traceback.
        if cid not in CHECKS:
            rows.append({"check": "preflight", "status": "warn",
                         "message": f"internal: check id {cid!r} is not in "
                                    f"doctor.CHECKS, so this row is unwatchable — "
                                    f"{msg}"})
            return
        row = {"check": cid, "status": status, "message": msg}
        if elsewhere:
            row["elsewhere"] = elsewhere
        rows.append(row)

    subject = "docker"
    # Docker daemon (everything else that needs Docker degrades gracefully).
    # max_age=0: the dashboard poll memoises this, but somebody running `doctor`
    # has explicitly asked whether Docker is up RIGHT NOW -- answering that from a
    # cache is how a diagnostic tells you the opposite of the truth.
    docker_up = runner.docker_available(max_age=0)
    if docker_up:
        line("ok", f"Docker daemon running ({runner.docker_server_version() or '?'})")
    else:
        line("fail", "Docker daemon not running — start Docker Desktop / dockerd")

    subject = "compose"
    # docker compose v2 or newer
    cv = runner.compose_version()
    cv_major = _major_version(cv)
    if cv_major is not None and cv_major >= 2:
        line("ok", f"docker compose v{cv_major} ({cv})")
    elif cv:
        line("warn", f"docker compose {cv} — rc-repro needs Compose v2 or newer")
    else:
        line("warn", "couldn't detect `docker compose` — install Compose v2 or newer")

    subject = "engine-kernel"
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

    subject = "hub-auth"
    # Docker Hub auth: anonymous pulls hit Hub's rate limit (registry.rocket.chat
    # counts against Hub too), which shows up as a silent, container-less `down`.
    hub = runner.hub_logged_in()
    if hub is True:
        line("ok", "logged in to Docker Hub (avoids anonymous pull-rate limits)")
    elif hub is False:
        line("warn", "not logged in to Docker Hub — anonymous pulls can hit the rate "
                     "limit; run `docker login`. registry.rocket.chat counts against Hub too")

    subject = "disk"
    # Disk headroom (RC images are ~1.5 GB each).
    try:
        free_gb = shutil.disk_usage(config.home().parent).free / 1e9
        if free_gb >= 10:
            line("ok", f"Disk: {free_gb:.0f} GB free")
        else:
            line("warn", f"Disk: only {free_gb:.0f} GB free — images are ~1.5 GB each")
    except OSError:
        line("warn", "couldn't check disk space")

    subject = "memory"
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
                         "slowdown, and the kernel picks its own victim", check="swap")

    subject = "version-lookup"
    # Live version lookup reachability.
    try:
        r = requests.get("https://releases.rocket.chat/8.5.1/info", timeout=5)
        if r.status_code == 200:
            line("ok", "releases.rocket.chat reachable (live version lookup available)")
        else:
            line("warn", "releases.rocket.chat returned non-200 — use `--offline` if needed")
    except requests.RequestException:
        line("warn", "releases.rocket.chat unreachable — use `--offline` (falls back to shipped map)")

    subject = "ports"
    # Ports.
    try:
        free = runner.pick_port()
        if runner.port_free(3000):
            line("ok", f"Port 3000 free (repros auto-pick from 3000; next free: {free})")
        else:
            line("warn", f"Port 3000 in use — `up` will auto-pick the next free port ({free})")
    except RuntimeError as exc:   # bounded scan found nothing bindable
        line("fail", str(exc))

    subject = "install-fresh"
    # WHICH BUILD IS ACTUALLY RUNNING. `__version__` comes from the INSTALLED
    # distribution metadata, so a stale editable install or a pipx that was never
    # refreshed reports an old number while the checkout beside it holds the fix.
    # Three shipped fixes looked ineffective for exactly this reason, and nothing in
    # the tool said which build was answering.
    try:
        from pathlib import Path

        import rc_repro as _pkg

        src = Path(_pkg.__file__).resolve().parent.parent / "pyproject.toml"
        if src.is_file():
            found = re.search(r'^version = "([^"]+)"', src.read_text(encoding="utf-8"), re.M)
            checkout = found.group(1) if found else ""
            if checkout and checkout != _pkg.__version__:
                line("warn", f"running rc-repro {_pkg.__version__}, but the checkout at "
                             f"{src.parent} is {checkout} — the installed metadata is "
                             f"stale, so a fix in that tree is NOT what is answering. "
                             f"`pip install -e .` (or `pipx reinstall`) to catch up")
            else:
                line("ok", f"rc-repro {_pkg.__version__}, matching the checkout at "
                           f"{src.parent}")
        else:
            # A packaged install with no source beside it: nothing to disagree with,
            # and saying the version is still the useful half of the answer.
            line("ok", f"rc-repro {_pkg.__version__} (installed, no checkout alongside)")
    except Exception:  # noqa: BLE001 - a check must never break the report
        line("warn", "could not determine which rc-repro build is running")

    subject = "edge"
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
            if docker_up and edgesvc.running():
                # CHANGED ON DISK AND NOT APPLIED. The container keeps the command
                # line it was created with, so a rewritten compose file reaches
                # nothing until the edge is recreated -- and every other message
                # went on saying the edge was fine. `edge start` re-applies now;
                # this is what says the two had diverged in the first place.
                stale_cfg, live_only = edgesvc.config_drift()
                if stale_cfg or live_only:
                    shown = ", ".join((stale_cfg + live_only)[:3])
                    line("warn", f"the edge's configuration on disk and the running "
                                 f"container disagree ({shown}"
                                 f"{'…' if len(stale_cfg + live_only) > 3 else ''}). "
                                 f"Whatever was changed has not been applied — "
                                 f"`rc-repro edge restart`",
                         check="edge-config-drift")
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

    subject = "identity"
    # Identity: who can sign in, and whether the files that decide it are sound.
    # Nothing else reports this, and an install with no admin cannot make one from
    # the GUI -- the repair is hand-editing a file most people would not think to
    # look for.
    try:
        from rc_repro.services import sessions as sessionsvc
        from rc_repro.services import users as usersvc

        if not usersvc.any_users():
            line("warn", "No GUI accounts — `rc-repro serve` will refuse to start on "
                         "anything but loopback (`rc-repro users add <name>`)", check="gui-accounts")
        else:
            admins = usersvc.admins()
            if admins:
                line("ok", f"{len(usersvc.list_users())} GUI account(s), "
                           f"admin: {', '.join(admins)}",
                     elsewhere="the People page", check="gui-accounts")
            else:
                line("fail", "No admin account — nobody can manage people from the "
                             "GUI. `rc-repro users role <name> admin`", check="gui-admin")
            implicit = usersvc.implicit_admins()
            if implicit:
                line("warn", f"admin by default (blank role column): "
                             f"{', '.join(implicit)} — that is the migration for "
                             "accounts made before roles existed, not a choice. "
                             f"`rc-repro users role {implicit[0]} member`", check="gui-roles")
            # Reported rather than warned about: this is the default and it is a
            # deliberate one, but "what may a member actually do on this box?" should
            # be answerable without reading source. Same reason `serve` names the
            # implicit admins -- a state that is visible is a state somebody can
            # disagree with.
            from rc_repro.services import lifecycle as lcsvc
            if config.load_config().get(lcsvc.CREATE_POLICY_KEY) == "admin":
                line("ok", "members may not set --rc-image/--reg-token/--bind "
                           "(gui.create_policy admin)",
                     elsewhere="which fields the New workspace form offers", check="gui-create-policy")
            else:
                line("ok", "members may set --rc-image/--reg-token/--bind — narrow "
                           "it with `rc-repro config set gui.create_policy admin`",
                     elsewhere="which fields the New workspace form offers", check="gui-create-policy")
        # `acme/dns.env` is a live DNS-provider API TOKEN, which the README tells people
        # to chmod by hand, and `ca/ca.key` mints trusted certificates for any name on
        # this box. Both were outside this loop while audit.log was inside it.
        from rc_repro import tls as _tlsmod
        for path in (usersvc.users_file(), sessionsvc.sessions_file(),
                     config.home() / "audit.log",
                     _tlsmod.acme_dir() / "dns.env",
                     config.home() / "ca" / "ca.key"):
            if path.exists() and (path.stat().st_mode & 0o077):
                line("warn", f"{path} is readable by other local users "
                             f"(mode {oct(path.stat().st_mode)[-3:]}); it should be 0600", check="state-file-perms")
        home = config.home()
        # WRITABLE, WHICH IS A DIFFERENT QUESTION FROM 0700. `home-perms` asks whether
        # other local users can READ the accounts and sessions; nothing asked whether
        # rc-repro can WRITE. On a home it cannot, `list` and `doctor` both exit 0
        # while `up` and `users add` raise a bare PermissionError -- and doctor's only
        # hint was "Could not tell whether cluster 'rc-repro-local' exists ([Errno 13]
        # Permission denied) - kind needs Docker", because the error surfaced through
        # the kind probe (which writes rc-repro's own kubeconfig dir) and was reported
        # as a Docker problem. The symptom was found and attributed to the wrong thing.
        #
        # Probed rather than derived from st_mode: a mode says what the bits are, and
        # a read-only mount, a full disk or a container's user mapping all deny a write
        # with the bits looking fine.
        from rc_repro.services import journal as journalsvc
        # WHERE THE HISTORY IS, and whether it is being written. A log nobody can find
        # is one nobody reads, and a log that has silently stopped writing is worse than
        # none -- so this reports the path, the size and the disabled case. Never a
        # fail: the tool works perfectly without it.
        try:
            from rc_repro.services import eventlog
            _lp = eventlog.log_path()
            if eventlog.max_bytes() <= 0:
                line("warn", "Event log disabled (RC_REPRO_LOG_MAX_MB=0) — nothing is "
                             "recording what rc-repro did", check="event-log")
            elif _lp.exists():
                _mb = _lp.stat().st_size / 1_048_576
                line("ok", f"Event log: {_lp} ({_mb:.1f} MB of "
                           f"{eventlog.max_bytes() / 1_048_576:.0f} MB, then one "
                           f"rotation)", check="event-log")
            else:
                line("ok", f"Event log: {_lp} — written from the first command that "
                           f"reports progress", check="event-log")
        except Exception:  # noqa: BLE001 - a check must never break the report
            line("warn", "Event log status could not be determined", check="event-log")
        for label, path in (("state root", home),
                            ("workspaces", home / "repros"),
                            ("locks", home / "locks"),
                            ("journal", journalsvc.journal_dir())):
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".rc-repro-write-probe"
                probe.write_text("x")
                probe.unlink()
            except OSError as exc:
                line("fail", f"{path} is not writable ({exc.strerror or exc}) — every "
                             f"command that creates or records anything will fail with "
                             f"a bare {type(exc).__name__}. rc-repro's {label}.",
                     check="home-writable")
                break
        if home.exists() and (home.stat().st_mode & 0o077):
            line("warn", f"{home} is 0{oct(home.stat().st_mode)[-3:]}, so another "
                         "local user can read the accounts and sessions inside it "
                         "— `serve` tightens it to 0700 at startup", check="home-perms")
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
                                     "than run against it", check="sessions-file")
                    break
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - a check must never break the report
        line("warn", "Identity status could not be determined")

    subject = "kubernetes"
    # --- Kubernetes -------------------------------------------------------
    # Severity depends on whether anything USES Kubernetes. A box with no cluster
    # is perfectly healthy if nobody wants one, and broken if a Kubernetes
    # workspace already exists on it -- the same finding, opposite severity. A
    # doctor that reports FAIL for a feature you are not using teaches people to
    # ignore its failures.
    #
    # kind is OPTIONAL throughout. Creating a cluster needs it; using one does
    # not, and k3s, minikube, Docker Desktop and remote clusters are all ordinary
    # Kubernetes to everything rc-repro does inside a namespace.
    try:
        from rc_repro.services import k8s, topology
        k8s_workspaces = [m.name for m in runner.list_meta()
                          if topology.of_meta(m) == topology.KUBERNETES]
        in_use = bool(k8s_workspaces)
        needed = "fail" if in_use else "warn"
        pre = k8s.preflight()

        if pre.missing_tools and not in_use:
            # One line, not five. Someone running only Compose repros should not
            # have their report padded with an unconfigured feature.
            line("ok", f"Kubernetes not set up ({', '.join(pre.missing_tools)} "
                       f"not installed) — Compose workspaces are unaffected", check="kubernetes-tools")
        else:
            if pre.missing_tools:
                line("fail", f"{len(k8s_workspaces)} Kubernetes workspace(s) exist but "
                             f"{', '.join(pre.missing_tools)} is not installed — they "
                             "cannot be reached or torn down", check="kubernetes-tools")
            for name in pre.outdated_tools:
                got = pre.tools[name]
                floor = ".".join(str(n) for n in k8s.TOOLS[name])
                line(needed, f"{name} {got.pretty} is older than {floor}, the floor "
                             "the official Rocket.Chat guide requires", check="kubernetes-tools")
            if pre.tools_ready:
                line("ok", "Kubernetes tools present (" + ", ".join(
                    f"{t.name} {t.pretty}" for t in pre.tools.values()
                    if t.present) + ")", check="kubernetes-tools")

            if pre.cluster_reachable:
                where = (f", {len(pre.namespaces)} workspace namespace(s)"
                         if pre.namespaces else "")
                # WHAT it is and what SHAPE it is, on the line that names it. The
                # distribution is a label for exactly this -- so a reader is told "k3s"
                # rather than left to infer it from a context called `default` -- and
                # the node count is what decides whether a node-local StorageClass can
                # bite.
                shape = ", ".join(
                    [f"{pre.node_count} node" + ("s" if pre.node_count != 1 else "")]
                    + ([", ".join(pre.architectures)] if pre.architectures else []))
                where = (f" ({pre.distribution or 'unknown'}, {shape})" if shape
                         else f" ({pre.distribution or 'unknown'})") + where
                if pre.provider == k8s.PROVIDER_EXTERNAL:
                    # Saying whose cluster it is, before anything is created in it.
                    # rc-repro owns the namespaces it labels and never the cluster,
                    # so `down` and `prune` will leave this cluster standing.
                    line("ok", f"Using your cluster {pre.context!r}{where} — rc-repro "
                               "creates namespaces in it and never removes the "
                               "cluster itself", check="kubernetes-cluster")
                else:
                    line("ok", f"Cluster {k8s.CLUSTER_NAME!r} reachable{where}", check="kubernetes-cluster")
                if not pre.default_storage_class:
                    # The guide's own warning, and the failure is silent: a PVC
                    # stays Pending forever and nothing names storage.
                    line(needed, f"Cluster {pre.context!r} has no default "
                                 "StorageClass — a workspace's volume would stay "
                                 "Pending with no error naming storage", check="kubernetes-storage")
                else:
                    line("ok", f"Storage: {pre.default_storage_class} (default)",
                         check="kubernetes-storage")

                # WHAT THIS CLUSTER PROVIDES, reported the same way on every
                # distribution so the difference between two of them is visible rather
                # than implied. All three are `ok` rows on purpose: a capability is a
                # FACT, and severity belongs to whatever needs it -- an absent ingress
                # controller cannot affect a workspace reached by port-forward, and
                # `ingress_blocker` is what refuses when something asks for a hostname.
                # A doctor that warns about a feature you are not using teaches people
                # to ignore its warnings.
                if pre.ingress_classes:
                    line("ok", "Ingress: " + ", ".join(pre.ingress_classes)
                         + " — a hostname can be served from this cluster",
                         check="kubernetes-ingress")
                else:
                    line("ok", "Ingress: none installed — workspaces are reached by "
                               "port-forward, which needs no controller",
                         check="kubernetes-ingress")
                if pre.loadbalancer:
                    line("ok", f"Load balancer: working — {pre.loadbalancer}",
                         check="kubernetes-loadbalancer")
                else:
                    # Not "absent": no address means either no controller or nobody
                    # asked for one, and those cannot be told apart from outside.
                    line("ok", "Load balancer: not confirmed — no LoadBalancer Service "
                               "here has an address", check="kubernetes-loadbalancer")
                if pre.metrics:
                    line("ok", "Metrics: metrics-server answering — `rc-repro stats` "
                               "works here", check="kubernetes-metrics")
                else:
                    line("ok", "Metrics: no metrics-server — `rc-repro stats` refuses "
                               "and says how to install it", check="kubernetes-metrics")
                # A FACT like the four above it, for the same reason: HTTPS on this
                # runtime needs an issuer in the cluster, and nothing else does.
                #
                # THE CHART, RESOLVED THROUGH THE HOME THE INSTALL WILL USE. `helm_env`
                # keeps rc-repro's Helm state separate, and for a while the repo was
                # added to that home while the install read the user's -- so `up` died
                # at 60% with "Error: repo rocketchat not found" on a box where `helm
                # repo list` looked fine. Asked here the same way, so the answer is
                # about the same home.
                try:
                    _repo = k8s.run(["helm", "search", "repo", k8s.CHART, "-o", "json"],
                                    env=k8s.helm_env(pre.context))
                    if _repo.returncode == 0 and (_repo.stdout or "").strip() not in ("", "[]"):
                        line("ok", f"chart {k8s.CHART} resolves in rc-repro's Helm home",
                             check="kubernetes-chart-repo")
                    else:
                        line("warn", f"chart {k8s.CHART} does not resolve in the Helm "
                                     f"home rc-repro installs from — `up` adds the repo "
                                     f"itself, so this matters only if a create fails "
                                     f"naming it", check="kubernetes-chart-repo")
                except Exception:  # noqa: BLE001 - a check must never break the report
                    pass
                if k8s.cert_manager_installed(pre.context):
                    line("ok", "cert-manager: installed — a Certificate can be issued "
                               "in this cluster", check="kubernetes-cert-manager")
                else:
                    line("ok", "cert-manager: not installed — installed on first use by "
                               "a workspace that asks for a certificate",
                         check="kubernetes-cert-manager")

            elif pre.probe_failed:
                # NOT "does not exist". `kind` talks to Docker, so this is what a
                # stopped Docker looks like -- and telling someone their cluster is
                # absent would send them to create one that is already there.
                line(needed, f"Could not tell whether cluster {k8s.CLUSTER_NAME!r} "
                             f"exists ({pre.probe_failed}) — kind needs Docker", check="kubernetes-cluster")
            elif pre.will_create:
                # Ordered BEFORE the not-answering branch, which would otherwise
                # claim a cluster nobody has made yet is broken.
                # NAMES THE BINARY THAT MAKES THE CLAIM TRUE. Reported as confusing
                # by someone who believed kind was not installed: the row promised a
                # cluster would be created and said nothing about what would create
                # it, so there was no way to tell the claim was well-founded. It
                # requires kind (`can_provision`), so saying which kind settles it.
                _kind = pre.tools.get("kind")
                _by = f" by kind {_kind.pretty}" if _kind and _kind.present else ""
                # WARN, NOT FAIL, and this is a deliberate downgrade. A record whose
                # cluster has been removed blocks nothing: the next `up` builds a fresh
                # cluster, and the stale record is a cleanup task rather than a broken
                # machine. Reported as a fail it made `doctor` answer "Not ready — fix
                # the ✗ item(s)" about a box that was completely usable, which is how a
                # verdict stops being read.
                #
                # And it says what to DO. The place to learn about a stale record is the
                # command that removes it, so the row names that command and the
                # workspaces it applies to -- the data went with the cluster either way.
                _stale = ", ".join(sorted(k8s_workspaces)[:3])
                _more = ("…" if len(k8s_workspaces) > 3 else "")
                line("warn" if in_use else "ok",
                     f"No cluster yet — {k8s.CLUSTER_NAME!r} is created on first use"
                     f"{_by}"
                     if not in_use else
                     f"Cluster {k8s.CLUSTER_NAME!r} is gone, so {len(k8s_workspaces)} "
                     f"record(s) point at nothing ({_stale}{_more}). Their data went "
                     f"with it; `rc-repro down --name <it> --volumes` clears each "
                     f"record, and the next `up` builds a fresh cluster.",
                     check="kubernetes-cluster")
                # The box that has BOTH. Saying which one is about to be used is not
                # enough on its own -- someone with a running k3s and no kind cluster
                # needs to be told their cluster was seen and set aside, or the next
                # question is why rc-repro is building a second one.
                other = k8s.active_context()
                if other and other != pre.context and k8s.reachable(other):
                    line("ok", f"Your cluster {other!r} "
                               f"({k8s.distribution(other) or 'unknown'}) is running and "
                               f"will NOT be used — rc-repro creates its own while kind "
                               f"is installed. Uninstall kind to use {other!r} instead",
                         check="kubernetes-other-clusters")
            elif pre.context:
                line(needed, f"Cluster {pre.context!r} is configured but its API "
                             "server is not answering", check="kubernetes-cluster")
            elif pre.tools_ready:
                # kubectl and helm but no kind and no kubeconfig: usable the moment
                # a cluster is pointed at, and the two ways to get one are named.
                line(needed, "No Kubernetes cluster configured — install kind so "
                             "rc-repro can create one, or point kubectl at an "
                             "existing cluster (k3s, minikube, Docker Desktop)", check="kubernetes-cluster")

        # OUTSIDE the reachable-cluster block, deliberately. This was inside it, so on a
        # box where rc-repro's own cluster does not exist yet the check never ran -- and
        # that is exactly the box where it fired for real: kind chosen and absent, k3s
        # alongside it holding the ports, the edge installed and its name dark. The
        # conflict has nothing to do with whether rc-repro has a cluster.
        #
        # Detection is `k8s.port_claiming_cluster`, which reports the ports the Service
        # actually asks for. This block used to say ":443" whatever it found, so the
        # HTTP-01 half of the same outage -- a cluster holding :80 while the challenge
        # is `http` -- read as healthy. Both candidate contexts are checked there, for
        # the reason recorded on that function.
        claim = None
        try:
            claim = k8s.port_claiming_cluster(
                contexts=tuple(c for c in (pre.context, k8s.active_context()) if c))
        except Exception:  # noqa: BLE001 - a check must not break the report
            claim = None
        if claim:
            try:
                from rc_repro.services import edge as edgesvc
                installed_edge = edgesvc.installed()
                fd = edgesvc.current() if installed_edge else None
                # The GUI's own name is served by the edge too, and on the box where
                # this was measured it was the ONLY affected name -- so counting
                # workspace routes alone would have said "0 names" about a real outage.
                harmed = (([fd.domain] if fd and fd.domain else [])
                          + list(edgesvc.registered())) if installed_edge else []
            except Exception:  # noqa: BLE001 - a check must not break the report
                installed_edge, harmed = False, []
            if installed_edge:
                what = (", ".join(harmed[:3]) + ("\u2026" if len(harmed) > 3 else "")
                        if harmed else "every name it is given")
                took = ", ".join(f":{p}" for p in claim.ports if p in (80, 443))
                # WHICH port decides which half is broken, so both are spelled out.
                # ACME on this box validates over whichever challenge is configured,
                # and each one needs a different port back.
                hurts = []
                if 443 in claim.ports:
                    hurts += ["serving https", "the tls-alpn challenge"]
                if 80 in claim.ports:
                    hurts += ["the http-01 challenge", "the :80 redirect"]
                # Guarded, not assumed. `port_claiming_cluster` only returns a claim
                # that overlaps (80, 443) today, so this list cannot be empty -- but an
                # IndexError here is swallowed by the section's except and the whole
                # Kubernetes report collapses to "could not be determined", which is
                # the failure mode that hides rather than shows.
                broken = (", ".join(hurts[:-1]) + " and " + hurts[-1] if len(hurts) > 1
                          else (hurts[0] if hurts else "the edge's ports"))
                line("warn",
                     f"cluster {claim.context!r} holds this host's {took} at "
                     f"{claim.address} ({claim.service}), so rc-repro's edge cannot "
                     f"receive there: {what} answers from the cluster through the host "
                     f"\u2014 a 404 and the cluster's own certificate \u2014 while the "
                     f"edge itself is fine and bound. This breaks "
                     f"{broken}. A box serves HTTPS for one runtime at a "
                     f"time: remove the edge on a Kubernetes box, or give the host "
                     f"ports back with `kubectl -n kube-system patch svc "
                     f"{claim.service.split('/')[-1]} -p "
                     f"'{{\"spec\":{{\"type\":\"NodePort\"}}}}'` \u2014 which the "
                     f"next k3s install undoes, so make it permanent with `--disable "
                     f"traefik --disable servicelb`",
                     check="kubernetes-edge-port")

        # "Other" must exclude the one we just said we are USING, or the report
        # describes the same cluster twice and the second mention reads as a
        # different one.
        others = [c for c in pre.other_clusters if f"kind-{c}" != pre.context]
        if others:
            # Named, not warned about. Another cluster on the box is normal and
            # none of rc-repro's business; saying so is how the boundary becomes
            # visible before somebody wonders why `prune` left it alone.
            line("ok", f"{len(others)} other kind cluster(s) on this box "
                       f"({', '.join(others)}) — rc-repro never deletes "
                       "a cluster it did not create", check="kubernetes-other-clusters")
        for row in inotify_headroom(len(others) + 1):
            line(row[0], row[1], check="inotify")
    except Exception:  # noqa: BLE001 - a check must never break the report
        line("warn", "Kubernetes status could not be determined")

    subject = "interrupted-work"
    # SIDE EFFECTS NOBODY MEANT TO LEAVE. A GUI job killed mid-operation runs no
    # `finally`, so a backup can leave Rocket.Chat stopped and a load test can leave
    # the API rate limiter off -- and `web/jobs.py` keeps its registry in memory, so
    # a restart loses even the knowledge that a job existed. The journal is the only
    # durable trace, and `serve` repairs from it at startup; this is what tells a
    # CLI-only box, which never starts one.
    try:
        from rc_repro.services import journal
        stranded = journal.abandoned()
        if stranded:
            for entry in stranded[:5]:
                # The suffix has to match what recovery will actually DO. An advisory
                # kind already names its own fix inside `describe`, and telling the
                # reader `serve` repairs it would be false -- serve reports those and
                # deliberately does not act, because finishing one means waiting for a
                # workspace to serve.
                tail = ("" if entry.kind in journal.ADVISORY
                        else " — `rc-repro serve` repairs this at startup, or fix it "
                             "by hand")
                line("warn", journal.describe(entry) + tail, check="interrupted-work")
        elif journal.open_entries():
            # Open but owned by a live process: a job is running right now, which is
            # not a fault and must not read as one.
            line("ok", f"{len(journal.open_entries())} operation(s) in progress "
                       f"(a running job holds them)", check="interrupted-work")
    except Exception:  # noqa: BLE001 - a check must never break the report
        pass

    subject = "preset-browser-host"
    # THE WORKSPACES ALREADY OUT THERE. `up` warns about this at create time now, but
    # every workspace made before that is silent: an IdP preset addresses its own
    # service as THIS machine by default, which on a shared box names each visitor's
    # own laptop -- the SAML button returns to the login page, the OIDC popup opens
    # blank, presigned previews fail, and none of them logs anything.
    _HOST_PARAM = {"saml": "idp_host", "oidc": "idp_host", "s3_minio": "s3_host"}
    try:
        for meta in runner.list_meta():
            param = _HOST_PARAM.get((meta.preset or "").strip())
            if not param:
                continue
            extra = meta.extra if isinstance(meta.extra, dict) else {}
            if (dict(extra.get("params") or {})).get(param):
                continue          # someone said where the browser is
            bind = str(extra.get("bind_host") or "")
            if bind in ("", "127.0.0.1", "localhost"):
                continue          # local-only, so localhost really is localhost
            line("warn", f"{meta.name!r} is bound to {bind} but its {meta.preset!r} "
                         f"preset still points the browser at this machine. From "
                         f"anywhere else that address is the visitor's own laptop and "
                         f"login fails silently. Re-create with "
                         f"`--set {param}=<the host people type>`",
                 check="preset-browser-host")
    except Exception:  # noqa: BLE001 - a check must never break the report
        pass

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


#: Roughly what one kind node plus the workloads on it consume. Measured, not
#: guessed: a single-node cluster running Rocket.Chat, MongoDB, NATS and a preset
#: sidecar sat around 40-60 instances, and the box below had five clusters' worth of
#: history behind it.
INOTIFY_PER_CLUSTER = 60


def inotify_in_use(proc: str = "/proc") -> int | None:
    """How many inotify instances this UID already holds, or None if unknowable.

    Counted rather than modelled, because modelling is what made this check useless:
    see `inotify_headroom`. Only fds this user may read are visible -- the limit is
    per-UID and container watchers run as root -- so a number here is a FLOOR, never
    the whole picture, and the caller must not present it as one.
    """
    try:
        uid = os.getuid()
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except (OSError, AttributeError):
        return None
    total = 0
    for pid in pids:
        base = f"{proc}/{pid}"
        try:
            if os.stat(base).st_uid != uid:
                continue
            for fd in os.listdir(f"{base}/fd"):
                try:
                    if os.readlink(f"{base}/fd/{fd}") == "anon_inode:inotify":
                        total += 1
                except OSError:
                    continue
        except OSError:
            continue                      # the process went away, or is not ours
    return total


def inotify_headroom(clusters: int = 1, path: str = "/proc/sys/fs/inotify/max_user_instances",
                     in_use: int | None = None) -> list[tuple[str, str]]:
    """Whether the kernel will let another watcher start.

    This check exists because the symptom points nowhere near the cause. With
    `fs.inotify.max_user_instances` exhausted, Traefik starts, stays up, logs
    `Cannot start the provider *file.Provider ... too many open files`, and then
    serves its OWN default certificate to every request -- which looks exactly like
    a broken route or a bad certificate, and is neither. It cost three full runs to
    find, and kind's own documentation raises these limits for multi-cluster use.

    **It also used to pass while that was happening.** `in_use` was a parameter the
    body never read: the check compared the LIMIT against a modelled
    `clusters * INOTIFY_PER_CLUSTER` and never against consumption. So on a box whose
    128 instances were already spoken for by Docker, k3s, MetalLB, cert-manager and the
    edge, it printed a green tick -- "inotify instances: 128 (~60 needed here)" -- while
    Traefik was failing with EMFILE and loading no dynamic configuration at all.
    Measured during the HTTPS work, which is the exact scenario the docstring above was
    written about. A model cannot see the other consumers; only a count can.

    So consumption is read when it can be read, and the verdict is about what is LEFT
    rather than about the ceiling. What can be read is a floor -- the limit is per-UID
    and containers run as root -- so a comfortable-looking margin is reported without
    the word that would make it a promise.

    Linux-only by construction: the file does not exist elsewhere, and a missing
    file is silence rather than a warning about a limit that does not apply.
    """
    try:
        with open(path) as fh:
            limit = int(fh.read().strip())
    except (OSError, ValueError):
        return []
    need = max(1, clusters) * INOTIFY_PER_CLUSTER
    used = inotify_in_use() if in_use is None else in_use
    free = limit if used is None else max(0, limit - used)
    seen = "" if used is None else f", {used} already in use by this user"
    # Three tiers, all on what is LEFT rather than on the ceiling. The old order asked
    # `limit >= need` after this, which meant the `fail` tier could not be reached once
    # consumption was subtracted -- a box with no room at all would have been reported
    # as merely tight.
    if free < need:
        return [("fail", f"inotify instances: {limit}{seen} — about {free} left for the "
                         f"~{need} that {max(1, clusters)} cluster(s) want. Traefik will "
                         f"start, load NO dynamic configuration and serve its own "
                         f"default certificate, which looks like a broken route rather "
                         f"than a kernel limit. `sudo sysctl -w "
                         f"fs.inotify.max_user_instances=1024`")]
    if free < need * 2:
        return [("warn", f"inotify instances: {limit}{seen} — about {free} left against "
                         f"the ~{need} wanted, which is tight. A watcher that cannot "
                         f"start makes Traefik serve its own default certificate. "
                         f"`sudo sysctl -w fs.inotify.max_user_instances=1024`")]
    return [("ok", f"inotify instances: {limit}{seen} (kind and Traefik watch files; "
                   f"~{need} needed here)")]
