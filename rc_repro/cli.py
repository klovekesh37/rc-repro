"""rc-repro command-line interface (Typer)."""

from __future__ import annotations

import dataclasses
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
from rc_repro.services import journal
from rc_repro.services import envvars as envsvc
from rc_repro.services import lifecycle as lcsvc
from rc_repro.services import topology
from rc_repro.services.events import Event, null_emit

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Launch version-matched Rocket.Chat reproduction environments.",
)


def _print_version(value: bool) -> None:
    """`--version`, eager so it answers before any command is resolved.

    It did not exist, while CONTRIBUTING and this project's own notes told people
    to run it to check what a box has -- and the only place the number appeared at
    all was the sign-in page footer, which needs the GUI up and reachable to read.
    So the one question you ask a remote box after deploying to it ("did the pull
    land?") had no answer from a shell, and a version bump meant to make that
    answerable was answerable only in a browser.

    Read from the INSTALLED distribution metadata, like `__version__` itself: that
    is the number the running code actually is, and a stale editable install
    reporting an old one is the true answer, not a bug to paper over.
    """
    if value:
        from rc_repro import __version__
        typer.echo(f"rc-repro {__version__}")
        raise typer.Exit(0)


@app.callback()
def _before_any_command(
    version: bool = typer.Option(False, "--version", "-V", is_eager=True,
                                 callback=lambda v: _print_version(v),
                                 help="print the installed rc-repro version and exit"),
) -> None:
    """Runs before every command.

    Publishes who is running this so the service layer can audit without every
    command passing an actor down. Without it, `rc-repro down --volumes` on a
    shared box appended a line reading `-` -- present, but useless.
    """
    from rc_repro.services import audit as auditsvc
    actor, how = _cli_actor_and_origin()
    auditsvc.set_actor(actor)
    auditsvc.set_origin(how)

# --- helpers ------------------------------------------------------------------


def _err(msg: str, exit_code: int = 1) -> NoReturn:
    """Error-exit: a red line on stderr and a non-zero status.

    In JSON mode it is an error envelope instead, because the contract is that
    EVERY reply is one. This wrapper exists so that the forty-eight call sites
    that predate jsonout.py -- `up`'s `--set` parsing, `config-import`'s missing
    file, every "can't seed" -- honour it without being touched. A contract that
    holds only on the paths somebody remembered is worse than none: a caller gets
    clean JSON until the moment something goes wrong, which is the moment it needed
    the JSON.

    The code is the base `REPRO_ERROR` and the exit stays 1, exactly as before.
    Saying "unclassified" is honest; picking a plausible code would tell a caller
    to retry, or not to, on no evidence.
    """
    _log_failure(errors.ReproError.code, msg)
    if jsonout.active():
        jsonout.fail(errors.ReproError(msg))
    ui.die(msg, exit_code=exit_code)


def _log_failure(code: str, message: str) -> None:
    """Record a failure in the durable log, with the stable code beside the prose.

    Both `_err` and `_fail` route through here, which between them are every error path
    in this file -- the same reason those two wrappers exist at all. Best-effort and
    silent: the failure being reported matters more than logging it.
    """
    try:
        from rc_repro.services import eventlog
        eventlog.failed(code, message)
    except Exception:  # noqa: BLE001
        pass


def _fail(exc: errors.ReproError) -> NoReturn:
    """Exit on a domain error with the exit code its CLASS defines.

    One helper instead of a mapping at each of the 26 handlers, so errors.py
    stays the only place an exit code is decided. Before this every failure was
    exit 1, so a script could tell that something went wrong but never what: a
    workspace that is still booting and one that is known dead looked identical.
    See errors.EXIT_CODES for the published map.
    """
    _log_failure(getattr(exc, "code", "") or errors.ReproError.code, str(exc))
    if jsonout.active():
        jsonout.fail(exc)
    ui.die(str(exc), exit_code=exc.exit_code)


def _resolve_name(name: str | None) -> str:
    """Return the target repro name: explicit, else the configured default.

    Delegates to the service layer so both front-ends share one implementation
    (and one set of name-validation rules) rather than drifting apart.

    The actor goes with it so a name typed WITHOUT the owner prefix still finds
    the workspace it created: `up --name test` makes `alice-test`, and every other
    command has to accept `test` for it or the documented workflow breaks the day
    somebody runs `users add`."""
    try:
        return lcsvc.resolve_name(name, actor=_cli_actor())
    except errors.ReproError as exc:
        _fail(exc)


def _require_docker() -> None:
    """Refuse early when the engine is down, with a code rather than a flat 1.

    It used to call `_err()` directly, so the single most common failure there is
    exited 1 -- the one value that says nothing. Delegating to the service means
    it now carries whatever code its class defines, and the class is decided in
    one place for both front-ends instead of here.
    """
    try:
        lcsvc.require_docker()
    except errors.ReproError as exc:
        _fail(exc)


def _require_engine(target: str) -> None:
    """The engine THIS workspace needs. See `lifecycle.require_engine`.

    Every caller resolves the name first, which is the whole point: the gate cannot
    know which engine to ask for until it knows what it is being asked about.
    """
    try:
        lcsvc.require_engine(target)
    except errors.ReproError as exc:
        _fail(exc)


def _not_answering(m: runner.Metadata, exc: Exception, what: str) -> NoReturn:
    """A REST command that could not reach or could not authenticate to a workspace.

    A CONNECTION failure is NOT_READY -- exit 5, "poll again" -- and not the
    unclassified 1. Every one of these messages already told the reader to run
    `rc-repro ready`, while exiting the one code that tells a script not to bother
    polling; and `services/monitor.py` raises NotReadyError for the very same
    condition, so an unreachable workspace answered differently depending on which
    verb you reached for. Measured: `api` against a workspace whose Rocket.Chat
    container was still coming back from an `env` change exited 1.

    Anything else -- a refused login, a 2FA challenge nothing can satisfy -- stays
    unclassified, because it is not something waiting will fix.
    """
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        _fail(errors.NotReadyError(
            f"{m.name!r} is not answering yet, so {what} cannot run — wait for it with "
            f"`rc-repro ready --name {m.name}`"))
    # A 429 is the other "wait and it will work": Rocket.Chat rate-limits token
    # creation per user, so two perf runs back to back -- which is exactly the
    # `--save baseline` then `--compare baseline` pair the README prints -- have the
    # second refused. Reported as unclassified it read like a broken workspace.
    if "429" in str(exc) or "Too Many Requests" in str(exc):
        _fail(errors.NotReadyError(
            f"Rocket.Chat is rate-limiting token creation on {m.name!r}, so {what} "
            "cannot run yet. Its limiter resets in about a minute — try again then."))
    # No "ready?" here: the two conditions waiting actually fixes are handled above, so
    # telling everybody else to poll sent them to a command that could not help.
    _err(f"{what} failed: {exc}")


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


def _cli_actor() -> str:
    return _cli_actor_and_origin()[0]


def _cli_actor_and_origin() -> tuple[str, str]:
    """Who is running the CLI, and how confident we are about it.

    The second value is the audit log's `origin` column, and it exists because
    these two cases are NOT the same evidence:

      local     os.getlogin() matched a known account. The OS said so.
      asserted  RC_REPRO_USER was set. Honoured even for an unknown name (see
                below), so it is a claim, not a check.

    Recording them identically would make every line only as trustworthy as the
    weakest one.

    Team mode is opt-in: it starts the moment somebody runs `rc-repro users add`.
    Until then this returns "" and every workspace keeps the name it has always
    had. Once accounts exist, the login name is used if it matches one, so nobody
    has to remember a flag before typing `up`. RC_REPRO_USER overrides it for the
    case where the OS account and the GUI account differ.
    """
    from rc_repro.services import users
    if not users.any_users():
        return "", "system"
    known = {u.name for u in users.list_users()}
    named = os.environ.get("RC_REPRO_USER", "").strip().lower()
    if named:
        # Explicit: honoured even if unknown, so the mismatch shows up in `list`
        # rather than silently falling back to the shared namespace.
        return named, "asserted"
    try:
        login = os.getlogin()
    except OSError:                     # no controlling terminal (cron, container)
        login = os.environ.get("USER", "")
    login = login.strip().lower()
    return (login, "local") if login in known else ("", "system")


def _summary_panel(meta: runner.Metadata, extra_rows: list[tuple[str, str]] | None = None) -> None:
    """The boxed repro summary (URL + login + versions), shared by up/ready/info,
    followed by multi-instance URLs. Title is the repro name only — kept pure
    ASCII so box-drawing alignment can't be thrown off by wide/emoji glyphs
    (status like "✓ ready" is printed on its own line by the caller)."""
    # `mongo_flavor` describes a COMPOSE image choice, and the Kubernetes path makes
    # its own -- so printing it there claimed an image that workspace does not run.
    # Where the runtime recorded what it actually built, say that instead.
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    managed_by = extra.get("mongo_managed_by", "")
    mongo = (f"{meta.mongo_tag} (via the {managed_by})" if managed_by
             else f"{meta.mongo_tag} ({meta.mongo_flavor})")
    rows = [
        ("Rocket.Chat", meta.rc_version),
        ("MongoDB", mongo),
        ("Preset", meta.preset),
        # external_url, not root_url: with --https the browser wants the https URL,
        # while root_url stays the plain http one rc-repro's own API calls use.
        ("URL", meta.external_url),
        ("Login", f"{config.ADMIN_USERNAME} / {config.ADMIN_PASSWORD}"),
    ]
    owner = meta.extra.get("created_by", "") if isinstance(meta.extra, dict) else ""
    if owner:
        rows.append(("Owner", owner))
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
    """Print a service progress event on the terminal. Terminal events, and the
    INFORMATIONAL `done` ones, are suppressed — the command wrapper prints the final
    panel itself and would otherwise say it twice.

    A `done` WARNING is a different thing and is printed. The suppression used to key
    on the phase alone, so six warnings a service raises at the end of its work reached
    nobody on the CLI: an orphaned namespace that could not be removed, a workspace
    skipped by `prune`, a cluster left alone, a monitoring volume left behind, and the
    one k8s.py:3560 exists to say -- that `stop` frees Rocket.Chat's memory and NOT
    operator-managed MongoDB's. Each was written to be read at exactly the moment it
    fires, and `phase="done"` is where a service says what it finished doing."""
    if ev.terminal or (ev.phase == "done" and ev.level == "info"):
        return
    if ev.level in ("warn", "error"):
        ui.warn("  " + ev.message)
    else:
        typer.echo(f"  {ev.message}")


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
    _print_notes(meta)


@app.command()
def up(
    version: str = typer.Option(..., "--version", "-v", help="Rocket.Chat version, e.g. 6.5.3"),
    preset: str = typer.Option("default", "--preset", "-p", help="scenario to apply: ldap, saml, oidc, email, s3_minio, livechat, airgapped"),
    runtime: str = typer.Option("", "--runtime", help="where it runs: docker (default) | kubernetes"),
    deployment: str = typer.Option("", "--deployment", help="how RC is arranged. docker: monolith (default) | multi-instance. kubernetes: microservices (default) | monolith"),
    replicas: int = typer.Option(0, "--replicas", help="Rocket.Chat instances (needs --deployment multi-instance on docker)"),
    mongo_operator: bool = typer.Option(False, "--mongo-operator", help="kubernetes: manage MongoDB with the official operator (adds SCRAM auth; needs MongoDB 6.0+)"),
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
    verify_seed: bool = typer.Option(False, "--verify-seed", help="with --seed: fail if the readback disagrees with the plan"),
    pin: bool = typer.Option(False, "--pin", help="mark persistent + set as default"),
    wait: bool = typer.Option(False, "--wait", help="block until RC is serving"),
    offline: bool = typer.Option(False, "--offline", help="skip the live version lookup"),
    no_pull: bool = typer.Option(False, "--no-pull", help="don't pull images first"),
    fresh: bool = typer.Option(False, "--fresh", help="wipe this repro's volume first"),
    force: bool = typer.Option(False, "--force", help="overwrite an existing repro"),
    monitor: bool = typer.Option(False, "--monitor", help="also add Prometheus + Grafana (RC metrics dashboard)"),
    stats: bool = typer.Option(False, "--stats", help="with --seed: report the CPU/RAM cost of seeding"),
    domain: str = typer.Option("", "--domain", help="serve HTTPS at this hostname with a Let's Encrypt certificate. The domain must already point at this host and 443 must be reachable"),
    email: str = typer.Option("", "--email", help="contact email for Let's Encrypt. Remembered after the first use (`rc-repro config set acme.email`)"),
    json_out: bool = typer.Option(False, "--json", help="NDJSON progress on stdout, then one final JSON envelope; prose moves to stderr"),
    https: bool = typer.Option(False, "--https", help="serve HTTPS with a local certificate instead — no domain needed (run `rc-repro trust-ca` once)"),
    env: list[str] = typer.Option(None, "--env", "-e", help="extra raw env var KEY=VALUE (repeatable). Persisted, so `up --force` keeps it; change it later with `rc-repro env`"),
    setting: list[str] = typer.Option(None, "--setting", help="Rocket.Chat SETTING Id=VALUE (repeatable) — adds the OVERWRITE_SETTING_ prefix a setting needs"),
) -> None:
    """Create and start a version-matched Rocket.Chat repro."""
    # Orchestration lives in the shared service layer (same code the web GUI
    # runs); this wrapper just parses options, prints progress, and formats the
    # result. --seed is applied after (with the CLI's richer --stats output), so
    # `wait` is forced on when seeding, as before.
    if json_out:
        jsonout.activate()
    req = lcsvc.CreateReq(
        version=version, preset=preset, name=name, port=port, root_url=root_url,
        bind=bind, rc_image=rc_image, mongo=mongo, reg_token=reg_token,
        # THE REAL SEED REQUEST, even though the CLI seeds itself afterwards.
        # `seed=False` meant the write-ahead note recorded `seed: False` with the
        # profile defaulted, and `create_repro` cleared the note before control ever
        # returned here -- so the `pending_seed` recovery landed in v0.72.2 covered the
        # GUI and NOT `rc-repro up --seed`, the invocation the README documents. That
        # is the exact scenario lifecycle.py describes: "told the workspace is
        # complete, doctor agrees, and the evidence that anything was missing has been
        # deleted."
        #
        # `seed_here=True` keeps the two-phase shape -- the CLI still runs the seed, so
        # it keeps its richer rendering and `--verify-seed` -- while the note tells the
        # truth about what was asked for and is cleared by whoever finishes it.
        params=_parse_set_params(set_), seed=seed, seed_here=True,
        seed_profile=seed_profile, stats=stats, pin=pin,
        wait=(wait or seed), offline=offline, no_pull=no_pull, fresh=fresh,
        force=force, monitor=monitor, actor=_cli_actor(),
        runtime=runtime, deployment=deployment, replicas=replicas,
        mongo_operator=mongo_operator,
        https=https, domain=domain, acme_email=email,
        env={**envsvc.parse_set(env or []), **envsvc.as_setting(setting or [])},
    )
    # In JSON mode the progress goes out as NDJSON `rc-repro.event.v1` lines rather
    # than as prose, and the envelope below is the LAST line -- so a caller reads to
    # end-of-stream and parses one line, with no incremental parser and no guessing
    # about where the machine-readable part starts.
    writer = jsonout.EventWriter() if json_out else None
    try:
        result = lcsvc.create_repro(req, emit=(writer.emit if writer else _cli_emit),
                                    stream_output=False)
    except errors.ReproError as exc:
        _fail(exc)
    if not json_out:
        _render_create_result(result)
    seeded = None
    if seed:
        seeded = _run_seed(runner.read_meta(result["name"]), seed_profile,
                           stats=stats, verify=verify_seed)
    # THE NOTE IS OURS TO CLOSE. `seed_here` told `create_repro` not to clear it,
    # because between its return and this line the workspace is serving with no seed
    # and a kill there is exactly what the note is for. `_run_seed` exits the process
    # on failure, so reaching this means the seed landed -- or none was asked for.
    journal.clear_kind(journal.CREATE_UNFINISHED, result["name"])
    if json_out:
        data = dict(result)
        if seeded is not None:
            # Folded in, not printed beside: exactly one envelope per command.
            data["seed"] = seeded
        jsonout.reply("up", data)


