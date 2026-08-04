"""rc-repro command-line interface (Typer)."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import textwrap
import time
from dataclasses import asdict as dc_asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Optional

import requests
import typer

from rc_repro import config, errors, jsonout, presets, perf, rcapi, runner, ui, versions
from rc_repro import seed as seeder
from rc_repro.perf import report as perf_report
from rc_repro.perf.timings import fmt_ms
from rc_repro.services import data as datasvc
from rc_repro.services import doctor as doctorsvc
from rc_repro.services import envvars as envsvc
from rc_repro.services import lifecycle as lcsvc
from rc_repro.services import onboarding as onboardsvc
from rc_repro.services import evidence as evidencesvc
from rc_repro.services import skill as skillsvc
from rc_repro.services import events
from rc_repro.services.events import Event, null_emit

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Launch version-matched Rocket.Chat reproduction environments.",
)

# --- helpers ------------------------------------------------------------------


_err = ui.die  # error-exit (red on stderr + exit 1), kept under the local name


def _fail(exc: errors.ReproError) -> NoReturn:
    """Exit on a domain error, using the exit code its class defines.

    One line per handler instead of a per-site mapping, so the taxonomy in
    errors.py stays the only place exit codes are decided.
    """
    ui.die(str(exc), exit_code=exc.exit_code)


def _resolve_name(name: str | None) -> str:
    """Return the target repro name: explicit, else the configured default.

    Delegates to the service so the exit code comes from the error taxonomy
    (missing repro -> 4, no default set -> 2) instead of a flat 1, and so this
    doesn't drift from the identical service-layer rule.
    """
    try:
        return lcsvc.resolve_name(name)
    except errors.ReproError as exc:
        _fail(exc)


def _require_docker() -> None:
    # Delegates so "engine down" exits 3 (preflight) rather than a flat 1. This is
    # the most common failure there is, so it is the one most worth classifying.
    try:
        lcsvc.require_docker()
    except errors.ReproError as exc:
        _fail(exc)


def _login(meta: runner.Metadata) -> rcapi.Auth:
    """Admin login for a repro. Passes the repro's Mailpit URL (email preset)
    so rcapi can satisfy an email-2FA challenge automatically."""
    return rcapi.login(meta.root_url, mailpit_url=meta.extra.get(config.EXTRA_MAILPIT_URL))


def _parse_set_params(set_: list[str] | None) -> dict[str, str]:
    """Parse repeated `--set KEY=VALUE` options into a preset params dict."""
    params: dict[str, str] = {}
    for item in set_ or []:
        if "=" not in item:
            _err(f"--set expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()
    return params


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


def _tls_label(meta: runner.Metadata) -> str:
    """How the cert was obtained, for the summary panel."""
    return {
        "local": "local CA (rc-repro trust-ca)",
        "acme": "Let's Encrypt",
        "own": "supplied certificate",
    }.get(str(meta.extra.get("tls") or ""), "enabled")


def _summary_panel(meta: runner.Metadata, extra_rows: list[tuple[str, str]] | None = None) -> None:
    """The boxed repro summary (URL + login + versions), shared by up/ready/info,
    followed by multi-instance URLs. Title is the repro name only — kept pure
    ASCII so box-drawing alignment can't be thrown off by wide/emoji glyphs
    (status like "✓ ready" is printed on its own line by the caller)."""
    rows = [
        ("Rocket.Chat", meta.rc_version),
        ("MongoDB", f"{meta.mongo_tag} ({meta.mongo_flavor})"),
        ("Preset", meta.preset),
        # external_url, not root_url: with --https the browser wants the https URL,
        # while root_url stays the plain http one rc-repro's own API calls use.
        ("URL", meta.external_url),
        ("Login", f"{config.ADMIN_USERNAME} / {config.ADMIN_PASSWORD}"),
    ]
    if meta.public_url:
        rows.append(("TLS", _tls_label(meta)))
        rows.append(("Direct HTTP", meta.root_url))
    rows += extra_rows or []
    ui.panel(meta.name, rows)
    n = meta.extra.get("instances")
    if n:
        ui.hint(f"  instances ({n}, load-balanced by Traefik):")
        for i in range(1, int(n) + 1):
            ui.hint(f"    rocketchat-{i}: http://localhost:{meta.host_port + i}")


# --- commands -----------------------------------------------------------------


def _cli_emit(ev: Event) -> None:
    """Print a service progress event on the terminal. Terminal/`done` events are
    suppressed — the command wrapper prints the final panel itself."""
    if ev.terminal or ev.phase == "done":
        return
    if ev.level in ("warn", "error"):
        ui.warn("  " + ev.message)
    else:
        typer.echo(f"  {ev.message}")


def _print_access(result: dict) -> None:
    """Show remote SSH tunnel instructions only when the session needs them."""
    access = result.get("access") if isinstance(result, dict) else None
    if not isinstance(access, dict) or access.get("mode") != "remote_ssh":
        return
    tunnel = access.get("tunnel_command")
    browser = access.get("browser_url")
    if tunnel:
        ui.hint("  remote access (repro is loopback-only on the server):")
        ui.hint(f"    tunnel : {tunnel}")
    if browser:
        ui.hint(f"    browser: {browser}")
    note = access.get("note")
    if note:
        ui.hint(f"    note   : {note}")


def _render_create_result(result: dict) -> None:
    """Format a create_repro result the way `up` used to (panel + notes + hints)."""
    meta = runner.read_meta(result["name"])
    if result.get("waited"):
        ui.ok("✓ ready")
        extra = [("Booted in", _fmt_duration(result["booted_s"]))] if result.get("booted_s") is not None else None
        _summary_panel(meta, extra_rows=extra)
        ui.hint(f"  next: rc-repro logs --name {meta.name} -f")
    else:
        ui.ok("✓ starting")
        _summary_panel(meta)
        ui.hint(f"  ready when serving : rc-repro ready --name {meta.name}")
        ui.hint(f"  follow logs        : rc-repro logs --name {meta.name} -f")
    _print_access(result)
    _print_notes(meta)


@app.command()
def up(
    version: str = typer.Option(..., "--version", "-v", help="Rocket.Chat version, e.g. 6.5.3"),
    preset: str = typer.Option("", "--preset", "-p", show_default="default",
                               help="legacy preset alias (deployment or scenario)"),
    deployment: str = typer.Option(
        "", "--deployment", "--deployment-type", "--topology",
        help="deployment preset: default | multi-instance | microservices"),
    scenario: list[str] = typer.Option(
        None, "--scenario", help="reusable scenario (repeat for a scenario set)"),
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
    stats: bool = typer.Option(False, "--stats", help="with --seed: report the CPU/RAM cost of seeding"),
    json_out: bool = typer.Option(False, "--json", help="stream NDJSON progress, then one result envelope"),
    https: bool = typer.Option(False, "--https", help="serve over HTTPS using rc-repro's own local CA — no domain needed (run `rc-repro trust-ca` once). For a real certificate, use --domain instead"),
    domain: str = typer.Option("", "--domain", help="serve over HTTPS at this hostname, e.g. rc1.example.com. Gets a Let's Encrypt certificate, or uses --tls-cert/--tls-key if given. Implies --https"),
    tls_cert: str = typer.Option("", "--tls-cert", help="use this PEM certificate chain instead of asking Let's Encrypt (needs --tls-key and --domain)"),
    tls_key: str = typer.Option("", "--tls-key", help="private key for --tls-cert"),
    acme_staging: bool = typer.Option(False, "--acme-staging", help="ask Let's Encrypt STAGING for the certificate — do this first: it proves the whole path works without spending the production quota"),
    acme_email: str = typer.Option("", "--acme-email", help="[usually not needed] Let's Encrypt contact email; remembered via `rc-repro config set acme.email`", hidden=True),
    acme_challenge: str = typer.Option("", "--acme-challenge", help="[usually not needed] force tlsalpn | dns. Inferred: dns when ~/.rc-repro/acme/dns.env exists, else tlsalpn", hidden=True),
    acme_dns_provider: str = typer.Option("", "--acme-dns-provider", help="[usually not needed] lego DNS provider name; inferred from the variables in dns.env", hidden=True),
    env: list[str] = typer.Option(None, "--env", "-e", help="extra raw env var KEY=VALUE (repeatable). Persisted, so `up --force` keeps it; change it later with `rc-repro env`"),
    setting: list[str] = typer.Option(None, "--setting", help="Rocket.Chat SETTING Id=VALUE (repeatable) — adds the OVERWRITE_SETTING_ prefix a setting needs"),
    tls_san: str = typer.Option("", "--tls-san", help="[usually not needed] with --https: extra names/IPs in the local certificate, e.g. your LAN IP", hidden=True),
) -> None:
    """Create and start a version-matched Rocket.Chat repro."""
    # Orchestration lives in the shared service layer (same code the web GUI
    # runs); this wrapper just parses options, prints progress, and formats the
    # result. --seed is applied after (with the CLI's richer --stats output), so
    # `wait` is forced on when seeding, as before.
    req = lcsvc.CreateReq(
        version=version, preset=preset, name=name, port=port, root_url=root_url,
        bind=bind, rc_image=rc_image, mongo=mongo, reg_token=reg_token,
        deployment=deployment, scenario=scenario or None,
        params=_parse_set_params(set_), seed=False, pin=pin,
        wait=(wait or seed), offline=offline, no_pull=no_pull, fresh=fresh,
        force=force, monitor=monitor, stats=stats if seed else False,
        https=https, domain=domain, tls_san=tls_san, tls_cert=tls_cert, tls_key=tls_key,
        acme_email=acme_email, acme_staging=acme_staging,
        # "" means "not given", so the service layer may infer it. tlsalpn is both
        # the default and a valid explicit choice, hence the separate flag.
        env={**envsvc.parse_set(env or []), **envsvc.as_setting(setting or [])},
        acme_challenge=(acme_challenge or "tlsalpn"),
        acme_challenge_given=bool(acme_challenge),
        acme_dns_provider=acme_dns_provider,
    )
    if json_out:
        writer = jsonout.EventWriter()
        try:
            result = lcsvc.create_repro(req, emit=writer.emit, stream_output=False)
        except errors.ReproError as exc:
            jsonout.fail(exc)
        if seed:
            # Seed through the same writer so its progress is part of one stream,
            # rather than a second burst after the envelope.
            summary = _run_seed(runner.read_meta(result["name"]), seed_profile,
                                stats=stats, emit=writer.emit)
            if summary:
                result = {**result, "seed": summary}
        jsonout.emit(jsonout.envelope("up", result))
        return
    try:
        result = lcsvc.create_repro(req, emit=_cli_emit, stream_output=False)
    except errors.ReproError as exc:
        _fail(exc)
    _render_create_result(result)
    if seed:
        _run_seed(runner.read_meta(result["name"]), seed_profile, stats=stats)


def _run_seed(meta: runner.Metadata, profile: str,
              users=None, channels=None, messages=None, stats: bool = False,
              emit=None) -> dict | None:
    """Seed a repro.

    `emit` routes progress through the service event stream instead of printing
    prose. That matters under --json: stdout is reserved for envelope and event
    objects, so a stray `typer.echo` here would corrupt the stream. When `emit` is
    given, the caller gets the seed summary back to fold into its envelope rather
    than a printed panel.

    Shared service path for the default profile (no count overrides) so CLI,
    JSON, HTTP, and GUI cannot diverge. Custom user/channel/message overrides
    remain a CLI convenience on the same REST seeder.
    """
    if stats:
        try:
            lcsvc.require_compose_topology(
                meta.name, "seed --stats",
                why="It reads container stats from the compose project; the "
                    "Kubernetes equivalent needs metrics-server.")
        except errors.ReproError as exc:
            if emit is not None:
                raise
            _fail(exc)
    # Revive a dead Kubernetes port-forward only after validating modes that
    # must refuse without changing runtime state.
    lcsvc.ensure_reachable(meta.name)
    # Default profile with no overrides: one shared lifecycle path.
    if users is None and channels is None and messages is None:
        try:
            summary = lcsvc.run_seed_inline(meta, profile, stats, emit or _cli_emit)
        except errors.ReproError as exc:
            if emit is not None:
                raise
            _fail(exc)
        if emit is not None:
            return {**summary, "elapsed_s": round(float(summary.get("total_s", 0)), 1)}
        _print_seed_result(summary, float(summary.get("total_s", 0)),
                           None, meta)
        return None
    try:
        auth = _login(meta)
    except Exception as exc:  # noqa: BLE001
        _err(f"can't seed — repro not ready (`rc-repro ready --name {meta.name}`): {exc}")
    try:
        plan = seeder.plan_from(profile, users, channels, messages)
    except ValueError as exc:
        _err(str(exc))
    headline = (f"Seeding {meta.name!r} (profile: {profile} — {plan.users} users, "
                f"{plan.channels} channels, {plan.messages} msgs/channel)…")
    if emit is not None:
        events.info(emit, headline, phase="seed")
    else:
        typer.echo(headline)
    mon = perf.ResourceMonitor(meta.name).start() if stats else None
    t0 = time.monotonic()
    if emit is not None:
        def _log(m: str) -> None:
            events.info(emit, m, phase="seed")
    else:
        def _log(m: str) -> None:
            typer.echo(f"  {m}")
    try:
        s = seeder.seed(meta.root_url, auth, plan, log=_log)
    except seeder.SeedVerificationError as exc:
        # Preserve the failed plan/readback for a later evidence capture before
        # translating the runtime failure into the CLI's exit contract.
        lcsvc.persist_seed_result(meta, exc.result)
        if emit is not None:
            raise
        _fail(exc)
    except errors.ReproError as exc:
        if emit is not None:
            raise
        _fail(exc)
    finally:
        resources = mon.stop() if mon else None   # stop the sampler thread even if seed raises
    total = time.monotonic() - t0
    lcsvc.persist_seed_result(meta, s)
    if emit is not None:
        return {**s, "elapsed_s": round(total, 1)}
    _print_seed_result(s, total, resources, meta)
    return None


def _run_scale(meta: runner.Metadata, spec_str: str) -> None:
    # Delegates to the shared service (same code the web GUI runs); the CLI
    # formats the result and prints its own headline warning.
    ui.warn("bulk Mongo prefill: users are credential-less and messages fire no "
            "app hooks — for scale/perf repros, not feature testing.")
    try:
        res = datasvc.run_scale(meta.name, spec_str, emit=null_emit)
    except errors.ReproError as exc:
        _fail(exc)
    if "users" in res:
        ui.ok(f"✓ inserted {res['users']:,} users")
    if "messages" in res:
        ui.ok(f"✓ inserted {res['messages']:,} messages into {res['room']!r}")


def _clear_scale(meta: runner.Metadata) -> None:
    try:
        res = datasvc.clear_scale(meta.name, emit=null_emit)
    except errors.ReproError as exc:
        _fail(exc)
    ui.ok(f"✓ removed {res['users']:,} scale users and {res['messages']:,} scale messages")


def _short_container(full: str, repro_name: str, keep_index: bool = False) -> str:
    """rcrepro-<name>-rocketchat-1 -> rocketchat (or rocketchat-1 if keep_index)."""
    s = full
    prefix = f"{config.PROJECT_PREFIX}{repro_name}-"
    if s.startswith(prefix):
        s = s[len(prefix):]
    return s if keep_index else re.sub(r"-\d+$", "", s)


def _short_res_map(resources: dict, repro_name: str) -> dict:
    """Short-name-keyed resource map, keeping the instance index when a base name
    repeats (multi-instance rocketchat-1/-2/-3) so no row overwrites another."""
    bases = [_short_container(k, repro_name) for k in resources]
    dup = {b for b in bases if bases.count(b) > 1}
    out = {}
    for k, v in resources.items():
        base = _short_container(k, repro_name)
        out[_short_container(k, repro_name, keep_index=True) if base in dup else base] = v
    return out


def _print_resources(report: dict, repro_name: str) -> None:
    if not report:
        return
    typer.echo("")
    ui.note("Resource cost (idle -> peak):")
    labelled = _short_res_map(report, repro_name)
    for name in sorted(labelled):
        r = labelled[name]
        mem_delta = (r.peak_mem - r.idle_mem) / 1e6
        typer.echo(
            f"  {name:<14} "
            f"CPU {r.idle_cpu:.0f}% -> {r.peak_cpu:.0f}%   "
            f"RAM {r.peak_mem/1e6:.0f} MB (+{mem_delta:.0f})"
        )


def _print_seed_result(s: dict, total: float, resources, meta: runner.Metadata) -> None:
    d = s.get("durations", {})
    lat = s.get("latency", {})
    ui.ok(f"✓ seeded in {fmt_ms(total * 1000)}")

    def row(label: str, count_num: int, dur_s: float, display: str = "", extra: str = "") -> None:
        rate = f"{count_num / dur_s:.1f}/s" if dur_s > 0.05 and count_num else ""
        typer.echo(f"  {label:<9} {(display or str(count_num)):>5}   {dur_s:4.1f}s   {rate:<8} {extra}")

    lat_str = ""
    if lat.get("count"):
        lat_str = (f"p50 {fmt_ms(lat['p50'])} · p95 {fmt_ms(lat['p95'])} · "
                   f"p99 {fmt_ms(lat['p99'])}  {s.get('latency_hist', '')}")
    row("users", s["users"], d.get("users", 0.0))
    row("channels", s["channels"], d.get("channels", 0.0))
    row("messages", s["messages"], d.get("messages", 0.0), extra=lat_str)
    row("DMs", s["dms"], d.get("dms", 0.0))
    readback = s.get("readback") or {}
    verification = s.get("verification") or {}
    if readback:
        status = "verified" if verification.get("ok") else "not verified"
        typer.echo(
            f"  readback  users {readback.get('users', '?')} · "
            f"channels {readback.get('channels', '?')} · "
            f"messages {readback.get('messages', '?')} ({status})"
        )
    _print_resources(resources or {}, meta.name)


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
    json_out: bool = typer.Option(False, "--json", help="stream NDJSON progress, then one result envelope"),
) -> None:
    """Block until Rocket.Chat is serving (polls /api/info)."""
    if json_out:
        writer = jsonout.EventWriter()
        try:
            lcsvc.require_docker()
            target = lcsvc.resolve_name(name)
            m = runner.read_meta(target)
            result = lcsvc.wait_and_finalize(m, emit=writer.emit, timeout=timeout)
        except errors.ReproError as exc:
            # NotReadyError here is exit 5: the clock ran out with the outcome
            # still unknown, which is distinct from a known-dead create (7).
            jsonout.fail(exc)
        jsonout.emit(jsonout.envelope("ready", {"name": m.name, **result}))
        return
    _require_docker()
    _target = _resolve_name(name)
    # Kubernetes reachability is a port-forward that may have died; revive it before
    # talking HTTP, or this fails for a reason unrelated to what was asked.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
    typer.echo(f"Waiting for {m.name!r} to serve {m.root_url} ...")
    try:
        result = lcsvc.wait_and_finalize(m, emit=_cli_emit, timeout=timeout)
    except errors.ReproError as exc:
        _fail(exc)
    ui.ok("✓ ready")
    _summary_panel(m, extra_rows=[("Booted in", _fmt_duration(result["booted_s"]))])
    ui.hint(f"  next: rc-repro logs --name {m.name} -f")
    _print_notes(m)


def _finalize(meta: runner.Metadata):
    """Complete the local setup wizard so the advertised admin is usable.

    Best-effort (custom-admin presets / 2FA may block it); returns the admin
    auth for post-ready actions, or None. This is not Enterprise registration.
    """
    try:
        auth = _login(meta)
        if rcapi.complete_setup_wizard(meta.root_url, auth, config.ADMIN_PASSWORD):
            typer.echo("  local setup wizard completed; admin is usable")
        return auth
    except Exception:  # noqa: BLE001 - finalize is best-effort
        return None


def _clear_default_if(name: str) -> None:
    cfg = config.load_config(with_env=False)   # read-modify-WRITE: file only
    if cfg.get("default_repro") == name:
        cfg.pop("default_repro", None)
        config.save_config(cfg)

@app.command()
def down(
    name: str = typer.Option("", "--name", "-n"),
    volumes: bool = typer.Option(False, "--volumes", help="also delete the data volume and forget the repro"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt (for scripts/CI)"),
    json_out: bool = typer.Option(False, "--json", help="emit the stable JSON result envelope"),
) -> None:
    """Remove a repro's containers. Keeps data (and the record) unless --volumes."""
    if json_out:
        writer = jsonout.EventWriter()
        try:
            target = lcsvc.resolve_name(name)
            if volumes and not yes:
                # There is nobody to prompt in JSON mode, and silently keeping the
                # volume would contradict the flag. Refuse instead of guessing.
                raise errors.ValidationError(
                    "--volumes is irreversible; pass --yes to confirm it non-interactively")
            result = lcsvc.teardown(target, volumes=volumes, confirm=True,
                                    emit=writer.emit)
        except errors.ReproError as exc:
            jsonout.fail(exc)
        jsonout.emit(jsonout.envelope(
            "down", {"name": target, "volumes_removed": bool(volumes), **(result or {})}))
        return
    target = _resolve_name(name)
    if volumes and not yes:
        # --volumes is irreversible (deletes the Mongo data + the record). Confirm.
        typer.confirm(
            f"This permanently deletes {target!r}'s data volume and record. Continue?",
            abort=True,
        )
    try:
        # confirm=True: the prompt above (or --yes) already gated it.
        lcsvc.teardown(target, volumes=volumes, confirm=True)
    except errors.ReproError as exc:
        _fail(exc)
    if volumes:
        ui.ok(f"✓ {target!r} removed (containers, data volume, and record).")
    else:
        ui.ok(f"✓ {target!r} down (data kept).")
        typer.echo(f"  bring it back: rc-repro up --version <same> --name {target}")
        typer.echo("  delete for good: add --volumes, or run `rc-repro prune`")


