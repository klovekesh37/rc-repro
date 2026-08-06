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
from rc_repro.services.events import Emit, info, null_emit, warn

_NAME_RE = re.compile(r"[^a-z0-9-]+")
# What sanitize() can produce, and therefore the only shape a real repro has.
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _require_valid_name(name: str) -> None:
    if not _VALID_NAME_RE.match(name):
        raise ValidationError(
            f"invalid repro name {name!r} (lowercase letters, digits and '-' only)")


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
    if not runner.docker_available():
        raise NotReadyError("Docker isn't running. Start Docker Desktop and try again.")


def resolve_name(name: str | None) -> str:
    """Explicit name (must exist) else the configured default (must exist).

    The name is shape-checked first: `sanitize()` runs only at creation, so every
    real repro matches, while the value here goes on to become a filesystem path
    (runner.workspace) and a compose project name. Validating beats trusting it.
    """
    if name:
        _require_valid_name(name)
        if not runner.exists(name):
            raise NotFoundError(f"no repro named {name!r} (run `rc-repro list`)")
        return name
    default = config.load_config().get("default_repro")
    if not default:
        raise ValidationError("no name given and no default repro set (use `rc-repro use <name>`)")
    _require_valid_name(default)
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
    # tls_ports too, or `up --force --https` on an existing repro would not
    # recognise its OWN TLS port as reusable and would drift to the next one --
    # leaking a port number on every recreate, and changing the URL each time.
    for key in ("sidecar_ports", "monitoring_ports", "tls_ports"):
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
    # --https and friends. `https` alone = a certificate from the local openssl CA;
    # + domain/acme_email = Let's Encrypt; + tls_cert/tls_key = one you supply.
    https: bool = False
    domain: str = ""
    tls_san: str = ""              # extra SANs for the local CA, comma-separated
    tls_cert: str = ""
    tls_key: str = ""
    acme_email: str = ""
    acme_staging: bool = False
    acme_challenge: str = "tlsalpn"
    # Whether the caller actually named a challenge. Needed because "tlsalpn" is
    # both the default and a valid explicit choice, so the value alone cannot say
    # if it may be inferred.
    acme_challenge_given: bool = False
    acme_dns_provider: str = ""
    # `up --env KEY=VALUE`. Merged over the preset's env; a None value removes a key.
    env: dict = field(default_factory=dict)
    # Set by _resolve_tls when an inbound ACME challenge requires a public bind.
    # Derived, not asked for -- an explicit --bind always wins.
    bind_public: bool = False


def _infer_acme(req: CreateReq, cfg: dict) -> None:
    """Fill in the challenge and DNS provider when the user did not name them.

    Both are answerable from what is already on disk, and making the user restate
    them every run was the bulk of the flag noise. An explicit flag always wins.
    """
    from rc_repro import tls as tlsmod

    if not req.acme_challenge_given:
        # Credentials present => the user set up dns-01. Otherwise the inbound
        # challenge, which needs nothing but an open port.
        req.acme_challenge = "dns" if tlsmod.dns_env_vars() else "tlsalpn"
    if req.acme_challenge != "dns":
        return
    req.acme_dns_provider = (req.acme_dns_provider
                             or str(cfg.get("acme_dns_provider") or ""))
    if req.acme_dns_provider:
        return
    provider, why = tlsmod.infer_dns_provider()
    if not provider:
        raise ValidationError(
            f"could not tell which DNS provider to use: {why}.\n"
            "  Name it with --acme-dns-provider (or `rc-repro config set "
            "acme.dns_provider <name>`).\n"
            f"  Providers and their variables: {tlsmod.LEGO_PROVIDER_DOCS}")
    req.acme_dns_provider = provider