def _run_seed(meta: runner.Metadata, profile: str,
              users=None, channels=None, messages=None, stats: bool = False,
              verify: bool = False) -> dict:
    """Seed, print the result, and RETURN the summary.

    The return value is new and is why: `up --seed --json` promises exactly one
    envelope, so the seed summary has to fold into the create's envelope rather
    than being printed beside it. Every error path in here goes through `_err`,
    which in JSON mode is an envelope -- the same seed failure used to leave a
    `--json` caller with an empty stdout and exit 1.
    """
    quiet = jsonout.active()
    try:
        auth = _login(meta)
    except Exception as exc:  # noqa: BLE001
        # A 429 IS NOT "NOT READY". Rocket.Chat's own login limiter answers 429 after
        # a few failed attempts, and pointing at `ready` there sends someone to a
        # command that will report success and change nothing -- the workspace is up,
        # it is the limiter refusing. Keyed on the status because the prose is RC's.
        if "429" in str(exc) or "too many requests" in str(exc).lower():
            _err(f"can't seed — Rocket.Chat's login rate limiter is refusing "
                 f"({exc}). The workspace is up; wait about a minute and retry, or "
                 f"turn the limiter off for this repro with "
                 f"`rc-repro api -n {meta.name} -X POST /settings/"
                 f"{config.RC_RATE_LIMITER_SETTING} -d '{{\"value\": false}}'`.")
        _err(f"can't seed — repro not ready (`rc-repro ready --name {meta.name}`): {exc}")
    try:
        plan = seeder.plan_from(profile, users, channels, messages)
    except ValueError as exc:
        _err(str(exc))
    if not quiet:
        typer.echo(
            f"Seeding {meta.name!r} (profile: {profile} — {plan.users} users, "
            f"{plan.channels} channels, {plan.messages} msgs/channel)…"
        )
    mon = perf.ResourceMonitor(meta.name).start() if stats else None
    t0 = time.monotonic()
    tokens: dict = {}
    try:
        s = seeder.seed(meta.root_url, auth, plan, tokens_out=tokens,
                        workspace=meta.name,
                        log=(lambda m: None) if quiet else (lambda m: typer.echo(f"  {m}")))
    finally:
        resources = mon.stop() if mon else None   # stop the sampler thread even if seed raises
    total = time.monotonic() - t0
    if not quiet:
        _print_seed_result(s, total, resources, meta)
    # NO `emit`. `check_seed` emits the same facts this renders from the returned
    # verdict, so passing one printed every line twice -- caught live, as duplicated
    # "readback: 11 rooms match the plan". The reason a readback could not run now
    # travels as `why`, so nothing is lost by rendering from the value alone.
    verdict = lcsvc.check_seed(meta, auth, plan, s, tokens=tokens)
    if not quiet:
        _print_seed_verification(verdict)
    # `--verify-seed` turns the report into a gate. Off by default on purpose: a
    # check that fails a healthy workspace is one people learn to bypass, and the
    # readback is worth having as information whether or not it refuses.
    # UNPROVEN IS NOT PASSED. This gated on `faults`, which is [] when the readback
    # could not run -- so `--verify-seed`, the flag whose whole job is to refuse an
    # unverified seed, exited 0 on a seed it had verified nothing about.
    if verify and verdict.get("ok") is None:
        _fail(errors.CreateFailedError(
            "seed verification could not run, so the seed is UNPROVEN (drop "
            "--verify-seed to accept it anyway; the attempt is recorded in "
            "repro.json either way)"))
    if verify and verdict.get("faults"):
        _fail(errors.CreateFailedError(
            f"seed verification failed: {len(verdict['faults'])} mismatch(es) between "
            "the plan and the workspace (drop --verify-seed to seed anyway; the "
            "readback is recorded in repro.json either way)"))
    return {**s, "profile": profile, "elapsed_s": round(total, 2)}


def _print_seed_verification(verdict: dict) -> None:
    """The readback, in one line when it agrees and a list when it does not."""
    if not verdict:
        return
    if verdict.get("ok") is None:
        # NOT SILENCE. `ok is None` means the readback could not run AT ALL, and
        # returning here printed nothing whatsoever -- so a verification that failed
        # looked exactly like one nobody asked for, the absence of the "✓ readback"
        # line being the only difference. The shipped agent skill says it in bold:
        # treat a missing `verification.ok` as UNPROVEN.
        why = verdict.get("why") or "no reason recorded"
        ui.warn(f"  ⚠ the seed readback could not run ({why}), so nothing about this "
                f"seed is verified. `rc-repro seed --name <it>` re-runs it.")
        return
    faults = verdict.get("faults") or []
    unreadable = verdict.get("unreadable") or []
    extra = verdict.get("extra") or []
    if not faults and not unreadable and not extra:
        ui.ok(f"✓ readback: {verdict['checked']} rooms match the plan")
        return
    for f in faults:
        ui.warn(f"  ⚠ {f['room'] or 'users'}: wanted {f['want']}, found {f['got']}"
                + (f" ({f['detail']})" if f.get("detail") else ""))
    if extra:
        ui.note(f"  · {len(extra)} room(s) hold more than this run planned — they had "
                "content already (a re-seed, a preset, a restore). Not a fault: "
                "seeding only ever adds.")
    for u in unreadable:
        # Reported apart from the faults, and never as one: a single unreadable
        # room is a gap in the CHECK, not a defect in the workspace.
        ui.hint(f"  · {u['what']} could not be read back ({u['why']})")


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
    # No tilde. It was there because the count was an estimate -- thread replies
    # were counted optimistically and nothing read the room back. Both are fixed,
    # so the number is now exactly what is in the workspace.
    row("messages", s["messages"], d.get("messages", 0.0), extra=lat_str)
    if s.get("threads"):
        row("threads", s["threads"], 0.0)
    if s.get("reactions"):
        row("reactions", s["reactions"], 0.0)
    if s.get("rooms_total"):
        row("rooms", s["rooms_total"], d.get("channels", 0.0),
            extra=" · ".join(f"{k} {v}" for k, v in sorted((s.get("rooms") or {}).items())))
    row("DMs", s["dms"], d.get("dms", 0.0))
    _print_resources(resources or {}, meta.name)


def _note_lines(line: str, width: int) -> list[str]:
    """One note line, as terminal lines.

    An INDENTED line is something to paste, and is never wrapped. It used to be, and
    the box it was wrapped inside made it worse: `kubectl -n rc-repro-x port-forward
    deployment/rocketchat-` on one line and `  rocketchat 3000:3000` on the next is a
    command that fails when it is pasted, which is the only thing anybody does with
    it. It overflows the width instead, and the terminal soft-wraps a line that is
    still one line to select.
    """
    text = _ascii(line)
    if text[:1] == " " and text.strip():
        return ["      " + text.strip()]
    # break_on_hyphens=False: the default splits "metrics-server" across two lines,
    # and a reader who greps for the word it named does not find it.
    return textwrap.wrap(text, width=width, initial_indent="    ",
                         subsequent_indent="    ", break_on_hyphens=False) or [""]


def _print_notes(meta: runner.Metadata) -> None:
    """What is in this workspace, as sections rather than one wall of bullets.

    Rendered from the GROUPS (see services/lifecycle.note_group), so the terminal and
    the browser show the same structure from one definition -- facts as an aligned
    two-column block, prose wrapped, and the things you have to paste set off on their
    own and never wrapped.

    No box. A box has to wrap its content to its own width, and the content here
    includes commands that must survive a copy; the group titles already say where one
    section ends and the next begins, which is the only thing the border was doing.
    """
    groups = lcsvc.note_groups_of(meta)
    if not groups:
        # A workspace with notes that no group accounts for: still shown, ungrouped,
        # through the same renderer rather than a second one.
        flat = list(meta.extra.get("notes") or []) if isinstance(meta.extra, dict) else []
        if not flat:
            return
        groups = [lcsvc.note_group("", body=flat)]
    width = min(shutil.get_terminal_size((90, 24)).columns, 88)
    ui.line("")
    # A dim divider, not a heading: the group titles below are the headings, and a
    # second cyan bold line above them made the two read as peers.
    head = "what is in this workspace "
    ui.line("  " + typer.style(head + "-" * max(1, width - len(head) - 3),
                               fg=typer.colors.BRIGHT_BLACK))
    for g in groups:
        rows = g.get("rows") or []
        body = g.get("body") or []
        cmds = g.get("commands") or []
        if not (rows or body or cmds):
            continue                      # a title with nothing under it is not a section
        ui.line("")
        if g.get("title"):
            ui.note("  " + _ascii(str(g["title"])), bold=True)
        if rows:
            kw = max(len(str(k)) for k, _ in rows)
            for k, v in rows:
                ui.line(f"    {_ascii(str(k)).ljust(kw)}   {_ascii(str(v))}")
        for line in body:
            for out in _note_lines(line, width - 6):
                ui.line(out)
        for c in cmds:
            ui.line("      " + _ascii(str(c)))


@app.command()
def ready(
    name: str = typer.Option("", "--name", "-n"),
    timeout: float = typer.Option(300.0, "--timeout", help="seconds to wait"),
    json_out: bool = typer.Option(False, "--json", help="NDJSON progress on stdout, then one final JSON envelope"),
) -> None:
    """Block until Rocket.Chat is serving (polls /api/info)."""
    if json_out:
        jsonout.activate()
    target = _resolve_name(name)
    _require_engine(target)
    m = runner.read_meta(target)
    if not json_out:
        typer.echo(f"Waiting for {m.name!r} to serve {m.root_url} ...")
    writer = jsonout.EventWriter() if json_out else None
    try:
        result = lcsvc.wait_and_finalize(
            m, emit=(writer.emit if writer else _cli_emit), timeout=timeout)
    except errors.ReproError as exc:
        # A TIMEOUT is NotReadyError -> exit 5 ("still unknown, poll again"), which
        # is deliberately not the exit 7 of a create that is known dead. A poller
        # that cannot tell those apart either gives up on a slow boot or waits
        # forever on a corpse.
        _fail(exc)
    if json_out:
        jsonout.reply("ready", {**result, "name": m.name, "url": m.external_url})
        return
    ui.ok("✓ ready")
    _summary_panel(m, extra_rows=[("Booted in", _fmt_duration(result["booted_s"]))])
    ui.hint(f"  next: rc-repro logs --name {m.name} -f")
    _print_notes(m)