def _rc_services_in(doc: dict) -> list[str]:
    return [s for s in doc.get("services", {}) if s == "rocketchat" or s.startswith("rocketchat-")]


@app.command()
def monitor(
    name: str = typer.Option("", "--name", "-n"),
    off: bool = typer.Option(False, "--off", help="detach: remove Prometheus + Grafana"),
) -> None:
    """Attach (or --off to detach) Prometheus + Grafana on a running repro."""
    from rc_repro.services import monitor as monitorsvc
    target = _resolve_name(name)
    # The monitoring stack is Prometheus + Grafana as compose services; there is no
    # cluster rendering of it, so on a Kubernetes repro it would act on a compose
    # project that does not exist. Refuse with the reason rather than no-op.
    try:
        lcsvc.require_compose_topology(target, "monitor",
            "It attaches Prometheus/Grafana as compose services; there is no Kubernetes equivalent yet.")
    except errors.ReproError as exc:
        _fail(exc)
    try:
        if off:
            res = monitorsvc.detach(target, emit=_cli_emit)
            ui.ok(f"✓ monitoring detached from {res['name']!r}"
                  + ("" if res["rc_setting_reset"] else " (metrics setting left as-is — repro not reachable)"))
        else:
            res = monitorsvc.attach(target, emit=_cli_emit)
            ui.ok(f"✓ monitoring attached to {res['name']!r}")
            typer.echo("")
            for line in res["notes"]:
                ui.note(line)
    except errors.ReproError as exc:
        _fail(exc)


