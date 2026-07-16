"""rc-repro command-line interface (Typer)."""

from __future__ import annotations

import json
import re
import shutil
import sys
import textwrap
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import typer

from rc_repro import compose, config, presets, rcapi, runner, ui, versions
from rc_repro import seed as seeder

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Launch version-matched Rocket.Chat reproduction environments.",
)

_NAME_RE = re.compile(r"[^a-z0-9-]+")


# --- helpers ------------------------------------------------------------------


_err = ui.die  # error-exit (red on stderr + exit 1), kept under the local name


def _sanitize(name: str) -> str:
    name = name.lower().replace(".", "-")
    name = _NAME_RE.sub("-", name)
    return name.strip("-")


def _derive_name(version: str, preset: str) -> str:
    base = "rc" + version
    if preset and preset != "default":
        base += "-" + preset
    return _sanitize(base)


def _resolve_name(name: str | None) -> str:
    """Return the target repro name: explicit, else the configured default."""
    if name:
        if not runner.exists(name):
            _err(f"no repro named {name!r} (run `rc-repro list`)")
        return name
    default = config.load_config().get("default_repro")
    if not default:
        _err("no --name given and no default repro set (use `rc-repro use <name>`)")
    if not runner.exists(default):
        _err(f"default repro {default!r} no longer exists; set another with `rc-repro use`")
    return default


def _require_docker() -> None:
    if not runner.docker_available():
        _err("Docker isn't running. Start Docker Desktop and try again.")


def _login(meta: runner.Metadata) -> rcapi.Auth:
    """Admin login for a repro. Passes the repro's Mailpit URL (email preset)
    so rcapi can satisfy an email-2FA challenge automatically."""
    return rcapi.login(meta.root_url, mailpit_url=meta.extra.get(config.EXTRA_MAILPIT_URL))


def _check_sidecar_ports(pre: presets.Preset, exclude: str = "") -> None:
    """Preset side services publish fixed host ports (config.PRESET_PORTS) —
    error early, naming the owner, instead of a cryptic docker bind failure.
    `exclude` is the repro being (re)created: its own claims don't count (a
    --force/--fresh recreate tears the old stack down first)."""
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
            # The claim lives in the repro's RECORD (survives a plain `down`),
            # so the remedy must delete the record, not just the containers.
            _err(
                f"preset {pre.name!r} publishes port(s) {overlap}, already claimed by "
                f"repro {m.name!r} — delete it first: rc-repro down --name {m.name} --volumes"
            )
    for p in sorted(wanted - own):
        if not runner.port_free(p):
            _err(f"preset {pre.name!r} needs host port {p}, which is already in use on this machine")


def _check_monitor_ports(exclude: str = "") -> None:
    """Preflight the Prometheus/Grafana ports for --monitor / attach."""
    wanted = set(config.MONITOR_PORTS)
    own: set[int] = set()
    for m in runner.list_meta():
        claimed = set(m.extra.get("monitoring_ports") or []) if isinstance(m.extra, dict) else set()
        if m.name == exclude:
            own = claimed
            continue
        overlap = sorted(claimed & wanted)
        if overlap:
            _err(f"monitoring needs port(s) {overlap}, already used by repro {m.name!r} "
                 f"(its monitoring) — stop it first: rc-repro monitor --name {m.name} --off")
    for p in sorted(wanted - own):
        if not runner.port_free(p):
            _err(f"monitoring needs host port {p}, which is already in use on this machine")


def _pretty_state(status: str) -> str:
    """Friendly label from a `docker compose ls` status.

    Status aggregates all services, e.g. 'exited(1), running(3)' — the official
    mongo flavor always has an exited one-shot mongo-init, so check for ANY
    running container first rather than the leading token.
    """
    if not status:
        return "down"           # no containers -> a plain `down`
    if "running(" in status:
        return "running"
    if "exited(" in status:
        return "stopped"        # `stop`-paused (all containers exited)
    return status.split("(")[0]