@app.command()
def down(
    name: str = typer.Option("", "--name", "-n"),
    volumes: bool = typer.Option(False, "--volumes", help="also delete the data volume and forget the repro"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt (for scripts/CI)"),
    json_out: bool = typer.Option(False, "--json", help="print the result as one JSON envelope; --volumes then requires --yes"),
) -> None:
    """Remove a repro's containers. Keeps data (and the record) unless --volumes."""
    if json_out:
        jsonout.activate()
    target = _resolve_name(name)
    if volumes and json_out and not yes:
        # REFUSED, not silently downgraded to keeping the volume. There is nobody
        # to prompt on this path, and the two ways out of that are "delete the data
        # without being asked" and "quietly do something other than what was typed".
        # Neither is acceptable for an irreversible verb, so it asks for the
        # confirmation to be made explicit instead.
        _fail(errors.ValidationError(
            "--volumes deletes the data volume and the record, and --json has "
            "nobody to prompt. Add --yes to confirm it in the command."))
    if volumes:
        # --volumes is irreversible (deletes the Mongo data + the record). On a
        # shared box the thing you most need to know first is whose it is, so the
        # owner leads the prompt -- and is still printed under --yes, where there
        # is no prompt to read but there is a log to read afterwards.
        owner, me = lcsvc.owner_of(target), _cli_actor()
        whose = f" -- owned by {owner}" if owner and owner != me else ""
        if not yes:
            if whose:
                ui.warn(f"{target!r} belongs to {owner}, not you.")
            typer.confirm(
                # No !r: the quotes it adds collide with the possessive and the
                # prompt reads "deletes 'alice-rc8-6-1''s data volume", which is
                # a confusing thing to put in front of somebody about to type y.
                f"This permanently deletes the data volume and record for {target}"
                f"{whose}. Continue?",
                abort=True,
            )
        elif whose:
            ui.warn(f"deleting {target!r}, owned by {owner}.")
    try:
        # confirm=True: the prompt above (or --yes) already gated it.
        # `emit` MATTERS HERE. This passed none, so every warning the teardown raises
        # went nowhere -- including the one that says the local record was KEPT because
        # the namespace could not be confirmed gone. The GUI streams these and showed
        # it; the CLI printed a success line over the top of it.
        out = lcsvc.teardown(target, volumes=volumes, confirm=True, emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    # The nouns depend on the runtime, and this line hardcoded Docker's. A
    # Kubernetes workspace has no containers and no Docker volume -- it has a
    # namespace and a PersistentVolumeClaim -- and `helm uninstall` does NOT delete
    # a PVC, so which of the two paths kept the data is exactly what needs saying.
    # The service layer already knows; asking it beats guessing here.
    kube = (out or {}).get("runtime") == topology.KUBERNETES
    if json_out:
        jsonout.reply("down", {**(out or {}), "name": target, "volumes": volumes})
        return
    what = ("namespace, PersistentVolumeClaim and record" if kube
            else "containers, data volume, and record")
    # `(out or {})` like the `kube` line above: teardown returning None is a shape the
    # tests around this rely on, and `.get` on it is an AttributeError that surfaces as
    # exit 1 from a command that worked.
    if volumes and not (out or {}).get("removed", True):
        # THE SERVICE SAID NO. This printed "✓ removed" from the `--volumes` FLAG rather
        # than from the result, so a teardown that refused and kept the record reported
        # complete success -- and `list` then went on showing the workspace, which is
        # how the contradiction reaches somebody. The warning above says why; this says
        # what to do, and the exit code stops a script believing it.
        ui.warn(f"{target!r} was NOT removed — see above. `rc-repro list` still shows "
                f"it, which is correct: the record is deliberately kept.")
        raise typer.Exit(errors.NotReadyError.exit_code)
    if volumes:
        ui.ok(f"✓ {target!r} removed ({what}).")
    else:
        kept = ("the namespace and its PersistentVolumeClaim are kept" if kube
                else "data kept")
        ui.ok(f"✓ {target!r} down ({kept}).")
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
    try:
        if off:
            res = monitorsvc.detach(target, emit=_cli_emit)
            ui.ok(f"✓ monitoring detached from {res['name']!r}"
                  + ("" if res["rc_setting_reset"] else " (metrics setting left as-is — repro not reachable)"))
            # On Kubernetes the stack is shared, so "detached" does not mean
            # "gone" -- and a user who came to free memory needs to know which
            # of the two happened.
            if res.get("stack_removed") is False:
                ui.note("the shared monitoring stack stays up — other workspaces "
                        "on this cluster are still using it")
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
    orphans: bool = typer.Option(False, "--orphans",
                                 help="also delete Kubernetes namespaces rc-repro owns "
                                      "that no local record explains"),
) -> None:
    """Delete every `down` repro — INCLUDING its data volume and record. Skips pinned and running ones."""
    try:
        targets = lcsvc.prunable()
        stray = lcsvc.orphan_namespaces() if orphans else []
    except errors.ReproError as exc:
        _fail(exc)
    # The CLUSTER is reclaimable separately from any workspace: with everything gone,
    # rc-repro's own kind control plane is 514 MiB holding nothing, and both the README
    # and the agent skill said `prune` gives it back -- which nothing did.
    reclaim = _cluster_reclaimable()
    if not targets and not stray:
        if not reclaim:
            typer.echo("Nothing to prune.")
            return
        if not yes:
            typer.echo("Nothing to prune, but rc-repro's own Kubernetes cluster is "
                       "empty and can be given back (about 0.5 GB).")
            typer.confirm("Delete the cluster?", abort=True)
        try:
            res = lcsvc.prune(confirm=True, orphans=orphans, emit=_cli_emit)
        except errors.ReproError as exc:
            _fail(exc)
        if res.get("cluster"):
            ui.ok("✓ the cluster is gone — the next Kubernetes workspace creates a new one.")
        else:
            typer.echo("Nothing to prune.")
        return
    if not yes:
        typer.echo("These down repros will be deleted — containers, data volumes, and records:")
        me = _cli_actor()
        for t in targets:
            owner = lcsvc.owner_of(t)
            typer.echo(f"  - {t}" + (f"   (owned by {owner})" if owner and owner != me else ""))
        if stray:
            # Named individually and warned about: on a shared cluster one of these
            # may be a colleague's workspace created from their own RC_REPRO_HOME,
            # in which case this machine having no record of it means nothing.
            typer.echo("")
            typer.echo("These Kubernetes namespaces are labelled rc-repro's but no "
                       "local record explains them — their PersistentVolumeClaims go too:")
            for ns in stray:
                typer.echo(f"  - {ns}")
            typer.echo("  (on a shared cluster one of these may belong to a colleague)")
        typer.confirm("Continue?", abort=True)
    try:
        res = lcsvc.prune(confirm=True, orphans=orphans, only=targets,
                          emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    # BOTH lists, because a sweep that removed only namespaces used to report
    # "Nothing to prune." while deleting them and their PersistentVolumeClaims -- the
    # irreversible half of this command, reported as the no-op half. `removed` is
    # local records; `orphans` is namespaces no local record explained, which is the
    # normal case for the situation --orphans exists for.
    if res["removed"]:
        ui.ok(f"✓ pruned {len(res['removed'])}: {', '.join(res['removed'])}")
    if res.get("orphans"):
        ui.ok(f"✓ removed {len(res['orphans'])} orphaned namespace(s): "
              f"{', '.join(res['orphans'])}")
    if not res["removed"] and not res.get("orphans"):
        typer.echo("Nothing to prune.")
    if res.get("cluster"):
        ui.ok("✓ the cluster is gone too — the next Kubernetes workspace creates a new one.")


def _cluster_reclaimable() -> bool:
    """Whether `prune` has a cluster to give back. Never raises: a cleanup verb must not
    fail because a probe did."""
    try:
        from rc_repro.services import k8s
        return k8s.cluster_reclaimable()
    except Exception:  # noqa: BLE001
        return False


@app.command()
def start(name: str = typer.Option("", "--name", "-n")) -> None:
    """Resume a stopped repro (fast, no rebuild)."""
    target = _resolve_name(name)
    try:
        lcsvc.set_state(target, "start", emit=_cli_emit)
    except errors.ReproError as exc:
        # Keeps the recreate hint AND the class's code: `stop` and `restart` exit 3
        # for this exact condition, and `start` exiting 1 made the same failure
        # answer differently depending on which verb you reached for.
        ui.die(f"could not start {target!r} — if it was `down`ed, use "
               "`rc-repro up` to recreate it", exit_code=exc.exit_code)
    ui.ok(f"✓ {target!r} started.")


@app.command()
def stop(name: str = typer.Option("", "--name", "-n")) -> None:
    """Pause a repro, keeping its containers and data."""
    target = _resolve_name(name)
    try:
        lcsvc.set_state(target, "stop", emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    ui.ok(f"✓ {target!r} stopped (resume with `rc-repro start`).")


@app.command()
def restart(name: str = typer.Option("", "--name", "-n")) -> None:
    """Restart a repro."""
    target = _resolve_name(name)
    try:
        lcsvc.set_state(target, "restart", emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    ui.ok(f"✓ {target!r} restarted.")


@app.command()
def use(name: str = typer.Argument(..., help="repro to make the default")) -> None:
    """Set the default repro for name-less commands."""
    # THROUGH resolve_name, like every other name-taking command. This called
    # `runner.exists()` on the literal string, so `use TICKET-1234` failed on a
    # workspace really named `ticket-1234` while `info --name TICKET-1234` found it --
    # and `name_candidates()`'s docstring is written about exactly that defect and says
    # it was fixed. `use` was missed. On a box with accounts the owner-prefix half
    # bites too: `up --name test` makes `alice-test`, and `use test` could not find it.
    # A typed error, so the exit code is 4 rather than a flat 1.
    try:
        target = lcsvc.resolve_name(name)
    except errors.ReproError as exc:
        _fail(exc)
    config.update_config(lambda cfg: cfg.__setitem__("default_repro", target))
    ui.ok(f"✓ default repro is now {target!r}.")


@app.command(name="list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="print the result as one JSON envelope (see `--json` on `info`)"),
) -> None:
    """List all repros with version, port, status and URL."""
    if json_out:
        jsonout.activate()
    # INSIDE the try. This call reads every repro.json and asks docker for the
    # project states, so it is not the "obviously safe" line it looks like -- and
    # unguarded, a ReproError here reached a --json caller as a traceback on stderr
    # with nothing on stdout, which is the one thing the envelope exists to prevent.
    try:
        repros = lcsvc.list_repros()
    except errors.ReproError as exc:
        _fail(exc)
    if json_out:
        # An empty list is a SUCCESS, not the prose "No repros yet". A caller
        # asking "what is on this box" and getting nothing has its answer.
        jsonout.reply("list", {"repros": repros, "count": len(repros)})
        return
    if not repros:
        typer.echo("No repros yet. Create one with `rc-repro up --version <X.Y.Z>`.")
        return
    # The owner column only appears on a shared box, so single-user output --
    # and anything parsing it -- is unchanged.
    shared = any(r.get("created_by") for r in repros)
    owner_h = f"{'OWNER':<12} " if shared else ""
    typer.echo(f"{'NAME':<20} {owner_h}{'RC':<9} {'MONGO':<7} {'WHERE':<8} "
               f"{'PORT':<6} {'STATE':<10} URL")
    for r in repros:
        flag = "*" if r["default"] else (" " if not r["pinned"] else "·")
        owner = f"{(r.get('created_by') or '-'):<12} " if shared else ""
        typer.echo(
            f"{flag}{r['name']:<19} {owner}{r['rc_version']:<9} {r['mongo_tag']:<7} "
            # "compose"/"k8s" rather than the canonical names: this is a narrow
            # column read at a glance, and the canonical value is in `info`.
            f"{('k8s' if r.get('runtime') == topology.KUBERNETES else 'compose'):<8} "
            f"{r['host_port']:<6} {r['state']:<10} {r.get('public_url') or r['root_url']}"
        )
    typer.echo("\n* = default repro   · = pinned")


@app.command()
def info(
    name: str = typer.Option("", "--name", "-n"),
    json_out: bool = typer.Option(False, "--json", help="print the result as one JSON envelope on stdout; prose moves to stderr"),
) -> None:
    """Show a repro's URL, admin credentials and a curl snippet."""
    if json_out:
        jsonout.activate()
    target = _resolve_name(name)
    if json_out:
        # The full detail payload, which is what makes this the command to paste
        # into a ticket. It carries the admin login -- a fixed sandbox credential
        # this tool prints on every `up` and documents in the README, not a secret.
        # Anything that IS one is masked before it gets here (lifecycle.redact_env).
        try:
            jsonout.reply("info", lcsvc.detail(target))
        except errors.ReproError as exc:
            _fail(exc)
        return
    m = runner.read_meta(target)
    _summary_panel(m)
    ui.hint(f"  api  : rc-repro api --name {m.name} GET /api/v1/me")
    ui.hint(f"  curl : {m.root_url}/api/info")
    _print_notes(m)


# Settings worth remembering, shown name -> config.yaml key. An allowlist, so a
# typo cannot write junk into the config file.
_CONFIG_KEYS: dict[str, str] = {
    "acme.email": "acme_email",
    # Not a flag on `up`, on purpose: HTTPS is meant to be --domain plus --email and
    # nothing else. It stays reachable here because Let's Encrypt allows only 5
    # failed validations per hostname per hour and 5 duplicate certificates per week
    # -- without a way to rehearse against staging, a misconfigured first attempt
    # locks the hostname out for a week.
    "acme.staging": "acme_staging",
    # `http` | `tlsalpn` | `dns`. Normally derived and never asked for -- dns-01 when
    # credentials exist, TLS-ALPN otherwise -- and this exists because a real box had
    # NEITHER available: TLS-ALPN validation failed on every attempt with `remote error:
    # tls: unrecognized name`, and dns-01 was impossible because the operator had the
    # machine and not the zone. http-01 validates on :80, which needs only host access,
    # and there was no way to select it. Setting it turns the edge's :80 redirect off,
    # because that redirect would send Let's Encrypt away from the challenge path.
    "acme.challenge": "acme_challenge",
    # Only needed when the variable names in dns.env do not identify the provider
    # on their own; normally it is inferred.
    "acme.dns_provider": "acme_dns_provider",
    # Which interface every workspace on this box publishes on, unless `up --bind`
    # overrides it. lifecycle.py has read this all along (`req.bind or
    # cfg.get("bind_host") or DEFAULT_BIND_HOST`) and config.py maps
    # RC_REPRO_BIND_HOST onto it -- but it was not in this table, so the only
    # supported way to set it was an environment variable on the serve process.
    #
    # That mattered once `bind` became admin-only on POST /api/repros: a member was
    # told "only an admin can set this" and the admin had nowhere to set it either.
    # A boundary with no door on the other side is just a wall.
    #
    # Box-level is also the right SHAPE for this decision. Every workspace runs
    # fixed admin/admin123 credentials, so "does this machine publish those beyond
    # loopback?" is one answer for the box, not a checkbox on each create.
    "bind_host": "bind_host",
    # Both GUI policies. Neither was in this table: lifecycle.py documented
    # `gui.destroy_policy` as the way to loosen who may delete somebody else's
    # workspace, and there was no supported way to write it -- the same missing door
    # as bind_host above, in the same file, unnoticed for the same reason. A policy
    # nobody can set is a comment.
    #
    #   gui.create_policy   anyone (default) | admin  -- rc_image, reg_token, bind
    #   gui.destroy_policy  owner (default)  | anyone  -- `down --volumes` on
    #                                                     somebody else's workspace
    #
    # They default OPPOSITE ways on purpose. A create field only ever affects the
    # workspace being created; destroying somebody else's loses a colleague's
    # in-progress work, which no create field can do.
    "gui.create_policy": "gui.create_policy",
    "gui.destroy_policy": "gui.destroy_policy",
}


@app.command(name="users")
def users_cmd(
    action: str = typer.Argument("list", help="list | add | passwd | remove | role"),
    name: str = typer.Argument("", help="the user name, for add/passwd/remove/role"),
    role: str = typer.Argument("", help="admin | member | readonly, for `role`"),
    ask_password: bool = typer.Option(
        False, "--ask-password",
        help="type a password instead of letting rc-repro generate one"),
) -> None:
    """Manage who can sign in to the web GUI.

    A shared `rc-repro serve` needs named accounts rather than one session token:
    the token is handed to everybody, changes on every restart, and cannot answer
    "who tore down TICKET-1234?". With users, every job records who ran it.

    \b
    `add` and `passwd` GENERATE the password and show it once:
        rc-repro users add bob
        rc-repro users add bob --ask-password    # type one instead

    The GUI has always minted them, and for two reasons that apply just as much at
    a terminal. An admin who types a colleague's password also knows it, which
    makes every audit line signed with that name deniable. And a generated one is
    ~96 bits, where a typed one clears a twelve-character minimum and is otherwise
    whatever somebody thought of — which is the only thing standing between an
    account and a distributed guessing attack, because the sign-in throttle bounds
    one address and nothing bounds a thousand.

    Either way the password is never an ARGUMENT: `ps` shows command lines to every
    user on the machine.
    """
    from rc_repro.services import sessions as sessionsvc
    from rc_repro.services import users as usersvc

    if action == "list":
        rows = usersvc.list_users()
        if not rows:
            ui.note("no users yet — add one with `rc-repro users add <name>`")
            ui.hint("  or just run `rc-repro serve` — it prints a one-time link that "
                "creates the first one.")
            return
        ui.panel("GUI users", [
            (u.name, u.created_at + "  [" + usersvc.role_of(u.name)
             + ("" if u.role else " (implicit)") + "]") for u in rows])
        implicit = usersvc.implicit_admins()
        if implicit:
            ui.hint("  (implicit) = the role column is blank, which means admin. "
                    "That is the\n  migration for accounts made before roles "
                    "existed, not a default.")
            ui.hint(f"  narrow one with: rc-repro users role {implicit[0]} member")
        return

    if not name:
        _err(f"`users {action}` needs a name, e.g. `rc-repro users {action} alice`")

    # One normalisation for every branch below, so `role`, `remove` and `passwd`
    # find the account that `add` created and every line printed names it the way
    # the file spells it. The service normalises too -- but it cannot fix what this
    # layer PRINTS, and `revoke_user` here takes the name straight from argv.
    typed, name = name, usersvc.normalize_name(name)

    if action == "role":
        if not role:
            _err(f"`users role {name}` needs a role "
                 f"({' | '.join(usersvc.ROLES)})")
        try:
            u = usersvc.set_role(name, role)
            ended = sessionsvc.revoke_user(name)
        except errors.ReproError as exc:
            _fail(exc)
        ui.ok(f"✓ {u.name!r} is now {u.role}.")
        if ended:
            ui.hint(f"  {ended} active session(s) signed out, so it takes effect now.")
        ui.hint("  roles bound the GUI. The CLI has no roles: anyone with a shell "
                "on this box\n  is already in the docker group. A readonly user "
                "with an ssh key is not readonly.")
        return

    if action == "remove":
        try:
            # remove() ends their sessions itself -- a removed account that is
            # still signed in has not been removed, and that is the service's job
            # to guarantee rather than each front end's to remember.
            ended = usersvc.remove(name)
        except errors.ReproError as exc:
            _fail(exc)
        ui.ok(f"✓ user {name!r} removed.")
        if ended:
            ui.hint(f"  {ended} active session(s) signed out.")
        return

    if action in ("add", "passwd"):
        ended = 0
        try:
            usersvc.require_valid_name(name)
        except errors.ReproError as exc:
            _fail(exc)
        if name != typed:
            ui.note(f"  {typed!r} becomes {name!r} — an account name becomes part "
                    "of a workspace name, and therefore a DNS label.")
        if ask_password:
            pw = typer.prompt(f"Password for {name}", hide_input=True)
            again = typer.prompt("Repeat", hide_input=True)
            if pw != again:
                _err("the two passwords do not match")
            minted = False
        else:
            # The same function the GUI's People dialog calls, so "a password
            # rc-repro generated" means one thing on this box rather than two.
            pw = usersvc.mint_password()
            minted = True
        try:
            if action == "add":
                added = usersvc.add(name, pw)
            else:
                # set_password() ends every session the OLD password minted, so
                # changing a compromised one does not leave the intruder signed in
                # for seven days. Enforced in the service, not here.
                ended = usersvc.set_password(name, pw)
        except errors.ReproError as exc:
            _fail(exc)
        ui.ok(f"✓ user {name!r} {'added' if action == 'add' else 'password changed'}.")
        if minted:
            # Printed, not logged and not stored: this is the only moment it
            # exists in readable form anywhere.
            ui.panel(f"password for {name}", [(pw, "")])
            ui.hint("  Shown once. rc-repro keeps only a scrypt hash, so nobody — "
                    "including you —\n  can read it back. Send it over something "
                    "the person can delete afterwards.")
        if action == "passwd" and ended:
            ui.hint(f"  {ended} active session(s) signed out.")
        if action == "add":
            ui.hint(f"  role: {added.role}" + (
                "  (the first account has to be an admin, or nobody could promote "
                "anyone)" if added.role == "admin"
                else "  — change it with `rc-repro users role %s <role>`" % name))
        if action == "add" and len(usersvc.list_users()) == 1:
            ui.hint("  `rc-repro serve` will now ask for this login, and the "
                    "first-run link is spent.")
        return

    _err(f"unknown action {action!r} (want: list | add | passwd | remove | role)")


def _port_claim_note() -> str:
    """A cluster holding this host's :80/:443, as a sentence, or "".

    THE EDGE STARTING IS NOT THE EDGE WORKING, and every message on the start path
    conflated the two. A k3s ServiceLB claims host ports by DNAT rather than by
    binding, so the edge binds both ports, compose reports success, and this printed
    "every registered name is served again" about a box where no name was served at
    all -- for two days, on a manager's install, with `rc-repro edge status` agreeing
    that the edge was running. It was running. It was also unreachable.

    Cheap enough to run on the start path: one `kubectl get svc` per candidate
    context, and only when a context answers at all.
    """
    from rc_repro.services import k8s

    try:
        # 3s, not the 8s probe default: this is a warning on the way to starting a
        # proxy, and four probes at the default would put half a minute on `edge
        # start` for any box with a kubeconfig pointing somewhere unreachable.
        claim = k8s.port_claiming_cluster(timeout=3.0)
    except Exception:  # noqa: BLE001 - a warning must never break a start
        return ""
    if not claim:
        return ""
    took = ", ".join(f":{p}" for p in claim.ports if p in (80, 443))
    return (f"cluster {claim.context!r} holds this host's {took} at {claim.address} "
            f"({claim.service}), so the edge is bound but cannot receive: names answer "
            f"from the cluster, not from rc-repro.\n"
            f"  Give the ports back: kubectl -n kube-system patch svc "
            f"{claim.service.split('/')[-1]} -p "
            f"'{{\"spec\":{{\"type\":\"NodePort\"}}}}'\n"
            f"  A k3s reinstall undoes that patch. Permanently: install with "
            f"`--disable traefik --disable servicelb`, or put those two under "
            f"`disable:` in /etc/rancher/k3s/config.yaml.")


def _report_gui_tls(domain: str, tries: int = 10, pause: float = 2.0) -> None:
    """Say whether `domain` is really being served over TLS yet, and why not.

    Polled rather than asked once: ACME takes a few seconds on a good day, so a single
    probe would report "not yet" for every healthy start and teach people to ignore it.
    Never fatal -- the GUI is about to serve, and a certificate that has not arrived is
    a thing to say rather than a reason to refuse.
    """
    import time

    from rc_repro import tls as tlsmod
    from rc_repro.services import edge as edgesvc

    # BEFORE the wait, not after it. With a cluster holding the ports no challenge of
    # any kind can complete, so the twenty seconds of "waiting for the certificate…"
    # were spent teaching the reader that this message means nothing.
    claim = _port_claim_note()
    if claim:
        ui.warn(f"  ⚠ https://{domain} cannot get a certificate — {claim}")
        return

    for attempt in range(tries):
        got = tlsmod.verify("127.0.0.1", 443, sni=domain, timeout=5.0)
        if got.get("serving") and not got.get("fallback"):
            ui.ok(f"https://{domain} is serving a real certificate "
                  f"({got.get('issuer', '')})".rstrip(" ()"))
            return
        why = edgesvc.acme_failure(domain)
        if why:
            # Conclusive: Let's Encrypt has answered and said no. Stop waiting.
            ui.warn(f"  ⚠ https://{domain} has NO certificate yet, and Let's Encrypt "
                    f"refused:\n      {why}")
            ui.hint(f"  The GUI itself is fine — this is the name, not the server. "
                    f"Check that {domain} resolves to this host's public address and "
                    f"that :443 is reachable from the internet (rc-repro validates over "
                    f"TLS-ALPN on 443, not :80), then `rc-repro edge restart`.")
            ui.hint("  Full detail: `rc-repro edge logs --tail 50`")
            return
        if attempt < tries - 1:
            if attempt == 0:
                ui.note(f"  waiting for the certificate for {domain}…")
            time.sleep(pause)
    ui.warn(f"  ⚠ https://{domain} is not serving a real certificate yet. Traefik "
            f"requests it in the background, so this may still complete.")
    ui.hint("  Watch it: `rc-repro edge logs -f`   ·   check it: `rc-repro tls-status`")


@app.command(name="edge")
def edge_cmd(
    action: str = typer.Argument("status", help="status | start | stop | restart | logs"),
    follow: bool = typer.Option(False, "--follow", "-f", help="follow the log (logs)"),
    tail: int = typer.Option(200, "--tail", help="how many lines of log to show"),
) -> None:
    """The shared Traefik that serves every HTTPS name on this box.

    It is not a workspace, so it never appears in `list` and `prune`/`down` cannot
    touch it — but something holding :443 and routing your traffic should be
    answerable, and this is where it answers.

    It starts by itself when the first workspace needs a name. `stop` frees :80
    and :443 for something else; routes are files, so starting it again restores
    every name with no re-registration.
    """
    from rc_repro.services import edge as edgesvc

    if action == "logs":
        # WHERE HTTPS FAILS, and there was no way to read it from here. Traefik requests
        # certificates in the background after it starts, so a name can be routed and the
        # edge healthy while issuance fails, and this is the only record of why.
        if not edgesvc.installed():
            ui.note("no edge yet — nothing to show. It starts with the first `--https` "
                    "or `--domain` workspace, or `serve --domain`.")
            return
        if not edgesvc.running():
            ui.warn("the edge is STOPPED — this is whatever it said before it stopped. "
                    "`rc-repro edge start`")
        raise typer.Exit(edgesvc.logs(tail=tail, follow=follow))

    if action == "status":
        st = edgesvc.status()
        if not st["installed"]:
            ui.note("no edge yet — it starts with the first `--https` or `--domain` "
                    "workspace.")
            return
        domain = edgesvc.served_domain()
        rows = [("State", "running" if st["running"] else "STOPPED"),
                ("Project", f"{edgesvc.PROJECT} ({edgesvc.edge_dir()})")]
        if domain:
            rows.append(("GUI name", domain))
        ui.panel("rc-repro edge", rows)
        if not st["running"]:
            ui.warn("  ⚠ every https name on this box is unreachable while it is "
                    "stopped — `rc-repro edge start`")
        else:
            # "running" was the whole answer here, and running is not reachable. The
            # box that took two days to diagnose printed State: running, 3 routes, and
            # served none of them. Same note as the start path, one place it is written.
            claim = _port_claim_note()
            if claim:
                ui.warn(f"  ⚠ running but NOT reachable — {claim}")
        # Declared and unservable, which is silent apart from a log nobody was
        # pointed at. Reported BEFORE the early return below, or the box where it
        # matters most -- one with a GUI name and no workspace routes -- never shows it.
        orphans = edgesvc.orphan_certs()
        if orphans:
            ui.warn(f"  ⚠ {len(orphans)} name(s) declared with no certificate file — "
                    f"{', '.join(orphans[:3])}{'…' if len(orphans) > 3 else ''}. Traefik "
                    f"logs 'failed to find any PEM data' for each, once a second, and "
                    f"serves none of them. `rc-repro edge logs`; if "
                    f"{edgesvc.certs_dir()} is root-owned (Docker creates a missing "
                    f"bind-mount target that way), `sudo chown -R $(id -u):$(id -g) "
                    f"{edgesvc.certs_dir()}` and re-run `up` for those workspaces.")
        if not st["routes"]:
            ui.hint("  no names registered yet.")
            return
        # Attachment and route side by side: a route whose network the edge never
        # joined is a 502, not an error, and that is the hardest failure to read.
        attached = set(st["attached"])
        typer.echo(f"\n{'NAME':<28} {'SERVES':<38} REACHABLE")
        for name in st["routes"]:
            host = _edge_route_host(edgesvc, name)
            ok = edgesvc.workspace_network(name) in attached
            typer.echo(f"{name:<28} {host:<38} {'yes' if ok else 'NO (502)'}")
        return

    if action in ("start", "stop", "restart"):
        if not edgesvc.installed() and action != "stop":
            edgesvc.write(edgesvc.Edge())
        elif action in ("start", "restart"):
            # RE-RESOLVE FROM CONFIG. `start` used to just `up` the compose file that was
            # already on disk, so a changed `acme.challenge` or `acme.staging` reached
            # nothing -- the container came back with the challenge and the CA it was
            # created with, and the log went on reporting the same failure. Measured:
            # `config set acme.challenge http` then `edge restart` still validated over
            # TLS-ALPN against the production CA.
            #
            # Rebuilt from the recorded spec, which is why the spec is recorded: the
            # older reconstruction knew only the domain, so regenerating from it would
            # have lost the GUI's host and port and pointed the front door at nothing.
            fd = edgesvc.current()
            if fd is not None:
                edgesvc.write(edgesvc.Edge.resolve(
                    fd.domain, fd.acme_email,
                    gui_host=fd.gui_host, gui_port=fd.gui_port,
                    http_port=fd.http_port, https_port=fd.https_port))
        if action in ("stop", "restart"):
            edgesvc.down()
            ui.ok("✓ edge stopped; :80 and :443 are free.")
        if action in ("start", "restart"):
            # RENDERED, not raised. `up()` refuses with a ConflictError when an edge from
            # a different RC_REPRO_HOME already holds the port -- a real, expected,
            # actionable condition, and starting this one would take that one's names
            # down. Uncaught here, so `rc-repro edge start` answered a two-user box with
            # a Python TRACEBACK, source of cli.py included. `serve` learned this
            # already; this path is the same call and did not.
            try:
                started_ok = edgesvc.up(pull=False).returncode == 0
            except errors.ReproError as exc:
                _fail(exc)
            if not started_ok:
                _err("the edge did not start. Check nothing else holds :80 or :443:\n"
                     "    sudo lsof -i :443")
            # BOUND IS NOT REACHABLE. See `_port_claim_note`: this line claimed every
            # name was served again on a box where a cluster was taking every packet
            # before Docker saw it, which is the state this whole check exists for.
            claim = _port_claim_note()
            if claim:
                ui.warn(f"  ⚠ edge running, but NOT reachable — {claim}")
            else:
                ui.ok("✓ edge running — every registered name is served again.")
        return

    _err(f"unknown action {action!r} (want: status | start | stop | restart)")


def _edge_route_host(edgesvc, name: str) -> str:
    """The hostname a route file serves, for display only."""
    try:
        for line in edgesvc.route_path(name).read_text().splitlines():
            if "rule:" in line and "Host(" in line:
                return line.split("Host(`", 1)[1].split("`", 1)[0]
    except (OSError, IndexError):
        pass
    return "?"


@app.command(name="config")
def config_cmd(
    action: str = typer.Argument("list", help="list | get | set | unset"),
    key: str = typer.Argument("", help="e.g. acme.email"),
    value: str = typer.Argument("", help="the value, for `set`"),
) -> None:
    """Read or write remembered settings, so they need not be retyped every run.

    Keys: acme.email, acme.staging, acme.dns_provider, bind_host,
    gui.create_policy, gui.destroy_policy. Stored in ~/.rc-repro/config.yaml.

    \b
    Members may set --rc-image/--reg-token/--bind by default. Where an account is
    not the same thing as trust, narrow it:
        rc-repro config set gui.create_policy admin
    \b
    And to publish every workspace on this box beyond loopback, one decision
    instead of one per create:
        rc-repro config set bind_host 0.0.0.0
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
        # A misspelt policy falls back to the strict reading, which is safe but
        # silent -- and somebody who typed `anyonr` would believe they had opened it.
        if key in ("gui.create_policy", "gui.destroy_policy"):
            strict = "admin" if key == "gui.create_policy" else "owner"
            if value not in ("anyone", strict):
                _err(f"{key} takes 'anyone' or '{strict}' (got {value!r})")
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
        # WRAPPED LIKE ITS SIBLINGS. `stats`, `logs`, `info`, `token`, `tls-status`,
        # `chown` and `seed` all catch this; the env READ path did not, so an unknown
        # name escaped to main() -- which then mishandled it (see `_exit_reporting`)
        # and exited 1 where the table says 4.
        try:
            cur = envsvc.current(name)
        except errors.ReproError as exc:
            _fail(exc)
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
        _fail(exc)
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
    from rc_repro.services import edge as edgesvc

    # THE GUI'S OWN NAME, when no workspace was asked for. `serve --domain` gives this
    # box an https name that is not a workspace, so `tls-status` -- the one command whose
    # entire job is "what is actually being served for this name" -- answered "no name
    # given and no default repro set". That is the exact question somebody has when their
    # GUI works on :7070 and not on its domain, and there was no command for it.
    if not name and not config.load_config().get("default"):
        gui_domain = edgesvc.served_domain()
        if gui_domain:
            ui.note(f"Checking the GUI's own name, {gui_domain} "
                    f"(no workspace given; `--name <n>` for one)")
            _report_gui_tls(gui_domain, tries=1, pause=0)
            raise typer.Exit(0)

    m = runner.read_meta(_resolve_name(name))
    if not m.public_url:
        ui.warn(f"  ⚠ {m.name!r} was not created with --https - it serves plain HTTP "
                f"at {m.root_url}.")
        raise typer.Exit(1)

    mode = str(m.extra.get("tls") or "")
    host = m.public_url.split("://", 1)[1].split(":")[0]
    # An edge-served workspace publishes no TLS port of its own, so tls_ports is
    # absent OR an empty list (adoption clears it). `[0]` on the empty list was an
    # IndexError -- a traceback from `tls-status` on exactly the workspaces the
    # edge serves, which is all of them now.
    claimed = [int(p) for p in (m.extra.get("tls_ports") or []) if str(p).isdigit()]
    port = claimed[0] if claimed else 443
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
    """The staging -> production command, rebuilt from what this repro actually used."""
    x = m.extra if isinstance(m.extra, dict) else {}
    return " ".join([
        f"rc-repro up -v {m.rc_version}", f"--name {m.name}",
        f"--domain {host}", f"--email {x.get('tls_email') or '<you@example.com>'}",
        "--force", "--wait",
    ])


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
    from rc_repro import tls_local
    key, crt = tls_local.ensure_ca()
    if show:
        typer.echo(f"CA certificate: {crt}")
        typer.echo(f"CA key:         {key}  (keep private)")
        _run_and_echo(["openssl", "x509", "-in", str(crt), "-noout",
                       "-subject", "-fingerprint", "-sha256", "-dates"])
        return
    try:
        installed, how = tls_local.trust(crt, uninstall=uninstall)
    except errors.ReproError as exc:
        _fail(exc)
    if installed:
        ui.ok(f"✓ CA {'removed from' if uninstall else 'installed into'} {how}.")
        ui.hint("  Restart the browser to pick it up. Firefox keeps its own store — "
                "if it still warns, import the CA under Settings → Privacy → Certificates.")
    else:
        ui.warn(f"  ⚠ could not {'remove' if uninstall else 'install'} it automatically "
                f"on this platform ({how}).")
        typer.echo(tls_local.manual_trust_instructions(crt, uninstall=uninstall))


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
    target = _resolve_name(name)
    _require_engine(target)
    m = runner.read_meta(target)
    try:
        auth = _login(m)
    except Exception as exc:  # noqa: BLE001 - surface any auth/connection failure
        _not_answering(m, exc, "logging in")
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
    target = _resolve_name(name)
    _require_engine(target)
    m = runner.read_meta(target)
    try:
        auth = _login(m)
        if pat:
            token = rcapi.generate_pat(m.root_url, auth, config.ADMIN_PASSWORD, bypass_2fa=True, workspace=m.name)
            auth = rcapi.Auth(token=token, user_id=auth.user_id)  # use the PAT as the auth token
    except Exception as exc:  # noqa: BLE001
        _not_answering(m, exc, "this API call")

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
    target = _resolve_name(name)
    _require_engine(target)
    m = runner.read_meta(target)
    try:
        auth = _login(m)
        token = rcapi.generate_pat(m.root_url, auth, config.ADMIN_PASSWORD, token_name=label, bypass_2fa=bypass_2fa, workspace=m.name)
    except Exception as exc:  # noqa: BLE001
        _not_answering(m, exc, "minting a token")
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
    verify: bool = typer.Option(
        False, "--verify-seed",
        help="fail if the readback disagrees with the plan (the readback runs and is recorded either way)"),
) -> None:
    """Populate a repro with sample users, channels, DMs and messages.

    --scale bulk-inserts users/messages straight into MongoDB (orders of
    magnitude faster than the REST seed) to reproduce SCALE/perf behaviour.
    Bulk users are credential-less and messages fire no app hooks; use the
    default REST seed when you need real, loginable users.
    """
    target = _resolve_name(name)
    _require_engine(target)
    m = runner.read_meta(target)
    if clear_scale:
        _clear_scale(m)
        return
    if scale:
        _run_scale(m, scale)
        return
    _run_seed(m, profile, users, channels, messages, stats=stats, verify=verify)


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
    path = Path(settings_file)
    if not path.is_file():
        _err(f"no such file: {settings_file}")
    target = _resolve_name(name)
    # `config-import` speaks REST to a URL -- `topology.require_compose`'s own
    # docstring names it as runtime-agnostic -- so gating it on Docker refused it on
    # a Kubernetes host that has none.
    _require_engine(target)
    m = runner.read_meta(target)
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


# --- backup / restore / upgrade -------------------------------------------------

def _human_bytes(n: int) -> str:
    from rc_repro.services.backup import _human
    return _human(n)


@app.command(name="backup")
def backup_cmd(
    name: str = typer.Option("", "--name", "-n"),
    out: str = typer.Option("", "--out", help="write the bundle here instead of ~/.rc-repro/backups/"),
    label: str = typer.Option("", "--label", help="a note stored in the bundle, e.g. 'before upgrade'"),
    live: bool = typer.Option(False, "--live", help="don't stop Rocket.Chat first (faster, but the dump may be inconsistent)"),
) -> None:
    """Back up a repro's Rocket.Chat database into a restorable bundle.

    Rocket.Chat is stopped for the dump so it is consistent, then started again;
    MongoDB keeps running throughout and its data volume is untouched.

    The bundle carries the database alongside the repro's version, preset and
    parameters, so `rc-repro restore --new` can rebuild the whole workspace from
    it — on this machine or somebody else's.
    """
    from rc_repro.services import backup as backupsvc
    try:
        # `note=`, not `label=`: JobManager.submit() owns the `label` keyword, so the
        # service takes the note under a different name (see backup.create).
        res = backupsvc.create(name, out=out, note=label, live=live, emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    m = res["manifest"]
    ui.ok(f"✓ backed up {res['name']!r} ({m['rc_version']}) — {_human_bytes(res['bytes'])}")
    typer.echo(f"    {res['path']}")
    if m.get("sidecar_volumes"):
        ui.warn("  sidecar data is NOT included: " + ", ".join(m["sidecar_volumes"]))
    ui.hint(f"  restore it:  rc-repro restore {res['path']}")


@app.command(name="backups")
def backups_cmd(
    name: str = typer.Option("", "--name", "-n", help="only bundles from this repro"),
) -> None:
    """List backup bundles in ~/.rc-repro/backups/, newest first."""
    from rc_repro.services import backup as backupsvc
    rows = backupsvc.list_backups(name)
    if not rows:
        ui.note("no backups yet — make one with `rc-repro backup`")
        return
    for r in rows:
        if r["error"]:
            ui.warn(f"  {Path(r['path']).name}  UNREADABLE: {r['error']}")
            continue
        label = f"  ({r['label']})" if r["label"] else ""
        typer.echo(f"  {Path(r['path']).name}")
        typer.echo(f"      {r['repro']}  RC {r['rc_version']}  "
                   f"{_human_bytes(r['bytes'])}  {r['created_at']}{label}")
        if r.get("predates_workspace"):
            # Bundles outlive their workspace and are matched by NAME, and rc-repro
            # derives names from the version -- so recreating a workspace inherits
            # the old one's backups with nothing to say they are not its own.
            ui.warn("      ^ from an EARLIER workspace of this name, not the one "
                    "running now — restoring it loads that workspace's data")


@app.command(name="restore")
def restore_cmd(
    bundle: str = typer.Argument(..., help="path to a .rcbak bundle"),
    name: str = typer.Option("", "--name", "-n", help="restore into this repro (default: the one it came from)"),
    new: bool = typer.Option(False, "--new", help="create a fresh repro from the bundle instead of restoring into an existing one"),
    allow_upgrade: bool = typer.Option(False, "--allow-upgrade", help="permit restoring older data into a newer workspace (Rocket.Chat will migrate it)"),
    force: bool = typer.Option(False, "--force", help="ignore compatibility refusals"),
) -> None:
    """Restore a backup bundle into a repro.

    Three targets:

      rc-repro restore B                  in place — the repro it came from
      rc-repro restore B --new            a fresh repro built from the bundle
      rc-repro restore B --name other     an existing, different repro

    Existing collections are DROPPED, so the result is the backup's data and not a
    merge of the two. Downgrading (newer data into an older workspace) is refused:
    Rocket.Chat does not migrate a database backwards.
    """
    from rc_repro.services import backup as backupsvc
    try:
        res = backupsvc.restore(bundle, name=name, new=new,
                                allow_upgrade=allow_upgrade, force=force,
                                emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    what = "created and restored" if res["created"] else "restored"
    ui.ok(f"✓ {what} {res['name']!r} from {Path(res['bundle']).name} "
          f"in {res['restore_seconds']}s")
    if res["direction"] == "upgrade":
        ui.warn(f"  {res['from_version']} data now runs on {res['to_version']} — "
                "Rocket.Chat has migrated it")
    if res.get("url"):
        ui.hint(f"  {res['url']}")


@app.command(name="upgrade")
def upgrade_cmd(
    to: str = typer.Option("", "--to", help="target Rocket.Chat version, e.g. 8.6.1"),
    name: str = typer.Option("", "--name", "-n"),
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would happen and stop"),
    no_backup: bool = typer.Option(False, "--no-backup", help="skip the automatic pre-upgrade backup (no rollback will be possible)"),
    no_rollback: bool = typer.Option(False, "--no-rollback", help="leave a failed upgrade in place instead of rolling it back"),
    rollback: bool = typer.Option(False, "--rollback", help="undo the last upgrade from its pre-upgrade backup"),
    bundle: str = typer.Option("", "--bundle", help="with --rollback: restore this bundle instead of the recorded one"),
    offline: bool = typer.Option(False, "--offline", help="resolve the version from the built-in map, no network (so a version that was never released still resolves — the map matches on the series)"),
    force: bool = typer.Option(False, "--force", help="ignore refusals (downgrades, MongoDB major changes)"),
) -> None:
    """Upgrade a RUNNING repro to another Rocket.Chat version.

    Rocket.Chat runs its database migrations on boot, so this is the honest way to
    reproduce "it broke after we upgraded": real data, real migrations. A
    pre-upgrade backup is taken automatically and `--rollback` restores it.

    The repro has to be running — the backup needs MongoDB up, and the migrations
    only happen when Rocket.Chat boots.
    """
    from rc_repro.services import upgrade as upgradesvc
    if rollback:
        try:
            res = upgradesvc.rollback(name, bundle=bundle, emit=_cli_emit)
        except errors.ReproError as exc:
            _fail(exc)
        ui.ok(f"✓ {res['name']!r} rolled back to {res['rolled_back_to']}")
        return

    if not to:
        _err("no target version given — use `--to 8.6.1` (or `--rollback` to undo)")
    try:
        p = upgradesvc.plan(name, to, offline=offline)
    except errors.ReproError as exc:
        _fail(exc)

    ui.panel(f"upgrade: {p['name']}", [
        ("Rocket.Chat", f"{p['from_version']}  ->  {p['to_version']}"),
        ("MongoDB", f"{p['from_mongo']}" + ("" if p["from_mongo"] == p["to_mongo"]
                                            else f"  ->  {p['to_mongo']}")),
        ("image", f"{p['rc_image']}:{p['to_version']}"),
        ("source", p["source"]),
    ])
    for line in p["warnings"]:
        ui.warn("  " + line)
    if not p["allowed"]:
        (ui.warn if force else ui.fail)("  " + p["blocked_reason"])
        if not force:
            ui.hint("  --force overrides this, but expect it to fail")
            raise typer.Exit(1)
    if dry_run:
        ui.note("  --dry-run: nothing changed")
        return

    try:
        res = upgradesvc.run(name, to, offline=offline, force=force,
                             no_backup=no_backup,
                             rollback_on_failure=not no_rollback, emit=_cli_emit)
    except errors.ReproError as exc:
        _fail(exc)
    ui.ok(f"✓ {res['name']!r} upgraded {res['from_version']} -> {res['to_version']} "
          f"in {res['boot_seconds']}s")
    if res["running_version"]:
        typer.echo(f"    Rocket.Chat reports {res['running_version']}")
    if res["migration_errors"]:
        ui.warn(f"  {len(res['migration_errors'])} migration error(s) in the boot log:")
        for line in res["migration_errors"]:
            typer.echo(f"      {line}")
    if res["backup"]:
        ui.hint(f"  roll back:  rc-repro upgrade --rollback --name {res['name']}")


@app.command()
def stats(
    name: str = typer.Option("", "--name", "-n"),
    for_: float = typer.Option(5.0, "--for", help="seconds to sample"),
    watch: bool = typer.Option(False, "--watch", "-w", help="stream live (Ctrl-C to stop)"),
) -> None:
    """Sample a repro's container CPU/RAM (peak over a window, or --watch live)."""
    target = _resolve_name(name)
    _require_engine(target)
    m = runner.read_meta(target)
    if topology.of_repro(m.name) == topology.KUBERNETES:
        # `kubectl top`, per pod, which is the shape that means something here: a
        # Compose repro has containers, a Kubernetes one has pods that come and go.
        # ResourceMonitor samples docker and has nothing to sample, so this path is
        # its own rather than a translation of it.
        from rc_repro.services import k8s
        context = str((m.extra or {}).get("context") or k8s.CONTEXT)
        # THE WHOLE NAMESPACE, not just the Rocket.Chat pods. Compose sums
        # `docker compose ps -q`, which is every container in the workspace --
        # MongoDB included, and on Kubernetes MongoDB is usually the largest single
        # consumer. Scoped to LOG_SELECTOR this reported 1 row (rocketchat, 793.8 MB)
        # for a 5-pod namespace also holding mongodb-0, nats-0, nats-1 and nats-box,
        # and called that the workspace's resource use. Two runtimes measuring
        # different sets is the quietly-wrong number `pod_metrics` exists to avoid.
        deadline = time.monotonic() + (0.0 if watch else max(0.0, for_))
        peak: dict[str, dict] = {}
        while True:
            try:
                rows = k8s.pod_metrics(m.name, context=context, selector="")
            except errors.ReproError as exc:
                _fail(exc)
            if watch:
                typer.echo("")
                for r in rows:
                    typer.echo(f"  {r['pod']:<44} CPU {r['cpu_millicores']:>6.0f}m"
                               f"   RAM {r['mem_bytes'] / 1e6:>7.1f} MB")
                if not rows:
                    ui.note("no pods are running in this workspace's namespace")
                try:
                    time.sleep(2)
                except KeyboardInterrupt:
                    return
                continue
            # `--for` WAS ACCEPTED AND DROPPED. It was read only after this branch
            # returned, so `stats --for 30` came back in 0s having sampled once --
            # exactly the accepted-and-ignored shape `_KUBERNETES_UNSUPPORTED` exists
            # to prevent for other flags. Sampled to a peak now, like Compose.
            for r in rows:
                prev = peak.get(r["pod"])
                if prev is None or r["mem_bytes"] > prev["mem_bytes"]:
                    peak[r["pod"]] = dict(r)
                else:
                    prev["cpu_millicores"] = max(prev["cpu_millicores"],
                                                 r["cpu_millicores"])
            if time.monotonic() >= deadline:
                typer.echo("")
                for r in sorted(peak.values(), key=lambda x: -x["mem_bytes"]):
                    typer.echo(f"  {r['pod']:<44} CPU {r['cpu_millicores']:>6.0f}m"
                               f"   RAM {r['mem_bytes'] / 1e6:>7.1f} MB")
                if not peak:
                    ui.note("no pods are running in this workspace's namespace")
                return
            # ONE sleep. There were two identical blocks here, so `--for 30` slept
            # four seconds a round and sampled 7 times instead of 15 -- half the
            # window, silently, in the code that had just been changed to honour the
            # flag at all.
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                return
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
    # FIRST, before `_require_docker` and before any `_err`. This was missing, and
    # the shape of the bug is the reason the contract is tested rather than reasoned
    # about: the success path called `jsonout.reply` directly, so stdout was a valid
    # envelope whenever the run worked -- and every failure before it went out as
    # prose on stderr with an EMPTY stdout. A JSON caller got a clean document right
    # up until something went wrong.
    if json_out:
        jsonout.activate()
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

    # Both front-ends ask the same question through the same function: a load test
    # against a Kubernetes workspace would measure the port-forward, and the line
    # below raised a bare FileNotFoundError naming a compose file that runtime does
    # not have. See services/perf.require_compose_for_perf.
    from rc_repro.services import perf as perfsvc
    try:
        perfsvc.require_compose_for_perf(_resolve_name(name), "loadtest")
    except errors.ReproError as exc:
        # Handled here because nothing else in this command is: `loadtest` and
        # `capacity` have no ReproError handler of their own, so a domain error
        # raised in the body prints a traceback instead of a red line.
        _fail(exc)
    m = runner.read_meta(_resolve_name(name))
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
                                   token_name="rc-repro-loadtest", bypass_2fa=True,
                                   workspace=m.name)
    except Exception as exc:  # noqa: BLE001
        _not_answering(m, exc, "this API call")

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
    # Journal ids. "" means "no note", and journal.clear("") is a no-op.
    limiter_note = constrain_note = metrics_note = mongo_note = ""
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
            # JOURNALLED HERE TOO. This function is a second implementation of
            # services/perf.py's run_loadtest, and only the service half wrote notes --
            # so a CLI run killed by SIGKILL or an OOM left the rate limiter off, the
            # containers capped, the metrics endpoint open and the profiler armed, with
            # nothing on disk recording any of it and `doctor` therefore silent.
            # Measured exactly that way: kill -9 a CLI loadtest, find
            # API_Enable_Rate_Limiter still false and zero journal notes.
            constrain_note = journal.record(
                journal.CONSTRAINTS_APPLIED, m.name,
                applied=[dataclasses.asdict(a) for a in applied_constraints])
            snapshot["constraints"] = constrain_mod.human(per_service)
            if not json_out:
                ui.note(f"  constrained: {snapshot['constraints']} (restored after the test)")

        # Disable the API rate limiter so the offered load isn't throttled into a
        # false result. Restored below — back ON unless it was already known-off
        # (an unreadable setting -> None -> restores to ON, never left disabled).
        limiter_was_off = rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                            config.RC_RATE_LIMITER_SETTING) is False
        if not limiter_was_off:
            limiter_note = journal.record(journal.RATE_LIMITER_OFF, m.name)
            if not rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                     config.RC_RATE_LIMITER_SETTING, False):
                journal.clear(limiter_note)
                limiter_note = ""
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
                metrics_note = journal.record(journal.RC_METRICS_ON, m.name)
                metrics_changed = rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                                    monitoring.RC_METRICS_SETTING, True)
            mongo_prior = mongoprof.start(m.name, slowms)
            if mongo_prior is None:
                _warn("  ⚠ Mongo slow-query capture unavailable (profiler could not be enabled)")
            else:
                mongo_note = journal.record(journal.MONGO_PROFILER_ON, m.name,
                                            prior=mongo_prior)

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
                journal.clear(limiter_note)
            except Exception:  # noqa: BLE001 - best-effort restore
                _warn("  ⚠ could not restore the API rate limiter setting")
        if metrics_changed:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  monitoring.RC_METRICS_SETTING, False)
                journal.clear(metrics_note)
            except Exception:  # noqa: BLE001
                _warn("  ⚠ could not restore the Prometheus metrics setting")
        if mongo_prior:
            # Wrapped like its neighbours: an exception here would skip the
            # resource-cap restore below and leave the containers capped.
            try:
                mongoprof.stop(m.name, mongo_prior)
                journal.clear(mongo_note)
            except Exception:  # noqa: BLE001
                _warn("  ⚠ could not restore the Mongo profiler level")
        # Cleared only on a SUCCESSFUL restore -- see services/perf.py. `restore`
        # returns problems rather than raising, so clearing first deleted the note on
        # the one path it is for.
        _cap_problems = constrain_mod.restore(applied_constraints)
        for problem in _cap_problems:
            _warn(f"  ⚠ could not restore resource limits — {problem}")
        if not _cap_problems:
            journal.clear(constrain_note)

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
        # THE ENVELOPE, not a bare payload. `jsonout.activate()` runs at the top of this
        # command, so the caller has been told stdout is a machine document -- and this
        # printed `json.dumps(..., indent=2)`: no `schema`, no `contract`, no `ok`, no
        # `error`, over many lines. `jsonout`'s own module docstring names loadtest and
        # capacity as the bug it was written to fix, and it was fixed for the ERROR path
        # only. README sells `--slo` as the thing that "drops into CI" and tells that CI
        # step to read to end of stream and parse the last line; the last line of this
        # was `}`.
        jsonout.reply("loadtest", result)

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
    if json_out:
        jsonout.activate()          # before any `_err`; see `loadtest`
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

    from rc_repro.services import perf as perfsvc
    try:
        perfsvc.require_compose_for_perf(_resolve_name(name), "capacity")
    except errors.ReproError as exc:
        # Handled here because nothing else in this command is: `loadtest` and
        # `capacity` have no ReproError handler of their own, so a domain error
        # raised in the body prints a traceback instead of a red line.
        _fail(exc)
    m = runner.read_meta(_resolve_name(name))
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
                                   token_name="rc-repro-loadtest", bypass_2fa=True,
                                   workspace=m.name)
    except Exception as exc:  # noqa: BLE001
        _not_answering(m, exc, "this API call")
    users = _login_seed_users(m, users_n) if users_n > 0 else []

    # As in loadtest: every mutation (resource caps, rate limiter, the Prometheus
    # setting) lives inside the try so a failure or Ctrl-C in setup or mid-search
    # still restores. Restore-tracked vars are initialised first.
    applied_constraints: list = []
    limiter_was_off = True
    limiter_note = constrain_note = metrics_note = ""
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
            constrain_note = journal.record(
                journal.CONSTRAINTS_APPLIED, m.name,
                applied=[dataclasses.asdict(a) for a in applied_constraints])
        limiter_was_off = rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                            config.RC_RATE_LIMITER_SETTING) is False
        if not limiter_was_off:
            limiter_note = journal.record(journal.RATE_LIMITER_OFF, m.name)
            rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                              config.RC_RATE_LIMITER_SETTING, False)
        if rcapi.get_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                             monitoring.RC_METRICS_SETTING) is not True:
            metrics_note = journal.record(journal.RC_METRICS_ON, m.name)
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
            else:
                journal.clear(limiter_note)
        if metrics_changed:
            try:
                rcapi.set_setting(m.root_url, auth, config.ADMIN_PASSWORD,
                                  monitoring.RC_METRICS_SETTING, False)
                journal.clear(metrics_note)
            except Exception:  # noqa: BLE001
                ui.warn("  ⚠ could not restore the Prometheus metrics setting")
        # Same as loadtest above: the note outlives a failed restore.
        _cap_problems = constrain_mod.restore(applied_constraints)
        for problem in _cap_problems:
            ui.warn(f"  ⚠ could not restore resource limits — {problem}")
        if not _cap_problems:
            journal.clear(constrain_note)
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
        # See loadtest above: an envelope, because that is what `capabilities` says this
        # command answers with and what the contract promises.
        jsonout.reply("capacity", {"ctx": ctx, "steps": steps,
                                   "capacity_vus": last_pass,
                                   "breach_vus": first_fail, "result": result,
                                   "why": why})