@app.command()
def prune(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt (for scripts/CI)"),
) -> None:
    """Delete every `down` repro and an empty rc-repro-owned Kind cluster."""
    try:
        plan = lcsvc.prune_plan()
    except errors.ReproError as exc:
        _fail(exc)
    targets = plan["targets"]
    cluster = plan["cluster"]
    if not targets and not cluster.get("prunable"):
        typer.echo("Nothing to prune.")
        return
    if not yes:
        if targets:
            typer.echo("These down repros will be deleted — containers, data volumes, and records:")
            for t in targets:
                typer.echo(f"  - {t}")
        if cluster.get("prunable"):
            typer.echo(f"The empty rc-repro-owned Kind cluster {cluster['cluster']!r} will also be deleted.")
        elif cluster.get("exists") and targets:
            typer.echo(f"The rc-repro-owned Kind cluster {cluster['cluster']!r} will also be deleted if these are its last namespaces.")
        typer.confirm("Continue?", abort=True)
    try:
        res = lcsvc.prune(confirm=True, emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    if res["removed"]:
        ui.ok(f"✓ pruned {len(res['removed'])}: {', '.join(res['removed'])}")
    if res["cluster"].get("deleted"):
        ui.ok(f"✓ deleted empty Kind cluster {res['cluster']['cluster']!r}")
    elif not res["removed"]:
        typer.echo("Nothing to prune.")


@app.command()
def start(name: str = typer.Option("", "--name", "-n")) -> None:
    """Resume a stopped repro (fast, no rebuild)."""
    target = _resolve_name(name)
    try:
        lcsvc.set_state(target, "start")
    except errors.ReproError:
        _err(f"could not start {target!r} — if it was `down`ed, use `rc-repro up` to recreate it")
    ui.ok(f"✓ {target!r} started.")


@app.command()
def stop(name: str = typer.Option("", "--name", "-n")) -> None:
    """Pause a repro, keeping its containers and data."""
    target = _resolve_name(name)
    try:
        lcsvc.set_state(target, "stop")
    except errors.ReproError as exc:
        _fail(exc)
    ui.ok(f"✓ {target!r} stopped (resume with `rc-repro start`).")


@app.command()
def restart(name: str = typer.Option("", "--name", "-n")) -> None:
    """Restart a repro."""
    target = _resolve_name(name)
    try:
        lcsvc.set_state(target, "restart")
    except errors.ReproError as exc:
        _fail(exc)
    ui.ok(f"✓ {target!r} restarted.")


@app.command()
def use(name: str = typer.Argument(..., help="repro to make the default")) -> None:
    """Set the default repro for name-less commands."""
    if not runner.exists(name):
        _err(f"no repro named {name!r}")
    config.update_config(lambda cfg: cfg.__setitem__("default_repro", name))
    ui.ok(f"✓ default repro is now {name!r}.")


@app.command(name="list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="emit the stable JSON record instead of a table"),
) -> None:
    """List all repros with version, port, status and URL."""
    repros = lcsvc.list_repros()
    if json_out:
        # Empty is a valid answer, not an error: an agent asking "what exists"
        # gets [] rather than having to special-case a prose line.
        jsonout.emit(jsonout.envelope("list", {"repros": repros}))
        return
    if not repros:
        typer.echo("No repros yet. Create one with `rc-repro up --version <X.Y.Z>`.")
        return
    typer.echo(f"{'NAME':<20} {'RC':<9} {'MONGO':<7} {'PORT':<6} {'STATE':<10} URL")
    for r in repros:
        flag = "*" if r["default"] else (" " if not r["pinned"] else "·")
        typer.echo(
            f"{flag}{r['name']:<19} {r['rc_version']:<9} {r['mongo_tag']:<7} "
            f"{r['host_port']:<6} {r['state']:<10} {r.get('public_url') or r['root_url']}"
        )
    typer.echo("\n* = default repro   · = pinned")


@app.command()
def info(
    name: str = typer.Option("", "--name", "-n"),
    json_out: bool = typer.Option(False, "--json", help="emit the stable JSON record instead of a panel"),
) -> None:
    """Show a repro's URL, admin credentials and a curl snippet."""
    if json_out:
        # Resolve inside the try so a missing repro is reported as an error
        # envelope with its code, not as a bare traceback or a prose line.
        try:
            target = lcsvc.resolve_name(name)
            jsonout.emit(jsonout.envelope("info", lcsvc.detail(target)))
        except errors.ReproError as exc:
            jsonout.fail(exc)
        return
    target = _resolve_name(name)
    m = runner.read_meta(target)
    _summary_panel(m)
    _print_access(lcsvc.describe(target))
    ui.hint(f"  api  : rc-repro api --name {m.name} GET /api/v1/me")
    ui.hint(f"  curl : {m.root_url}/api/info")
    _print_notes(m)


# Settings worth remembering, shown name -> config.yaml key. An allowlist, so a
# typo cannot write junk into the config file.
_CONFIG_KEYS: dict[str, str] = {
    "acme.email": "acme_email",
    "acme.dns_provider": "acme_dns_provider",
}


@app.command(name="config")
def config_cmd(
    action: str = typer.Argument("list", help="list | get | set | unset"),
    key: str = typer.Argument("", help="e.g. acme.email"),
    value: str = typer.Argument("", help="the value, for `set`"),
) -> None:
    """Read or write remembered settings, so they need not be retyped every run.

    Keys: acme.email, acme.dns_provider. Stored in ~/.rc-repro/config.yaml.
    """
    if action == "list":
        cfg = config.load_config()
        for shown, stored in _CONFIG_KEYS.items():
            typer.echo(f"  {shown:20} {cfg.get(stored) or '(unset)'}")
        return
    if key not in _CONFIG_KEYS:
        _err(f"unknown key {key!r} (want: {', '.join(_CONFIG_KEYS)})")
    stored = _CONFIG_KEYS[key]
    if action == "get":
        typer.echo(config.load_config().get(stored) or "")
        return
    if action == "unset":
        config.update_config(lambda c: c.pop(stored, None))
        ui.ok(f"✓ {key} unset.")
        return
    if action == "set":
        if not value:
            _err(f"`config set {key}` needs a value")
        config.update_config(lambda c: c.__setitem__(stored, value))
        ui.ok(f"✓ {key} = {value}")
        return
    _err(f"unknown action {action!r} (want: list | get | set | unset)")


@app.command(name="env")
def env_cmd(
    name: str = typer.Option("", "--name", "-n"),
    set_: list[str] = typer.Option(None, "--set", help="raw env var KEY=VALUE (repeatable)"),
    setting: list[str] = typer.Option(None, "--setting", help="Rocket.Chat SETTING Id=VALUE (repeatable) — adds the OVERWRITE_SETTING_ prefix for you, which a setting needs to take effect"),
    unset: list[str] = typer.Option(None, "--unset", help="KEY to remove entirely, including a preset default (repeatable)"),
    no_restart: bool = typer.Option(False, "--no-restart", help="write the change but don't recreate the container yet"),
) -> None:
    """Show or change a repro's Rocket.Chat environment variables.

    With no --set/--unset, lists the effective environment (credentials masked) and
    marks which keys you have overridden.

    An env var cannot be changed inside a running container, so applying a change
    recreates the Rocket.Chat container. MongoDB keeps running and its volume is
    untouched, so no data is lost and it takes seconds.
    """
    if not set_ and not setting and not unset:
        cur = envsvc.current(name)
        rows = [(e["key"], e["value"] + ("   <- yours" if e["override"] else ""))
                for e in cur["env"]]
        ui.panel(f"env: {cur['name']}", rows)
        if cur["overrides"]:
            ui.hint("  your overrides: " + ", ".join(cur["overrides"]))
        else:
            ui.hint("  no overrides — this is the preset/base environment.")
        ui.hint("  a Rocket.Chat setting: rc-repro env --setting Some_Setting_Id=value" +
                (f" --name {cur['name']}" if name else ""))
        ui.hint("  a raw env var:        rc-repro env --set KEY=VALUE" +
                (f" --name {cur['name']}" if name else ""))
        return
    try:
        sets = {**envsvc.parse_set(set_ or []), **envsvc.as_setting(setting or [])}
        result = envsvc.set_env(name, sets, list(unset or []),
                                restart=not no_restart, emit=_cli_emit)
    except errors.ReproError as exc:
        _err(str(exc))
    verb = "applied" if result["restarted"] else "written (not yet applied)"
    ui.ok(f"✓ env {verb} on {result['name']!r}.")
    for o in result["overrides"]:
        typer.echo(f"    {o['key']} = " + ("(removed)" if o["removed"] else o["value"]))
    if result["restarted"]:
        ui.hint(f"  verify: rc-repro env --name {result['name']}")


@app.command(name="tls-status")
def tls_status(name: str = typer.Option("", "--name", "-n")) -> None:
    """Report what is ACTUALLY being served over HTTPS for a repro.

    `up --wait` only proves Rocket.Chat booted -- it polls the internal http port.
    Traefik obtains certificates in the background AFTER it starts and falls back
    to a self-signed dummy when ACME fails, so a repro can look ready while HTTPS
    serves nothing usable. This makes the real TLS connection and says so.
    """
    from rc_repro import tls as tlsmod
    m = runner.read_meta(_resolve_name(name))
    if not m.public_url:
        ui.warn(f"  ⚠ {m.name!r} was not created with --https - it serves plain HTTP "
                f"at {m.root_url}.")
        raise typer.Exit(1)

    mode = str(m.extra.get("tls") or "")
    host = m.public_url.split("://", 1)[1].split(":")[0]
    port = int(m.extra.get("tls_ports", [443])[0])
    cafile = str(tlsmod.ca_dir() / tlsmod.CA_CRT) if mode == tlsmod.MODE_LOCAL else None
    typer.echo(f"Checking {m.public_url} ...")

    # Probe THIS host, not the hostname, so a proxy in front cannot answer for us.
    # Checking only the public name reported Cloudflare's edge certificate as
    # "serving HTTPS, trusted" while our own Traefik had no certificate at all.
    r = tlsmod.verify("127.0.0.1", port, cafile=cafile, sni=host)

    if not r["serving"]:
        ui.warn(f"  ✗ nothing is serving TLS on this host's port {port} - {r['error']}")
        _tls_troubleshoot(m, mode, host, port)
        raise typer.Exit(1)
    rows = [("Endpoint", f"{host}:{port}"), ("Issuer", r["issuer"] or "?"),
            ("Subject", r["subject"] or "?"), ("Expires", r["dates"].replace("notAfter=", ""))]
    ui.panel(f"TLS: {m.name}", rows)

    if r["fallback"]:
        # Traefik's own placeholder: it started, but never got a real certificate.
        ui.warn("  ✗ this is Traefik's built-in placeholder certificate, which means "
                "ACME never succeeded.")
        _tls_troubleshoot(m, mode, host, port)
        raise typer.Exit(1)

    # For a real domain, also report what the PUBLIC name serves. If a proxy sits in
    # front (Cloudflare's orange cloud), that is a different certificate from ours,
    # and saying so beats letting the two be confused for each other.
    if mode == tlsmod.MODE_ACME:
        pub = tlsmod.verify(host, 443)
        if not pub["serving"]:
            ui.warn(f"  ⚠ {host}:443 is not reachable from here ({pub['error']}) — "
                    "this host serves TLS, but the public name does not resolve to it "
                    "or nothing forwards to it.")
        elif pub["issuer"] and pub["issuer"] != r["issuer"]:
            ui.warn(f"  ⚠ {host} is fronted by something else: it serves a certificate "
                    f"issued by {pub['issuer']!r}, not the one this host serves "
                    f"({r['issuer']!r}).")
            ui.hint("  That is what an orange-clouded Cloudflare record looks like. "
                    "Clients get the proxy's certificate, not this one.")
    if mode == tlsmod.MODE_LOCAL:
        # Two separate facts: is it OUR certificate, and has trust-ca been run?
        if not r["trusted_via_ca"]:
            ui.warn("  ✗ serving a certificate that does not chain to rc-repro's CA.")
            _tls_troubleshoot(m, mode, host, port)
            raise typer.Exit(1)
        ui.ok("  ✓ serving HTTPS with rc-repro's local CA.")
        if r["trusted"]:
            ui.hint("  This machine trusts it — `trust-ca` has been run; no browser warnings.")
        else:
            ui.hint("  Not trusted by this machine yet. Run `rc-repro trust-ca` once to "
                    "silence browser warnings (the warning itself is harmless locally).")
        return
    if r["trusted"]:
        ui.ok("  ✓ serving HTTPS with a certificate this machine trusts.")
        return
    # Untrusted is EXPECTED for staging.
    if mode == tlsmod.MODE_ACME:
        ui.ok("  ✓ serving HTTPS with a real Let's Encrypt-issued certificate.")
        ui.hint("  Not trusted here, which is normal for --acme-staging: DNS and the "
                "challenge both worked.")
        ui.hint("  For a trusted certificate, re-run the SAME command without "
                "--acme-staging:")
        ui.hint("    " + _promote_command(m, host))
    else:
        ui.ok("  ✓ serving HTTPS.")
        ui.hint("  This machine does not trust the issuer; that may be fine if the "
                "clients that matter do.")


def _promote_command(m: runner.Metadata, host: str) -> str:
    """The staging -> production command, rebuilt from what this repro ACTUALLY used.

    Guessing it dropped --acme-challenge/--acme-dns-provider and bolted on
    --bind 0.0.0.0, so the suggestion silently switched challenge type and failed.
    """
    x = m.extra if isinstance(m.extra, dict) else {}
    challenge = str(x.get("tls_challenge") or "tlsalpn")
    parts = [f"rc-repro up -v {m.rc_version}", f"--name {m.name}", "--https",
             f"--domain {host}",
             f"--acme-email {x.get('tls_email') or '<you@example.com>'}"]
    if challenge != "tlsalpn":
        parts.append(f"--acme-challenge {challenge}")
    if challenge == "dns" and x.get("tls_dns_provider"):
        parts.append(f"--acme-dns-provider {x['tls_dns_provider']}")
    # Only the inbound challenges need a public bind; dns-01 does not, and adding
    # it would expose a workspace running admin/admin123 for no reason.
    if challenge == "tlsalpn":
        parts.append("--bind 0.0.0.0")
    parts += ["--force", "--wait"]
    return " ".join(parts)


def _tls_troubleshoot(m: runner.Metadata, mode: str, host: str, port: int) -> None:
    """The next things to actually check, ordered by how often they are the cause."""
    ui.hint(f"  Rocket.Chat itself is fine at {m.root_url} - this is the TLS layer.")
    if mode == "acme":
        ui.hint("  Most likely, in order:")
        ui.hint(f"    1. inbound TCP/{port} not reachable from the internet "
                "(cloud security group / host firewall)")
        ui.hint(f"    2. DNS: `dig +short {host}` must return this host's PUBLIC IP")
        ui.hint("    3. behind Cloudflare's orange cloud? it terminates TLS, so "
                "tlsalpn cannot work - put a provider token in "
                "~/.rc-repro/acme/dns.env to switch to dns-01")
        ui.hint(f"  What Traefik says:  rc-repro logs --name {m.name} | grep -i acme")
        ui.hint("  Debug on staging (--acme-staging): production allows only 5 failed "
                f"validations per hour for {host}.")
    else:
        ui.hint(f"  Check Traefik started:  rc-repro logs --name {m.name} | grep -i traefik")


@app.command(name="trust-ca")
def trust_ca(
    uninstall: bool = typer.Option(False, "--uninstall", help="remove it again"),
    show: bool = typer.Option(False, "--show", help="just print the CA path and fingerprint"),
) -> None:
    """Install rc-repro's local CA so `--https` repros are trusted without warnings.

    Only needed for `up --https` on its own (the local-CA mode). A Let's Encrypt
    or self-supplied certificate is already trusted, so this does nothing for those.
    """
    from rc_repro import tls as tlsmod
    key, crt = tlsmod.ensure_ca()
    if show:
        typer.echo(f"CA certificate: {crt}")
        typer.echo(f"CA key:         {key}  (keep private)")
        _run_and_echo(["openssl", "x509", "-in", str(crt), "-noout",
                       "-subject", "-fingerprint", "-sha256", "-dates"])
        return
    try:
        installed, how = tlsmod.trust(crt, uninstall=uninstall)
    except errors.ReproError as exc:
        _err(str(exc))
    if installed:
        ui.ok(f"✓ CA {'removed from' if uninstall else 'installed into'} {how}.")
        ui.hint("  Restart the browser to pick it up. Firefox keeps its own store — "
                "if it still warns, import the CA under Settings → Privacy → Certificates.")
    else:
        ui.warn(f"  ⚠ could not {'remove' if uninstall else 'install'} it automatically "
                f"on this platform ({how}).")
        typer.echo(tlsmod.manual_trust_instructions(crt, uninstall=uninstall))


def _run_and_echo(cmd: list[str]) -> None:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        ui.warn(f"  ⚠ {cmd[0]} failed: {exc}")
        return
    for line in (r.stdout or "").splitlines():
        typer.echo(f"  {line}")


@app.command()
def token(name: str = typer.Option("", "--name", "-n")) -> None:
    """Mint an API auth token (X-Auth-Token / X-User-Id headers)."""
    _require_docker()
    _target = _resolve_name(name)
    # Kubernetes reachability is a port-forward that may have died; revive it before
    # talking HTTP, or this fails for a reason unrelated to what was asked.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
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
    _target = _resolve_name(name)
    # Kubernetes reachability is a port-forward that may have died; revive it before
    # talking HTTP, or this fails for a reason unrelated to what was asked.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
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
    _t = time.monotonic()
    try:
        status, text = rcapi.call(m.root_url, method, path, auth=auth, data=body, extra_headers=extra)
    except requests.RequestException as exc:
        _err(f"request failed: {exc}")
    elapsed = fmt_ms((time.monotonic() - _t) * 1000)
    tag = "PAT" if pat else "admin"
    if two_fa:
        tag += "+2fa"
    typer.secho(f"HTTP {status}  [{tag}]  in {elapsed}", fg=typer.colors.GREEN if status < 400 else typer.colors.RED)
    typer.echo(text)
    # Exit non-zero on an HTTP error so `rc-repro api ... || handle` (the
    # customer-script / CI use case) can detect it, not just transport failures.
    if status >= 400:
        raise typer.Exit(1)


@app.command()
def pat(
    name: str = typer.Option("", "--name", "-n"),
    label: str = typer.Option("rc-repro", "--label", help="token name shown in the admin UI"),
    bypass_2fa: bool = typer.Option(True, "--bypass-2fa/--no-bypass-2fa", help='create with "Ignore Two Factor Authentication"'),
) -> None:
    """Mint a Personal Access Token and print ready-to-use headers (curl/Postman)."""
    _require_docker()
    _target = _resolve_name(name)
    # Kubernetes reachability is a port-forward that may have died; revive it before
    # talking HTTP, or this fails for a reason unrelated to what was asked.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
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
    stats: bool = typer.Option(False, "--stats", help="also report CPU/RAM cost of the seed"),
    scale: str = typer.Option(
        None, "--scale",
        help="bulk Mongo prefill for scale repros, e.g. users=50000,messages=800000@team-chat"),
    clear_scale: bool = typer.Option(
        False, "--clear-scale", help="remove data a prior --scale added, then exit"),
) -> None:
    """Populate a repro with sample users, channels, DMs and messages.

    --scale bulk-inserts users/messages straight into MongoDB (orders of
    magnitude faster than the REST seed) to reproduce SCALE/perf behaviour.
    Bulk users are credential-less and messages fire no app hooks; use the
    default REST seed when you need real, loginable users. --scale and --stats
    are Compose-only; ordinary REST seed works on Compose and Kubernetes.
    """
    _target = _resolve_name(name)
    # Refuse Compose-only modes before reviving a Kubernetes port-forward. The
    # service layer repeats these guards for HTTP/GUI and direct callers.
    try:
        if clear_scale:
            lcsvc.require_compose_topology(_target, "clear-scale")
        elif scale:
            lcsvc.require_compose_topology(_target, "scale")
        elif stats:
            lcsvc.require_compose_topology(_target, "seed --stats")
    except errors.ReproError as exc:
        _fail(exc)
    # Kubernetes reachability is a port-forward that may have died; revive it
    # only for an operation supported by this topology.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
    if clear_scale:
        try:
            datasvc.clear_scale(_target, emit=_cli_emit)
        except errors.ReproError as exc:
            _fail(exc)
        return
    if scale:
        try:
            datasvc.run_scale(_target, scale, emit=_cli_emit)
        except errors.ReproError as exc:
            _fail(exc)
        return
    # REST seed needs a container engine only on Compose; Kubernetes talks HTTP
    # through the port-forward. Require docker only when the topology is compose.
    if lcsvc.topology_of_repro(_target) == "compose":
        _require_docker()
    _run_seed(m, profile, users, channels, messages, stats=stats)