def _resolve_tls(req: CreateReq, repro_name: str, bind_host: str, exclude: str = "",
                 emit: Emit = null_emit):
    """Turn the --https flags into a tls.TlsSpec, or None when --https is absent.

    Validates here rather than letting Traefik fail at boot: a bad combination
    otherwise produces a repro that comes up and serves nothing, with the reason
    only in `docker compose logs traefik`.
    """
    # Any of these means HTTPS. Requiring --https alongside them was ceremony: a
    # domain or a certificate path has no other meaning. --https on its own still
    # selects the local-CA mode, which needs nothing else.
    if not (req.https or req.domain or req.tls_cert or req.tls_key
            or req.acme_email or req.tls_san):
        return None

    from rc_repro import tls as tlsmod

    # Normalized before anything else reads it: it becomes ROOT_URL, an ACME
    # `domains` entry and a TLS SNI name, so a scheme or trailing slash surviving
    # this far corrupts all three at once.
    domain, fixed = (tlsmod.normalize_domain(req.domain) if req.domain else ("", ""))
    if fixed:
        req.domain = domain          # so downstream and repro.json agree with reality

    if bool(req.tls_cert) != bool(req.tls_key):
        raise ValidationError("--tls-cert and --tls-key must be given together")
    if req.tls_cert and req.acme_email:
        raise ValidationError("--tls-cert supplies a certificate; --acme-email would "
                              "request another. Pick one.")

    cfg = config.load_config()
    if req.tls_cert:
        mode = tlsmod.MODE_OWN
    elif domain:
        # A domain with no certificate path means Let's Encrypt. The email can be
        # remembered (`rc-repro config set acme.email`) instead of retyped.
        mode = tlsmod.MODE_ACME
        req.acme_email = req.acme_email or str(cfg.get("acme_email") or "")
        if not req.acme_email:
            raise ValidationError(
                "a Let's Encrypt certificate needs a contact email. Either:\n"
                "  rc-repro config set acme.email you@example.com     (remembered)\n"
                "  ...or pass --acme-email on this run.\n"
                "To use a certificate you already have instead, pass "
                "--tls-cert/--tls-key.")
    else:
        mode = tlsmod.MODE_LOCAL

    if mode == tlsmod.MODE_ACME:
        _infer_acme(req, cfg)
    if req.acme_challenge not in ("tlsalpn", "dns"):
        raise ValidationError(f"--acme-challenge {req.acme_challenge!r} "
                              "(want tlsalpn | dns)")

    # An inbound challenge is validated by Let's Encrypt CONNECTING here, so it can
    # only work on a public interface. That is derivable, so derive it rather than
    # failing on a missing --bind: `bind_public` tells the caller to widen the bind
    # and warn about the exposure. An explicit --bind always wins.
    # dns-01 is validated by a TXT record and never connects here, so it stays on
    # loopback -- these repros run fixed weak credentials, and widening the bind for
    # a challenge that does not need it would expose them for nothing.
    req.bind_public = (mode == tlsmod.MODE_ACME
                       and req.acme_challenge == "tlsalpn"
                       and not req.bind)

    # dns-01 credentials: checked here, not left to fail inside Traefik minutes
    # later. Each lego provider reads its own variables, so this verifies the file
    # exists and has content, not which keys it holds.
    if mode == tlsmod.MODE_ACME and req.acme_challenge == "dns":
        ok, detail = tlsmod.dns_credentials(req.acme_dns_provider)
        if not ok:
            raise ValidationError(detail)

    host = domain or tlsmod.local_host_for(repro_name)
    # A real domain answers on 443 so the URL carries no port; a .localhost name
    # gets an allocated port, because every repro would otherwise want 443.
    port = 443 if mode != tlsmod.MODE_LOCAL else _pick_tls_port(exclude=exclude)
    # Ports this repro already claims are exempt from the probe, exactly as
    # pick_host_port does it: on `up --force` its OWN Traefik is still running and
    # still holding 443, so probing would report "in use" and refuse to recreate
    # the very repro that owns it.
    own = own_ports(exclude) if exclude else set()
    if mode != tlsmod.MODE_LOCAL and port not in own and not runner.port_free(port):
        raise ConflictError(
            f"port {port} is already in use on this machine, and a repro on a real "
            "domain has to own it. Free it, or `rc-repro down` whatever holds it.")
    # A domain-backed repro also publishes :80 and redirects it to https, the way the
    # official rocketchat-compose Traefik files do -- otherwise typing the bare
    # hostname reaches nothing, because browsers try http first.
    #
    # Best-effort, NOT required: refusing to create the repro because something else
    # holds 80 would block --domain entirely for anyone running a web server there,
    # even under dns-01 where 80 plays no part. No challenge needs 80 any more, so
    # there is no case where a busy 80 has to be fatal.
    redirect = False
    if tlsmod.can_redirect_http(mode, port):
        redirect = 80 in own or runner.port_free(80)
        if not redirect:
            warn(emit, "port 80 is in use, so http:// will not redirect to https. The "
                       "workspace still serves fine on https - you just have to type "
                       "the scheme.", phase="tls")

    return tlsmod.TlsSpec(
        mode=mode, host=host, port=port, acme_email=req.acme_email,
        acme_staging=req.acme_staging, acme_challenge=req.acme_challenge,
        acme_dns_provider=req.acme_dns_provider,
        cert_path=req.tls_cert, key_path=req.tls_key,
        http_redirect=redirect)