@app.command()
def logs(
    name: str = typer.Option("", "--name", "-n"),
    follow: bool = typer.Option(False, "--follow", "-f", help="stream logs"),
    tail: int = typer.Option(0, "--tail", help="only the last N lines (0 = all)"),
) -> None:
    """Tail a repro's logs."""
    target = _resolve_name(name)
    _require_engine(target)
    # Logs are the first thing anyone reads when reproducing a ticket, so this
    # dispatches rather than refusing. It used to send Kubernetes users away with a
    # `kubectl` line -- workable in a terminal, and nothing at all in the browser,
    # where `serve` exists precisely so a support engineer needs no shell.
    if topology.of_repro(target) == topology.KUBERNETES:
        from rc_repro.services import k8s
        meta = runner.read_meta(target)
        context = str((meta.extra or {}).get("context") or k8s.CONTEXT)
        proc = k8s.log_process(target, context=context, tail=tail or 200,
                               follow=follow)
        try:
            for line in proc.stdout or []:
                typer.echo(line.rstrip("\n"))
        except KeyboardInterrupt:      # Ctrl-C on a follow is how you leave
            pass
        finally:
            proc.terminate()
        raise typer.Exit(proc.wait() or 0)
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


@app.command(name="audit")
def audit_cmd(
    actor: str = typer.Option("", "--actor", help="only this account"),
    kind: str = typer.Option("", "--kind", help="only this action, e.g. down-volumes"),
    since: str = typer.Option("", "--since", help="only after this ISO date, e.g. 2026-08-01"),
    q: str = typer.Option("", "--grep", help="substring of the action or its target"),
    n: int = typer.Option(50, "-n", "--lines", help="how many to show"),
    denied: bool = typer.Option(False, "--denied", help="only refusals"),
    tsv: bool = typer.Option(False, "--tsv", help="raw tab-separated, for awk/cut and log shipping"),
) -> None:
    """Read the activity trail: who did what, and whether it was allowed.

    The log has been written since accounts landed and there was no way to read
    it -- "who tore down TICKET-1234?" meant ssh and `cat`. Six tab-separated
    columns: time, who, action, target, origin, outcome.

    `origin` says how the identity was established, which is what makes a line
    evidence or not:

      session   a signed-in GUI request; the server checked a credential
      local     the CLI, where the OS login matched a known account
      asserted  the CLI with RC_REPRO_USER set -- honoured as given, so a CLAIM
      system    rc-repro acting on its own behalf
    """
    from rc_repro.services import audit as auditsvc
    res = auditsvc.read(limit=max(1, n), actor_name=actor, kind=kind, q=q, since=since)
    rows = [r for r in res["lines"] if not denied or r["outcome"] == "denied"]
    if not rows:
        ui.note(f"nothing matched in {res['path']}")
        return
    if tsv:
        for r in rows:
            typer.echo(f"{r['ts']}\t{r['actor'] or '-'}\t{r['kind']}\t{r['label']}\t"
                       f"{r['origin'] or '-'}\t{r['outcome']}")
        return
    width = max(len(r["actor"] or "-") for r in rows)
    for r in reversed(rows):            # oldest first, like a log
        colour = typer.colors.RED if r["outcome"] == "denied" else None
        # The origin is only worth the column width when it is NOT a checked
        # session -- that is the common case and the trustworthy one.
        mark = "" if r["origin"] == "session" else f"  [{r['origin'] or 'legacy'}]"
        typer.secho(f"{r['ts'][:19]}  {(r['actor'] or '-'):<{width}}  "
                    f"{r['kind']:<14} {r['label']}{mark}"
                    + ("  DENIED" if r["outcome"] == "denied" else ""), fg=colour)
    if res["truncated"]:
        ui.hint(f"  (stopped early: {res['path']} is large. Narrow it with --since "
                "or --actor, or ship it somewhere with an index.)")