def _parse_set_params(set_: list[str] | None) -> dict[str, str]:
    """Parse repeated `--set KEY=VALUE` options into a preset params dict."""
    params: dict[str, str] = {}
    for item in set_ or []:
        if "=" not in item:
            _err(f"--set expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()
    return params


def _unknown_params(params: dict, pre: presets.Preset) -> list[str]:
    """`--set` keys the preset doesn't accept (typos like `agent` for `agents`
    were silently ignored before). Known keys = the preset's params_help."""
    return sorted(set(params) - set(pre.params_help))


def _reuse_existing(repro_name: str, wait: bool, seed: bool, seed_profile: str,
                    monitor: bool = False) -> None:
    """The idempotent `up` path for an existing repro: `docker compose up -d`
    handles both a `stop`-paused repro (containers exist -> started) and a
    `down`ed one (containers removed, volume kept -> recreated with its data).
    `start` alone can't do the latter — "no container to start"."""
    state = runner.rc_state(repro_name)
    if state == "running":
        typer.echo(f"{repro_name!r} is already running.")
    else:
        typer.echo(f"{repro_name!r} already exists — bringing it back up.")
        if runner.up(repro_name, pull=False) != 0:
            _err("`docker compose up` failed (see output above)")
    typer.echo("  (creation flags like --set/--bind/--port are ignored on an existing repro; --force recreates)")
    if monitor:
        ui.hint(f"  add monitoring to this running repro: rc-repro monitor --name {repro_name}")
    meta = runner.read_meta(repro_name)
    _post_up(meta, wait)
    if seed:
        _run_seed(meta, seed_profile)


def _own_ports(name: str) -> set[int]:
    """All host ports an existing repro's record claims (RC + instance block +
    sidecars) — subtracted when validating a --force/--fresh recreate of it."""
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


def _pick_host_port(port: int, pre: presets.Preset, exclude: str = "") -> int:
    """Explicit --port is validated (whole block for multi-instance) against
    other repros' claims and the host; else first free >= 3000. `exclude` is a
    repro being recreated — its own claims/bindings don't count (torn down
    before launch)."""
    span = pre.instances + 1 if pre.instances > 1 else 1
    if port:
        if port + span - 1 > runner.PORT_MAX:
            _err(f"--port {port}: a {pre.instances}-instance repro needs ports up to {port + span - 1} (past 65535)")
        own = _own_ports(exclude)
        used = runner.used_ports() - own
        for p in range(port, port + span):
            if p in used:
                _err(f"--port {port}: port {p} is already claimed by another repro (see `rc-repro list`)")
            if p not in own and not runner.port_free(p):
                _err(f"--port {port}: port {p} is already in use on this machine")
        return port
    try:
        return runner.pick_port_range(span) if span > 1 else runner.pick_port()
    except RuntimeError as exc:
        _err(str(exc))


def _print_plan(repro_name: str, resolved, pre: presets.Preset, root: str, token: str) -> None:
    # Compact one-liner before the (possibly slow) image pull; the full summary
    # panel is shown once the repro is ready.
    typer.echo(
        f"Creating {repro_name!r} — RC {resolved.rc_version}, "
        f"Mongo {resolved.mongo_tag} ({resolved.mongo_flavor}), preset {pre.name}…"
    )
    if pre.requires_license and not token:
        ui.warn("  note: this preset needs an Enterprise license — pass --reg-token.")


def _fmt_duration(secs: int) -> str:
    """Human duration: 42s, 1m03s."""
    return f"{secs}s" if secs < 60 else f"{secs // 60}m{secs % 60:02d}s"


# Map the non-ASCII punctuation that shows up in preset descriptions to ASCII —
# em/en dashes, ellipsis, curly quotes, arrows etc. are East-Asian "ambiguous"
# width and render double-wide in some terminals, breaking box alignment.
_ASCII_MAP = str.maketrans({
    "—": "-", "–": "-", "…": "...", "’": "'", "‘": "'",
    "“": '"', "”": '"', "→": "->", "←": "<-", "·": "-", " ": " ",
})


def _ascii(text: str) -> str:
    return text.translate(_ASCII_MAP)


def _summary_panel(meta: runner.Metadata, extra_rows: list[tuple[str, str]] | None = None) -> None:
    """The boxed repro summary (URL + login + versions), shared by up/ready/info,
    followed by multi-instance URLs. Title is the repro name only — kept pure
    ASCII so box-drawing alignment can't be thrown off by wide/emoji glyphs
    (status like "✓ ready" is printed on its own line by the caller)."""
    rows = [
        ("Rocket.Chat", meta.rc_version),
        ("MongoDB", f"{meta.mongo_tag} ({meta.mongo_flavor})"),
        ("Preset", meta.preset),
        ("URL", meta.root_url),
        ("Login", f"{config.ADMIN_USERNAME} / {config.ADMIN_PASSWORD}"),
    ]
    rows += extra_rows or []
    ui.panel(meta.name, rows)
    n = meta.extra.get("instances")
    if n:
        ui.hint(f"  instances ({n}, load-balanced by Traefik):")
        for i in range(1, int(n) + 1):
            ui.hint(f"    rocketchat-{i}: http://localhost:{meta.host_port + i}")


# --- commands -----------------------------------------------------------------


@app.command()
def up(
    version: str = typer.Option(..., "--version", "-v", help="Rocket.Chat version, e.g. 6.5.3"),
    preset: str = typer.Option("default", "--preset", "-p", help="preset to apply"),
    name: str = typer.Option("", "--name", "-n", help="repro name (default: derived)"),
    port: int = typer.Option(0, "--port", help="host port (default: first free >= 3000)"),
    root_url: str = typer.Option("", "--root-url", help="override ROOT_URL"),
    bind: str = typer.Option("", "--bind", help="host interface for published ports (default 127.0.0.1 — local only). 0.0.0.0 exposes RC AND sidecars with well-known credentials to your whole network — use deliberately"),
    rc_image: str = typer.Option("", "--rc-image", help="override the RC image repo"),
    mongo: str = typer.Option("", "--mongo", help="override the resolved MongoDB tag"),
    reg_token: str = typer.Option("", "--reg-token", help="cloud registration token (EE license)"),
    set_: list[str] = typer.Option(None, "--set", help="preset parameter KEY=VALUE (repeatable), e.g. --set users=5"),
    seed: bool = typer.Option(False, "--seed", help="populate with sample users/channels/messages after boot"),
    seed_profile: str = typer.Option("small", "--seed-profile", help="seed size: small | standard | large"),
    pin: bool = typer.Option(False, "--pin", help="mark persistent + set as default"),
    wait: bool = typer.Option(False, "--wait", help="block until RC is serving"),
    offline: bool = typer.Option(False, "--offline", help="skip the live version lookup"),
    no_pull: bool = typer.Option(False, "--no-pull", help="don't pull images first"),
    fresh: bool = typer.Option(False, "--fresh", help="wipe this repro's volume first"),
    force: bool = typer.Option(False, "--force", help="overwrite an existing repro"),
    monitor: bool = typer.Option(False, "--monitor", help="also add Prometheus + Grafana (RC metrics dashboard)"),
) -> None:
    """Create and start a version-matched Rocket.Chat repro."""
    _require_docker()
    cfg = config.load_config()

    try:
        resolved = versions.resolve(version, offline=offline)
    except ValueError as exc:
        _err(str(exc))
    # Image override precedence: --rc-image flag > config/env (RC_REPRO_RC_IMAGE).
    if rc_image or cfg.get("rc_image"):
        resolved.rc_image = rc_image or cfg["rc_image"]
    if mongo:
        versions.apply_mongo_override(resolved, mongo)

    params = _parse_set_params(set_)
    try:
        pre = presets.load(preset, params)
    except ValueError as exc:
        _err(str(exc))
    unknown = _unknown_params(params, pre)
    if unknown:
        valid = ", ".join(sorted(pre.params_help)) or "(this preset takes no --set params)"
        _err(f"unknown --set param(s) for preset {preset!r}: {', '.join(unknown)} — valid: {valid}")

    # Post-ready preset actions (e.g. Keycloak SAML) and --seed both need RC to
    # be serving first, so imply --wait for them.
    if (pre.post_ready or seed) and not wait:
        wait = True
        typer.echo("(waiting for readiness — preset self-config / --seed run after boot)")

    repro_name = _sanitize(name) if name else _derive_name(version, preset)
    if not repro_name:
        _err(f"--name {name!r} contains no usable characters (want a-z, 0-9, '-')")
    if port and not (1024 <= port <= 65535):
        _err(f"--port {port} is out of range (want 1024-65535)")

    # Idempotent: an existing repro (unless --fresh/--force recreates it) is
    # simply brought back up with its data intact.
    if runner.exists(repro_name) and not force and not fresh:
        _reuse_existing(repro_name, wait, seed, seed_profile, monitor=monitor)
        return

    _check_sidecar_ports(pre, exclude=repro_name)
    if monitor:
        _check_monitor_ports(exclude=repro_name)
    host_port = _pick_host_port(port, pre, exclude=repro_name)
    root = root_url or f"http://localhost:{host_port}"
    token = reg_token or cfg.get("reg_token") or ""
    # Bind precedence: --bind flag > config/env (RC_REPRO_BIND_HOST) > loopback.
    bind_host = bind or cfg.get("bind_host") or config.DEFAULT_BIND_HOST

    spec = compose.Spec.from_resolved(
        resolved,
        project_name=runner.project_name(repro_name),
        root_url=root,
        host_port=host_port,
        reg_token=token or None,
        preset=pre,
        bind_host=bind_host,
        monitoring=monitor,
    )
    doc = compose.build(spec)

    meta = runner.Metadata(
        name=repro_name,
        project=spec.project_name,
        rc_version=resolved.rc_version,
        rc_image=resolved.rc_image,
        mongo_tag=resolved.mongo_tag,
        mongo_flavor=resolved.mongo_flavor,
        preset=pre.name,
        root_url=root,
        host_port=host_port,
        version_source=resolved.source,
        pinned=pin,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
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
    if monitor:
        from rc_repro import monitoring
        targets = compose.rc_service_names(pre.instances)
        files += monitoring.files(targets)
        meta.extra["monitoring"] = True
        meta.extra["monitoring_ports"] = list(config.MONITOR_PORTS)
        meta.extra.setdefault("notes", [])
        meta.extra["notes"] = list(meta.extra["notes"]) + monitoring.notes()

    # Recreate (--force/--fresh): tear the OLD release down BEFORE overwriting
    # its compose file — the old file still describes the running services, so
    # a preset-shape change can't leave orphan containers behind.
    if runner.exists(repro_name):
        if runner.down(repro_name, volumes=fresh) != 0:
            _err(f"could not tear down the existing {repro_name!r} (see output above); not overwriting it")

    runner.write(repro_name, compose.to_yaml(doc), meta, files=files)

    if pin:
        # Persist into the FILE only — never the env-merged view (with_env
        # would write e.g. RC_REPRO_REG_TOKEN into config.yaml).
        raw = config.load_config(with_env=False)
        raw["default_repro"] = repro_name
        config.save_config(raw)

    _print_plan(repro_name, resolved, pre, root, token)

    rc = runner.up(repro_name, pull=not no_pull)
    if rc != 0:
        _err(
            "`docker compose up` failed (see output above). The workspace is kept "
            f"for inspection — retry with `rc-repro up ... --name {repro_name} --force`, "
            f"or discard it: rc-repro down --name {repro_name} --volumes"
        )

    _post_up(meta, wait)
    if seed:
        _run_seed(meta, seed_profile)


def _run_seed(meta: runner.Metadata, profile: str,
              users=None, channels=None, messages=None) -> None:
    try:
        auth = _login(meta)
    except Exception as exc:  # noqa: BLE001
        _err(f"can't seed — repro not ready (`rc-repro ready --name {meta.name}`): {exc}")
    try:
        plan = seeder.plan_from(profile, users, channels, messages)
    except ValueError as exc:
        _err(str(exc))
    typer.echo(
        f"Seeding {meta.name!r} (profile: {profile} — {plan.users} users, "
        f"{plan.channels} channels, {plan.messages} msgs/channel)…"
    )
    s = seeder.seed(meta.root_url, auth, plan, log=lambda m: typer.echo(f"  {m}"))
    ui.ok(
        f"✓ seeded: {s['users']} users, {s['channels']} channels, "
        f"~{s['messages']} messages, {s['dms']} DMs"
    )


def _post_up(meta: runner.Metadata, wait: bool) -> None:
    if wait:
        _do_ready(meta)
    else:
        ui.ok("✓ starting")
        _summary_panel(meta)
        ui.hint(f"  ready when serving : rc-repro ready --name {meta.name}")
        ui.hint(f"  follow logs        : rc-repro logs --name {meta.name} -f")
    _print_notes(meta)


def _print_notes(meta: runner.Metadata) -> None:
    notes = meta.extra.get("notes")
    if not notes:
        return
    inner = min(shutil.get_terminal_size((90, 24)).columns, 88) - 4
    lines: list[str] = []
    for n in notes:
        n = _ascii(n)
        lead = len(n) - len(n.lstrip())               # keep a note's own indent
        lines += textwrap.wrap(n, width=inner, subsequent_indent=" " * (lead + 2)) or [""]
    typer.echo("")
    ui.box("notes", lines, inner, title_color=typer.colors.CYAN)


@app.command()
def ready(
    name: str = typer.Option("", "--name", "-n"),
    timeout: float = typer.Option(300.0, "--timeout", help="seconds to wait"),
) -> None:
    """Block until Rocket.Chat is serving (polls /api/info)."""
    _require_docker()
    target = _resolve_name(name)
    _do_ready(runner.read_meta(target), timeout=timeout)


def _wait_serving(meta: runner.Metadata, timeout: float) -> dict:
    """Poll /api/info until RC serves (fail fast if the container died)."""
    typer.echo(f"Waiting for {meta.name!r} to serve {meta.root_url} ...")

    def is_alive() -> bool:
        return runner.rc_state(meta.name) in ("running", "restarting")

    def tick(elapsed: float) -> None:
        typer.echo(f"  ... still booting ({int(elapsed)}s)")

    try:
        return rcapi.wait_ready(
            meta.root_url, timeout=timeout, is_alive=is_alive, on_tick=tick
        )
    except rcapi.NotReady as exc:
        _err(str(exc))


def _finalize(meta: runner.Metadata):
    """Skip the setup wizard's cloud-registration step so the repro is usable
    immediately. Best-effort (custom-admin presets / 2FA may block it); returns
    the admin auth for post-ready actions, or None."""
    try:
        auth = _login(meta)
        if rcapi.complete_setup_wizard(meta.root_url, auth, config.ADMIN_PASSWORD):
            typer.echo("  setup wizard skipped — no registration needed.")
        return auth
    except Exception:  # noqa: BLE001 - finalize is best-effort
        return None


# --- post-ready actions: presets self-configure once RC is serving -------------


def _pr_saml_idp_cert(meta: runner.Metadata, auth: rcapi.Auth, action: dict) -> None:
    """Fetch Keycloak's SAML signing cert and apply it to RC — no manual exchange."""
    typer.echo("  fetching IdP cert (Keycloak first boot can take ~30s)...")
    cert = rcapi.fetch_saml_idp_cert(action["descriptor_url"])
    if cert and rcapi.set_setting(
        meta.root_url, auth, config.ADMIN_PASSWORD, action["setting"], cert
    ):
        # Reload the SAML provider so the login button registers: RC rejected it
        # at boot (empty cert), so toggle the enable flag now the cert is present.
        enable = action.get("enable_setting")
        if enable:
            rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, enable, False)
            time.sleep(1)
            rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, enable, True)
        typer.echo("  ✓ IdP cert applied; SAML login button registered.")
    else:
        ui.warn("  ⚠ could not fetch/apply IdP cert (is the IdP up?)")