def _pick_tls_port(exclude: str = "") -> int:
    """A free host port for the TLS entrypoint, avoiding every port other repros
    claim. Separate from pick_host_port so RC keeps its own contiguous range.

    `own` is exempted from the port_free() probe, the same way pick_host_port does
    it: on `up --force` the repro's OWN Traefik is still running and still holding
    the port, so probing it would report "in use" and drift to the next number --
    changing the workspace URL on every recreate.
    """
    own = own_ports(exclude) if exclude else set()
    used = runner.used_ports() - own
    for p in range(8443, runner.PORT_MAX):
        if p in used:
            continue
        if p in own or runner.port_free(p):
            return p
    raise ConflictError("no free host port for the HTTPS entrypoint (tried 8443+)")


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


def create_repro(req: CreateReq, emit: Emit = null_emit, *, stream_output: bool = False) -> dict:
    """Create-or-reuse a repro. Returns a result dict (meta + boot/seed info).

    `stream_output=True` streams docker's line output through `emit` (for the web
    job log); False leaves docker's own progress on the terminal (CLI default).
    """
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
        return _reuse(repro_name, wait, req, emit, stream_output=stream_output,
                      resolved=resolved, preset_name=pre.name)

    _guard_project_collision(repro_name)
    check_sidecar_ports(pre, exclude=repro_name)
    if req.monitor:
        check_monitor_ports(exclude=repro_name)
    host_port = pick_host_port(req.port, pre, exclude=repro_name)
    root = req.root_url or f"http://localhost:{host_port}"
    token = req.reg_token or cfg.get("reg_token") or ""
    bind_host = req.bind or cfg.get("bind_host") or config.DEFAULT_BIND_HOST
    tlsspec = _resolve_tls(req, repro_name, bind_host, exclude=repro_name, emit=emit)
    if req.bind_public and bind_host not in ("0.0.0.0", "::"):
        # Derived from the challenge (see _resolve_tls), not requested.
        bind_host = "0.0.0.0"
        warn(emit, f"binding 0.0.0.0 so Let's Encrypt can reach this host for the "
                   f"{req.acme_challenge} challenge - that exposes the workspace, and "
                   "it runs the fixed admin/admin123 credentials. `rc-repro down` when "
                   "you are finished.", phase="tls")
    # RC advertises the https URL (links, OAuth callbacks, CORS all derive from
    # ROOT_URL); `root` stays http so rc-repro's own API calls need no CA.
    public = tlsspec.root_url if tlsspec else ""

    # Overrides already on this repro are carried forward: `up --force` rebuilds the
    # compose file from the spec, so without this a rebuild would silently drop env
    # the user had set. Anything named on THIS run wins.
    env_overrides: dict = {}
    if runner.exists(repro_name):
        try:
            prev = runner.read_meta(repro_name).extra.get("env")
            if isinstance(prev, dict):
                env_overrides.update(prev)
        except Exception:  # noqa: BLE001 - half-written record; nothing to carry
            pass
    env_overrides.update(req.env or {})

    spec = compose.Spec.from_resolved(
        resolved, project_name=runner.project_name(repro_name),
        root_url=(req.root_url or public or root),
        host_port=host_port, reg_token=token or None, preset=pre,
        bind_host=bind_host, monitoring=req.monitor, tls=tlsspec,
        env_overrides=env_overrides)
    try:
        doc = compose.build(spec)
    except ValueError as exc:
        # e.g. a preset naming an entry_service it doesn't define.
        raise ValidationError(str(exc)) from exc

    meta = runner.Metadata(
        name=repro_name, project=spec.project_name, rc_version=resolved.rc_version,
        rc_image=resolved.rc_image, mongo_tag=resolved.mongo_tag,
        mongo_flavor=resolved.mongo_flavor, preset=pre.name, root_url=root,
        host_port=host_port, version_source=resolved.source, pinned=req.pin,
        public_url=public,
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
    if env_overrides:
        meta.extra["env"] = env_overrides
    files = list(pre.files)
    if tlsspec:
        from rc_repro import tls as tlsmod
        cert_pem = key_pem = ""
        if tlsspec.mode == tlsmod.MODE_LOCAL:
            info(emit, f"issuing a local certificate for {tlsspec.host}", phase="tls")
            sans = [s for s in (req.tls_san or "").split(",") if s.strip()]
            cert_pem, key_pem = tlsmod.issue_leaf(tlsspec.host, sans)
        elif tlsspec.mode == tlsmod.MODE_OWN:
            cert_pem, key_pem = tlsmod.read_own_cert(tlsspec.cert_path, tlsspec.key_path)
        else:
            # Rule out the DNS mistakes that are CERTAIN to fail, before an attempt
            # spends quota (5 failed validations per hostname per hour). Cannot
            # prove inbound reachability from here — that depends on NAT/firewalls
            # this process cannot see — so a pass is necessary, not sufficient.
            ok, detail = tlsmod.dns_preflight(tlsspec.host, tlsspec.acme_challenge)
            if not ok:
                raise ValidationError(detail)
            info(emit, f"DNS {detail}", phase="tls")
            if tlsspec.acme_challenge != "dns" and not tlsmod.host_has_public_address():
                warn(emit, "no interface on this host has a public address, so an "
                           "inbound challenge can only work through a port-forward. "
                           "If validation fails, use --acme-challenge dns.", phase="tls")
            gaps = tlsmod.reachability_gaps(tlsspec, bind_host)
            if gaps:
                warn(emit, "the certificate will be valid, but the workspace will NOT "
                           f"be reachable at {tlsspec.root_url} — {'; '.join(gaps)}. "
                           "Locally: curl --resolve "
                           f"{tlsspec.host}:{tlsspec.port}:127.0.0.1 {tlsspec.root_url}",
                     phase="tls")
            info(emit, f"requesting a certificate from Let's Encrypt "
                       f"({'staging' if tlsspec.acme_staging else 'production'}) "
                       f"for {tlsspec.host} — Traefik does this in the background; "
                       f"confirm with `rc-repro tls-status -n {repro_name}`", phase="tls")
            # Traefik writes acme.json here; it must exist and be private BEFORE
            # the container mounts it, or Traefik refuses to use it.
            tlsmod.acme_dir().mkdir(parents=True, exist_ok=True)
            tlsmod.acme_dir().chmod(0o700)
        files += tlsmod.files(tlsspec, compose.rc_service_names(pre.instances),
                             cert_pem, key_pem)
        meta.extra["tls"] = tlsspec.mode
        meta.extra["tls_ports"] = [tlsspec.port]
        if tlsspec.mode == tlsmod.MODE_ACME:
            # Recorded so `tls-status` can print a promote command that actually
            # works. Without these it rebuilt a guess that dropped the challenge
            # flags and added --bind, i.e. a copy-pasteable command that fails.
            meta.extra["tls_challenge"] = tlsspec.acme_challenge
            meta.extra["tls_dns_provider"] = tlsspec.acme_dns_provider
            meta.extra["tls_staging"] = tlsspec.acme_staging
            meta.extra["tls_email"] = tlsspec.acme_email
        meta.extra.setdefault("notes", [])
        meta.extra["notes"] = list(meta.extra["notes"]) + tlsmod.notes(tlsspec, repro_name)
    if req.monitor:
        from rc_repro import monitoring
        targets = compose.rc_service_names(pre.instances)
        files += monitoring.files(targets, project=spec.project_name)
        meta.extra["monitoring"] = True
        meta.extra["monitoring_ports"] = list(config.MONITOR_PORTS)
        meta.extra.setdefault("notes", [])
        meta.extra["notes"] = list(meta.extra["notes"]) + monitoring.notes()

    if runner.exists(repro_name):
        # Volumes the OLD compose file declared but the new one doesn't (switching
        # preset with --force, dropping --monitor) would be orphaned: `down -v`
        # only removes what the file declares, so once it's rewritten nothing can
        # ever reach them again.
        stale: set[str] = set()
        if not req.fresh:
            try:
                stale = set(runner.read_compose(repro_name).get("volumes") or {}) - set(doc["volumes"])
            except Exception:  # noqa: BLE001 - unreadable old file: nothing to reconcile
                stale = set()
        if runner.down(repro_name, volumes=req.fresh) != 0:
            raise DockerError(f"could not tear down the existing {repro_name!r}; not overwriting it")
        for bad in runner.remove_volumes(repro_name, sorted(stale)):
            warn(emit, f"could not remove {bad}, left over from the previous preset",
                 phase="create")

    runner.write(repro_name, compose.to_yaml(doc), meta, files=files)
    if req.pin:
        config.update_config(lambda cfg: cfg.__setitem__("default_repro", repro_name))

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


def _reuse(name: str, wait: bool, req: CreateReq, emit: Emit, *, stream_output: bool,
           resolved=None, preset_name: str = "") -> dict:
    meta = runner.read_meta(name)
    # An existing repro is reused EXACTLY as recorded. Say so when the request
    # asked for something else: `up --version 9.9.9 --name existing` silently
    # booted the old version, which is fatal for a tool whose whole premise is
    # version matching. (The version check in wait_and_finalize compares RC's
    # reported version against the STORED one, so it stays quiet here.)
    mismatch: list[str] = []
    if resolved is not None and resolved.rc_version != meta.rc_version:
        mismatch.append(f"version {resolved.rc_version} (existing: {meta.rc_version})")
    if preset_name and preset_name != meta.preset:
        mismatch.append(f"preset {preset_name!r} (existing: {meta.preset!r})")
    if req.monitor and not (isinstance(meta.extra, dict) and meta.extra.get("monitoring")):
        mismatch.append("monitoring (existing: not attached)")
    if mismatch:
        warn(emit, f"{name!r} already exists and is reused as-is, ignoring requested "
                   + "; ".join(mismatch)
                   + ". Use --force to rebuild it, or --fresh to also wipe its data.",
             phase="create")

    state = runner.rc_state(name)
    if state == "running":
        info(emit, f"{name!r} is already running.", phase="create")
    else:
        info(emit, f"{name!r} already exists - bringing it back up.", phase="create")
        if _up(name, pull=False, emit=emit, stream_output=stream_output) != 0:
            raise DockerError("`docker compose up` failed")
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
    links = [{"label": "Rocket.Chat", "url": m.external_url, "kind": "rc"}]
    extra = m.extra if isinstance(m.extra, dict) else {}
    if m.public_url:
        # Keep the plain http way in as well: with the local CA the browser warns
        # until `trust-ca` has run, and this is the link that always just works.
        links.append({"label": "direct HTTP", "url": m.root_url, "kind": "rc"})
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


def repro_state(rc_status: str, has_containers: bool) -> str:
    """The state to report for a repro, from its Rocket.Chat container's docker
    `Status` string ("Up 2 hours (healthy)", "Restarting (1) 5 seconds ago", ...).

    BOTH the list and the detail panel must derive state from this, because they
    used to disagree. The list read compose's PROJECT-level aggregate, where the
    always-running Mongo put "running(" in the string and made a crash-looping RC
    report "running"; the panel checked only whether RC was literally "running"
    and flattened everything else to "stopped". So one repro showed two different
    states at the same moment, and neither of them said "restarting" -- during a
    crash loop, which is exactly when the answer matters.

    `has_containers` separates "the stack is gone" (down) from "containers exist
    but Rocket.Chat has none" (stopped).
    """
    s = (rc_status or "").strip()
    if not s:
        return "stopped" if has_containers else "down"
    low = s.lower()
    if low.startswith("up"):
        # docker reports a paused container as "Up 3 days (Paused)".
        return "paused" if "(paused)" in low else "running"
    if low.startswith("exited"):
        return "stopped"
    # "Restarting (1) 5 seconds ago" -> restarting; "Created"/"Dead" -> as-is.
    return low.split(" (")[0].split(" ")[0] or "down"


# States beyond running/stopped/down that repro_state() can now hand to the UI:
# mid-transition or wedged, rather than plainly up/stopped/gone. This is the
# backend->UI contract for them -- tests/test_core.py asserts the web UI styles
# each one and offers it as a status filter, so adding a state here fails loudly
# until the dashboard handles it (a state it does not know renders unstyled and,
# before this existed, with no lifecycle buttons at all).
TRANSIENT_STATES = ("restarting", "created", "paused", "dead")


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
        rc_status = status_map.get(m.project, "")
        # `states` is only consulted for "does this project have ANY container",
        # never for the state itself -- see repro_state().
        state = "?" if not docker_up else repro_state(rc_status, bool(states.get(m.project)))
        uptime, health = _uptime_health(rc_status)
        monitored = bool(isinstance(m.extra, dict) and m.extra.get("monitoring"))
        out.append({"name": m.name, "rc_version": m.rc_version, "mongo_tag": m.mongo_tag,
                    "host_port": m.host_port, "root_url": m.root_url, "state": state,
                    # The https URL when `up --https` was used; "" otherwise. The CLI
                    # and GUI show this in preference to root_url, which stays http.
                    "public_url": m.public_url, "tls": m.extra.get("tls", "") if isinstance(m.extra, dict) else "",
                    "preset": m.preset, "pinned": m.pinned, "default": m.name == default,
                    "monitoring": monitored, "created_at": m.created_at,
                    "uptime": uptime, "health": health or (state if state == "running" else ""),
                    "grafana_url": f"http://localhost:{config.MONITOR_PORTS[1]}" if monitored else None,
                    "links": repro_links(m)})
    return out


def describe(name: str) -> dict:
    return _summary(runner.read_meta(resolve_name(name)))


# Substrings that mark an env value as a credential. The env tab is a debugging
# aid served to any client holding the session token, and it carried real secrets
# verbatim: REG_TOKEN (an EE license), the LDAP bind password, MinIO's secret key.
# The workspace's docker-compose.yml on disk stays the source of truth for anyone
# who genuinely needs a value.
_SECRET_KEY_HINTS = ("password", "pass", "secret", "token", "_key", "apikey",
                     "credential")
REDACTED = "********"


def redact_env(key: str, value: str) -> str:
    """Mask an env value whose KEY names a credential; pass anything else through."""
    low = key.lower()
    return REDACTED if value and any(h in low for h in _SECRET_KEY_HINTS) else value


def _env_rows(doc: dict, overrides: dict | None = None) -> list[dict]:
    """The RC service's env as [{key, value, override}], credentials masked.

    `override` marks the keys the user set with `env --set`, so the panel can tell
    those apart from the preset/base defaults -- "remove" means a different thing
    for each.
    """
    own = set(overrides or {})
    svcs = doc.get("services", {})
    rc_svc = svcs.get("rocketchat") or svcs.get("rocketchat-1") or {}
    env = rc_svc.get("environment", {})
    if isinstance(env, dict):
        pairs = sorted(env.items())
    elif isinstance(env, list):  # compose list form "K=V"
        pairs = [(k, v) for k, v in ((e.split("=", 1) + [""])[:2] for e in env)]
    else:
        return []
    return [{"key": k, "value": redact_env(k, str(v)), "override": k in own}
            for k, v in pairs]


def detail(name: str) -> dict:
    """Rich detail for the GUI panel: summary + state/uptime/health + links +
    containers + the RC service's env vars."""
    target = resolve_name(name)
    m = runner.read_meta(target)
    d = _summary(m)
    # The env tab masks credentials and tells the reader that the workspace's
    # docker-compose.yml is the source of truth for a real value (see redact_env)
    # -- but nothing in the GUI said where that workspace is.
    d["workspace"] = str(runner.workspace(target))
    # The list payload has carried `default` all along; the panel needs it too, so
    # it can offer "Make default" only where that would change something.
    d["is_default"] = target == config.load_config().get("default_repro")
    # The panel keys its HTTPS row and its "Check TLS" action off these. list_repros()
    # carried them and detail() did not, so the feature was invisible in the panel.
    d["public_url"] = m.public_url
    d["tls"] = m.extra.get("tls", "") if isinstance(m.extra, dict) else ""
    # `container_details` returns [] both for "no containers" AND for "docker could
    # not be asked", so deriving state from it alone asserted `down` whenever the
    # daemon was unreachable — while list_repros() reported "?" for the same repro.
    # The two views must agree, and neither may claim to know what it cannot.
    if not runner.docker_available():
        d["state"], d["uptime"], d["health"] = "?", "", ""
        d["containers"] = []
        d["links"] = repro_links(m)
        d["env"] = _env_rows(runner.read_compose(target), m.extra.get("env")
                             if isinstance(m.extra, dict) else None)
        return d
    containers = runner.container_details(target)
    rc = [c for c in containers if c["service"] == "rocketchat" or c["service"].startswith("rocketchat-")]
    rc_status = next((c["status"] for c in rc), "")
    d["state"] = repro_state(rc_status, bool(containers))
    up, health = _uptime_health(rc_status)
    d["uptime"] = up
    d["health"] = health or (d["state"] if d["state"] != "down" else "")
    # A climbing restart count is the difference between "slow to boot" and
    # "crash-looping"; wait_serving already warns on it during a create, but after
    # that nothing surfaced it. Only asked when containers exist -- it costs two
    # docker calls, and a `down` repro has nothing to inspect.
    if containers:
        d["restarts"] = runner.rc_restart_count(target)
    d["links"] = repro_links(m)
    d["containers"] = containers
    d["env"] = _env_rows(runner.read_compose(target), m.extra.get("env")
                         if isinstance(m.extra, dict) else None)
    return d


def set_state(name: str, action: str) -> None:
    target = resolve_name(name)
    # Looked up by a hashable key: `action` arrives from a JSON body, and a dict or
    # list reached .get() and raised "unhashable type" -- a 500 rather than the
    # "unknown action" this already knows how to say.
    if not isinstance(action, str):
        raise ValidationError(f"action must be a string (want start|stop|restart), "
                              f"got {type(action).__name__}")
    fn = {"start": runner.start, "stop": runner.stop, "restart": runner.restart}.get(action)
    if fn is None:
        raise ValidationError(f"unknown action {action!r} (want start|stop|restart)")
    if fn(target) != 0:
        # A `down`ed repro has no containers, so `compose start` can never revive
        # it. Say what actually works instead of just reporting the exit code.
        hint = ""
        if action in ("start", "restart") and runner.rc_state(target) == "absent":
            hint = (f" - {target!r} was `down`ed, so it has no containers to "
                    "start; recreate them from its stored metadata instead")
        raise DockerError(f"`docker compose {action}` failed for {target!r}{hint}")


def _clear_default_if(name: str) -> None:
    def mutate(cfg: dict) -> None:
        if cfg.get("default_repro") == name:
            cfg.pop("default_repro", None)

    # Locked read-modify-write: prune() calls this in a loop and the GUI runs it on
    # worker threads, so a plain read/write pair can lose an update.
    config.update_config(mutate)


def teardown(name: str, *, volumes: bool = False, confirm: bool = False, emit: Emit = null_emit) -> dict:
    require_docker()
    target = resolve_name(name)
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


def prunable() -> list[str]:
    """Names of repros that are safe to prune: not pinned and with no containers
    (a plain `down`). Raises DockerError if docker can't be queried — deleting on
    that ambiguity would be destructive."""
    require_docker()
    states = runner.project_states()
    if states is None:
        raise DockerError("couldn't query docker compose projects - not pruning (is Docker healthy?)")
    return [m.name for m in runner.list_meta() if not m.pinned and m.project not in states]


def prune(*, confirm: bool = False, emit: Emit = null_emit) -> dict:
    targets = prunable()
    if not targets:
        return {"targets": [], "removed": []}
    if not confirm:
        raise ValidationError(f"prune deletes {len(targets)} down repro(s) incl. data - pass confirm=true")
    removed = []
    for name in targets:
        if runner.down(name, volumes=True) != 0:
            warn(emit, f"could not clean up {name!r} - skipping", phase="done")
            continue
        runner.remove(name)
        _clear_default_if(name)
        removed.append(name)
        info(emit, f"pruned {name!r}", phase="done")
    return {"targets": targets, "removed": removed}