@app.command(name="chown")
def chown_cmd(
    name: str = typer.Option("", "--name", "-n"),
    to: str = typer.Option(..., "--to", help="the account taking it over"),
) -> None:
    """Hand a workspace over to somebody else.

    Ticket handover is the workflow the shared box is built around, and the record
    only ever knew who typed `up` -- so "belongs to alice" kept warning the wrong
    person, which is how people learn to click through warnings. Who CREATED it
    stays in the record; who owns it now is what moves.
    """
    try:
        res = lcsvc.set_owner(name, to, by=_cli_actor())
    except errors.ReproError as exc:
        _fail(exc)
    ui.ok(f"✓ {res['name']!r} now belongs to {res['to']}"
          + (f" (was {res['from']})" if res["from"] else ""))


@app.command()
def capabilities() -> None:
    """What this build can do, as JSON, for an agent or a script.

    No `--json` flag, deliberately: this command IS JSON. A flag that is accepted
    and then ignored is worse than no flag.

    Every field is derived from the registered app rather than written out, because
    a hardcoded list is right on the day it is written and wrong the first time a
    flag moves -- which is exactly the drift a caller reads this to avoid. Answers
    offline and with no container engine: it is asked BEFORE anybody knows whether
    the environment works, and the question "does this box work" is `doctor`.
    """
    jsonout.activate()
    jsonout.reply("capabilities", jsonout.capabilities(app))