def _pr_keycloak_master_ssl_off(meta: runner.Metadata, auth: rcapi.Auth, action: dict) -> None:
    """The admin console authenticates against the master realm, which defaults
    to sslRequired=external and rejects HTTP via the docker port-forward. Relax
    it so the console is reachable over HTTP."""
    svc = action.get("service", "keycloak")
    port = action.get("port", 8080)   # Keycloak's internal HTTP port
    kcadm = "/opt/keycloak/bin/kcadm.sh"
    script = (
        f'{kcadm} config credentials --server http://localhost:{port} '
        f'--realm master --user admin --password admin >/dev/null && '
        f'{kcadm} update realms/master -s sslRequired=NONE'
    )
    if runner.compose_exec(meta.name, svc, ["bash", "-lc", script]) == 0:
        typer.echo("  ✓ Keycloak admin console enabled over HTTP.")


def _pr_create_oauth_provider(meta: runner.Metadata, auth: rcapi.Auth, action: dict) -> None:
    """Custom OAuth providers can't be configured via OVERWRITE env (their
    settings don't exist until created) — create, then set."""
    if rcapi.add_oauth_service(meta.root_url, auth, config.ADMIN_PASSWORD, action["name"]):
        for sid, val in action["settings"].items():
            rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, sid, val)
        typer.echo("  ✓ OIDC provider created; login button registered.")
    else:
        ui.warn("  ⚠ could not create the OAuth provider")