@app.command(name="config-import")
def config_import(
    settings_file: str = typer.Argument(
        ..., help="path to a support-dump *-settings.json"),
    name: str = typer.Option("", "--name", "-n"),
    only: str = typer.Option(
        None, "--only", help="comma-separated id prefixes, e.g. Livechat,LDAP,Accounts"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="show the import plan without changing anything"),
) -> None:
    """Apply a customer's exported settings (from a support dump) to a repro.

    Imports only settings the customer CHANGED from default, skipping secrets the
    dump redacts and identity/environment settings (license, Site_Url, assets)
    that would break or pollute a local repro.
    """
    _require_docker()
    path = Path(settings_file)
    if not path.is_file():
        _err(f"no such file: {settings_file}")
    _target = _resolve_name(name)
    # Kubernetes reachability is a port-forward that may have died; revive it before
    # talking HTTP, or this fails for a reason unrelated to what was asked.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
    onlyset = {p.strip() for p in only.split(",")} if only else None
    try:
        plan = datasvc.import_plan(m.name, str(path), only=onlyset)
    except errors.ReproError as exc:
        _fail(exc)

    lines = [f"apply    {plan['counts']['apply']} customized setting(s)",
             f"skip     {plan['counts']['redacted']} redacted secret(s), "
             f"{plan['counts']['denied']} identity/environment setting(s)"]
    if plan["oauth_services"]:
        lines.append(f"oauth    pre-create: {', '.join(plan['oauth_services'])}")
    typer.echo("")
    ui.box("config import" + (" (dry run)" if dry_run else ""), lines, 64,
           title_color=typer.colors.CYAN)
    if plan["redacted"]:
        ui.warn("  set these by hand (redacted in the dump): " + ", ".join(plan["redacted"]))
    if dry_run:
        for item in plan["apply"]:
            typer.echo(f"    {item['id']:<48} = {item['value']}")
        return

    try:
        res = datasvc.import_apply(m.name, str(path), only=onlyset, emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    if res["failed"]:
        ui.warn(f"  {res['failed']} setting(s) rejected: {', '.join(res['failures'][:10])}"
                + (" ..." if res["failed"] > 10 else ""))
    ui.ok(f"✓ imported {res['applied']} setting(s), skipped {res['skipped']}")
    ui.hint("  some settings need an RC restart to fully take effect: "
            f"rc-repro restart --name {m.name}")


@app.command()
def stats(
    name: str = typer.Option("", "--name", "-n"),
    for_: float = typer.Option(5.0, "--for", help="seconds to sample"),
    watch: bool = typer.Option(False, "--watch", "-w", help="stream live (Ctrl-C to stop)"),
) -> None:
    """Sample a repro's container CPU/RAM (peak over a window, or --watch live)."""
    _require_docker()
    _target = _resolve_name(name)
    # Refuse rather than silently report nothing: a command that is
    # accepted and then does nothing is the failure rc-repro exists to remove.
    lcsvc.require_compose_topology(_target, 'stats', 'It reads container stats from the compose project; the Kubernetes equivalent needs metrics-server.')
    # Kubernetes reachability is a port-forward that may have died; revive it before
    # talking HTTP, or this fails for a reason unrelated to what was asked.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
    if watch:
        typer.echo(f"Live stats for {m.name!r} (Ctrl-C to stop)…")
        try:
            while True:
                ids = runner.container_ids(m.name)
                out = runner.docker_stats(ids)
                typer.echo("")
                for line in out.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        typer.echo(f"  {_short_container(parts[0], m.name):<14} CPU {parts[1]:>7}   RAM {parts[2]}")
                time.sleep(2)
        except KeyboardInterrupt:
            return
    typer.echo(f"Sampling {m.name!r} for {for_:.0f}s…")
    with perf.ResourceMonitor(m.name) as mon:
        time.sleep(for_)
    _print_resources(mon.report(), m.name)


@app.command()
def benchmark(
    versions_: str = typer.Option(..., "--versions", help="comma-separated versions to compare, e.g. 8.4.1,8.5.1"),
    seed_profile: str = typer.Option("standard", "--seed-profile", help="workload size: small | standard | large"),
    regress_pct: float = typer.Option(25.0, "--regress-pct", help="flag a version if seed time or p95 rises more than this % vs the previous"),
    offline: bool = typer.Option(False, "--offline"),
    no_pull: bool = typer.Option(False, "--no-pull"),
    report: bool = typer.Option(False, "--report", help=f"write a detailed markdown report to {config.reports_dir()}"),
    report_path: str = typer.Option("", "--report-path", help="write the report to this file/dir instead (implies --report)"),
) -> None:
    """Boot several RC versions, run the identical seed workload against each, and
    compare — a version performance-regression check unique to rc-repro."""
    _require_docker()
    vers = [v.strip() for v in versions_.split(",") if v.strip()]
    if len(vers) < 2:
        _err("give at least two --versions to compare, e.g. --versions 8.4.1,8.5.1")

    typer.echo(f"Benchmarking {len(vers)} versions (workload: seed {seed_profile}, sequential)…\n")
    from rc_repro.services import perf as perfsvc
    results = [perfsvc.bench_one(v, seed_profile, offline, no_pull, emit=_cli_emit) for v in vers]

    typer.echo("")
    headers, rows, flags = perf_report.table_rows(results, regress_pct)
    typer.secho(headers[0], bold=True)
    for row, flag in zip(rows, flags):
        suffix = typer.style(f"   <- {flag}", fg=typer.colors.YELLOW) if flag else ""
        typer.echo(row + suffix)
    typer.echo("")
    ui.note("Deltas between versions are the signal; absolute numbers are host-specific.")
    if report or report_path:
        host = {
            "os": platform.platform(), "cpu": os.cpu_count() or "?",
            "docker": runner.docker_server_version() or "?",
            "compose": runner.compose_version() or "?",
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = perf_report.write_benchmark(
            results, seed_profile, regress_pct, stamp, host, dest=report_path or None
        )
        ui.ok(f"✓ wrote {path}")


_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


def _parse_endpoint(endpoint: str) -> tuple[str, str]:
    """'GET /api/v1/x' -> ('GET', '/api/v1/x'); a bare '/api/v1/x' defaults to GET.
    Raises ValueError on an empty/non-absolute path or an unsupported method."""
    e = endpoint.strip()
    if not e:
        raise ValueError("empty endpoint")
    parts = e.split(None, 1)
    if len(parts) == 2 and parts[0].isalpha():
        # First token looks like a method — it must be a supported one.
        if parts[0].upper() not in _HTTP_METHODS:
            raise ValueError(f"unsupported method {parts[0]!r} (use {', '.join(sorted(_HTTP_METHODS))})")
        method, path = parts[0].upper(), parts[1].strip()
    else:
        method, path = "GET", e
    if not path.startswith("/"):
        raise ValueError(f"path must start with '/': {path!r}")
    return method, path


def _parse_ramp(ramp: str) -> tuple[int, int]:
    """'10:200' -> (10, 200). Raises ValueError on a malformed spec."""
    parts = ramp.split(":")
    if len(parts) != 2:
        raise ValueError("ramp must be START:END, e.g. 10:200")
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("ramp START and END must be integers, e.g. 10:200")
    if start < 0 or end < 1:
        raise ValueError("ramp needs START >= 0 and END >= 1, e.g. 10:200")
    return start, end


def _loadtest_target(doc: dict) -> str:
    """The in-network URL k6 should hit. Multi-instance repros front RC with
    Traefik; single-instance ones expose `rocketchat` (or `rocketchat-1`)."""
    svcs = doc.get("services", {})
    if "traefik" in svcs:
        return "http://traefik:80"
    rc = _rc_services_in(doc)
    if "rocketchat" in rc:
        return "http://rocketchat:3000"
    if rc:
        return f"http://{rc[0]}:3000"
    return "http://rocketchat:3000"


def _status_breakdown(summary: dict) -> str:
    """'2xx 1158 · 429 61 · 5xx 41' from the summary's status buckets (non-zero only)."""
    st = summary.get("status") or {}
    order = [("2xx", "2xx"), ("429", "429"), ("4xx", "4xx"), ("5xx", "5xx"), ("other", "other")]
    parts = [f"{lbl} {int(st[k])}" for k, lbl in order if st.get(k)]
    return " · ".join(parts)


def _login_seed_users(m: runner.Metadata, count: int) -> list[dict]:
    """Plain-login up to `count` seed users (alice, bob, …; password=username) and
    return [{username, password, token, uid}] for those that succeed. Failures are
    simply skipped (unseeded repro, or 2FA-guarded logins on the email preset)."""
    users: list[dict] = []
    url = m.root_url.rstrip("/") + "/api/v1/login"
    for i in range(count):
        uname = seeder.username(i)
        try:
            r = requests.post(url, json={"user": uname, "password": uname}, timeout=10)
        except requests.RequestException:
            break   # workspace unreachable — no point trying the rest
        if r.status_code == 200:
            d = r.json().get("data") or {}
            if d.get("authToken"):
                users.append({"username": uname, "password": uname,
                              "token": d["authToken"], "uid": d["userId"]})
    return users


def _workspace_snapshot(m: runner.Metadata, auth: rcapi.Auth, instances: int) -> dict:
    """Best-effort workspace context for the report/baseline: version, topology,
    and dataset size — the numbers that make a perf result comparable."""
    snap = {"rc_version": m.rc_version, "preset": m.preset, "instances": instances}
    try:
        # refresh=true: without it RC returns the last cron-generated stats,
        # which are all zeros on a fresh workspace.
        status, text = rcapi.call(m.root_url, "GET", "/api/v1/statistics?refresh=true", auth=auth)
        if status == 200:
            j = json.loads(text)
            for key, field_ in (("users", "totalUsers"), ("rooms", "totalRooms"),
                                ("messages", "totalMessages")):
                if j.get(field_) is not None:
                    snap[key] = j[field_]
    except Exception:  # noqa: BLE001 - snapshot must never fail the run
        pass
    return snap


def _print_steps(steps: dict) -> None:
    if not steps:
        return
    from rc_repro.perf import baseline
    typer.echo("")
    ui.note("Per-step latency:")
    typer.echo(f"  {'step':<8} {'count':>6}   {'p50':>7} {'p95':>7} {'p99':>7}")
    for s in baseline.step_order(steps):
        v = steps[s]
        typer.echo(f"  {s:<8} {v.get('count', 0):>6.0f}   "
                   f"{fmt_ms(v.get('p50') or 0):>7} {fmt_ms(v.get('p95') or 0):>7} "
                   f"{fmt_ms(v.get('p99') or 0):>7}")


def _fmt_compare_value(metric: str, v: float) -> str:
    if "rps" in metric:
        return f"{v:.1f}"
    if "error" in metric:
        return f"{v * 100:.2f}%"
    return fmt_ms(v)


def _print_compare(rows: list[dict], base: dict) -> None:
    ctxb = base.get("ctx") or {}
    typer.echo("")
    ui.note(f"vs baseline {base.get('label', '?')!r} "
            f"({ctxb.get('label', ctxb.get('scenario', '?'))}, saved {str(base.get('saved_at', ''))[:19]}):")
    width = max((len(r["metric"]) for r in rows), default=0)
    for r in rows:
        before = _fmt_compare_value(r["metric"], r["before"])
        after = _fmt_compare_value(r["metric"], r["after"])
        line = f"  {r['metric']:<{width}}  {before:>8} -> {after:<8} {r['pct']:+6.0f}%"
        if r["flag"]:
            typer.secho(line + "   <- regression", fg=typer.colors.YELLOW)
        elif not r["worse"] and abs(r["pct"]) > 25:
            typer.secho(line, fg=typer.colors.GREEN)
        else:
            typer.echo(line)


def _print_diag(rcm: dict, mongo_slow: dict | None, tl: dict | None,
                verdict_lines: list[str], repro_name: str) -> None:
    """Phase C console output: timeline, RC internals, slow queries, verdict."""
    from rc_repro.perf import timeline as timeline_mod
    if tl:
        typer.echo("")
        for line in timeline_mod.render_ascii(tl):
            typer.echo(f"  {line}")
    if rcm:
        typer.echo("")
        ui.note("RC internals during the test:")
        for svc in sorted(rcm):
            m = rcm[svc]
            bits = []
            # Histogram peak (per-interval) over the run; instantaneous as fallback.
            peak = m.get("eventloop_lag_max_s") or m.get("eventloop_lag_s")
            p99 = m.get("eventloop_lag_p99_s")
            if peak:
                lag_bit = f"event-loop lag peak {fmt_ms(peak['max'] * 1000)}"
                if p99:
                    lag_bit += f" / p99 {fmt_ms(p99['max'] * 1000)}"
                bits.append(lag_bit)
            heap = m.get("heap_used_bytes")
            if heap:
                bits.append(f"heap {heap['max'] / 1e6:.0f}MB")
            ddp = m.get("ddp_users")
            if ddp:
                bits.append(f"ddp users {ddp['max']:.0f}")
            if bits:
                typer.echo(f"  {svc:<14} {'   '.join(bits)}")
    if mongo_slow and mongo_slow.get("slow"):
        typer.echo("")
        ui.note(f"Slow MongoDB queries ({mongo_slow['total']} profiled, "
                f"{mongo_slow['collscan']} COLLSCAN):")
        for s in mongo_slow["slow"]:
            plan = s.get("plan") or "?"
            typer.echo(f"  {fmt_ms(s['millis']):>7}  {s['ns']}  {s['op']}  [{plan}]  "
                       f"docs {s['docs']}/ret {s['ret']}")
    if verdict_lines:
        typer.echo("")
        typer.secho("Verdict:", bold=True)
        for line in verdict_lines:
            wrapped = textwrap.wrap(_ascii(line), width=84, subsequent_indent="    ")
            typer.secho("  - " + wrapped[0], fg=typer.colors.CYAN)
            for cont in wrapped[1:]:
                typer.secho("  " + cont, fg=typer.colors.CYAN)


def _load_shape(ctx: dict) -> str:
    """The offered-load shape recorded in a run context ('spike 10:100' / 'ramp
    10:200' / '50 VUs'), for panels and baseline-mismatch warnings."""
    if ctx.get("spike"):
        return f"spike {ctx['spike']}"
    if ctx.get("ramp"):
        return f"ramp {ctx['ramp']}"
    return f"{ctx.get('vus', '?')} VUs"


def _metric(summary: dict, key: str, fmt: str = "{:.0f}ms") -> str:
    """A summary metric for display, or '-' when it was not measured.

    A zero-request run emits no latency/checks keys at all, so `.get(key, 0)`
    would present an absent measurement as a confident 0ms."""
    v = summary.get(key)
    return "-" if v is None else fmt.format(v)


def _print_loadtest(ctx: dict, summary: dict, slo_results: list[dict]) -> None:
    from rc_repro.perf import slo as slo_mod
    rows = [
        ("throughput", f"{_metric(summary, 'rps', '{:.1f}')} req/s   "
                       f"({_metric(summary, 'count', '{:.0f}')} requests)"),
        ("latency", f"p50 {_metric(summary, 'p50')}  p90 {_metric(summary, 'p90')}  "
                    f"p95 {_metric(summary, 'p95')}  p99 {_metric(summary, 'p99')}"),
        ("", f"avg {_metric(summary, 'avg')}  min {_metric(summary, 'min')}  "
             f"max {_metric(summary, 'max')}"),
        ("errors", f"{_metric(summary, 'error_rate', '{:.2%}')}   "
                   f"checks {_metric(summary, 'checks_rate', '{:.0%}')} ok"),
    ]
    breakdown = _status_breakdown(summary)
    if breakdown:
        rows.append(("responses", breakdown))
    if ctx.get("constrained"):
        rows.append(("constrained", ctx["constrained"]))
    load = _load_shape(ctx) + f" / {ctx['duration']}"
    if ctx.get("users"):
        load += f", {ctx['users']} users"
    ui.panel(f"loadtest {ctx.get('label', ctx['scenario'])} ({load})", rows)
    _print_steps(summary.get("steps") or {})
    if slo_results:
        typer.echo("")
        passed = all(r["ok"] for r in slo_results)
        for r in slo_results:
            sym, color = ("✓", typer.colors.GREEN) if r["ok"] else ("✗", typer.colors.RED)
            detail = ("not measured" if not r.get("measured", True)
                      else f"actual {slo_mod.fmt_actual(r['key'], r['actual'])}")
            typer.secho(f"  {sym} {r['key']} {r['op']} {r['raw']}  ({detail})", fg=color)
        typer.secho(f"\nSLO gate: {'PASS' if passed else 'FAIL'}",
                    fg=typer.colors.GREEN if passed else typer.colors.RED, bold=True)


@app.command()
def loadtest(
    name: str = typer.Option("", "--name", "-n"),
    scenario: str = typer.Option("messages", "--scenario", help="messages | login | read | mixed | journey | webhook | badbot | custom"),
    endpoint: str = typer.Option("", "--endpoint", help="custom scenario: the call to hit, e.g. \"GET /api/v1/channels.list?count=100\""),
    body: str = typer.Option("", "--body", help="custom scenario: JSON request body for POST/PUT/PATCH"),
    vus: int = typer.Option(10, "--vus", help="virtual users (k6 concurrent workers — not RC accounts)"),
    users_n: int = typer.Option(10, "--users", help="spread load across up to N seeded users (alice, bob, …); 0 = admin token only"),
    duration: str = typer.Option("30s", "--duration", help="test duration, e.g. 60s, 2m"),
    ramp: str = typer.Option("", "--ramp", help="ramp VUs start:end over --duration, e.g. 10:200"),
    spike: str = typer.Option("", "--spike", help="spike test base:peak over --duration (base 1/3, peak 1/3, recovery 1/3), e.g. 10:100 — reports recovery time"),
    live: bool = typer.Option(False, "--live", help="stream k6 metrics into the attached monitoring stack's Prometheus (watch live in Grafana)"),
    slo: str = typer.Option("", "--slo", help="pass/fail gate, e.g. p95=300ms,error=1%,rps=100"),
    constrain: str = typer.Option("", "--constrain", help="cap services to customer-sized hardware for the test, e.g. \"rc=2cpu/2g,mongo=1cpu/1g\" (live docker update; restored after)"),
    diag: bool = typer.Option(True, "--diag/--no-diag", help="server-side diagnosis: RC event-loop lag, Mongo slow queries, latency-over-time, verdict"),
    slowms: int = typer.Option(100, "--slowms", help="Mongo profiler threshold in ms (queries slower than this are captured)"),
    stats: bool = typer.Option(False, "--stats", help="also report container CPU/RAM during the test"),
    save: str = typer.Option("", "--save", help="save this run as a named baseline (~/.rc-repro/loadtests/)"),
    compare: str = typer.Option("", "--compare", help="compare this run against a saved baseline"),
    json_out: bool = typer.Option(False, "--json", help="print the result as JSON (for CI/scripts); suppresses pretty output"),
    report: bool = typer.Option(False, "--report", help=f"write a markdown report to {config.reports_dir()}"),
    report_path: str = typer.Option("", "--report-path", help="write the report to this file/dir instead (implies --report)"),
) -> None:
    """Drive real HTTP load at a repro with k6 and check it against an SLO.

    Load is spread across seeded users when available (--users, default 10) so
    it carries real per-user identity; the journey scenario times each step of a
    realistic session. --save/--compare give before/after deltas across runs.
    k6 runs on the repro's docker network (works with loopback-only binds); the
    REST rate limiter is disabled for the run and restored after. Exits non-zero
    if a --slo rule is not met — usable as a CI gate.
    """
    _require_docker()
    from rc_repro import monitoring
    from rc_repro.perf import (baseline, constrain as constrain_mod, k6, mongoprof,
                               rcmetrics, slo as slo_mod, timeline as timeline_mod,
                               verdict as verdict_mod)
    if scenario not in k6.SCENARIOS:
        _err(f"unknown scenario {scenario!r} (choose: {', '.join(k6.SCENARIOS)})")
    # Custom scenario: parse "METHOD /path" and pass it to the k6 script via env.
    extra_env, method, path = None, "", ""
    if scenario == "custom":
        if not endpoint:
            _err("--scenario custom needs --endpoint, e.g. --endpoint \"GET /api/v1/channels.list\"")
        try:
            method, path = _parse_endpoint(endpoint)
        except ValueError as exc:
            _err(f"bad --endpoint: {exc}")
        if body and method in ("GET", "DELETE"):
            _err(f"--body is not sent with a {method} request")
        extra_env = {"RC_METHOD": method, "RC_PATH": path, "RC_BODY": body or None}
    elif endpoint or body:
        _err("--endpoint/--body only apply to --scenario custom")
    if vus < 1:
        _err("--vus must be >= 1")
    if users_n < 0:
        _err("--users must be >= 0")

    # In --json mode informational warnings are collected into the JSON payload
    # instead of printed, so stdout stays a single parseable object.
    warnings: list[str] = []

    def _warn(msg: str) -> None:
        if json_out:
            warnings.append(msg.strip().lstrip("⚠ "))
        else:
            ui.warn(msg)

    if ramp and spike:
        _err("--ramp and --spike are mutually exclusive load shapes")
    if ramp:
        try:
            _parse_ramp(ramp)
        except ValueError as exc:
            _err(f"bad --ramp: {exc}")
        if vus != 10:   # 10 is the --vus default; a non-default value is ignored under --ramp
            _warn("  note: --vus is ignored when --ramp is given")
    if spike:
        try:
            s_base, s_peak = _parse_ramp(spike)   # same START:END grammar
        except ValueError as exc:
            _err(f"bad --spike: {exc}")
        if s_peak <= s_base:
            _err(f"--spike peak must exceed base ({spike!r})")
        if vus != 10:
            _warn("  note: --vus is ignored when --spike is given")
    for lbl in (save, compare):
        if lbl:
            try:
                baseline.sanitize_label(lbl)
            except ValueError as exc:
                _err(str(exc))
    constraints = {}
    if constrain:
        try:
            constraints = constrain_mod.parse(constrain)
        except ValueError as exc:
            _err(f"bad --constrain: {exc}")
    rules = []
    if slo:
        try:
            rules = slo_mod.parse(slo)
        except ValueError as exc:
            _err(f"bad --slo: {exc}")

    _target = _resolve_name(name)
    # Refuse rather than silently report nothing: a command that is
    # accepted and then does nothing is the failure rc-repro exists to remove.
    lcsvc.require_compose_topology(_target, 'loadtest', 'It measures resource cost through the compose project, so results would be missing on Kubernetes.')
    # Kubernetes reachability is a port-forward that may have died; revive it before
    # talking HTTP, or this fails for a reason unrelated to what was asked.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
    doc = runner.read_compose(m.name)
    target = _loadtest_target(doc)
    if live:
        if not (isinstance(m.extra, dict) and m.extra.get("monitoring")):
            _err(f"--live needs the monitoring stack — attach it first: rc-repro monitor --name {m.name}")
        # Monitoring attached before this version has a Prometheus without the
        # remote-write receiver, so k6's push would be silently rejected.
        prom_cmd = (doc.get("services", {}).get("prometheus") or {}).get("command", [])
        if not any("remote-write-receiver" in str(c) for c in prom_cmd):
            _err("--live needs Prometheus with remote-write enabled, but this repro's "
                 "monitoring predates it. Re-attach it: "
                 f"rc-repro monitor --name {m.name} --off && rc-repro monitor --name {m.name}")
    per_service = {}
    if constraints:
        try:
            per_service = constrain_mod.resolve_services(constraints, list(doc.get("services", {})))
        except ValueError as exc:
            _err(f"bad --constrain: {exc}")
    # Load the baseline up front: a typo'd label must fail before the run, not after.
    base = None
    if compare:
        try:
            base = baseline.load(compare)
        except (FileNotFoundError, ValueError) as exc:
            _err(str(exc))

    # Auth as a bypass-2FA PAT — exactly how a customer's script would hit the API.
    try:
        auth = _login(m)
        token = rcapi.generate_pat(m.root_url, auth, config.ADMIN_PASSWORD,
                                   token_name="rc-repro-loadtest", bypass_2fa=True)
    except Exception as exc:  # noqa: BLE001
        _err(f"could not authenticate (ready? `rc-repro ready --name {m.name}`): {exc}")

    # Real per-user identity: log in as seeded users and hand them to k6 so VUs
    # round-robin across them. The custom scenario stays on the admin PAT —
    # customer scripts are usually admin calls, and admin-only endpoints must
    # keep working. No seeded logins -> fall back to the admin token (v1).
    users: list[dict] = []
    if users_n > 0 and scenario != "custom":
        users = _login_seed_users(m, users_n)
        if not users:
            _warn("  ⚠ no seeded users could log in — using the admin token "
                  "(run `rc-repro seed` first for realistic multi-user load)")
    # The webhook scenario posts through a real incoming-webhook integration —
    # create (or reuse) it now and hand its tokenized path to k6.
    if scenario == "webhook":
        hook_path = rcapi.create_incoming_webhook(m.root_url, auth, config.ADMIN_PASSWORD)
        if not hook_path:
            _err("could not create the incoming webhook integration (check admin permissions)")
        extra_env = {**(extra_env or {}), "RC_HOOK_PATH": hook_path}
    snapshot = _workspace_snapshot(m, auth, instances=max(1, len(_rc_services_in(doc))))

    label = f"custom {method} {path}" if scenario == "custom" else scenario
    load = (f"spike {spike}" if spike else f"ramp {ramp}" if ramp else f"{vus} VUs") + f" for {duration}"
    identity = f"{len(users)} seeded users" if users else "admin token"
    rc_services = _rc_services_in(doc) or ["rocketchat"]
    # The timeline (k6 point stream) powers latency-over-time AND spike recovery,
    # so collect it whenever diag is on OR a spike is requested.
    want_timeline = diag or bool(spike)

    # Everything below mutates workspace state (resource caps, rate limiter, the
    # Prometheus setting, Mongo profiling) — all of it lives inside this try so a
    # failure OR a Ctrl-C anywhere in setup or the run still hits the finally and
    # restores. Restore-tracked vars are initialised first so the finally is
    # always valid even if we abort before setting them.
    applied_constraints: list = []
    limiter_was_off = True
    metrics_changed, mongo_prior, sampler, mon = False, None, None, None
    resources = None
    summary = None
    rcm_report: dict = {}
    since_ms = int(time.time() * 1000)
    try:
        # Customer-sized hardware: cap the services first, so a failed apply
        # can't leave later settings changed. apply() self-rolls-back mid-way.
        if per_service:
            try:
                applied_constraints = constrain_mod.apply(m.name, per_service)
            except RuntimeError as exc:
                _err(f"could not apply --constrain: {exc}")
            snapshot["constraints"] = constrain_mod.human(per_service)
            if not json_out:
                ui.note(f"  constrained: {snapshot['constraints']} (restored after the test)")

        # Disable the API rate limiter so the offered load isn't throttled into a
        # false result. Restored below — back ON unless it was already known-off
        # (an unreadable setting -> None -> restores to ON, never left disabled).
        limiter_was_off = rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                            config.RC_RATE_LIMITER_SETTING) is False
        if not limiter_was_off and not rcapi.set_setting(
            m.root_url, auth, config.ADMIN_PASSWORD, config.RC_RATE_LIMITER_SETTING, False
        ):
            _warn("  ⚠ could not disable the API rate limiter — results may be throttled (429s)")

        if not json_out:
            typer.secho(f"Load test: {label} @ {load} as {identity} -> {target} "
                        f"(via k6 on {m.name!r}'s network)\n", bold=True)
            if live:
                grafana = f"http://localhost:{config.MONITOR_PORTS[1]}"
                ui.note(f"  live: k6 metrics streaming into Prometheus — open the "
                        f"'k6 Load Test' dashboard in Grafana ({grafana}), "
                        "or Explore -> k6_*")

        # Server-side diagnosis (Phase C): RC's own /metrics (event-loop lag) and
        # Mongo's query profiler, armed for the run. Both best-effort.
        if diag:
            if rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                 monitoring.RC_METRICS_SETTING) is not True:
                metrics_changed = rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                                    monitoring.RC_METRICS_SETTING, True)
            mongo_prior = mongoprof.start(m.name, slowms)
            if mongo_prior is None:
                _warn("  ⚠ Mongo slow-query capture unavailable (profiler could not be enabled)")

        mon = perf.ResourceMonitor(m.name).start() if stats else None
        since_ms = int(time.time() * 1000)
        if diag:
            sampler = rcmetrics.RCMetricsSampler(m.name, rc_services).start()
        summary = k6.run(m.name, scenario, vus=vus, duration=duration, ramp=ramp or None,
                         token=token, uid=auth.user_id, target=target, extra_env=extra_env,
                         users=users or None, quiet=json_out, timeline=want_timeline,
                         spike=spike or None, live=live)
    except RuntimeError as exc:
        _err(str(exc))   # raises typer.Exit; finally still runs (mon stopped, limiter restored)
    finally:
        # users.json holds live seeded-user auth tokens — delete it FIRST, so no
        # later restore step failing can leave credentials on disk.
        (runner.workspace(m.name) / "loadtest" / "users.json").unlink(missing_ok=True)
        if sampler:
            rcm_report = sampler.stop()
        if mon:
            resources = mon.stop()
        if not limiter_was_off:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  config.RC_RATE_LIMITER_SETTING, True)
            except Exception:  # noqa: BLE001 - best-effort restore
                _warn("  ⚠ could not restore the API rate limiter setting")
        if metrics_changed:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  monitoring.RC_METRICS_SETTING, False)
            except Exception:  # noqa: BLE001
                _warn("  ⚠ could not restore the Prometheus metrics setting")
        if mongo_prior:
            # Wrapped like its neighbours: an exception here would skip the
            # resource-cap restore below and leave the containers capped.
            try:
                mongoprof.stop(m.name, mongo_prior)
            except Exception:  # noqa: BLE001
                _warn("  ⚠ could not restore the Mongo profiler level")
        for problem in constrain_mod.restore(applied_constraints):
            _warn(f"  ⚠ could not restore resource limits — {problem}")

    # Collect the diagnosis artifacts (profile entries survive the level reset).
    mongo_slow = mongoprof.collect(m.name, since_ms) if (diag and mongo_prior) else None
    tl = None
    if want_timeline:
        points = runner.workspace(m.name) / "loadtest" / "points.json"
        tl = timeline_mod.parse(points)
        points.unlink(missing_ok=True)   # can be tens of MB — don't leave it around

    ctx = {"name": m.name, "version": m.rc_version, "scenario": scenario, "vus": vus,
           "duration": duration, "ramp": ramp, "spike": spike, "target": target,
           "label": label, "users": len(users),
           "constrained": snapshot.get("constraints", "")}
    slo_results = slo_mod.evaluate(rules, summary) if rules else []
    compare_rows = baseline.compare({"summary": summary}, base) if base else []
    if base and (base.get("ctx") or {}).get("scenario") not in (None, scenario):
        _warn(f"  ⚠ baseline {compare!r} was a {(base['ctx']or{}).get('scenario')!r} run — "
              f"comparing across scenarios")
    # Load shape too, not just scenario: a spike baseline diffed against a steady
    # run compares different offered loads, which the deltas can't account for.
    if base and _load_shape(base.get("ctx") or {}) != _load_shape(ctx):
        _warn(f"  ⚠ baseline {compare!r} ran a different load shape "
              f"({_load_shape(base.get('ctx') or {})} vs {_load_shape(ctx)}) — "
              "deltas reflect the offered load, not just the workspace")
    if base and (base.get("snapshot") or {}).get("constraints") != snapshot.get("constraints"):
        _warn(f"  ⚠ baseline {compare!r} ran under different resource constraints "
              f"({(base.get('snapshot') or {}).get('constraints') or 'none'} vs "
              f"{snapshot.get('constraints') or 'none'}) — deltas reflect the hardware change")

    short_res = _short_res_map(resources, m.name) if resources else None
    spike_rec = timeline_mod.spike_recovery(tl) if (spike and tl) else None
    # RAM slope over the run (only meaningful on long runs) — the soak signal.
    soak = _short_res_map(mon.mem_slopes(), m.name) if mon else None
    verdict_lines = (verdict_mod.analyze(summary, rcmetrics=rcm_report or None,
                                         mongo=mongo_slow, resources=short_res, timeline=tl,
                                         soak=soak or None, spike=spike_rec)
                     if diag else [])
    diag_payload = {"rcmetrics": rcm_report, "mongo": mongo_slow, "timeline": tl,
                    "spike": spike_rec, "verdict": verdict_lines} if diag else None

    if not json_out:
        typer.echo("")
        _print_loadtest(ctx, summary, slo_results)
        if spike_rec:
            rec = spike_rec["recovered_after_s"]
            msg = (f"  spike: baseline p95 {fmt_ms(spike_rec['baseline_p95'])} -> peak "
                   f"{fmt_ms(spike_rec['spike_p95'])} -> "
                   + (f"recovered ~{rec}s after load dropped" if rec is not None
                      else "NOT recovered within the run"))
            (ui.ok if rec is not None and rec <= 30 else ui.warn)(msg)
        if diag:
            _print_diag(rcm_report, mongo_slow, tl, verdict_lines, m.name)
        if compare_rows:
            _print_compare(compare_rows, base)
        _print_resources(resources or {}, m.name)

    saved_to = report_file = ""
    if save:
        saved_to = baseline.save(save, {
            "label": baseline.sanitize_label(save),
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ctx": ctx, "summary": summary, "snapshot": snapshot,
        })
        if not json_out:
            typer.echo("")
            ui.ok(f"✓ saved baseline {baseline.sanitize_label(save)!r} "
                  f"(compare later with --compare {baseline.sanitize_label(save)})")

    if report or report_path:
        host = {"os": platform.platform(), "cpu": os.cpu_count() or "?",
                "docker": runner.docker_server_version() or "?",
                "compose": runner.compose_version() or "?"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        report_file = perf_report.write_loadtest(
            ctx, summary, slo_results, short_res, host, stamp,
            dest=report_path or None, snapshot=snapshot,
            compare={"label": base.get("label"), "saved_at": base.get("saved_at"),
                     "rows": compare_rows} if base else None,
            diag=diag_payload,
        )
        if not json_out:
            typer.echo("")
            ui.ok(f"✓ wrote {report_file}")

    passed = (not slo_results) or all(r["ok"] for r in slo_results)
    if json_out:
        result = {"ctx": ctx, "summary": summary, "slo": slo_results, "passed": passed,
                  "snapshot": snapshot, "warnings": warnings}
        if diag_payload:
            result["diag"] = diag_payload
        if resources:
            result["resources"] = {k: dc_asdict(v) for k, v in (short_res or {}).items()}
        if base:
            result["compare"] = {"baseline": base.get("label"), "rows": compare_rows}
        if saved_to:
            result["saved_baseline"] = saved_to
        if report_file:
            result["report"] = report_file
        typer.echo(json.dumps(result, indent=2))

    if not passed:
        raise typer.Exit(1)


@app.command()
def capacity(
    name: str = typer.Option("", "--name", "-n"),
    scenario: str = typer.Option("journey", "--scenario", help="which workload to scale: journey | messages | read | mixed | login | badbot"),
    users_n: int = typer.Option(10, "--users", help="spread load across up to N seeded users; 0 = admin only"),
    slo: str = typer.Option("p95=500ms,error=2%", "--slo", help="the limit that defines 'capacity'"),
    start: int = typer.Option(10, "--start", help="first VU step"),
    max_vus: int = typer.Option(640, "--max", help="stop doubling past this many VUs"),
    step_duration: str = typer.Option("20s", "--step-duration", help="how long each step runs"),
    constrain: str = typer.Option("", "--constrain", help="find capacity on customer-sized hardware, e.g. \"rc=2cpu/2g\" (restored after)"),
    report: bool = typer.Option(False, "--report", help=f"write a markdown report to {config.reports_dir()}"),
    report_path: str = typer.Option("", "--report-path", help="write the report to this file/dir instead (implies --report)"),
    json_out: bool = typer.Option(False, "--json", help="print the result as JSON"),
) -> None:
    """Find how much concurrency a repro sustains before the SLO breaks.

    Doubles VUs (start, 2x, 4x, …) running the scenario at each step until a
    rule fails, then bisects between the last pass and first fail — ending with
    "handles ~N concurrent VUs" plus why it broke (event-loop lag at the wall).
    """
    _require_docker()
    from rc_repro import monitoring
    from rc_repro.perf import constrain as constrain_mod, k6, rcmetrics, slo as slo_mod
    if scenario not in k6.SCENARIOS or scenario in ("custom", "webhook"):
        _err("capacity supports the built-in scenarios (journey/messages/read/mixed/login/badbot)")
    try:
        rules = slo_mod.parse(slo)
    except ValueError as exc:
        _err(f"bad --slo: {exc}")
    if start < 1 or max_vus < start:
        _err("--start must be >= 1 and --max >= --start")
    constraints = {}
    if constrain:
        try:
            constraints = constrain_mod.parse(constrain)
        except ValueError as exc:
            _err(f"bad --constrain: {exc}")

    _target = _resolve_name(name)
    # Refuse rather than silently report nothing: a command that is
    # accepted and then does nothing is the failure rc-repro exists to remove.
    lcsvc.require_compose_topology(_target, 'capacity', 'It measures resource cost through the compose project, so results would be missing on Kubernetes.')
    # Kubernetes reachability is a port-forward that may have died; revive it before
    # talking HTTP, or this fails for a reason unrelated to what was asked.
    lcsvc.ensure_reachable(_target)
    m = runner.read_meta(_target)
    doc = runner.read_compose(m.name)
    target = _loadtest_target(doc)
    rc_services = _rc_services_in(doc) or ["rocketchat"]
    per_service = {}
    if constraints:
        try:
            per_service = constrain_mod.resolve_services(constraints, list(doc.get("services", {})))
        except ValueError as exc:
            _err(f"bad --constrain: {exc}")

    try:
        auth = _login(m)
        token = rcapi.generate_pat(m.root_url, auth, config.ADMIN_PASSWORD,
                                   token_name="rc-repro-loadtest", bypass_2fa=True)
    except Exception as exc:  # noqa: BLE001
        _err(f"could not authenticate (ready? `rc-repro ready --name {m.name}`): {exc}")
    users = _login_seed_users(m, users_n) if users_n > 0 else []

    # As in loadtest: every mutation (resource caps, rate limiter, the Prometheus
    # setting) lives inside the try so a failure or Ctrl-C in setup or mid-search
    # still restores. Restore-tracked vars are initialised first.
    applied_constraints: list = []
    limiter_was_off = True
    metrics_changed = False
    steps: list[dict] = []
    last_pass = first_fail = None

    def run_step(n: int, tag: str = "") -> dict:
        sampler = rcmetrics.RCMetricsSampler(m.name, rc_services).start()
        try:
            s = k6.run(m.name, scenario, vus=n, duration=step_duration, ramp=None,
                       token=token, uid=auth.user_id, target=target,
                       users=users or None, quiet=True)
        finally:
            rcm = sampler.stop()
        res = slo_mod.evaluate(rules, s)
        lag_max = 0.0
        for svc_m in rcm.values():
            lag = svc_m.get("eventloop_lag_max_s") or svc_m.get("eventloop_lag_s")
            if lag:
                lag_max = max(lag_max, lag["max"])
        row = {"vus": n, "rps": s.get("rps", 0.0), "p95": s.get("p95", 0.0),
               "error_rate": s.get("error_rate", 0.0), "ok": all(r["ok"] for r in res),
               "lag_max_s": lag_max,
               "breached": [f"{r['key']} {r['op']} {r['raw']} "
                            f"(actual {slo_mod.fmt_actual(r['key'], r['actual'])})"
                            for r in res if not r["ok"]]}
        steps.append(row)
        if not json_out:
            mark = typer.style("PASS", fg=typer.colors.GREEN) if row["ok"] else \
                typer.style(f"FAIL ({'; '.join(row['breached'])})", fg=typer.colors.RED)
            typer.echo(f"  {n:>4} VUs{tag:<9}  {row['rps']:>7.1f} req/s   "
                       f"p95 {fmt_ms(row['p95']):>7}   err {row['error_rate'] * 100:>5.2f}%   {mark}")
        return row

    try:
        if per_service:
            try:
                applied_constraints = constrain_mod.apply(m.name, per_service)
            except RuntimeError as exc:
                _err(f"could not apply --constrain: {exc}")
        limiter_was_off = rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                            config.RC_RATE_LIMITER_SETTING) is False
        if not limiter_was_off:
            rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                              config.RC_RATE_LIMITER_SETTING, False)
        if rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                             monitoring.RC_METRICS_SETTING) is not True:
            metrics_changed = rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                                monitoring.RC_METRICS_SETTING, True)
        identity = f"{len(users)} seeded users" if users else "admin token"
        if not json_out:
            typer.secho(f"Capacity search: {scenario} as {identity}, SLO {slo} "
                        f"(steps of {step_duration}"
                        + (f", constrained {constrain_mod.human(per_service)}" if per_service else "")
                        + ")\n", bold=True)
        n = start
        while n <= max_vus:
            row = run_step(n)
            if row["ok"]:
                last_pass = n
                n *= 2
            else:
                first_fail = n
                break
        if first_fail and last_pass:
            lo, hi = last_pass, first_fail
            for _ in range(2):   # two bisect rounds tighten the estimate enough
                mid = (lo + hi) // 2
                if mid <= lo or mid >= hi:
                    break
                row = run_step(mid, tag=" (bisect)")
                if row["ok"]:
                    lo = last_pass = mid
                else:
                    hi = first_fail = mid
    finally:
        if not limiter_was_off:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  config.RC_RATE_LIMITER_SETTING, True)
            except Exception:  # noqa: BLE001
                ui.warn("  ⚠ could not restore the API rate limiter setting")
        if metrics_changed:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  monitoring.RC_METRICS_SETTING, False)
            except Exception:  # noqa: BLE001
                ui.warn("  ⚠ could not restore the Prometheus metrics setting")
        for problem in constrain_mod.restore(applied_constraints):
            ui.warn(f"  ⚠ could not restore resource limits — {problem}")
        # users.json holds seeded-user tokens — don't leave them on disk.
        (runner.workspace(m.name) / "loadtest" / "users.json").unlink(missing_ok=True)

    if last_pass is None:
        result = f"breaches the SLO even at {start} VUs — start lower (--start)"
    elif first_fail is None:
        result = f"holds the SLO up to {last_pass} VUs (never breached; raise --max to push further)"
    else:
        result = f"~{last_pass} concurrent VUs (holds at {last_pass}, breaks at {first_fail})"
    why = ""
    # Explain the breach at the refined boundary (post-bisect), not the first
    # chronological fail — "breaks at 20" should be justified by the 20-VU step.
    breach_row = next((r for r in steps if r["vus"] == first_fail), None) if first_fail \
        else next((r for r in steps if not r["ok"]), None)
    if breach_row:
        if breach_row["lag_max_s"] >= 0.5:
            why = (f"at {breach_row['vus']} VUs the RC event loop saturated "
                   f"(lag peaked at {fmt_ms(breach_row['lag_max_s'] * 1000)})")
        else:
            why = f"at {breach_row['vus']} VUs: {'; '.join(breach_row['breached'])}"

    if not json_out:
        typer.echo("")
        typer.secho(f"Capacity: {result}", bold=True,
                    fg=typer.colors.GREEN if last_pass else typer.colors.RED)
        if why:
            ui.note(f"  why it broke: {why}")

    ctx = {"name": m.name, "version": m.rc_version, "scenario": scenario,
           "slo": slo, "users": len(users), "step_duration": step_duration,
           "target": target, "constrained": constrain_mod.human(per_service) if per_service else ""}
    if report or report_path:
        host = {"os": platform.platform(), "cpu": os.cpu_count() or "?",
                "docker": runner.docker_server_version() or "?",
                "compose": runner.compose_version() or "?"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path_ = perf_report.write_capacity(ctx, steps, result, why, host, stamp,
                                           dest=report_path or None)
        if not json_out:
            typer.echo("")
            ui.ok(f"✓ wrote {path_}")
    if json_out:
        typer.echo(json.dumps({"ctx": ctx, "steps": steps, "capacity_vus": last_pass,
                               "breach_vus": first_fail, "result": result, "why": why},
                              indent=2))


@app.command()
def logs(
    name: str = typer.Option("", "--name", "-n"),
    follow: bool = typer.Option(False, "--follow", "-f", help="stream logs"),
    tail: int = typer.Option(0, "--tail", help="only the last N lines (0 = all)"),
) -> None:
    """Tail a repro's logs."""
    _require_docker()
    target = _resolve_name(name)
    if lcsvc.topology_of_repro(target) == "kubernetes":
        from rc_repro.services import k8s as k8ssvc
        raise typer.Exit(k8ssvc.logs(target, follow=follow, tail=tail or None))
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


def _prompt_choice(prompt: str, choices: list[dict], default: str = "") -> str:
    """Numbered progressive choice without a full-screen TUI dependency."""
    if not choices:
        return default
    ids = [str(c.get("id", "")) for c in choices]
    typer.echo(prompt)
    for i, choice in enumerate(choices, 1):
        marker = " (default)" if str(choice.get("id", "")) == str(default) else ""
        typer.echo(f"  {i}. {choice.get('label', choice.get('id'))}{marker}")
    raw = typer.prompt("Choice", default=str(default or ids[0]), show_default=True)
    raw = str(raw).strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(choices):
            return str(choices[idx - 1].get("id", ""))
    if raw in ids:
        return raw
    if raw == "" and default:
        return default
    raise errors.ValidationError(
        f"invalid choice {raw!r}; pick 1-{len(choices)} or a known id")


def _prompt_scenarios(choices: list[dict], current: list[str]) -> list[str]:
    if not choices or (len(choices) == 1 and choices[0].get("id") == ""):
        return []
    typer.echo("Optional scenario (blank = deployment only):")
    for index, choice in enumerate(choices, start=1):
        typer.echo(f"  {index}. {choice.get('label') or choice.get('id')}")
    default = current[0] if current else ""
    valid = {str(c.get("id")).lower(): str(c.get("id")) for c in choices}
    while True:
        raw = str(typer.prompt(
            "Scenario", default=default, show_default=bool(default))).strip().lower()
        if not raw:
            return []
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return [str(choices[int(raw) - 1].get("id"))]
        if raw in valid:
            return [valid[raw]]
        ui.warn(
            f"  unknown scenario {raw!r}; pick 1-{len(choices)} or type an id")


@app.command()
def onboard(
    accept_defaults: bool = typer.Option(False, "--accept-defaults",
                                         help="take every default without prompting (for scripts/CI)"),
    grant: list[str] = typer.Option(None, "--grant",
                                    help="authority to hand over, repeatable (owned-cluster, engine-resize)"),
    retain_runs: Optional[bool] = typer.Option(
        None, "--retain-runs/--teardown-by-default",
        help="persist whether agent-driven runs are retained after evidence capture"),
    deployment: Optional[str] = typer.Option(
        None, "--deployment",
        help="default deployment type: default | multi-instance | microservices"),
    scenario: list[str] = typer.Option(
        None, "--scenario",
        help="default scenario (repeatable); empty list clears scenarios when passed as flag with accept-defaults"),
    seed_profile: Optional[str] = typer.Option(
        None, "--seed-profile",
        help="seed dataset for first-run command: none | small | standard | large"),
    section: Optional[str] = typer.Option(
        None, "--section",
        help="reconfigure only one section: deployment|scenarios|seed|cleanup|authority|capacity"),
    show: bool = typer.Option(
        False, "--show",
        help="print the setup snapshot without changing anything"),
    reconfigure: bool = typer.Option(
        False, "--reconfigure",
        help="ask settled human questions again so their answers can be changed"),
    json_out: bool = typer.Option(False, "--json", help="emit the resulting state as JSON"),
) -> None:
    """Goal-first setup: deployment, scenario, seed, cleanup, then Kubernetes only if needed.

    Durable choices are asked once. Compose never asks Kubernetes authority or
    capacity questions. Non-TTY runs use --accept-defaults plus explicit flags.
    """
    # --show is always non-mutating and never blocks on prompts.
    if show:
        snap = onboardsvc.setup_snapshot(section=section)
        if json_out:
            jsonout.emit(jsonout.envelope("setup", snap))
            return
        typer.echo(f"setup completed: {snap['completed']}")
        typer.echo(f"  deployment: {snap['selection']['deployment']}")
        typer.echo(f"  scenarios: {', '.join(snap['selection']['scenarios']) or '(none)'}")
        typer.echo(f"  seed: {snap['selection']['seed_profile']}")
        typer.echo(f"  retain runs: {snap['selection']['retain_runs']}")
        if snap["first_run_command"]:
            typer.echo(f"  first run: {snap['first_run_command']}")
        else:
            ui.warn(f"  first run: blocked by {snap['compatibility']['code']}")
        return

    structured = any(v is not None for v in (
        deployment, seed_profile, retain_runs)) or bool(grant) or scenario is not None
    interactive = not accept_defaults and not json_out and not structured

    if json_out and not accept_defaults and not structured and not show:
        exc = errors.ValidationError(
            "--json is non-interactive; pair it with --accept-defaults, explicit "
            "flags (--deployment/--scenario/--seed-profile/--grant), or --show")
        jsonout.fail(exc)

    current = onboardsvc.state()
    environment = onboardsvc.detect_environment()
    snap = onboardsvc.setup_snapshot(environment=environment, section=section)

    # Returning user with nothing to do: settle silently unless reconfigure/section.
    has_unanswered_applicable = any(
        not question.get("answered", False) for question in snap["questions"])
    if (current["completed"] and not has_unanswered_applicable and
            not reconfigure and not section and interactive and not structured):
        typer.echo("Settled choices are unchanged; use --reconfigure or "
                   "--section <name> to change them.")
        if snap["first_run_command"]:
            ui.hint(f"  first run: {snap['first_run_command']}")
        else:
            ui.warn(f"  first run: blocked by {snap['compatibility']['code']}")
        return

    patch: dict = {}
    if deployment is not None:
        patch["deployment"] = deployment
    if scenario is not None:
        patch["scenarios"] = list(scenario)
    if seed_profile is not None:
        patch["seed_profile"] = seed_profile
    if retain_runs is not None:
        patch["retain_runs"] = retain_runs
    if grant:
        patch["grants"] = {name: True for name in grant}

    if interactive:
        typer.echo("rc-repro setup — goal first, durable choices asked once.\n")
        typer.echo("Detected environment")
        typer.echo(f"  OS: {environment['os']} ({environment['architecture']})")
        typer.echo(f"  host: {environment['cpus']} CPUs, "
                   f"{environment['memory_gib']:.1f} GiB memory, "
                   f"{environment['disk_free_gib']:.1f} GiB disk free")
        for tool in ("docker", "compose", "kind", "kubectl", "helm"):
            typer.echo(f"  {tool}: {environment['tools'].get(tool, 'missing')}")
        if environment.get("docker_ready"):
            typer.echo(
                f"  engine ({environment.get('engine_provider', '?')}): "
                f"{environment['engine_cpus']} CPUs, "
                f"{environment['engine_memory_gib']:.1f} GiB")
            if environment.get("engine_kernel_version"):
                typer.echo(f"  engine kernel: {environment['engine_kernel_version']}")
        typer.echo("")

        draft: dict = {}
        want_sections = (
            [section] if section else
            list(onboardsvc.SETUP_SECTIONS)
        )

        # Progressive draft: later questions use earlier answers.
        if "deployment" in want_sections and (
                reconfigure or section == "deployment" or
                not snap["persisted"]["answered_deployment"] or not current["completed"]):
            dep_q = next(q for q in onboardsvc.setup_snapshot(
                environment=environment, draft=draft)["questions"]
                         if q["id"] == "deployment")
            draft["deployment"] = _prompt_choice(
                dep_q["prompt"], dep_q["choices"],
                default=str(dep_q.get("value") or "default"))

        live = onboardsvc.setup_snapshot(environment=environment, draft=draft)
        topology = live["selection"]["topology"]

        if "scenarios" in want_sections and (
                reconfigure or section == "scenarios" or
                not snap["persisted"]["answered_scenarios"] or not current["completed"]):
            scen_q = next((q for q in live["questions"] if q["id"] == "scenarios"), None)
            if scen_q and scen_q["applicable"]:
                draft["scenarios"] = _prompt_scenarios(
                    scen_q["choices"], list(scen_q.get("value") or []))

        if "seed" in want_sections and (
                reconfigure or section == "seed" or
                not snap["persisted"]["answered_preferences"].get("seed_profile")
                or not current["completed"]):
            live = onboardsvc.setup_snapshot(environment=environment, draft=draft)
            seed_q = next(q for q in live["questions"] if q["id"] == "seed_profile")
            draft["seed_profile"] = _prompt_choice(
                seed_q["prompt"], seed_q["choices"],
                default=str(seed_q.get("value") or "small"))

        if "cleanup" in want_sections and (
                reconfigure or section == "cleanup" or
                not snap["persisted"]["answered_preferences"].get("retain_runs")
                or not current["completed"]):
            typer.echo("\nAgent-driven runs capture evidence and tear down by default.")
            draft["retain_runs"] = typer.confirm(
                "Retain repros after evidence capture instead?",
                default=bool(live["selection"].get("retain_runs")))

        live = onboardsvc.setup_snapshot(environment=environment, draft=draft)
        topology = live["selection"]["topology"]

        # Kubernetes-only questions. Compose never reaches these.
        if topology == "kubernetes":
            if "authority" in want_sections:
                cluster_answered = live["persisted"]["answered_grants"].get(
                    "owned_cluster", False)
                if reconfigure or section == "authority" or not cluster_answered:
                    typer.echo("\nrc-repro uses one local Kind cluster and one namespace "
                               "per repro.")
                    ui.note("  It creates or deletes only resources with ownership labels.")
                    owned = typer.confirm(
                        "May rc-repro create and later delete those owned resources?",
                        default=bool(live["persisted"]["grants"].get("owned_cluster")))
                    draft.setdefault("grants", {})
                    draft["grants"] = {
                        **dict(draft.get("grants") or {}),
                        "owned-cluster": owned,
                    }

            live = onboardsvc.setup_snapshot(environment=environment, draft=draft)
            cap = live["capacity"]
            if "capacity" in want_sections and cap.get("applicable"):
                typer.echo("\nKubernetes capacity")
                typer.echo(f"  provider: {cap.get('provider')}")
                typer.echo(
                    f"  observed: {cap.get('observed_cpu')} CPUs, "
                    f"{cap.get('observed_memory_gib')} GiB")
                typer.echo(
                    f"  required: {cap.get('required_cpu')} CPUs, "
                    f"{cap.get('required_memory_gib')} GiB")
                if not cap.get("ready"):
                    ui.warn(f"  {cap.get('code')}: {cap.get('remediation')}")
                    if cap.get("verification"):
                        ui.note(f"  verify: {cap.get('verification')}")
                resize_q = next(
                    (q for q in live["questions"] if q["id"] == "engine_resize"), None)
                resize_answered = live["persisted"]["answered_grants"].get(
                    "engine_resize", False)
                if (resize_q and resize_q["applicable"] and
                        (reconfigure or section == "capacity" or not resize_answered)):
                    ui.warn("  Resizing restarts the engine and stops unrelated containers.")
                    granted = typer.confirm(
                        "May rc-repro stop, resize, and restart the Podman machine?",
                        default=bool(live["persisted"]["grants"].get("engine_resize")))
                    draft.setdefault("grants", {})
                    draft["grants"] = {
                        **dict(draft.get("grants") or {}),
                        "engine-resize": granted,
                    }

            typer.echo("\nThe microservices deployment is an Enterprise topology.")
            ui.warn("  Without a Rocket.Chat Enterprise licence it may not behave as licensed.")
        else:
            # Compose path: never print kubernetes capacity questions.
            typer.echo("\nCompose deployment selected — Kubernetes authority and "
                       "capacity questions are skipped.")

        live = onboardsvc.setup_snapshot(environment=environment, draft=draft)
        typer.echo("\nReview")
        typer.echo(f"  deployment: {live['review']['deployment']}")
        typer.echo(f"  scenarios: {', '.join(live['review']['scenarios']) or '(none)'}")
        typer.echo(f"  seed: {live['review']['seed_profile']}")
        if live["license"].get("seed_deferred"):
            ui.warn("  seed is deferred until an Enterprise registration token "
                    "is supplied")
        typer.echo(f"  retain runs: {live['review']['retain_runs']}")
        if live["selection"]["topology"] == "kubernetes":
            typer.echo(f"  owned-cluster: {live['review']['grants']['owned_cluster']}")
            typer.echo(f"  engine-resize: {live['review']['grants']['engine_resize']}")
        if live["first_run_command"]:
            typer.echo(f"  first run: {live['first_run_command']}")
        else:
            ui.warn(
                f"  first run: blocked by {live['compatibility']['code']}")
        if not typer.confirm("Apply these choices?", default=True):
            ui.note("No changes written.")
            raise typer.Exit(0)
        patch.update(draft)
        if "grants" in draft:
            patch["grants"] = draft["grants"]
    elif accept_defaults or structured:
        # Non-interactive: only fill unanswered questions with safe defaults.
        # Never revoke an earlier standing grant on a safe rerun unless flags say so.
        if "deployment" not in patch and (
                not current["completed"] or reconfigure or section == "deployment"):
            if not snap["persisted"]["answered_deployment"]:
                patch["deployment"] = snap["selection"]["deployment"] or "default"
        if "scenarios" not in patch and scenario is None and (
                not snap["persisted"]["answered_scenarios"] and not current["completed"]):
            patch["scenarios"] = list(snap["selection"]["scenarios"] or [])
        if "seed_profile" not in patch and (
                not snap["persisted"]["answered_preferences"].get("seed_profile")
                and not current["completed"]):
            patch["seed_profile"] = "small"
        if "retain_runs" not in patch and (
                not snap["persisted"]["answered_preferences"].get("retain_runs")
                and not current["completed"]):
            patch["retain_runs"] = False
        # Deny unanswered grants only when accepting defaults on a fresh machine,
        # or when the selected deployment needs an explicit grant list.
        grants_map = dict(patch.get("grants") or {})
        preview = onboardsvc.setup_snapshot(environment=environment, draft=patch)
        applicable_grants = {
            question["grant"] for question in preview["questions"]
            if question.get("grant")
        }
        for name in applicable_grants:
            key = onboardsvc.grant_key(name)
            if key in grants_map or name in grants_map:
                continue
            if not current["answered_grants"].get(key, False) and accept_defaults:
                grants_map[name] = False
        if grants_map:
            patch["grants"] = grants_map

    try:
        result_snap = onboardsvc.apply_setup_patch(patch, mark_complete=True)
    except errors.ReproError as exc:
        jsonout.fail(exc) if json_out else _fail(exc)

    # Re-read environment for the final command so readiness is not stale.
    result_snap = onboardsvc.setup_snapshot(environment=environment)
    first_run = result_snap["first_run_command"]
    state = onboardsvc.state()

    if json_out:
        jsonout.emit(jsonout.envelope("onboard", {
            **state,
            "setup": result_snap,
            "first_run_command": first_run,
            "selection": result_snap["selection"],
            "capacity": result_snap["capacity"],
            "compatibility": result_snap["compatibility"],
            "gates": result_snap["gates"],
            "actions": result_snap["actions"],
        }))
        return

    ui.ok("✓ onboarding recorded")
    typer.echo(f"  deployment: {result_snap['selection']['deployment']}")
    typer.echo(f"  scenarios: {', '.join(result_snap['selection']['scenarios']) or '(none)'}")
    typer.echo(f"  seed: {result_snap['selection']['seed_profile']}")
    if result_snap["license"].get("seed_deferred"):
        ui.warn("  seed is deferred until an Enterprise registration token is supplied")
    for name in sorted(onboardsvc.GRANTS):
        key = onboardsvc.grant_key(name)
        mark = "granted" if state["grants"].get(key) else "not granted"
        # Only print kubernetes grants when they apply or were answered.
        if (result_snap["selection"]["topology"] == "kubernetes" or
                state["answered_grants"].get(key)):
            typer.echo(f"  {name}: {mark}")
    typer.echo(f"  retain runs: {state['preferences']['retain_runs']}")
    if first_run:
        ui.hint(f"  first run: {first_run}")
    else:
        ui.warn(
            f"  first run: blocked by {result_snap['compatibility']['code']}")
    ui.hint("  change an answer with `rc-repro onboard --reconfigure` "
            "or `--section <name>`")


skill_app = typer.Typer(help="Install the rc-repro agent skill into an agent host.")
app.add_typer(skill_app, name="skill")


@skill_app.command("install")
def skill_install(
    host: str = typer.Option("all", "--host", help="claude | codex | all"),
    scope: str = typer.Option("user", "--scope", help="user | project"),
    force: bool = typer.Option(False, "--force", help="overwrite a locally edited skill"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Install the skill that shipped with this rc-repro.

    The bundle lives inside the package, so the installed skill always describes
    the rc-repro you are actually running. Idempotent: re-run it to repair drift.
    """
    try:
        results = (skillsvc.install_all(scope, force=force) if host == "all"
                   else [skillsvc.install(host, scope, force=force)])
    except errors.ReproError as exc:
        jsonout.fail(exc) if json_out else _fail(exc)
    payload = [{"host": r.host, "scope": r.scope, "path": str(r.path),
                "state": r.state, "version": r.installed_version} for r in results]
    if json_out:
        jsonout.emit(jsonout.envelope("skill-install", {"installed": payload}))
        return
    for r in payload:
        ui.ok(f"✓ {r['host']}: {r['path']}")
    # Cursor and Copilot read these same directories, so they need no separate copy.
    ui.hint("  cursor and copilot read these directories too — nothing more to do")


@skill_app.command("status")
def skill_status(
    scope: str = typer.Option("user", "--scope", help="user | project"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Report whether each host's installed skill matches this rc-repro."""
    rows = []
    for h in sorted(skillsvc.HOSTS):
        st = skillsvc.status(h, scope)
        rows.append({"host": h, "scope": st.scope, "path": str(st.path),
                     "state": st.state, "installed_version": st.installed_version})
    if json_out:
        jsonout.emit(jsonout.envelope(
            "skill-status", {"bundled_version": skillsvc.__version__, "installs": rows}))
        return
    for r in rows:
        line = f"  {r['host']:8} {r['state']:9} {r['path']}"
        (ui.ok if r["state"] == "current" else ui.warn)(line)
    if any(r["state"] != "current" for r in rows):
        ui.hint("  repair with: rc-repro skill install")


@app.command()
def evidence(
    name: str = typer.Option("", "--name", "-n"),
    bundle: str = typer.Option("", "--bundle", help="also write logs and the rendered artifact to this directory"),
    retain_for_task: bool = typer.Option(
        False, "--retain-for-task",
        help="record that the current task explicitly requires retaining this repro"),
    json_out: bool = typer.Option(True, "--json/--no-json", help="the record is JSON; --no-json prints a summary"),
) -> None:
    """Emit a secret-safe record of what was deployed and how it is behaving.

    Safe to attach to a support case: the root URL is reduced to its origin and no
    token, licence, or password appears anywhere.
    """
    try:
        payload = evidencesvc.record(
            name,
            retained=True if retain_for_task else None,
            reason="explicit task" if retain_for_task else None,
        )
        if bundle:
            payload["bundle"] = evidencesvc.write_bundle(payload["repro"]["name"],
                                                         bundle, payload)
    except errors.ReproError as exc:
        jsonout.fail(exc) if json_out else _fail(exc)
    if json_out:
        jsonout.emit(jsonout.envelope("evidence", payload))
        return
    r = payload["repro"]
    typer.echo(f"{r['name']}  {r['rc_version']}  {r['topology']}  {payload['runtime']['state']}")
    typer.echo(f"  artifact  {payload['artifact']['name']} sha256:{payload['artifact']['sha256'][:12]}")
    typer.echo(f"  licensed  required={payload['license']['required']} supplied={payload['license']['supplied']}")
    typer.echo(f"  retained  {payload['retention']['retained']}"
               + (f" ({payload['retention']['reason']})"
                  if payload['retention']['reason'] else ""))
    typer.echo(f"  cleanup   {payload['retention']['cleanup']}")


@app.command()
def capabilities() -> None:
    """Report what this rc-repro build can do, for scripts and agent skills.

    No --json flag: the record is JSON by definition, and a flag that is accepted
    and then ignored is worse than no flag.

    Answers offline and without a container engine on purpose: a caller asks this
    before it knows whether the environment works. Engine checks live in `doctor`.
    """
    jsonout.emit(jsonout.envelope("capabilities", jsonout.capabilities(app)))


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
def doctor(
    json_out: bool = typer.Option(False, "--json", help="emit the stable preflight record instead of a report"),
) -> None:
    """Preflight: check Docker, Compose, disk, connectivity and ports.

    This is the call an agent makes before committing to a create, so `--json`
    returns the same envelope every other verb uses, with one entry per check
    carrying a stable id and ok|warn|fail, and exits 3 (preflight) on any fail so
    the agent stops before attempting `up`.
    """
    counts = {"ok": 0, "warn": 0, "fail": 0}
    marks = {
        "ok": ("\u2713", typer.colors.GREEN),
        "warn": ("\u26a0", typer.colors.YELLOW),
        "fail": ("\u2717", typer.colors.RED),
    }
    checks: list[dict] = []

    def line(status: str, msg: str, check: str = "") -> None:
        counts[status] += 1
        # A stable id per check so an agent branches on the id, not the prose. If a
        # call site gives none, derive a slug from the message's first words — good
        # enough to be stable across reworded tails, and every load-bearing check
        # passes one explicitly below.
        cid = check or "-".join(re.findall(r"[a-z0-9]+", msg.lower())[:3])
        checks.append({"check": cid, "status": status, "message": msg})
        if json_out:
            return
        sym, color = marks[status]
        typer.secho(f"{sym} {msg}", fg=color)

    # Docker daemon (everything else that needs Docker degrades gracefully).
    docker_up = runner.docker_available()
    if docker_up:
        line("ok", f"Docker daemon running ({runner.docker_server_version() or '?'})", "docker-daemon")
    else:
        line("fail", "Docker daemon not running — start Docker Desktop / dockerd", "docker-daemon")

    # Compose v2 and v5 are the supported CLIs. v5 deliberately keeps the v2
    # command surface and Compose Specification; its new major identifies the
    # official Go SDK rather than a breaking CLI/file-format generation.
    cv = runner.compose_version()
    compose_match = re.match(r"v?(\d+)(?:\.|$)", (cv or "").strip())
    compose_major = int(compose_match.group(1)) if compose_match else None
    if compose_major in {2, 5}:
        line("ok", f"docker compose v{compose_major} ({cv})", "compose-version")
    elif cv:
        line("warn", f"docker compose {cv} — rc-repro expects Compose v2 or v5",
             "compose-version")
    else:
        line("warn", "couldn't detect `docker compose` — install Compose v2 or v5",
             "compose-version")

    # Engine/VM kernel vs Mongo 8 (SERVER-121912): mongod 8.0 hard-exits on
    # kernel >= 6.19, which recent RC versions require. Common on fresh Podman /
    # FCOS machines and easy to misread as a volume/permission failure.
    if docker_up:
        kv = runner.docker_kernel_version()
        mm = doctorsvc._kernel_major_minor(kv) if kv else None
        if mm and mm >= (6, 19):
            line("warn", f"engine kernel {kv} — MongoDB 8.0 will not start (SERVER-121912); "
                         "use an engine on kernel < 6.19 for RC versions that require Mongo 8",
                 "kernel-mongo8")
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

    # Repro summary.
    repros = lcsvc.list_repros() if docker_up else []
    if repros:
        running = sum(1 for repro in repros if repro["state"] == "running")
        typer.echo(f"  repros: {len(repros)} total, {running} running")

    # Kubernetes topology readiness. Reported rather than required: the Kubernetes
    # preset is opt-in, so a missing toolchain is a warning for the Docker user and
    # only becomes an error when `up --preset microservices` actually asks for it.
    from rc_repro.services import k8s as _k8s
    k8s_tools = {t: shutil.which(t) for t in ("kind", "kubectl", "helm")}
    missing_k8s = [t for t, path in k8s_tools.items() if not path]
    if missing_k8s:
        line("warn", "Kubernetes presets need " + ", ".join(missing_k8s) +
                     " on PATH (not needed for Docker presets)", "k8s-tools")
    else:
        line("ok", "kind, kubectl and helm present (Kubernetes presets available)", "k8s-tools")
        k8s_state_ok = True
        try:
            state = _k8s.prepare_client_state()
            line("ok", "rc-repro Kubernetes and Helm client state is writable at "
                       f"{state.kubeconfig.parent.parent}", "k8s-client-state")
        except OSError as exc:
            k8s_state_ok = False
            line("fail", "rc-repro Kubernetes and Helm client state is not writable: "
                         f"{exc}", "k8s-client-state")
        mem_gib, cpus = _k8s.engine_capacity()
        if mem_gib or cpus:
            floor_ok = mem_gib >= _k8s.FLOOR_MEMORY_GIB and cpus >= _k8s.FLOOR_CPUS
            msg = (f"engine has {mem_gib:.1f} GiB and {cpus} CPUs; the microservices "
                   f"preset needs {_k8s.FLOOR_MEMORY_GIB:g} GiB and {_k8s.FLOOR_CPUS}")
            # CPU is the binding constraint during start-up, so it is named too: a
            # memory-only report would look fine on a host that then crawls.
            line("ok" if floor_ok else "warn", msg, "k8s-floor")
        if k8s_state_ok and _k8s.cluster_exists():
            line("ok", f"rc-repro cluster {_k8s.CLUSTER_NAME} exists (reused, so `up` is faster)")
        stale = lcsvc.stale_forwards() if k8s_state_ok else []
        if stale:
            names = ", ".join(r["name"] for r in stale)
            line("warn", f"port-forward down for: {names} "
                         "(re-established on the next ready/info; not an error)", "k8s-forwards")

    if json_out:
        payload = {"checks": checks, "counts": counts,
                   "ready": counts["fail"] == 0}
        jsonout.emit(jsonout.envelope("doctor", payload))
        # Exit 3 (preflight) on any fail, so an agent stops before `up`. A warn is
        # usable, so it does not change the exit code.
        if counts["fail"]:
            raise typer.Exit(errors.DockerError.exit_code)
        return

    typer.echo("")
    if counts["fail"]:
        typer.secho("Not ready — fix the ✗ item(s) above.", fg=typer.colors.RED)
        raise typer.Exit(errors.DockerError.exit_code)
    if counts["warn"]:
        typer.secho("Usable, with warnings above.", fg=typer.colors.YELLOW)
    else:
        typer.secho("All good — rc-repro is ready.", fg=typer.colors.GREEN)


@app.command()
def serve(
    port: int = typer.Option(7070, "--port", help="host port for the web GUI"),
    bind: str = typer.Option("127.0.0.1", "--bind", help="interface to bind (loopback by default; use 0.0.0.0 behind a proxy/remote lab)"),
    allow_host: list[str] = typer.Option(None, "--allow-host", help="extra Host header to accept, e.g. a reverse-proxy domain (repeatable; '*' = any host). Needed for iximiuz/Codespaces/remote access"),
    no_open: bool = typer.Option(False, "--no-open", help="don't open a browser"),
    no_token: bool = typer.Option(False, "--no-token", help="disable the session token (loopback dev / trusted proxy only)"),
) -> None:
    """Launch the local web GUI (needs `pip install 'rc-repro[gui]'`).

    Behind a reverse proxy (iximiuz Labs, Codespaces, ngrok, …): bind a reachable
    interface and allow the proxy's hostname, e.g.
      rc-repro serve --bind 0.0.0.0 --allow-host '*' --no-token
    (in an ephemeral lab that's fine; otherwise keep the token and append
    `?t=<token>` to the proxy URL, and pass the real --allow-host domain).
    """
    try:
        import uvicorn
        from rc_repro.web.app import create_app
    except ImportError:
        _err("the web GUI needs extra deps — install them with: pip install 'rc-repro[gui]'")
    import secrets
    import webbrowser

    allow = list(allow_host or [])
    token = "" if no_token else secrets.token_urlsafe(16)
    loopback = bind in ("127.0.0.1", "localhost", "::1")
    if not loopback:
        ui.warn(f"  ⚠ binding {bind} exposes docker control (create/delete repros + volumes) "
                "to your network — use only if you mean to.")
    if "*" in allow:
        ui.warn("  ⚠ --allow-host '*' accepts ANY Host header — only on a trusted/ephemeral network.")
    url = f"http://localhost:{port}/" + (f"?t={token}" if token else "")
    typer.secho(f"rc-repro GUI: {url}", bold=True)
    if token:
        ui.hint("  (the ?t=... token authorizes this browser session)")
        if not loopback or allow:
            ui.hint(f"  via a proxy? open the proxy URL with the token appended: ...<proxy-url>/?t={token}")
    app_obj = create_app(token=token, allow_hosts=allow)
    if not no_open and loopback:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless / no browser is fine
            pass
    uvicorn.run(app_obj, host=bind, port=port, log_level="warning")


if __name__ == "__main__":
    sys.exit(app())