skill_app = typer.Typer(help="Install the agent skill that teaches a caller to drive rc-repro.")
app.add_typer(skill_app, name="skill")


@skill_app.command("install")
def skill_install(
    host: str = typer.Option("all", "--host", help="claude | agents | all"),
    force: bool = typer.Option(False, "--force", help="overwrite a locally-edited copy"),
    json_out: bool = typer.Option(False, "--json", help="print the result as one JSON envelope"),
) -> None:
    """Copy the packaged skill where an agent will find it."""
    from rc_repro.services import skill as skillsvc
    if json_out:
        jsonout.activate()
    try:
        res = skillsvc.install(host, force=force)
    except errors.ReproError as exc:
        _fail(exc)
    if json_out:
        jsonout.reply("skill-install", res)
        return
    for row in res["hosts"]:
        (ui.ok if row["action"] != "unchanged" else ui.hint)(
            f"  {row['action']:<9} {row['path']}")
    ui.hint(f"  version {res['version']}")


@skill_app.command("status")
def skill_status(
    json_out: bool = typer.Option(False, "--json", help="print the result as one JSON envelope"),
) -> None:
    """Whether the installed skill matches this build."""
    from rc_repro.services import skill as skillsvc
    if json_out:
        jsonout.activate()
    st = skillsvc.state()
    if json_out:
        jsonout.reply("skill-status", st)
        return
    for host, row in sorted(st["hosts"].items()):
        mark = {"current": ui.ok, "absent": ui.hint}.get(row["status"], ui.warn)
        mark(f"  {host:<8} {row['status']:<9} {row['path']}")
    if not st["current"]:
        ui.hint("  `rc-repro skill install` writes the copy this build ships.")


@app.command()
def doctor(
    json_out: bool = typer.Option(False, "--json", help="print the report as one JSON envelope; exits 3 on any failing check"),
) -> None:
    """Preflight: check Docker, Compose, disk, connectivity and ports."""
    from rc_repro.services import doctor as doctorsvc
    if json_out:
        jsonout.activate()
    report = doctorsvc.run_checks()
    if json_out:
        # `doctor --json` used to answer `{"ok": true, "warnings": [], "data":
        # {"verdict": "warn", "counts": {"warn": 6}}}`. Six warnings, and the field
        # designed to carry them empty -- reachable through data.checks[].status, which
        # is not what a caller is told to read.
        for row in report.get("checks", []):
            if row.get("status") in ("warn", "fail"):
                jsonout.warn_once(f"DOCTOR_{str(row.get('check') or 'check').upper()}"
                                  .replace("-", "_"),
                                  str(row.get("message") or ""),
                                  check=row.get("check"), status=row.get("status"))
        jsonout.reply("doctor", report)
        # PreflightError's own code, not a bare 1: "this box is not ready" is a
        # different answer from "rc-repro broke", and a CI step branching on the
        # exit needs to tell them apart. The human path keeps its exit 1, because
        # changing it would move the ground under every existing script.
        if report["verdict"] == "fail":
            raise typer.Exit(errors.PreflightError.exit_code)
        return
    marks = {
        "ok": ("\u2713", typer.colors.GREEN),
        "warn": ("\u26a0", typer.colors.YELLOW),
        "fail": ("\u2717", typer.colors.RED),
    }
    for row in report["checks"]:
        sym, color = marks[row["status"]]
        typer.secho(f'{sym} {row["message"]}', fg=color)
    if report["repros"]:
        typer.echo(f'  repros: {report["repros"]["total"]} total, '
                   f'{report["repros"]["running"]} running')
    typer.echo("")
    if report["verdict"] == "fail":
        typer.secho("Not ready — fix the ✗ item(s) above.", fg=typer.colors.RED)
        raise typer.Exit(1)
    if report["verdict"] == "warn":
        typer.secho("Usable, with warnings above.", fg=typer.colors.YELLOW)
    else:
        typer.secho("All good — rc-repro is ready.", fg=typer.colors.GREEN)