def _pr_livechat_setup(meta: runner.Metadata, auth: rcapi.Auth, action: dict) -> None:
    """Full Omnichannel setup: make admin (+ agent1..N) available agents, create a
    department and assign them all to it. Canned responses / business hours are
    Enterprise-only — attempted best-effort, noted if the license isn't present."""
    url, pw = meta.root_url, config.ADMIN_PASSWORD
    # 1. Agents: admin always, plus agent1..N.
    agents = [{"agentId": auth.user_id, "username": config.ADMIN_USERNAME}]
    rcapi.add_livechat_agent(url, auth, pw, config.ADMIN_USERNAME)
    for i in range(2, int(action.get("agents", 1)) + 1):
        u = f"agent{i}"
        rcapi.create_user(url, auth, pw, u)
        rcapi.add_livechat_agent(url, auth, pw, u)
        uid = rcapi.get_user_id(url, auth, u)
        if uid:
            agents.append({"agentId": uid, "username": u})
    available = rcapi.set_livechat_available(url, auth, pw)

    # 2. Department + assign every agent to it.
    dept, dept_ok = action.get("department"), False
    if dept:
        dept_id = rcapi.ensure_livechat_department(url, auth, pw, dept)
        if dept_id:
            dept_ok = rcapi.assign_livechat_agents(url, auth, pw, dept_id, agents)

    # 3. Canned response (Enterprise — best effort).
    canned = rcapi.save_canned_response(url, auth, pw, "hello",
                                        "Hi! Thanks for reaching out — how can I help?")

    if available:
        summary = f"  ✓ Omnichannel: {len(agents)} agent(s) available"
        if dept_ok:
            summary += f", '{dept}' department created + assigned"
        typer.echo(summary + " — log into RC to go online.")
    else:
        ui.warn("  ⚠ set up the Omnichannel agent manually (Admin → Omnichannel → Agents)")
    if not canned:
        ui.note("  (canned responses & business hours are Enterprise features — pass "
                "--reg-token to enable, else set them up manually)")


