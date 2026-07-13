"""rc-repro command-line interface (Typer)."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import NoReturn, Optional

import typer

from rc_repro import compose, config, presets, rcapi, runner, versions
from rc_repro import seed as seeder

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Launch version-matched Rocket.Chat reproduction environments.",
)

_NAME_RE = re.compile(r"[^a-z0-9-]+")


# --- helpers ------------------------------------------------------------------


def _err(msg: str) -> NoReturn:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


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
    return rcapi.login(meta.root_url, mailpit_url=meta.extra.get("mailpit_url"))


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


# --- commands -----------------------------------------------------------------


@app.command()
def up(
    version: str = typer.Option(..., "--version", "-v", help="Rocket.Chat version, e.g. 6.5.3"),
    preset: str = typer.Option("default", "--preset", "-p", help="preset to apply"),
    name: str = typer.Option("", "--name", "-n", help="repro name (default: derived)"),
    port: int = typer.Option(0, "--port", help="host port (default: first free >= 3000)"),
    root_url: str = typer.Option("", "--root-url", help="override ROOT_URL"),
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
) -> None:
    """Create and start a version-matched Rocket.Chat repro."""
    _require_docker()

    try:
        resolved = versions.resolve(version, offline=offline)
    except ValueError as exc:
        _err(str(exc))
    if rc_image:
        resolved.rc_image = rc_image
    if mongo:
        versions.apply_mongo_override(resolved, mongo)

    params: dict[str, str] = {}
    for item in set_ or []:
        if "=" not in item:
            _err(f"--set expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()
    try:
        pre = presets.load(preset, params)
    except ValueError as exc:
        _err(str(exc))

    # Post-ready preset actions (e.g. Keycloak SAML) and --seed both need RC to
    # be serving first, so imply --wait for them.
    if (pre.post_ready or seed) and not wait:
        wait = True
        typer.echo("(waiting for readiness — preset self-config / --seed run after boot)")

    repro_name = _sanitize(name) if name else _derive_name(version, preset)

    cfg = config.load_config()
    # Idempotent: an existing repro (unless --fresh/--force recreates it) is brought
    # back up with `docker compose up -d`. That handles both cases: a `stop`-paused
    # repro (containers exist) is started, and a `down`ed repro (containers removed,
    # volume kept) is recreated with its data intact. `start` alone can't do the
    # latter -- "no container to start".
    if runner.exists(repro_name) and not force and not fresh:
        state = runner.rc_state(repro_name)
        if state == "running":
            typer.echo(f"{repro_name!r} is already running.")
        else:
            typer.echo(f"{repro_name!r} already exists — bringing it back up.")
            if runner.up(repro_name, pull=False) != 0:
                _err("`docker compose up` failed (see output above)")
        meta = runner.read_meta(repro_name)
        _post_up(meta, wait)
        if seed:
            _run_seed(meta, seed_profile)
        return

    # Multi-instance repros need a contiguous block: the load balancer + one
    # port per instance (host_port+1..+N for direct access).
    if port:
        host_port = port
    elif pre.instances > 1:
        host_port = runner.pick_port_range(pre.instances + 1)
    else:
        host_port = runner.pick_port()
    root = root_url or f"http://localhost:{host_port}"
    token = reg_token or cfg.get("reg_token") or ""

    spec = compose.Spec(
        project_name=runner.project_name(repro_name),
        rc_image=resolved.rc_image,
        rc_tag=resolved.rc_version,
        mongo_tag=resolved.mongo_tag,
        mongo_flavor=resolved.mongo_flavor,
        mongo_shell=resolved.mongo_shell,
        oplog=resolved.oplog,
        root_url=root,
        host_port=host_port,
        reg_token=token or None,
        preset=pre,
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
    runner.write(repro_name, compose.to_yaml(doc), meta, files=pre.files)

    if pin:
        cfg["default_repro"] = repro_name
        config.save_config(cfg)

    typer.echo(f"Reproduction {repro_name!r}")
    typer.echo(f"  Rocket.Chat : {resolved.rc_image}:{resolved.rc_version}")
    typer.echo(
        f"  MongoDB     : {resolved.mongo_tag} ({resolved.mongo_flavor}) via {resolved.source}"
    )
    typer.echo(f"  Preset      : {pre.name} ({pre.source})")
    typer.echo(f"  URL         : {root}")
    if pre.requires_license and not token:
        typer.secho(
            "  note        : this preset needs an Enterprise license — pass --reg-token.",
            fg=typer.colors.YELLOW,
        )
    typer.echo("")

    if fresh and runner.exists(repro_name):
        runner.down(repro_name, volumes=True)
    rc = runner.up(repro_name, pull=not no_pull)
    if rc != 0:
        _err("`docker compose up` failed (see output above)")

    _post_up(meta, wait)
    if seed:
        _run_seed(meta, seed_profile)


def _run_seed(meta: runner.Metadata, profile: str,
              users=None, channels=None, messages=None) -> None:
    try:
        auth = _login(meta)
    except Exception as exc:  # noqa: BLE001
        _err(f"can't seed — repro not ready (`rc-repro ready --name {meta.name}`): {exc}")
    plan = seeder.plan_from(profile, users, channels, messages)
    typer.echo(
        f"Seeding {meta.name!r} (profile: {profile} — {plan.users} users, "
        f"{plan.channels} channels, {plan.messages} msgs/channel)…"
    )
    s = seeder.seed(meta.root_url, auth, plan, log=lambda m: typer.echo(f"  {m}"))
    typer.secho(
        f"✓ seeded: {s['users']} users, {s['channels']} channels, "
        f"~{s['messages']} messages, {s['dms']} DMs",
        fg=typer.colors.GREEN,
    )


def _post_up(meta: runner.Metadata, wait: bool) -> None:
    if wait:
        _do_ready(meta)
    else:
        typer.secho(f"✓ {meta.name!r} starting.", fg=typer.colors.GREEN)
        typer.echo(f"  {meta.root_url}  (admin / {config.ADMIN_PASSWORD})")
        typer.echo(f"  wait until ready: rc-repro ready --name {meta.name}")
        typer.echo(f"  follow logs:      rc-repro logs --name {meta.name} -f")
        _print_workspace(meta)   # the wait path prints it via _do_ready

    notes = meta.extra.get("notes")
    if notes:
        typer.echo("")
        for line in notes:
            typer.secho(line, fg=typer.colors.CYAN)


def _print_workspace(meta: runner.Metadata) -> None:
    """For a multi-instance repro, print the load-balanced workspace URL plus the
    direct URL of each instance (host_port+i). No-op for single-instance repros."""
    n = meta.extra.get("instances")
    if not n:
        return
    typer.echo("")
    typer.secho(f"Multi-instance ({n} instances, load-balanced by Traefik):", fg=typer.colors.CYAN)
    typer.echo(f"  Workspace URL (open this) : {meta.root_url}")
    for i in range(1, int(n) + 1):
        typer.echo(f"    rocketchat-{i} (direct)   : http://localhost:{meta.host_port + i}")


@app.command()
def ready(
    name: str = typer.Option("", "--name", "-n"),
    timeout: float = typer.Option(300.0, "--timeout", help="seconds to wait"),
) -> None:
    """Block until Rocket.Chat is serving (polls /api/info)."""
    _require_docker()
    target = _resolve_name(name)
    _do_ready(runner.read_meta(target), timeout=timeout)


def _do_ready(meta: runner.Metadata, timeout: float = 300.0) -> None:
    typer.echo(f"Waiting for {meta.name!r} to serve {meta.root_url} ...")

    def is_alive() -> bool:
        return runner.rc_state(meta.name) in ("running", "restarting")

    def tick(elapsed: float) -> None:
        typer.echo(f"  ... still booting ({int(elapsed)}s)")

    try:
        info = rcapi.wait_ready(
            meta.root_url, timeout=timeout, is_alive=is_alive, on_tick=tick
        )
    except rcapi.NotReady as exc:
        _err(str(exc))

    # Finalize: skip the setup wizard's cloud-registration step so the repro is
    # usable immediately. Best-effort (custom-admin presets / 2FA may block it).
    auth = None
    try:
        auth = _login(meta)
        if rcapi.complete_setup_wizard(meta.root_url, auth, config.ADMIN_PASSWORD):
            typer.echo("  setup wizard skipped — no registration needed.")
    except Exception:  # noqa: BLE001 - finalize is best-effort
        pass

    # Preset post-ready actions (e.g. fetch Keycloak's SAML signing cert and
    # apply it to RC, so there's no manual cert exchange).
    if auth is not None:
        for action in meta.extra.get("post_ready", []):
            if action.get("action") == "saml_idp_cert":
                typer.echo("  fetching IdP cert (Keycloak first boot can take ~30s)...")
                cert = rcapi.fetch_saml_idp_cert(action["descriptor_url"])
                if cert and rcapi.set_setting(
                    meta.root_url, auth, config.ADMIN_PASSWORD, action["setting"], cert
                ):
                    # Reload the SAML provider so the login button registers: RC
                    # rejected it at boot (empty cert), so toggle the enable flag
                    # now that the cert is present.
                    enable = action.get("enable_setting")
                    if enable:
                        rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, enable, False)
                        time.sleep(1)
                        rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, enable, True)
                    typer.echo("  ✓ IdP cert applied; SAML login button registered.")
                else:
                    typer.secho(
                        "  ⚠ could not fetch/apply IdP cert (is the IdP up?)",
                        fg=typer.colors.YELLOW,
                    )
            elif action.get("action") == "keycloak_master_ssl_off":
                # The admin console authenticates against the master realm, which
                # defaults to sslRequired=external and rejects HTTP via the docker
                # port-forward. Relax it so the console is reachable over HTTP.
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
            elif action.get("action") == "create_oauth_provider":
                # Custom OAuth providers can't be configured via OVERWRITE env
                # (their settings don't exist until created) — create, then set.
                if rcapi.add_oauth_service(meta.root_url, auth, config.ADMIN_PASSWORD, action["name"]):
                    for sid, val in action["settings"].items():
                        rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, sid, val)
                    typer.echo("  ✓ OIDC provider created; login button registered.")
                else:
                    typer.secho("  ⚠ could not create the OAuth provider", fg=typer.colors.YELLOW)

    running = info.get("version", "?")
    typer.secho(f"✓ ready — Rocket.Chat {running} at {meta.root_url}", fg=typer.colors.GREEN)
    _print_workspace(meta)
    # The public /api/info redacts the patch (returns only major.minor), so
    # treat the running version as a prefix of the requested one.
    if running != "?" and not meta.rc_version.startswith(running):
        typer.secho(
            f"  note: running version {running} != requested {meta.rc_version}",
            fg=typer.colors.YELLOW,
        )


def _clear_default_if(name: str) -> None:
    cfg = config.load_config()
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
    runner.down(target, volumes=volumes)
    if volumes:
        # Data is gone, so there's nothing to bring back — forget it entirely.
        runner.remove(target)
        _clear_default_if(target)
        typer.secho(f"✓ {target!r} removed (containers, data volume, and record).", fg=typer.colors.GREEN)
    else:
        typer.secho(f"✓ {target!r} down (data kept).", fg=typer.colors.GREEN)
        typer.echo(f"  bring it back: rc-repro up --version <same> --name {target}")
        typer.echo("  delete for good: add --volumes, or run `rc-repro prune`")


@app.command()
def prune() -> None:
    """Delete all down repros (kept volumes + records). Skips pinned and running ones."""
    _require_docker()
    states = runner.project_states()
    removed = []
    for m in runner.list_meta():
        if m.pinned:
            continue
        # Only sweep repros whose containers are already gone (a plain `down`).
        # Running or `stop`-paused repros still appear in project_states.
        if m.project not in states:
            runner.down(m.name, volumes=True)
            runner.remove(m.name)
            _clear_default_if(m.name)
            removed.append(m.name)
    if removed:
        typer.secho(f"✓ pruned {len(removed)}: {', '.join(removed)}", fg=typer.colors.GREEN)
    else:
        typer.echo("Nothing to prune.")


@app.command()
def start(name: str = typer.Option("", "--name", "-n")) -> None:
    """Resume a stopped repro (fast, no rebuild)."""
    _require_docker()
    target = _resolve_name(name)
    runner.start(target)
    typer.secho(f"✓ {target!r} started.", fg=typer.colors.GREEN)


@app.command()
def stop(name: str = typer.Option("", "--name", "-n")) -> None:
    """Pause a repro, keeping its containers and data."""
    _require_docker()
    target = _resolve_name(name)
    runner.stop(target)
    typer.secho(f"✓ {target!r} stopped (resume with `rc-repro start`).", fg=typer.colors.GREEN)


@app.command()
def restart(name: str = typer.Option("", "--name", "-n")) -> None:
    """Restart a repro."""
    _require_docker()
    target = _resolve_name(name)
    runner.restart(target)
    typer.secho(f"✓ {target!r} restarted.", fg=typer.colors.GREEN)


@app.command()
def use(name: str = typer.Argument(..., help="repro to make the default")) -> None:
    """Set the default repro for name-less commands."""
    if not runner.exists(name):
        _err(f"no repro named {name!r}")
    cfg = config.load_config()
    cfg["default_repro"] = name
    config.save_config(cfg)
    typer.secho(f"✓ default repro is now {name!r}.", fg=typer.colors.GREEN)


@app.command(name="list")
def list_cmd() -> None:
    """List all repros with version, port, status and URL."""
    metas = runner.list_meta()
    if not metas:
        typer.echo("No repros yet. Create one with `rc-repro up --version <X.Y.Z>`.")
        return
    default = config.load_config().get("default_repro")
    docker_up = runner.docker_available()
    states = runner.project_states() if docker_up else {}
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
    typer.echo(f"Repro   : {m.name}  (RC {m.rc_version}, mongo {m.mongo_tag}/{m.mongo_flavor})")
    typer.echo(f"URL     : {m.root_url}")
    typer.echo(f"Admin   : {config.ADMIN_USERNAME} / {config.ADMIN_PASSWORD}")
    typer.echo(f"Preset  : {m.preset}")
    _print_workspace(m)
    typer.echo("")
    typer.echo("Example API call:")
    typer.echo(f"  rc-repro api --name {m.name} GET /api/v1/me")
    typer.echo(f"  curl {m.root_url}/api/info")
    notes = m.extra.get("notes")
    if notes:
        typer.echo("")
        for line in notes:
            typer.secho(line, fg=typer.colors.CYAN)


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
    except rcapi.requests.RequestException as exc:
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
    for p in presets.list_presets():
        ee = "  [needs license]" if p.requires_license else ""
        typer.echo(f"{p.name:<14} {p.description.strip()}{ee}")
        for key, help_text in p.params_help.items():
            typer.echo(f"{'':<14}   --set {key}=…  {help_text}")


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
        r = rcapi.requests.get("https://releases.rocket.chat/8.5.1/info", timeout=5)
        if r.status_code == 200:
            line("ok", "releases.rocket.chat reachable (live version lookup available)")
        else:
            line("warn", "releases.rocket.chat returned non-200 — use `--offline` if needed")
    except rcapi.requests.RequestException:
        line("warn", "releases.rocket.chat unreachable — use `--offline` (falls back to shipped map)")

    # Ports.
    free = runner.pick_port()
    if runner.port_free(3000):
        line("ok", f"Port 3000 free (repros auto-pick from 3000; next free: {free})")
    else:
        line("warn", f"Port 3000 in use — `up` will auto-pick the next free port ({free})")

    # Repro summary.
    metas = runner.list_meta()
    if docker_up and metas:
        states = runner.project_states()
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


def main() -> None:  # pragma: no cover - console entry indirection
    app()


if __name__ == "__main__":
    sys.exit(app())