@app.command()
def serve(
    port: int = typer.Option(7070, "--port", help="host port for the web GUI"),
    domain: str = typer.Option("", "--domain", help="serve the GUI over HTTPS on this hostname, via rc-repro's shared front door"),
    email: str = typer.Option("", "--email", help="contact address for the Let's Encrypt certificate (remembered after the first use)"),
    no_domain: bool = typer.Option(False, "--no-domain", help="ignore the domain this box was set up with and serve locally"),
    # Default resolved below, not here: with --domain the GUI should bind the
    # Docker bridge, and an empty default is the only way to tell "the user chose
    # 127.0.0.1" from "the user chose nothing".
    bind: str = typer.Option("", "--bind", help="interface to bind (default: loopback, or the Docker bridge with --domain)"),
    allow_host: list[str] = typer.Option(None, "--allow-host", help="extra Host header to accept, e.g. a reverse-proxy domain (repeatable; '*' = any host). Needed for iximiuz/Codespaces/remote access"),
    no_open: bool = typer.Option(False, "--no-open", help="don't open a browser"),
    no_token: bool = typer.Option(False, "--no-token", hidden=True,
                                  help="deprecated no-op: there is no session token any more"),
    print_service: bool = typer.Option(False, "--print-service", help="print how to keep this running (systemd unit, or nohup) and exit — writes nothing"),
    # Hidden, not removed. It is the only way to serve plain http on a reachable
    # interface (an EC2 public IP with no proxy), so it has to keep working -- but
    # putting it in --help meant every first-time reader met a security decision
    # that does not apply to them, on localhost or behind a lab's TLS. The one
    # moment it IS needed, `serve` refuses and names it, with the command to add
    # it to. That refusal is a better teacher than a line in a list.
    insecure: bool = typer.Option(False, "--insecure", hidden=True,
                                  help="no TLS anywhere: the password crosses the network readable, and you accept that. Only needed on a reachable bind with no proxy in front"),
    trust_proxy: list[str] = typer.Option(None, "--trust-proxy", help="TLS ends at this proxy: believe its X-Forwarded-Proto/-For and mark the cookie Secure (address or CIDR, repeatable)"),
) -> None:
    # `\\[gui]` and not `[gui]`: typer renders this docstring as Rich markup, which
    # eats a bare bracket group as a style tag -- so the one line telling you what
    # to install rendered as `pip install 'rc-repro'`, which installs the core
    # package WITHOUT fastapi and uvicorn, and `serve` then refuses to start. The
    # backslash cannot come from a raw docstring either: the `\b` markers below are
    # real backspaces, which is how Click is told not to rewrap the blocks.
    """Launch the web GUI (needs `pip install 'rc-repro\\[gui]'`).

    \b
    TRYING IT OUT? There is nothing to learn:
        rc-repro serve

    Binds localhost — nothing crosses a network, and none of the options below
    apply. The first run prints a one-time link ending in /setup#k=... ; open the
    whole url, including the part after #, to make the first account.

    (On a box already set up with --domain, plain `serve` keeps serving that name.
    Add --no-domain for a local session.)

    The rest is only about letting OTHER machines reach it:

    \b
        your team, over HTTPS    --domain rc.example.com --email you@example.com
        a lab or proxy in front  --bind 0.0.0.0 --allow-host <name> --trust-proxy <ip>
        a plain IP, no TLS       --bind 0.0.0.0 --allow-host <ip>

    --bind decides whether anything off this machine can reach it; --allow-host
    says which names may be used to. Whatever you type in the address bar must be
    listed there or every request is a 403, and either flag alone does nothing.

    --trust-proxy names whatever already terminates TLS (a lab url, ngrok, a load
    balancer) so the session cookie can be marked Secure. Address or CIDR — a bare
    0.0.0.0 is one address and matches nothing; 0.0.0.0/0 is anywhere.

    The third row is refused until you say what protects the password. `serve`
    prints the flag to add, and why, at the point it stops.

    Binding anything but localhost exposes docker control — creating and deleting
    repros and their volumes — to whoever can reach the port.
    """
    try:
        import uvicorn
        from rc_repro.web.app import create_app, trusted_problems, usable_trusted
    except ImportError:
        _err("the web GUI needs extra deps — install them with: pip install 'rc-repro[gui]'")
    import webbrowser

    from rc_repro import tls as tlsmod
    from rc_repro.services import edge as edgesvc
    from rc_repro.services import users as usersvc

    from rc_repro.services import firstrun as frsvc

    # Normalised HERE, before anything reads it: `allow` becomes the Host allow-list
    # AND the host in the printed URL, so a value carrying a scheme, a port or a
    # trailing slash used to produce both a list that matched nothing and a URL like
    # `http://https://lab.example.com/:9944/`. Reported as three separate attempts
    # that each started, claimed to be healthy, and 403'd every request.
    #
    # Reported the way --domain already reports its own fix-ups, a few lines below,
    # so the correction is visible rather than silent.
    allow = []
    for _given in (allow_host or []):
        try:
            _host, _fixed = tlsmod.normalize_allow_host(_given)
        except errors.ReproError as exc:
            _fail(exc)
        if _fixed:
            ui.note(f"  using --allow-host {_host} ({_fixed})")
        allow.append(_host)
    allow_host = list(allow)      # so _cmdline() echoes the corrected form back
    # Posture is decided by the BIND, not by whether <home>/users happens to be
    # empty. Security should not be a side effect of a file's contents.
    basic = usersvc.any_users()

    door: "edgesvc.Edge | None" = None
    # Reuse the name this box was already set up with. `serve --domain X --email Y`
    # records both, and without this you had to retype them on every restart --
    # and a plain `serve` did not merely forget, it went back to loopback while
    # the edge's GUI route still pointed at a process it could no longer reach,
    # so the name 502'd. Pass --no-domain for a deliberately local session.
    if not domain and not no_domain:
        domain = edgesvc.served_domain()
        if domain:
            ui.note(f"  serving {domain} (set up earlier; --no-domain for a local "
                    "session)")
    if domain:
        # Normalized before anything reads it: it becomes the Host() rule, a TLS
        # SNI name and the printed URL, so a surviving scheme corrupts all three.
        domain, fixed = tlsmod.normalize_domain(domain)
        if fixed:
            ui.note(f"  using --domain {domain} ({fixed})")
        cfg = config.load_config()
        given_email = email
        email = email or str(cfg.get("acme_email") or "")
        if not email:
            _err("a Let's Encrypt certificate needs a contact email:\n"
                 f"  rc-repro serve --domain {domain} --email you@example.com\n"
                 "It is remembered after the first use, or set it once with:\n"
                 "  rc-repro config set acme.email you@example.com")
        if given_email and given_email != str(cfg.get("acme_email") or ""):
            # `up` documents the email as remembered after first use; it was not
            # true here, so every restart needed it retyped.
            config.save_config({**cfg, "acme_email": email})
        # No "this publishes the GUI with no login" warning here any more. With a
        # --domain the bind is never loopback, so the accounts check below ALWAYS
        # refuses -- nothing was ever published, and warning about a thing that
        # cannot happen, one line above refusing to do it, is what made this screen
        # read as two contradictory answers to the same question.
        # The bridge, not 0.0.0.0: the front door is the only thing that needs to
        # reach the GUI, so the port never has to be exposed to the network.
        gui_bind = bind or edgesvc.bridge_address()
        if not gui_bind:
            _err("could not read the Docker bridge address, which is how the front "
                 "door reaches the GUI.\n"
                 "  Is Docker running? `rc-repro doctor` checks.")
        bind = gui_bind
        # Traefik forwards the original Host, so the allow-list has to accept it
        # or every proxied request is a 403.
        allow.append(domain)
        # resolve(), not the constructor: the challenge and whether a wildcard is
        # possible are both derived from whether DNS credentials exist, the same
        # rule `up --domain` already uses. Never asked for.
        door = edgesvc.Edge.resolve(domain, email, gui_host=bind, gui_port=port)
    elif not bind:
        bind = "127.0.0.1"

    loopback = bind in ("127.0.0.1", "localhost", "::1")

    def _cmdline(exe: str = "rc-repro", *, service: bool = False) -> str:
        """This invocation, rebuilt from the RESOLVED options.

        Two callers: the no-accounts refusal, which prints the line to run again,
        and --print-service, which bakes it into a systemd unit. Rebuilt rather
        than read from sys.argv because argv is not this command's arguments in
        every context that matters -- under the test runner it is pytest's, and it
        would carry through any embedding. Resolved, so a `serve` that inherited
        its --domain from the box prints the domain rather than dropping it.
        """
        import shlex
        parts = [exe, "serve"]
        if domain:
            # --insecure and --trust-proxy are both about a transport rc-repro is
            # not arranging, so neither belongs beside a --domain that says it is.
            parts += ["--domain", domain, "--email", email]
        else:
            parts += ["--bind", bind]
            if insecure:
                parts.append("--insecure")
            for cidr in (trust_proxy or []):
                parts += ["--trust-proxy", cidr]
        if port != 7070:
            parts += ["--port", str(port)]
        # allow_host, not `allow`: --domain appends itself to the latter, and
        # echoing that back adds a flag the reader never typed.
        for h in (allow_host or []):
            parts += ["--allow-host", h]
        if service:
            parts.append("--no-open")    # no browser to open from a service
        return " ".join(shlex.quote(p) for p in parts)

    # No accounts? Two cases, and only one of them starts.
    #
    # A session token in the URL is gone: it was a standing credential with no
    # identity behind it, regenerated on every restart, landing in shell history
    # and screenshots -- and it made `audit.log` record `-` for every action, in
    # the mode that shipped by default.
    setup_key = ""
    if not basic:
        if loopback and not domain:
            # A fresh key per start. The design proposed reporting an outstanding
            # one instead, which cannot be done: only its sha256 is stored, on
            # purpose, so it cannot be reprinted. Minting again is the honest
            # version -- the URL just printed is always the one that works.
            setup_key = frsvc.mint()
        else:
            # Amber and two steps, NOT a red `error:`. Nothing has gone wrong
            # here: this is the documented first-run sequence on a shared box, and
            # on a rebuilt one -- where ~/.rc-repro is empty, so EVERY serve says
            # it -- a red failure reads as rc-repro being broken rather than as
            # two commands in a row. Red is reserved for something that failed.
            #
            # It still EXITS NON-ZERO, and it has to: `serve` did not serve, and a
            # systemd unit reporting success while nothing is listening is the
            # worse lie. Exit 3, `preflight` -- the taxonomy's name for a
            # condition the caller must fix before this can run at all.
            # Whole block on stderr, heading and steps together: it precedes a
            # non-zero exit, so `serve > /dev/null` must still say why nothing
            # started -- and a block split across two streams interleaves into
            # nonsense the moment either one is redirected.
            ui.warn("rc-repro needs an account before it will serve on an "
                    "interface other than loopback.", err=True)
            typer.echo("\n  1. create one — the password is generated and shown "
                       "once:\n"
                       "       rc-repro users add <name>\n"
                       "  2. start it again:\n"
                       f"       {_cmdline()}\n", err=True)
            raise typer.Exit(3)

    # --domain means rc-repro is arranging TLS itself, through its own front door.
    # It therefore KNOWS the browser hop is https and the plain-http last hop is
    # container-to-host on the bridge -- so the refusal below would be refusing
    # its own most secure configuration.
    # --trust-proxy is a STRONGER statement than --insecure: it names which peer
    # terminates TLS, rather than merely asserting that somebody does. So it
    # satisfies the same bind-time check, and unlike --insecure it also makes the
    # cookie Secure and silences the (then false) transport warning.
    tls_upstream = bool(domain) or insecure or bool(trust_proxy)

    if basic and not loopback and not tls_upstream:
        # Sessions send the password ONCE, at sign-in -- but that one time is
        # still in the clear on plain http, and the cookie minted from it then
        # rides every request. So a plain-http bind on a reachable interface is
        # still refused unless somebody says the transport is protected.
        #
        # But this process cannot tell whether the password is actually exposed:
        # behind a TLS-terminating proxy the browser speaks https and only the
        # last hop is plain, which is safe and is how a shared box normally runs.
        # So both ways out have to be offered. The first version named only the
        # loopback one, which is no route at all when the proxy lives on another
        # machine -- it cannot reach a loopback bind -- and that made accounts
        # unusable on exactly the shared server they exist for.
        #
        # The second route is spelled out rather than echoed back as a full
        # command: an echo built from bind+port drops any --allow-host that was
        # passed, and the copy-pasted result then 403s the proxy's Host header.
        _err(f"signing in over plain http on {bind} puts the password on the wire "
             "once, and the\n  session cookie on every request after it — both "
             "readable by anyone on the path.\n  Refused by default.\n"
             "  Have a hostname pointing here? Let rc-repro do the TLS:\n"
             "    rc-repro serve --domain <your-domain> --email you@example.com\n"
             "  TLS proxy on THIS box? Keep the GUI on loopback and point the "
             "proxy at it:\n"
             f"    rc-repro serve --bind 127.0.0.1 --port {port} "
             "--allow-host <your-domain>\n"
             "  TLS terminating upstream (remote proxy, lab, load balancer)?\n"
             "    name the peer, so the cookie is marked Secure too:\n"
             "      --trust-proxy <the proxy's address>\n"
             "  Nothing doing TLS at all, on a network you trust?\n"
             "    add --insecure to the command you just ran\n"
             "  --trust-proxy and --insecure answer different questions: the first "
             "says who is\n"
             "  encrypting it, the second says nobody is and you accept that. If "
             "there is a\n"
             "  proxy, name it — it is the better answer and --insecure is then "
             "not needed.",
             # Exit 3 (`preflight`), matching the no-accounts branch a few lines
             # up. These are the two halves of one question -- may this bind
             # serve? -- and they reported it as 3 and 1, so a script could not
             # treat "fix your invocation" as one case. 1 means an internal fault,
             # which neither of them is.
             exit_code=3)

    # Not for the bridge: that address is reachable from containers on this box,
    # not from the network, which is the entire reason the front door uses it.
    if not loopback and not door:
        ui.warn(f"  ⚠ binding {bind} exposes docker control (create/delete repros + volumes) "
                "to your network — use only if you mean to.")
    if "*" in allow:
        ui.warn("  ⚠ --allow-host '*' accepts ANY Host header — only on a trusted/ephemeral network.")
    # Said BEFORE the server comes up, because the symptom otherwise is silence:
    # it starts, it serves, and the one thing the flag was for quietly did not
    # happen.
    for problem in trusted_problems(trust_proxy):
        ui.warn(f"  ⚠ {problem}")
    # --allow-host names who may reach it; --bind decides whether anyone can. Given
    # one without the other, the flag is inert and the server comes up answering
    # loopback only -- which it then reports as working, so the reader retries the
    # same command or concludes the address is wrong. Observed costing three
    # attempts on a real box. It is a note, not a refusal: naming a Host you will
    # reach through a proxy on loopback is a legitimate setup.
    if allow_host and loopback and not domain:
        ui.note(f"  note: --allow-host {allow_host[0]} names who may reach this, but "
                "the bind is still\n        loopback, so nothing off this machine "
                "can. Add --bind 0.0.0.0 if you\n        meant to publish it.")
    # The mirror image, and the one that actually cost the time: bound where others
    # can reach it, but the allow-list still holds nothing, so only localhost passes
    # the Host guard and every request by the name somebody types is a 403. `serve`
    # used to print a healthy startup screen and say nothing about it.
    if not loopback and not door and not allow_host:
        ui.warn("  ⚠ nothing is in --allow-host, so only localhost is accepted — a "
                "request by any\n    other name will be a 403. Add --allow-host "
                "<the name you will type>, or '*'.")

    if print_service:
        # Printed after the checks above, never before: a unit that reproduces a
        # command which would be refused is worse than no unit at all.
        # systemd REFUSES a relative ExecStart, so this has to be absolute even
        # when rc-repro is not on PATH. In a venv the console script sits next to
        # the interpreter, which is the case `which` misses.
        exe = shutil.which("rc-repro") or str(Path(sys.executable).parent / "rc-repro")
        cmdline = _cmdline(exe, service=True)

        typer.echo(f"\n# systemd — survives logout, restarts on crash, starts on boot.\n"
                   f"# Write this to {edgesvc.UNIT_PATH}:\n")
        typer.echo(edgesvc.systemd_unit(cmdline, os.environ.get("USER", "rcrepro")))
        typer.echo("# Then:")
        typer.echo(f"    sudo tee {edgesvc.UNIT_PATH} > /dev/null   # paste the above")
        typer.echo("    sudo systemctl daemon-reload")
        typer.echo("    sudo systemctl enable --now rc-repro")
        typer.echo("    systemctl status rc-repro && journalctl -u rc-repro -f")
        typer.echo("\n# No systemd? nohup survives logout — and nothing else:")
        typer.echo(f"    nohup {cmdline} > {config.home() / 'serve.log'} 2>&1 &")
        typer.echo("# It will NOT restart on crash, NOT come back after a reboot,")
        typer.echo("# NOT rotate that log, and there is no `status` to ask.")
        if not basic:
            typer.echo("\n# Note: no accounts exist, so this would refuse to start on a")
            typer.echo("# reachable interface. `rc-repro users add <name>` first.")
        raise typer.Exit(0)

    if door:
        # PREFLIGHT BEFORE THE EDGE. `serve --domain` had none: it went straight to
        # `docker compose up` in the edge's project, so an engine problem arrived as
        # "the edge did not start" with `docker ps -a` empty -- no container, nothing to
        # inspect. Reported from a fresh EC2 box, where `docker` was installed and the
        # COMPOSE PLUGIN was not; `docker ps` works there and every `docker compose`
        # call fails. `up` has always called require_docker(); this path never did, and
        # it is the one people reach for first on a server.
        # ONE preflight, shared with `up`. `require_docker` checks the engine AND the
        # compose plugin, which is the pair that made this path fail with nothing to
        # read: `docker ps` worked, every `docker compose` call did not, and the edge
        # simply "did not start".
        lcsvc.require_docker()
        ui.note("starting the edge (one Traefik, :80 and :443 for every name)…")
        # RENDERED, not raised. `up()` refuses with a ConflictError when another home's
        # edge already holds the port -- a real, expected, actionable condition -- and
        # nothing here caught it, so it reached the top as a Python TRACEBACK with the
        # source of cli.py printed at somebody trying to start a server. Every other
        # command in this file wraps its service call; this one did not.
        try:
            edgesvc.write(door)
            started = edgesvc.ensure_running(acme_email=email,
                                         acme_staging=door.acme_staging)
        except errors.ReproError as exc:
            _fail(exc)
        if not started:
            holder = edgesvc.port_holder(443)
            # DOCKER'S OWN REASON first. This said only "the edge did not start." on a
            # fresh EC2 box where `docker ps -a` was empty -- no container to inspect,
            # no port holder to name, and the compose error captured and discarded.
            _err("the edge did not start"
                 + (f" — {holder} is holding :443." if holder else ".")
                 + (f"\n  docker said: {started.why}" if started.why else "")
                 + f"\n  Its compose project is `{edgesvc.PROJECT}` in "
                 f"{edgesvc.edge_dir()}; `rc-repro edge status` reports it."
                 + f"\n  Full output: cd {edgesvc.edge_dir()} && docker compose -p "
                 f"{edgesvc.PROJECT} up")
        # DID THE NAME ACTUALLY GET A CERTIFICATE? Nothing asked. Traefik obtains them
        # in the BACKGROUND after it starts, so the edge comes up, the route loads, and
        # this printed `https://<domain>/` as though it worked -- while the name served
        # Traefik's own default certificate and a 404 for as long as issuance kept
        # failing. Reported by someone whose GUI was fine on :7070 and dead on its
        # domain, with the reason sitting in a container log there was no command to
        # read. A URL this prints should have been checked.
        _report_gui_tls(domain)
        url = f"https://{domain}/"
    else:
        # The host somebody can actually TYPE. Printing `localhost` while bound to
        # 0.0.0.0 was wrong on the one run where the URL is not obvious: the whole
        # reason for that bind is that the useful address is somewhere else, and
        # the reader was left to assemble it from the --allow-host they had just
        # passed. '*' is skipped -- it allows any Host but names none.
        host = "localhost"
        if not loopback:
            # `0.0.0.0` is every interface, not an address anyone can open, so a
            # wildcard bind with no named host gets a placeholder rather than a URL
            # that looks copy-pasteable and is not. That is the `--allow-host '*'`
            # case, which is exactly how a lab box is run.
            wildcard = bind in ("0.0.0.0", "::", "[::]")
            host = (next((h for h in (allow_host or []) if h != "*"), "")
                    or ("<this-box>" if wildcard else bind))
        if setup_key:
            # The key rides in the FRAGMENT: a fragment is never sent to the
            # server, so it cannot land in an access log, a proxy log or a Referer.
            url = f"http://{host}:{port}/setup#k={setup_key}"
        else:
            url = f"http://{host}:{port}/"
    # Only what can actually match a peer. The posture line and the app both read
    # this, and testing the LIST for emptiness made `--trust-proxy 0.0.0.0`
    # announce "https, trusted from 0.0.0.0" while trusting nothing at all -- the
    # one line an operator reads to learn the posture, saying the opposite of it.
    trusted = usable_trusted(trust_proxy)
    if domain:
        # The edge terminates TLS for --domain, so its own container addresses are
        # trusted automatically -- the EXACT set, never the bridge subnet, which
        # also contains every workspace's containers.
        trusted += edgesvc.container_addresses()

    # Nothing tightens <home> today -- runner._restrict only covers workspaces --
    # and it holds the users file, the sessions file and the audit trail. That is
    # the design's own adversary #2: another local user on the shared box.
    try:
        home = config.home()
        home.mkdir(parents=True, exist_ok=True)
        if (home.stat().st_mode & 0o077):
            home.chmod(0o700)
            ui.note(f"  tightened {home} to 0700 (it holds accounts, sessions and "
                    "the audit log)")
        # And the files themselves. They are CREATED 0600, but os.open does not
        # change the mode of a file that already exists -- so one written by an
        # older rc-repro keeps whatever the umask gave it, forever. Found by
        # `doctor` on a real box: audit.log was 664.
        from rc_repro.services import sessions as _s
        from rc_repro.services import users as _u
        for _f in (_u.users_file(), _s.sessions_file(), home / "audit.log"):
            try:
                if _f.exists() and (_f.stat().st_mode & 0o077):
                    _f.chmod(0o600)
                    ui.note(f"  tightened {_f.name} to 0600")
            except OSError:
                pass
    except OSError:
        pass
    typer.secho(f"rc-repro GUI: {url}", bold=True)
    if door:
        if door.wildcard:
            ui.hint(f"  one *.{domain} certificate covers every workspace — no "
                    "per-name issuance, no weekly limit to hit.")
        else:
            ui.hint(f"  each workspace name gets its own certificate. A DNS API "
                    f"token in {tlsmod.dns_env_path()} would make it one wildcard.")
        ui.hint(f"  {domain} must already resolve to this machine, or the "
                "certificate cannot be issued.")
        ui.hint("  the first request may take a few seconds while Let's Encrypt "
                "issues it.")
        ui.hint(f"  workspaces published under this name: rc-repro up -v <X.Y.Z> "
                f"--domain <ticket>.{domain}")
    # One line naming the posture, so an operator can read how people sign in and
    # whether the transport is believed, without inferring it from flags.
    if basic:
        how = ("https, arranged by rc-repro" if domain
               # "https, trusted from X" read as a statement of fact, and it is
               # conditional: --trust-proxy grants permission to believe
               # X-Forwarded-Proto, and until a peer in X actually sends it this is
               # plain http with a non-Secure cookie. The line said otherwise.
               else f"https when the proxy says so, trusted from {', '.join(trusted)}"
               if trusted
               # Loopback is not "plain http" in any sense that matters: the
               # password crosses no network. Saying so keeps the warning that
               # DOES matter (the line below, on a reachable bind) meaningful.
               else "loopback, so nothing crosses a network" if loopback
               else "PLAIN HTTP on a reachable interface")
        ui.hint(f"  auth: named accounts over {how}")
        if loopback and not domain:
            # The commonest question after "it works": how do I let anyone else
            # in. Answered here rather than only in --help, because this is the
            # screen somebody is actually looking at.
            # The two answers that need no security decision from the reader. The
            # plain-http one is deliberately absent: this is the screen a
            # first-time user is looking at, and it used to hand them --insecure
            # before they had any reason to weigh it. Somebody who wants that path
            # reaches it by adding --bind, and the refusal explains it there.
            ui.hint("  reachable from this machine only. To share it:")
            ui.hint("    rc-repro serve --domain <name.example.com> --email "
                    "you@example.com")
            ui.hint("    …or, behind a lab/proxy already doing TLS: --bind 0.0.0.0 "
                    "--allow-host <name>\n      --trust-proxy <its address>")
        if not domain and not trusted and not loopback:
            ui.warn("  ⚠ the session cookie will NOT be marked Secure, and the "
                    "sign-in page will warn\n    about an unencrypted connection. "
                    "If TLS terminates at a proxy in front,\n    tell rc-repro "
                    "which one: --trust-proxy <its address>")
    if basic:
        names = ", ".join(f"{u.name} ({usersvc.role_of(u.name)})"
                          for u in usersvc.list_users())
        ui.hint(f"  sign in as: {names}")
        implicit = usersvc.implicit_admins()
        if implicit:
            # Said out loud rather than left to be discovered: these accounts are
            # admin because their role column is blank, which is the migration for
            # everything created before roles existed.
            ui.warn(f"  ⚠ admin by default (blank role): {', '.join(implicit)}")
            ui.hint(f"    narrow one with: rc-repro users role {implicit[0]} member")
        ui.hint("  every action is recorded against the account that took it.")
    elif setup_key:
        ui.hint("  this link creates the first account. It is single-use and "
                "expires in 15 minutes.")
        ui.hint("  the part after # never reaches the server, so it cannot appear "
                "in a log.")
        ui.hint("  prefer a terminal? `rc-repro users add <name>`, then start this "
                "again.")
    # public_https is a server-side FACT, not a guess from a header: with --domain
    # rc-repro arranged the TLS itself, so it knows the browser hop is https even
    # though the request reaches uvicorn as plain http over the docker bridge. It
    # decides the session cookie's Secure flag and __Host- prefix.
    app_obj = create_app(allow_hosts=allow, accounts=True,
                         public_https=bool(domain), first_run=bool(setup_key),
                         trust_proxy=trusted)
    if not no_open and loopback:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless / no browser is fine
            pass
    # proxy_headers=False: uvicorn trusts X-Forwarded-For/-Proto from 127.0.0.1 by
    # DEFAULT, so on the default bind any other local user could rewrite the client
    # address and the scheme. Both now feed security decisions -- the sign-in
    # throttle key and the cookie's Secure flag -- so a client must not choose
    # them. Trusting a real proxy is `--trust-proxy`, deliberately still absent.
    uvicorn.run(app_obj, host=bind, port=port, log_level="warning",
                proxy_headers=False)