_POST_READY_ACTIONS = {
    "saml_idp_cert": _pr_saml_idp_cert,
    "keycloak_master_ssl_off": _pr_keycloak_master_ssl_off,
    "create_oauth_provider": _pr_create_oauth_provider,
    "livechat_setup": _pr_livechat_setup,
}


def _run_post_ready(meta: runner.Metadata, auth) -> None:
    if auth is None:
        return
    for action in meta.extra.get("post_ready", []):
        handler = _POST_READY_ACTIONS.get(action.get("action"))
        if handler:
            handler(meta, auth, action)


def _do_ready(meta: runner.Metadata, timeout: float = 300.0) -> None:
    started = time.monotonic()
    info = _wait_serving(meta, timeout)
    elapsed = int(time.monotonic() - started)   # time to actually serve /api/info
    auth = _finalize(meta)
    _run_post_ready(meta, auth)

    ui.ok("✓ ready")
    _summary_panel(meta, extra_rows=[("Booted in", _fmt_duration(elapsed))])
    ui.hint(f"  next: rc-repro logs --name {meta.name} -f")
    # The public /api/info redacts the patch (returns only major.minor), so
    # treat the running version as a prefix of the requested one.
    running = info.get("version", "?")
    if running != "?" and not meta.rc_version.startswith(running):
        ui.warn(f"  note: running version {running} != requested {meta.rc_version}")


def _clear_default_if(name: str) -> None:
    cfg = config.load_config(with_env=False)   # read-modify-WRITE: file only
    if cfg.get("default_repro") == name:
        cfg.pop("default_repro", None)
        config.save_config(cfg)


@app.command()
def down(
    name: str = typer.Option("", "--name", "-n"),
    volumes: bool = typer.Option(False, "--volumes", help="also delete the data volume and forget the repro"),
) -> None:
    """Remove a repro's containers. Keeps data (and the record) unless --volumes."""
    _require_docker()
    target = _resolve_name(name)
    if runner.down(target, volumes=volumes) != 0:
        _err(f"`docker compose down` failed for {target!r} (see output above)")
    if volumes:
        # Data is gone, so there's nothing to bring back — forget it entirely.
        runner.remove(target)
        _clear_default_if(target)
        ui.ok(f"✓ {target!r} removed (containers, data volume, and record).")
    else:
        ui.ok(f"✓ {target!r} down (data kept).")
        typer.echo(f"  bring it back: rc-repro up --version <same> --name {target}")
        typer.echo("  delete for good: add --volumes, or run `rc-repro prune`")


def _detect_bind(doc: dict) -> str:
    """Read the host bind interface from an existing published port (host:hp:cp)."""
    for svc in doc.get("services", {}).values():
        for p in svc.get("ports", []):
            parts = str(p).split(":")
            if len(parts) == 3:
                return parts[0]
    return config.DEFAULT_BIND_HOST


def _rc_services_in(doc: dict) -> list[str]:
    return [s for s in doc.get("services", {}) if s == "rocketchat" or s.startswith("rocketchat-")]


@app.command()
def monitor(
    name: str = typer.Option("", "--name", "-n"),
    off: bool = typer.Option(False, "--off", help="detach: remove Prometheus + Grafana"),
) -> None:
    """Attach (or --off to detach) Prometheus + Grafana on a running repro."""
    _require_docker()
    from rc_repro import monitoring
    m = runner.read_meta(_resolve_name(name))
    doc = runner.read_compose(m.name)

    if off:
        rcapi_ok = False
        try:
            auth = _login(m)
            rcapi_ok = rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
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
        ui.ok(f"✓ monitoring detached from {m.name!r}"
              + ("" if rcapi_ok else " (metrics setting left as-is — repro not reachable)"))
        return

    # Attach.
    _check_monitor_ports(exclude=m.name)
    # Enable RC metrics live via the API (persists in Mongo; no RC restart).
    try:
        auth = _login(m)
        if not rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD, monitoring.RC_METRICS_SETTING, True):
            ui.warn("  ⚠ could not enable RC metrics via the API (is it ready?)")
    except Exception as exc:  # noqa: BLE001
        _err(f"repro not reachable to enable metrics (`rc-repro ready --name {m.name}` first): {exc}")

    mon = monitoring.bind_ports(monitoring.services(), _detect_bind(doc))
    doc.setdefault("services", {}).update(mon)
    doc.setdefault("volumes", {}).update(monitoring.volumes())

    m.extra["monitoring"] = True
    m.extra["monitoring_ports"] = list(config.MONITOR_PORTS)
    notes = [n for n in m.extra.get("notes", []) if n not in monitoring.notes()] + monitoring.notes()
    m.extra["notes"] = notes
    targets = _rc_services_in(doc) or ["rocketchat"]
    runner.write(m.name, compose.to_yaml(doc), m, files=monitoring.files(targets))

    if runner.up(m.name, pull=True) != 0:   # starts prometheus+grafana; RC unchanged -> not recreated
        _err("`docker compose up` failed bringing up monitoring (see output above)")
    ui.ok(f"✓ monitoring attached to {m.name!r}")
    typer.echo("")
    for line in monitoring.notes():
        ui.note(line)


@app.command()
def prune() -> None:
    """Delete all down repros (kept volumes + records). Skips pinned and running ones."""
    _require_docker()
    states = runner.project_states()
    if states is None:
        # Can't tell "no containers" from "docker didn't answer" — deleting
        # volumes on that ambiguity would be destructive. Refuse.
        _err("couldn't query docker compose projects — not pruning (is Docker healthy?)")
    removed = []
    for m in runner.list_meta():
        if m.pinned:
            continue
        # Only sweep repros whose containers are already gone (a plain `down`).
        # Running or `stop`-paused repros still appear in project_states.
        if m.project not in states:
            if runner.down(m.name, volumes=True) != 0:
                ui.warn(f"⚠ could not clean up {m.name!r} — skipping")
                continue
            runner.remove(m.name)
            _clear_default_if(m.name)
            removed.append(m.name)
    if removed:
        ui.ok(f"✓ pruned {len(removed)}: {', '.join(removed)}")
    else:
        typer.echo("Nothing to prune.")


@app.command()
def start(name: str = typer.Option("", "--name", "-n")) -> None:
    """Resume a stopped repro (fast, no rebuild)."""
    _require_docker()
    target = _resolve_name(name)
    if runner.start(target) != 0:
        _err(f"could not start {target!r} — if it was `down`ed, use `rc-repro up` to recreate it")
    ui.ok(f"✓ {target!r} started.")


@app.command()
def stop(name: str = typer.Option("", "--name", "-n")) -> None:
    """Pause a repro, keeping its containers and data."""
    _require_docker()
    target = _resolve_name(name)
    if runner.stop(target) != 0:
        _err(f"`docker compose stop` failed for {target!r} (see output above)")
    ui.ok(f"✓ {target!r} stopped (resume with `rc-repro start`).")


@app.command()
def restart(name: str = typer.Option("", "--name", "-n")) -> None:
    """Restart a repro."""
    _require_docker()
    target = _resolve_name(name)
    if runner.restart(target) != 0:
        _err(f"`docker compose restart` failed for {target!r} (see output above)")
    ui.ok(f"✓ {target!r} restarted.")


@app.command()
def use(name: str = typer.Argument(..., help="repro to make the default")) -> None:
    """Set the default repro for name-less commands."""
    if not runner.exists(name):
        _err(f"no repro named {name!r}")
    cfg = config.load_config(with_env=False)   # read-modify-WRITE: file only
    cfg["default_repro"] = name
    config.save_config(cfg)
    ui.ok(f"✓ default repro is now {name!r}.")