def main() -> None:
    """The console-script entry point, and the last line of the error contract.

    FOUR REACHABLE STATES RAISED A NON-ReproError and blew straight past every handler
    in this file: a workspace directory holding a docker-compose.yml and no repro.json
    (FileNotFoundError -- which is exactly what an interrupted create leaves between
    the two atomic_writes, and `runner.exists()` returns True for it while
    `list_meta()` skips it, so the workspace is invisible to `list` and reachable by
    name only as a traceback); a repro.json missing fields, from an older build or a
    truncated write (TypeError: Metadata.__init__() missing 7 required positional
    arguments); a malformed config.yaml (yaml.parser.ParserError); and a non-writable
    RC_REPRO_HOME (PermissionError on .../locks).

    Each printed a Python traceback, exited 1, and under `--json` wrote ZERO BYTES to
    stdout. That last part is the one that matters: "exactly one envelope, and it is
    the last line" is the single promise a caller is told it may depend on, and it was
    broken precisely when the caller most needed it. Exit 1 is `internal` in
    EXIT_CODES, so a script could not tell a corrupt state file from a bug either.

    The traceback still goes to stderr, unabridged -- this changes where the failure is
    reported, not whether it is.
    """
    # THE TIMELINE'S TWO ENDS. `run` ties every event between them to one invocation,
    # which is what makes the log readable backwards from a failure. Best-effort: a log
    # that cannot be written must not stop the command.
    _t0 = time.monotonic()
    try:
        from rc_repro.services import eventlog
        eventlog.started(" ".join(sys.argv[1:]), actor=_cli_actor())
    except Exception:  # noqa: BLE001
        pass

    def _done(code: int) -> None:
        try:
            from rc_repro.services import eventlog
            eventlog.ended(code, time.monotonic() - _t0)
        except Exception:  # noqa: BLE001
            pass

    try:
        app()
    except SystemExit as exc:
        _done(int(exc.code or 0) if isinstance(exc.code, int) else 0)
        # A command that exited normally. Click's standalone mode has already turned
        # its `typer.Exit` into this, which is why the branches BELOW have to do that
        # conversion themselves -- they run outside click.
        raise
    except errors.ReproError as exc:
        _done(getattr(exc, "exit_code", 1))
        _exit_reporting(exc)        # a domain error that escaped a command's handler
    except KeyboardInterrupt:
        ui.warn("interrupted")
        _done(130)
        sys.exit(130)
    except BaseException as exc:    # noqa: BLE001 - the contract's last line
        import traceback
        traceback.print_exc()
        _done(1)
        _exit_reporting(errors.ReproError(
            f"{type(exc).__name__}: {exc} - this is unhandled, and the traceback is "
            f"on stderr. If it names a file under RC_REPRO_HOME, that file is the "
            f"problem."), code=1)


def _exit_reporting(exc: errors.ReproError, code: int | None = None) -> NoReturn:
    """Report `exc` through the normal contract and exit, from OUTSIDE a command.

    `_fail` -> `ui.die` signals by raising `typer.Exit`, and typer.Exit is
    `RuntimeError`-derived, NOT a SystemExit:

        >>> [c.__name__ for c in typer.Exit.__mro__][:3]
        ['Exit', 'RuntimeError', 'Exception']

    Click converts it inside `app()`. Out here nothing does, so calling `_fail`
    directly from `main()` let it propagate as an uncaught exception: a second
    traceback after the message, and the wrong exit code -- 1 instead of the class's
    own. Measured: `rc-repro env --name nope` printed the right error, then a
    traceback, then exited 1 where the published table says 4. The BaseException
    branch was written with this guard and the ReproError branch beside it was not,
    which is how the same defect survived the commit that fixed it.

    Rejected: making `ui.die` raise SystemExit instead. That changes behaviour inside
    every command, where click's standalone mode handles typer.Exit correctly today --
    a large blast radius for a two-line problem out here.
    """
    want = code if code is not None else getattr(exc, "exit_code", 1)
    # `--json` MAY NOT HAVE BEEN PARSED YET. The flag is a command parameter, so
    # `jsonout.activate()` runs inside the command -- and some failures happen before
    # any command does. `_before_any_command` is the sharp one: it reads the accounts
    # file on EVERY invocation, so an unreadable or wrong-type `~/.rc-repro/users` (a
    # stale directory, a file owned by another user on the shared box) failed there and
    # left a `--json` caller with the empty stdout this whole contract exists to
    # prevent. If the caller asked for JSON on the command line, they get JSON.
    if not jsonout.active() and "--json" in sys.argv[1:]:
        jsonout.activate()
    try:
        _fail(exc)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - typer.Exit, whose code we already have
        pass
    sys.exit(want)


if __name__ == "__main__":
    main()