@app.command(name="list")
def list_cmd() -> None:
    """List all repros with version, port, status and URL."""
    metas = runner.list_meta()
    if not metas:
        typer.echo("No repros yet. Create one with `rc-repro up --version <X.Y.Z>`.")
        return
    default = config.load_config().get("default_repro")
    docker_up = runner.docker_available()
    states = (runner.project_states() or {}) if docker_up else {}   # None -> unknown
    header = f"{'NAME':<20} {'RC':<9} {'MONGO':<7} {'PORT':<6} {'STATE':<10} URL"
    typer.echo(header)
    for m in metas:
        state = "?" if not docker_up else _pretty_state(states.get(m.project, ""))
        flag = "*" if m.name == default else (" " if not m.pinned else "·")
        typer.echo(
            f"{flag}{m.name:<19} {m.rc_version:<9} {m.mongo_tag:<7} "
            f"{m.host_port:<6} {state:<10} {m.root_url}"
        )
    typer.echo("\n* = default repro   · = pinned")


@app.command()
def info(name: str = typer.Option("", "--name", "-n")) -> None:
    """Show a repro's URL, admin credentials and a curl snippet."""
    target = _resolve_name(name)
    m = runner.read_meta(target)
    _summary_panel(m)
    ui.hint(f"  api  : rc-repro api --name {m.name} GET /api/v1/me")
    ui.hint(f"  curl : {m.root_url}/api/info")
    _print_notes(m)


@app.command()
def token(name: str = typer.Option("", "--name", "-n")) -> None:
    """Mint an API auth token (X-Auth-Token / X-User-Id headers)."""
    _require_docker()
    m = runner.read_meta(_resolve_name(name))
    try:
        auth = _login(m)
    except Exception as exc:  # noqa: BLE001 - surface any auth/connection failure
        _err(f"could not log in (is it ready? `rc-repro ready --name {m.name}`): {exc}")
    typer.echo(f'-H "X-Auth-Token: {auth.token}" -H "X-User-Id: {auth.user_id}"')


@app.command()
def api(
    method: str = typer.Argument(..., help="HTTP method, e.g. GET / POST"),
    path: str = typer.Argument(..., help="API path, e.g. /api/v1/users.update"),
    name: str = typer.Option("", "--name", "-n"),
    data: str = typer.Option("", "--data", "-d", help="JSON request body (for POST/PUT)"),
    pat: bool = typer.Option(False, "--pat", help="auth with a bypass-2FA Personal Access Token (like a customer script)"),
    two_fa: bool = typer.Option(False, "--2fa", help="add the admin password-2FA header (to pass 2FA-guarded endpoints)"),
) -> None:
    """Make an authenticated REST call against a repro and print the response.

    Examples:
      rc-repro api GET  /api/v1/me
      rc-repro api POST /api/v1/users.update --pat -d '{"userId":"ID","data":{"name":"X"}}'
      rc-repro api POST /api/v1/users.update --2fa -d '{"userId":"ID","data":{"name":"X"}}'
    """
    _require_docker()
    m = runner.read_meta(_resolve_name(name))
    try:
        auth = _login(m)
        if pat:
            token = rcapi.generate_pat(m.root_url, auth, config.ADMIN_PASSWORD, bypass_2fa=True)
            auth = rcapi.Auth(token=token, user_id=auth.user_id)  # use the PAT as the auth token
    except Exception as exc:  # noqa: BLE001
        _err(f"could not authenticate (ready? `rc-repro ready --name {m.name}`): {exc}")

    try:
        body = json.loads(data) if data else None
    except json.JSONDecodeError as exc:
        _err(f"--data is not valid JSON: {exc}")

    extra = rcapi.password_2fa_headers(config.ADMIN_PASSWORD) if two_fa else None
    try:
        status, text = rcapi.call(m.root_url, method, path, auth=auth, data=body, extra_headers=extra)
    except requests.RequestException as exc:
        _err(f"request failed: {exc}")
    tag = "PAT" if pat else "admin"
    if two_fa:
        tag += "+2fa"
    typer.secho(f"HTTP {status}  [{tag}]", fg=typer.colors.GREEN if status < 400 else typer.colors.RED)
    typer.echo(text)


@app.command()
def pat(
    name: str = typer.Option("", "--name", "-n"),
    label: str = typer.Option("rc-repro", "--label", help="token name shown in the admin UI"),
    bypass_2fa: bool = typer.Option(True, "--bypass-2fa/--no-bypass-2fa", help='create with "Ignore Two Factor Authentication"'),
) -> None:
    """Mint a Personal Access Token and print ready-to-use headers (curl/Postman)."""
    _require_docker()
    m = runner.read_meta(_resolve_name(name))
    try:
        auth = _login(m)
        token = rcapi.generate_pat(m.root_url, auth, config.ADMIN_PASSWORD, token_name=label, bypass_2fa=bypass_2fa)
    except Exception as exc:  # noqa: BLE001
        _err(f"could not create PAT (ready? `rc-repro ready --name {m.name}`): {exc}")
    typer.echo(f"# Personal Access Token for {m.name} ({m.root_url}) — bypass_2fa={bypass_2fa}")
    typer.echo(f'-H "X-Auth-Token: {token}" -H "X-User-Id: {auth.user_id}"')


@app.command(name="seed")
def seed_cmd(
    name: str = typer.Option("", "--name", "-n"),
    profile: str = typer.Option("small", "--profile", help="small | standard | large"),
    users: Optional[int] = typer.Option(None, "--users", help="override user count"),
    channels: Optional[int] = typer.Option(None, "--channels", help="override channel count"),
    messages: Optional[int] = typer.Option(None, "--messages", help="override messages per channel"),
) -> None:
    """Populate a repro with sample users, channels, DMs and messages."""
    _require_docker()
    m = runner.read_meta(_resolve_name(name))
    _run_seed(m, profile, users, channels, messages)


@app.command()
def logs(
    name: str = typer.Option("", "--name", "-n"),
    follow: bool = typer.Option(False, "--follow", "-f", help="stream logs"),
    tail: int = typer.Option(0, "--tail", help="only the last N lines (0 = all)"),
) -> None:
    """Tail a repro's logs."""
    _require_docker()
    target = _resolve_name(name)
    runner.logs(target, follow=follow, tail=tail or None)


@app.command(name="presets")
def presets_cmd() -> None:
    """List available presets."""
    items = presets.list_presets()
    inner = min(shutil.get_terminal_size((90, 24)).columns, 88) - 4   # box content width
    typer.secho("Presets", bold=True)
    typer.echo("")
    for p in items:
        lines = textwrap.wrap(_ascii(" ".join(p.description.split())), width=inner) or [""]
        if p.params_help:
            lines.append("")
            key_w = max(len(k) for k in p.params_help)
            for key, help_text in p.params_help.items():
                entry = f"--set {key.ljust(key_w)}   {_ascii(' '.join(help_text.split()))}"
                cont = " " * (len("--set ") + key_w + 3)   # hang-indent wrapped help
                lines += textwrap.wrap(entry, width=inner, subsequent_indent=cont)
        title = p.name + ("  [needs license]" if p.requires_license else "")
        ui.box(title, lines, inner)
        typer.echo("")
    ui.hint("run: rc-repro up --version <X.Y.Z> --preset <name> [--set key=value]")


@app.command(name="versions")
def versions_cmd(
    version: str = typer.Argument(..., help="Rocket.Chat version, e.g. 7.4.1"),
    offline: bool = typer.Option(False, "--offline"),
) -> None:
    """Show the resolved MongoDB pairing for a Rocket.Chat version."""
    try:
        r = versions.resolve(version, offline=offline)
    except ValueError as exc:
        _err(str(exc))
    typer.echo(f"Rocket.Chat {r.rc_version}")
    typer.echo(f"  image        : {r.rc_image}:{r.rc_version}")
    typer.echo(f"  mongo        : {r.mongo_tag}  (flavor: {r.mongo_flavor}, shell: {r.mongo_shell})")
    typer.echo(f"  oplog url    : {'yes' if r.oplog else 'no (deprecated in 8.x)'}")
    typer.echo(f"  resolved via : {r.source}")
    if r.note:
        typer.echo(f"  note         : {r.note}")


@app.command()
def doctor() -> None:
    """Preflight: check Docker, Compose, disk, connectivity and ports."""
    import shutil

    counts = {"ok": 0, "warn": 0, "fail": 0}
    marks = {
        "ok": ("✓", typer.colors.GREEN),
        "warn": ("⚠", typer.colors.YELLOW),
        "fail": ("✗", typer.colors.RED),
    }

    def line(status: str, msg: str) -> None:
        counts[status] += 1
        sym, color = marks[status]
        typer.secho(f"{sym} {msg}", fg=color)

    # Docker daemon (everything else that needs Docker degrades gracefully).
    docker_up = runner.docker_available()
    if docker_up:
        line("ok", f"Docker daemon running ({runner.docker_server_version() or '?'})")
    else:
        line("fail", "Docker daemon not running — start Docker Desktop / dockerd")

    # docker compose v2
    cv = runner.compose_version()
    if cv and cv.lstrip("v")[:1] == "2":
        line("ok", f"docker compose v2 ({cv})")
    elif cv:
        line("warn", f"docker compose {cv} — rc-repro expects Compose v2")
    else:
        line("warn", "couldn't detect `docker compose` — install Compose v2")

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

    # Repro summary.
    metas = runner.list_meta()
    if docker_up and metas:
        states = runner.project_states() or {}
        running = sum(1 for m in metas if _pretty_state(states.get(m.project, "")) == "running")
        typer.echo(f"  repros: {len(metas)} total, {running} running")

    typer.echo("")
    if counts["fail"]:
        typer.secho("Not ready — fix the ✗ item(s) above.", fg=typer.colors.RED)
        raise typer.Exit(1)
    if counts["warn"]:
        typer.secho("Usable, with warnings above.", fg=typer.colors.YELLOW)
    else:
        typer.secho("All good — rc-repro is ready.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    sys.exit(app())
