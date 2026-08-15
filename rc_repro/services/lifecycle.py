"""Repro lifecycle orchestration shared by the CLI and the web API.

Extracted from cli.py so both front-ends run the identical flow. Functions raise
`rc_repro.errors` and report progress through `emit` (see services.events); they
never call typer / sys.exit / typer.confirm.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dataclasses import asdict
from pathlib import Path

from rc_repro import compose, config, presets, rcapi, runner, versions
from rc_repro import seed as seeder
from rc_repro.errors import (ConflictError, DockerError, NotFoundError,
                             NotReadyError, PreflightError, ReproError,
                             ValidationError)
from rc_repro.services import audit as auditsvc
from rc_repro.services import edge as edgesvc
from rc_repro.services import diagnose, postready, topology
from rc_repro.services.events import Emit, info, null_emit, warn

_NAME_RE = re.compile(r"[^a-z0-9-]+")
# What sanitize() can produce, and therefore the only shape a real repro has.
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# The name becomes a directory entry (255 bytes on ext4/APFS) and a compose project
# name, and the generated container names add `rcrepro-<name>-<service>-1` on top.
# Unbounded, it passed validation and then raised ENAMETOOLONG from the filesystem
# -- a 500 from the web API and a raw traceback from the CLI. 64 leaves ample room
# for every suffix rc-repro appends and is longer than any real repro name.
NAME_MAX = 64

#: How long an INTERACTIVE mutator waits for the repro lock. repro_lock's own
#: default is 900s, which is right for a create or a restore queued behind
#: another -- and wrong for a button: nobody wants a Stop click to hang for
#: fifteen minutes rather than say "something else is working on this".
_INTERACTIVE_LOCK_WAIT = 60.0


def _require_valid_name(name: str) -> None:
    if not _VALID_NAME_RE.match(name):
        raise ValidationError(
            f"invalid repro name {name!r} (lowercase letters, digits and '-' only)")
    if len(name) > NAME_MAX:
        raise ValidationError(
            f"repro name is {len(name)} characters; the limit is {NAME_MAX} "
            "(it becomes a directory and a set of container names)")


# --- naming (pure) ------------------------------------------------------------

def sanitize(name: str) -> str:
    name = name.lower().replace(".", "-")
    return _NAME_RE.sub("-", name).strip("-")


def derive_name(version: str, preset: str, actor: str = "") -> str:
    """The workspace name for a version+preset, namespaced by who asked.

    Without `actor` this returns the same name for everybody -- so on a shared
    server the second person to run `up -v 8.5.1` silently REUSED the first
    person's workspace, data and all, with no warning. Prefixing by the
    authenticated user makes two people running the same version two workspaces.

    Left off entirely on a single-user machine, where the prefix would be noise
    and would break every existing name.
    """
    base = "rc" + version
    if preset and preset != "default":
        base += "-" + preset
    if actor:
        base = f"{actor}-{base}"
    return sanitize(base)


def owner_prefix(name: str, actor: str) -> str:
    """`name` namespaced to `actor`, unless it already is.

    An explicit --name still wins, but on a shared server it has to be somebody's
    or two people typing `--name test` collide exactly as the derived names did.
    """
    if not actor:
        return name
    return name if name.startswith(f"{actor}-") else f"{actor}-{name}"


# --- preconditions ------------------------------------------------------------

def require_docker() -> None:
    """Refuse when the container engine is not answering.

    `DockerError`, not `NotReadyError`. The distinction is the whole point of the
    exit-code taxonomy: `NOT_READY` (exit 5, HTTP 409) means "it is coming up,
    poll again", and an absent engine is never that -- polling cannot start
    Docker. It is a preflight problem the caller must fix, so `ENGINE_UNAVAILABLE`
    (exit 3, HTTP 502): the dependency this tool sits on is not there.

    It matters more once a second topology exists: "the engine is not answering"
    will cover a missing cluster and an unreachable API server too, and those are
    the same kind of answer -- fix your environment -- not the same kind as a
    workspace that has not finished booting.
    """
    if not runner.docker_available():
        raise DockerError("Docker isn't running. Start Docker Desktop and try again.")


#: Rocket.Chat settled at 670-770 MB and MongoDB at 124-184 MB per workspace on
#: the host that OOMed -- so ~950 MB at REST. Rounded UP to 1100, deliberately:
#: those were idle figures, RC's Node heap reached 721 MB under a mild load test,
#: and calibrating to the resting case would have permitted exactly the seven
#: stacks that took the machine down. Matches design §8's "roughly 1.2 GB".
WORKSPACE_MB = 1100
#: Sidecars a preset adds. Keycloak (saml/oidc) is by far the largest.
PRESET_MB = {"saml": 450, "oidc": 450, "s3_minio": 120, "ldap": 80,
             "livechat": 60, "multi-instance": 400, "email": 40}
#: Prometheus + Grafana + Loki + the OTel collector + two exporters.
MONITORING_MB = 280

# Kubernetes costs on top of a workspace, and both are load-bearing for the same
# reason WORKSPACE_MB is: the formula that refuses a create has to know what it is
# about to start, or it refuses the wrong things and permits the fatal ones.
#
# CLUSTER_MB is MEASURED -- `docker stats kind-control-plane` reads 573 MiB idle on
# this box -- and is charged ONCE, only when the cluster still has to be created.
# Every workspace after that shares it.
CLUSTER_MB = 600

# KUBE_CHART_MB is what the CHART costs beyond the app itself, and it applies to
# both deployments. Measured, and it corrected an assumption: a "monolith" on this
# chart is FIVE pods, not two -- it runs NATS (two pods plus nats-box) regardless
# of `microservices.enabled`, which Compose's monolith does not. A live monolith
# workspace plus a fresh cluster took the host from 8322 MB available to 6351, so
# 1971 MB total with NATS still starting; against WORKSPACE_MB + CLUSTER_MB = 1700
# that leaves roughly this much unaccounted for.
KUBE_CHART_MB = 500

# MICROSERVICES_MB is now HALF measured, and the halves are worth separating.
#
# MEASURED: a microservices workspace on chart 7.0.2 is nine pods against a
# monolith's five, and the four extra are `account`, `authorization`,
# `ddp-streamer` and `presence`. Not five -- this chart version ships no
# `stream-hub` deployment, which the earlier estimate assumed it did.
#
# STILL ESTIMATED: what those four cost. The memory delta could NOT be measured,
# and the reason is worth recording so nobody trusts the number more than it
# deserves: both readings -- 1971 MB for the monolith, 1787 MB for microservices --
# were taken at readiness, while several pods were still ContainerCreating. They
# are early reads, they are within noise of each other, and the microservices one
# came out LOWER, which cannot be true. A real figure needs a settle-time wait and
# per-pod `docker stats`.
#
# So this is 4 pods x ~200 MB, and it stays deliberately on the generous side:
# under-charging lets through a create that OOMs a swapless host, where the kernel
# picks its own victim and destroys somebody else's work.
MICROSERVICES_MB = 800

#: Left unspent: for the OS, Docker, the page cache -- and, mostly, for GROWTH.
#: A fifth of the host, never below 1 GB.
#:
#: The proportion is the lesson from the incident. That box OOMed while
#: MemAvailable still read ~3.3 GB, because the danger is not the memory a
#: workspace takes at admission but the memory seven already-admitted ones take
#: an hour later: Rocket.Chat is Node, its heap grows with use, and a mild load
#: test alone pushed one to 721 MB. A fixed 1 GB reserve waved the fatal
#: configuration through when I tested it against the real numbers; a fifth
#: refuses it.
#:
#: This is why the check cannot be the only defence -- see the growth caveat in
#: check_capacity().
def host_reserve_mb(total_mb: int) -> int:
    return max(1024, total_mb // 5)


def capacity() -> dict:
    """How much room this machine has left, in workspaces.

    ONE formula, three consumers: `check_capacity` refuses a create with it,
    `rc-repro doctor` reports it, and the GUI's home page leads with it. They used
    to agree by coincidence -- doctor carried its own copy of the arithmetic -- and
    a home page that says "room for 2 more" while the create is refused is worse
    than one that says nothing.

    Megabytes are not the answer to the question anybody is asking. "3.2 GB
    available" does not tell you whether you can start another workspace; `room`
    does. Written after seven concurrent stacks OOM-killed a 10 GB host.
    """
    mem = runner.host_memory()
    if mem is None:                      # not Linux: say so rather than guess
        return {"known": False}
    total_mb, avail_mb, swap_mb = mem
    reserve = host_reserve_mb(total_mb)
    return {"known": True, "total_mb": total_mb, "available_mb": avail_mb,
            "reserve_mb": reserve, "swap_mb": swap_mb,
            "workspace_mb": WORKSPACE_MB,
            "room": max(0, avail_mb - reserve) // WORKSPACE_MB}


def _kube_overhead_mb(req: "CreateReq") -> int:
    """What Kubernetes adds to a workspace's memory bill, in MB.

    Zero for Compose, which is every workspace today. The control plane is charged
    only when it still has to be created: it is shared, so billing it to the second
    and third workspace would refuse creates the host could actually hold -- and a
    capacity check that is wrong in the safe direction still stops people using the
    tool, which is how they learn to pass --force by reflex.
    """
    from rc_repro.services import topology
    if topology.normalize(getattr(req, "runtime", "")) != topology.KUBERNETES:
        return 0
    # The chart's own baseline, on both deployments -- see KUBE_CHART_MB.
    need = KUBE_CHART_MB
    try:
        from rc_repro.services import k8s
        # Specifically OUR cluster, not "a reachable cluster". rc-repro creates its
        # own, so somebody else's cluster being up does not mean the control plane
        # is already paid for. Charging on `cluster_reachable` billed zero on a box
        # whose only cluster belonged to someone else.
        if not k8s.preflight().cluster_exists:
            need += CLUSTER_MB
    except Exception:  # noqa: BLE001 - an unprobeable cluster is charged for
        need += CLUSTER_MB
    # An empty deployment means "that runtime's default", which for Kubernetes is
    # microservices -- the expensive one. Reading empty as free would under-charge
    # the common case, and `check_capacity` can be reached with an unresolved
    # request from `restore` and the GUI as well as from `up`.
    deployment = getattr(req, "deployment", "") or topology.DEPLOYMENTS[
        topology.KUBERNETES][0]
    if deployment == topology.MICROSERVICES:
        need += MICROSERVICES_MB
    return need


def check_capacity(req: "CreateReq", preset_name: str = "", emit: Emit = null_emit) -> None:
    """Refuse to create a workspace the host cannot hold.

    Written after seven concurrent stacks OOM-killed a 10 GB host with no swap:
    the kernel killed a Rocket.Chat process, load average hit 165, and the box had
    to be recovered. Every individual `up` had succeeded -- nothing anywhere asked
    whether there was room for one more, which is exactly the gap
    docs/design/team-server.md §8 recorded as "made visible, not solved".

    It cannot be the only defence, and should not be presented as one: the memory
    that kills a host is the memory workspaces take LATER, not at admission. This
    stops the obviously-doomed create and keeps the ceiling visible; container
    memory limits and enough RAM are what actually bound the total.

    A REFUSAL rather than a warning, because the failure is not confined to the
    workspace being created: the OOM killer picks its own victim, so on a shared
    server this destroys a colleague's work. A warning would be advice nobody
    reads until after the machine is down. Overridable with --force, and the
    message says what to stop instead.
    """
    mem = runner.host_memory()
    if mem is None:                     # not Linux: skip rather than guess
        return
    total_mb, available_mb, swap_mb = mem
    need = WORKSPACE_MB + PRESET_MB.get(preset_name, 0)
    if req.monitor:
        need += MONITORING_MB
    # A Kubernetes workspace is not a Compose workspace with a different label. It
    # brings a control plane if there is not one yet, and microservices runs six
    # more processes than a monolith -- charging it as Compose would let through
    # exactly the create this function exists to refuse.
    need += _kube_overhead_mb(req)

    reserve = host_reserve_mb(total_mb)
    headroom = available_mb - reserve
    if need <= headroom:
        # Say so while it is still comfortable, so the ceiling is visible before
        # it is hit rather than only in the refusal.
        if headroom - need < WORKSPACE_MB:
            warn(emit, f"after this one, about {max(0, headroom - need)} MB is left "
                       f"— roughly {max(0, (headroom - need)) // WORKSPACE_MB} more "
                       "workspace(s). `rc-repro list` shows what is running.",
                 phase="create")
        return

    running = [m.name for m in runner.list_meta()]
    swap_note = ("\n  This host has NO SWAP, so there is no buffer at all: memory "
                 "pressure becomes an OOM kill rather than slowdown."
                 if swap_mb == 0 else "")
    raise PreflightError(
        f"not enough memory: this workspace needs about {need} MB and only "
        f"{max(0, headroom)} MB is free to use "
        f"({available_mb} MB available, {reserve} MB kept for the OS, Docker and "
        f"the page cache, {total_mb} MB total).{swap_note}\n"
        f"  {len(running)} workspace(s) exist: {', '.join(running[:6]) or 'none'}"
        f"{' …' if len(running) > 6 else ''}\n"
        "  Free some memory first — `rc-repro stop --name <it>` keeps the data:\n"
        "    rc-repro list\n"
        "  Or override this check if you are sure:  --force")


def name_candidates(name: str, actor: str = "") -> list[str]:
    """The workspaces `name` could mean, best match first.

    Creation TRANSFORMS what you type -- `sanitize()` always, and the owner prefix
    when there are accounts -- so resolution has to apply the same transforms or
    the name never round-trips. It did not, and in two separate ways:

      up   --name TICKET-1234  ->  creates 'ticket-1234'   (sanitize)
      down --name TICKET-1234  ->  ValidationError         (validate, no sanitize)

    which broke the README's own first example on a plain single-user install,
    and then:

      up   --name test         ->  creates 'alice-test'    (owner prefix)
      down --name test         ->  NotFoundError

    once any account existed. The first is older than the team work; the prefix
    only widened it. Both are the same bug -- create and resolve disagreeing about
    what a name means -- so both are fixed by deriving the candidates the same way.

    Order matters, and is: what you typed, then what creating it would have made,
    then the bare sanitized form.

      * exact first, so a full name always wins -- `alice-test` resolves to itself
        rather than being prefixed again, and a colleague's `bob-rc8-5-1` is
        reachable by typing it;
      * the OWNER-PREFIXED form before the bare one, because that is what `up`
        would target. With both `ticket-1234` (made before accounts) and
        `alice-ticket-1234` present, `--name TICKET-1234` has to mean the same
        workspace here as it does there, or the round trip is broken again in a
        subtler way;
      * the bare form last, so pre-accounts workspaces stay reachable.
    """
    out: list[str] = []
    # Only if it is already a legal name: this one is used as a filesystem path,
    # and the sanitized forms below are safe by construction while raw input is
    # not ('../../x' must never be probed as a path).
    if _VALID_NAME_RE.match(name or "") and len(name) <= NAME_MAX:
        out.append(name)
    clean = sanitize(name or "")
    if actor and clean:
        prefixed = owner_prefix(clean, actor)
        if prefixed not in out:
            out.append(prefixed)
    if clean and clean not in out:
        out.append(clean)
    return out


def resolve_name(name: str | None, actor: str = "") -> str:
    """Explicit name (must exist) else the configured default (must exist).

    `actor` lets a name typed without the owner prefix find the workspace it
    created; without one this behaves exactly as it did before accounts existed.
    """
    if name:
        for candidate in name_candidates(name, actor):
            if runner.exists(candidate):
                return candidate
        # Nothing matched. Report the shape problem when there is one -- "invalid
        # name" is more use than "not found" for `--name 'my repro!'` -- and
        # otherwise say what was looked for, in the form it would have been made.
        _require_valid_name(sanitize(name) or name)
        raise NotFoundError(f"no repro named {name!r} (run `rc-repro list`)")
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


def owner_of(name: str) -> str:
    """Who owns `name` NOW, or "" for a pre-team workspace or one that is gone.

    `owner` wins over `created_by`, which is deliberately immutable: who made a
    workspace is a fact about the past and stays in the record, while who is
    responsible for it today is a thing that changes when a ticket is handed
    over. Before this, "belongs to alice" kept warning bob about data that had
    been his for a week -- which teaches people to click through the warning.
    """
    try:
        meta = runner.read_meta(name)
    except Exception:
        return ""
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    return extra.get("owner") or extra.get("created_by", "")


def set_owner(name: str, to: str, *, by: str = "") -> dict:
    """Hand a workspace over. Returns {name, from, to}.

    Ownership is a real verb because the workflow team-server.md §3.3 was written
    around -- support engineers handing tickets to each other -- was never
    modelled at all: the record only ever knew who typed `up`.
    """
    target = resolve_name(name)
    to = (to or "").strip().lower()
    if not to:
        raise ValidationError("no new owner given (--to <name>)")
    from rc_repro.services import users as usersvc
    if usersvc.any_users() and not usersvc.role_of(to):
        raise NotFoundError(
            f"no account named {to!r} (see `rc-repro users list`) — a workspace "
            "cannot be handed to somebody who cannot sign in")
    was = owner_of(target)

    def mutate(meta):
        extra = meta.extra if isinstance(meta.extra, dict) else {}
        history = list(extra.get("owner_history") or [])
        history.append({"from": was, "to": to, "by": by or auditsvc.actor(),
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        extra["owner"] = to
        extra["owner_history"] = history[-20:]
        meta.extra = extra

    runner.update_meta(target, mutate)
    auditsvc.record("chown", f"{target} {was or '-'} -> {to}")
    return {"name": target, "from": was, "to": to}


def _derive_for(req: "CreateReq") -> str:
    """The workspace name for this request. Used twice -- by the lock wrapper and
    by the body -- so they cannot drift apart and lock a different repro than the
    one being written."""
    if req.name:
        return sanitize(owner_prefix(sanitize(req.name), req.actor))
    return derive_name(req.version, req.preset, req.actor)


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
    # The three axes (services/topology.py). Empty/0 means "that runtime's
    # default", never "unset but meaningful" -- resolve_axes turns them into
    # canonical values, and writes the deployment back out as a preset + params so
    # the rest of create is unchanged.
    runtime: str = ""
    deployment: str = ""
    replicas: int = 0
    # Kubernetes only. The operator brings SCRAM auth and owns the MongoDB
    # bootstrap; it is opt-in because its default would replace a path proven on a
    # live cluster with one that is not. See services/k8s.py.
    mongo_operator: bool = False
    # HTTPS. Two ways in, matching the official docs' DOMAIN + LETSENCRYPT_EMAIL:
    #   --domain [+ --email]  a Let's Encrypt certificate for a public hostname
    #   --https               a certificate from rc-repro's own local CA, no domain
    https: bool = False
    domain: str = ""
    acme_email: str = ""
    # Not a flag: `rc-repro config set acme.staging true`. Let's Encrypt allows 5
    # failed validations per hostname per hour, so a way to rehearse has to exist --
    # but it is not part of the everyday two-flag path.
    acme_staging: bool = False
    # Derived by _pick_challenge, never asked for: dns-01 when provider credentials
    # exist, otherwise the inbound TLS-ALPN-01 the official compose uses.
    acme_challenge: str = "tlsalpn"
    acme_dns_provider: str = ""
    # `up --env KEY=VALUE`. Merged over the preset's env; a None value removes a key.
    env: dict = field(default_factory=dict)
    # Who asked for this, when the GUI has named accounts. Namespaces the derived
    # workspace name so two people can run the same version, and is recorded on the
    # workspace so `list` can show who owns what.
    actor: str = ""
    # Set by _resolve_tls: TLS-ALPN is validated by Let's Encrypt CONNECTING here,
    # so --domain has to publish on a public interface. Derived, not asked for --
    # an explicit --bind always wins.
    bind_public: bool = False


def _tls_from_record(name: str) -> "tuple[bool, str] | None":
    """(https, domain) an EXISTING workspace was created with, or None.

    Read from repro.json rather than remembered in the request, because the `up`
    that brings a workspace back is a different invocation -- usually a different
    day -- from the one that gave it a name.
    """
    if not name or not runner.exists(name):
        return None
    try:
        meta = runner.read_meta(name)
    except Exception:  # noqa: BLE001 - half-written record
        return None
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    mode = extra.get("tls")
    if not mode or not meta.public_url:
        return None
    from rc_repro import tls as tlsmod

    if mode == tlsmod.MODE_LOCAL:
        return (True, "")
    host = meta.public_url.split("://", 1)[-1].split("/", 1)[0]
    return (False, host) if host else None


def _resolve_tls(req: CreateReq, repro_name: str, bind_host: str, exclude: str = "",
                 emit: Emit = null_emit):
    """Turn the --https flags into a tls.TlsSpec, or None when --https is absent.

    Validates here rather than letting Traefik fail at boot: a bad combination
    otherwise produces a repro that comes up and serves nothing, with the reason
    only in `docker compose logs traefik`.
    """
    # A domain means HTTPS; requiring --https alongside it was ceremony. --https on
    # its own selects the local-CA mode, which needs nothing else.
    if not (req.https or req.domain or req.acme_email):
        # ...unless this workspace ALREADY has an https name. `down` then `up` is
        # the documented way to bring a workspace back ("data intact"), and it
        # silently dropped TLS: the record went on claiming public_url while
        # nothing served it. Harmless to re-derive, because the name and the
        # certificate both live outside the workspace -- restoring it is writing a
        # route file, where before it would have meant rebuilding a Traefik.
        restored = _tls_from_record(exclude or repro_name)
        if restored is None:
            return None
        req.https, req.domain = restored
        if not (req.https or req.domain):
            return None
        info(emit, f"restoring the https name this workspace already had"
                   f"{' (' + req.domain + ')' if req.domain else ''}", phase="tls")

    from rc_repro import tls as tlsmod

    # Normalized before anything else reads it: it becomes ROOT_URL, the Host()
    # router rule and a TLS SNI name, so a scheme or trailing slash surviving this
    # far corrupts all three at once.
    domain, fixed = (tlsmod.normalize_domain(req.domain) if req.domain else ("", ""))
    if fixed:
        req.domain = domain          # so downstream and repro.json agree with reality

    cfg = config.load_config()
    if domain:
        mode = tlsmod.MODE_ACME
        # DOMAIN + LETSENCRYPT_EMAIL, the two the official compose asks for. The
        # email is remembered so only the first run needs it.
        req.acme_email = req.acme_email or str(cfg.get("acme_email") or "")
        if not req.acme_email:
            raise ValidationError(
                "a Let's Encrypt certificate needs a contact email:\n"
                "  rc-repro up ... --domain " + domain + " --email you@example.com\n"
                "It is remembered after the first use, or set it once with:\n"
                "  rc-repro config set acme.email you@example.com")
        req.acme_staging = req.acme_staging or bool(cfg.get("acme_staging"))
        _pick_challenge(req, cfg, emit)
    else:
        mode = tlsmod.MODE_LOCAL

    # The workspace stays on loopback whatever the challenge. It publishes no TLS
    # port at all now, so there is nothing for Let's Encrypt to connect TO here --
    # the EDGE answers the TLS-ALPN challenge on 443, for every name it serves.
    # Widening a workspace's bind (these run fixed weak credentials) for a
    # challenge that no longer reaches it would expose them for nothing.
    req.bind_public = False

    host = domain or tlsmod.local_host_for(repro_name)
    # Every name answers on the edge's 443. No port is allocated per workspace and
    # no port is probed: this workspace terminates no TLS, so there is nothing to
    # conflict with. That deletes the whole port-443 arbitration -- the conflict
    # error, the `own_ports` exemption, `_pick_tls_port`, and the http-redirect
    # probe, which the edge does once for every name instead of each workspace
    # fighting over :80.
    port = 443

    return tlsmod.TlsSpec(
        mode=mode, host=host, port=port, acme_email=req.acme_email,
        acme_staging=req.acme_staging, acme_challenge=req.acme_challenge,
        acme_dns_provider=req.acme_dns_provider)


def _pick_challenge(req: CreateReq, cfg: dict, emit: Emit = null_emit) -> None:
    """Choose the ACME challenge. Not a flag -- there is a right answer.

    TLS-ALPN-01 is the default and what the official compose uses. It needs Let's
    Encrypt to reach this host on 443, which is impossible behind NAT, behind a
    tunnel, or behind a proxy that terminates TLS in front of the origin.

    dns-01 needs none of that: the provider's API writes a TXT record and Let's
    Encrypt never connects here. So it is selected exactly when credentials for it
    exist, which is the only signal that says "the operator set this up on purpose".
    """
    from rc_repro import tls as tlsmod

    if not tlsmod.dns_env_vars():
        req.acme_challenge = "tlsalpn"
        return
    provider = str(cfg.get("acme_dns_provider") or "")
    if not provider:
        provider, why = tlsmod.infer_dns_provider()
        if not provider:
            # Credentials are present but unreadable as a provider. Falling back to
            # tlsalpn silently would ignore a file the user deliberately created.
            raise ValidationError(
                f"{tlsmod.dns_env_path()} exists, so dns-01 was assumed, but the "
                f"provider could not be worked out: {why}.\n"
                f"  Name it with `rc-repro config set acme.dns_provider <name>` — "
                f"providers and their variables: {tlsmod.LEGO_PROVIDER_DOCS}\n"
                f"  Or delete the file to use the inbound challenge instead.")
    ok, detail = tlsmod.dns_credentials(provider)
    if not ok:
        raise ValidationError(detail)
    req.acme_challenge = "dns"
    req.acme_dns_provider = provider
    info(emit, f"using the dns-01 challenge via {provider} ({detail}) — no inbound "
               "connection needed, so the workspace stays on loopback", phase="tls")


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

    Serialised per repro, like every other mutating operation: `up --force` racing
    a backup or an env change makes compose reconcile against itself. The name is
    derived here rather than inside, because the lock needs it before any work
    starts -- it depends only on the request, so it is cheap to compute twice.
    """
    name = _derive_for(req)
    if not name:
        raise ValidationError(
            f"name {req.name!r} contains no usable characters (want a-z, 0-9, '-')")
    _require_valid_name(name)
    with runner.repro_lock(name):
        return _create_repro_locked(req, emit, stream_output=stream_output)


def _create_repro_locked(req: CreateReq, emit: Emit = null_emit, *,
                         stream_output: bool = False) -> dict:
    # An existing workspace's RECORDED runtime beats the flag default. Without this,
    # `rc-repro up -v 8.5.1 --name X` on a Kubernetes workspace defaulted to docker
    # and ran `docker compose up` against a workspace with no compose file:
    #
    #     'rc8-5-1' already exists - bringing it back up.
    #     no configuration file provided: not found
    #     error: `docker compose up` failed
    #
    # The flag says what to CREATE. What already exists is a fact, not a preference,
    # and `--runtime` is only consulted when there is nothing to consult instead.
    # Read BEFORE resolve_axes: it normalises an empty --runtime to "docker",
    # after which `not req.runtime` is never true and this could not fire.
    existing = _derive_for(req)
    if not req.runtime and existing and runner.exists(existing):
        req.runtime = topology.of_repro(existing)
    # Runtime x deployment x scenario, decided before anything else looks at the
    # request. The resolved axes are written BACK onto `req` as a preset name and
    # `--set` params, so every reader below -- name derivation, the preset loader,
    # capacity, repro.json -- is untouched and `--deployment multi-instance
    # --replicas 3` reaches compose.build by the exact path `--preset
    # multi-instance --set instances=3` always did.
    axes = topology.resolve_axes(runtime=req.runtime, deployment=req.deployment,
                                 replicas=req.replicas, preset=req.preset,
                                 params=req.params)
    topology.require_registered(axes.runtime)
    for hint in axes.hints:
        info(emit, hint, phase="plan")
    req.preset, req.params = axes.preset, axes.params
    req.runtime, req.deployment, req.replicas = (axes.runtime, axes.deployment,
                                                 axes.replicas)
    if req.runtime == topology.KUBERNETES:
        # A PARALLEL path, not a branch woven through this function. Everything
        # below is compose-shaped -- host ports, a compose document, `docker
        # compose up` -- and two front-ends depend on it behaving exactly as it
        # does. Handing Kubernetes off here keeps the Docker default byte-identical
        # and puts the Kubernetes sequence in the module that owns it.
        return _create_kubernetes(req, emit=emit)
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

    # Before any docker work: this pairing cannot boot on this engine, and mongod
    # says so by exiting with a message that reads like a permission fault. `doctor`
    # has known the rule for a while and could only warn, because it does not know
    # which version you are about to ask for; here the pairing is resolved, so the
    # answer is exact. Imported inside the function -- doctor imports this module.
    from rc_repro.services import doctor as doctorsvc
    conflict = doctorsvc.mongo_kernel_conflict(resolved.mongo_tag)
    if conflict:
        raise PreflightError(conflict)

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
    repro_name = _derive_for(req)
    # Before any docker work, and only for a workspace that does not exist yet:
    # `up` on an existing one reuses its containers and adds nothing.
    if not runner.exists(_derive_for(req)) and not req.force:
        check_capacity(req, req.preset, emit)
    if not repro_name:
        raise ValidationError(f"name {req.name!r} contains no usable characters (want a-z, 0-9, '-')")
    # sanitize() fixes the CHARACTERS but not the length, and the first thing below
    # is a stat on the derived path -- which raises ENAMETOOLONG rather than
    # returning False for an over-long name.
    _require_valid_name(repro_name)
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
    # ALWAYS the published local port, never an override. `--root-url` says what
    # Rocket.Chat should ADVERTISE, and it still does that through the compose spec
    # below -- but `root` is what rc-repro calls itself, and runner.Metadata's own
    # docstring makes that a contract: root_url "stays the plain
    # http://localhost:<port> that rc-repro's own API calls (login, PAT, seeding,
    # load tests) use".
    #
    # Letting the override through broke that contract, and readiness is where it
    # showed: `ready` polls meta.root_url, so a workspace whose root_url held a
    # public https name reported "Rocket.Chat did not become ready within 300s"
    # while /api/info on the local port answered 200 the whole time. Reproduced with
    # `up --root-url https://lab.example.com`: still booting at 127s, 200 locally.
    # Nothing about readiness should depend on DNS, a certificate, or the edge.
    root = f"http://localhost:{host_port}"
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
    # Stamped on every create, compose included, so "absent" keeps meaning exactly
    # one thing: a workspace older than the key. See services/topology.py.
    topology.stamp(meta.extra, req.runtime or topology.DOCKER)
    meta.extra[config.EXTRA_DEPLOYMENT] = req.deployment or topology.MONOLITH
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
    if req.actor:
        # Who owns this. Shown by `list` and the GUI card, so a shared server can
        # say whose workspace this is without guessing from the name prefix.
        meta.extra["created_by"] = req.actor
    if req.params:
        # Recorded so a repro can be rebuilt exactly: `backup` copies these into its
        # manifest, and `restore --new` would otherwise recreate an s3_minio repro
        # with the DEFAULT bucket rather than the one whose data it is loading.
        meta.extra["params"] = dict(req.params)
    files = list(pre.files)
    if tlsspec:
        from rc_repro import tls as tlsmod

        # ONE path, whether the name is a real domain or a .rcrepro.localhost one.
        # The only difference is where the certificate comes from, and that is the
        # edge's business -- so no tls/ files are written here, no port is claimed,
        # and nothing about this workspace's containers depends on it.
        local = tlsspec.mode == tlsmod.MODE_LOCAL
        if local:
            info(emit, f"issuing a local certificate for {tlsspec.host}", phase="tls")
            edgesvc.issue_local_cert(tlsspec.host)
        else:
            # Traefik writes acme.json here; it must exist and be private BEFORE
            # the container mounts it, or Traefik refuses to use it.
            tlsmod.acme_dir().mkdir(parents=True, exist_ok=True)
            tlsmod.acme_dir().chmod(0o700)

        # Started HERE, not in _resolve_tls. Lazily, by the first workspace that
        # needs a name -- not at install, not by `serve`, since whether the GUI has
        # a public hostname is an unrelated question. It has to happen where docker
        # work legitimately happens, though: putting it in _resolve_tls made a
        # resolve function start containers, and the TEST SUITE then started a real
        # edge that held :443 and broke a live run. Caught by running it.
        # The email goes WITH it: an edge started bare by an earlier `--https`
        # declares no resolver, and a --domain route naming one it does not have
        # is rejected at load, so the name 404s while looking correct outside.
        if not edgesvc.running() or (not local and not edgesvc.has_acme()):
            info(emit, "starting the edge (one Traefik, :80 and :443 for every name)",
                 phase="tls")
            if not edgesvc.ensure_running(
                    acme_email="" if local else tlsspec.acme_email,
                    acme_staging=tlsspec.acme_staging):
                holder = edgesvc.port_holder(443)
                because = f" — {holder} is holding :443" if holder else ""
                warn(emit, f"the edge did not start{because}, so {tlsspec.host} will "
                           "not answer yet. The workspace itself is unaffected; free "
                           "the port and `rc-repro edge start`.", phase="tls")

        needs_cert = edgesvc.register(repro_name, tlsspec.host,
                                      instances=pre.instances or 1, local=local)
        if local:
            pass
        elif needs_cert:
            # No reachability checks. Whether the domain resolves here, and how
            # traffic gets to this host, is the operator's business -- rc-repro
            # cannot see a tunnel, a port-forward or a firewall from in here, and
            # refusing to create the repro over a guess was the single biggest
            # source of confusion in this path.
            info(emit, f"requesting a certificate from Let's Encrypt "
                       f"({'staging' if tlsspec.acme_staging else 'production'}) "
                       f"for {tlsspec.host} — the edge does this in the background; "
                       f"confirm with `rc-repro tls-status -n {repro_name}`",
                 phase="tls")
            # Counted BEFORE the warning, so the name being created is included --
            # the useful number is "after this one", not "before it".
            tlsmod.record_issuance(tlsspec.host)
            budget = tlsmod.budget_warning(tlsspec.host)
            if budget:
                warn(emit, budget, phase="tls")
        else:
            info(emit, f"registering {tlsspec.host} — covered by the wildcard, "
                       "no certificate request", phase="tls")

        meta.extra["tls"] = tlsspec.mode
        meta.extra["edge"] = True
        # Deliberately NO tls_ports: used_ports() feeds port allocation, and
        # claiming a port this workspace does not publish would make every later
        # workspace route around one it never held.
        if not local:
            meta.extra["tls_staging"] = tlsspec.acme_staging
            meta.extra["tls_email"] = tlsspec.acme_email
        meta.extra.setdefault("notes", [])
        meta.extra["notes"] = list(meta.extra["notes"]) + [
            f"served by the edge at https://{tlsspec.host}",
            "the edge holds :443 for the whole box; this workspace publishes none",
        ]
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

    # AFTER the workspace is up, because attaching needs the network `docker
    # compose up` has only just created. Registering during the build wrote a
    # correct route to a backend the edge could not resolve, so the name answered
    # 502 -- and 502 is not an error anyone sees, it just looks like a broken
    # workspace. Idempotent: register() attaches too, for every later call.
    if tlsspec:
        edgesvc.attach(repro_name)

    result = _summary(meta)
    result["reused"] = False
    result["waited"] = wait
    if wait:
        result.update(wait_and_finalize(meta, emit))
    if req.seed:
        result["seed"] = run_seed_inline(meta, req.seed_profile, req.stats, emit)
    return result


def _restore_route(meta: "runner.Metadata", emit: Emit = null_emit) -> None:
    """Re-register a workspace's https name with the edge. Best-effort.

    Never fatal: the workspace is up and usable either way, and failing a reuse
    over the ingress would be the wrong trade.
    """
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    if not (extra.get("tls") and meta.public_url):
        return
    from rc_repro import tls as tlsmod

    host = meta.public_url.split("://", 1)[-1].split("/", 1)[0]
    local = extra.get("tls") == tlsmod.MODE_LOCAL
    try:
        if not edgesvc.running():
            edgesvc.ensure_running(acme_email="" if local else extra.get("tls_email", ""),
                                   acme_staging=bool(extra.get("tls_staging")))
        if local:
            edgesvc.issue_local_cert(host)
        instances = extra.get("instances") if isinstance(extra.get("instances"), int) else 1
        edgesvc.register(meta.name, host, instances=instances, local=local)
        info(emit, f"{host} is served by the edge again", phase="tls")
    except Exception as exc:  # noqa: BLE001 - the workspace is fine regardless
        warn(emit, f"could not restore the https name {host}: {exc}. "
                   "`rc-repro edge status` reports it.", phase="tls")


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
    # A reused workspace gets its https name back. This path never resolved TLS --
    # it reuses the record as-is -- so after a `down`, `up` printed
    # "URL https://…" from that record while the name served nothing at all: the
    # route was removed by the teardown and never rewritten. Cheap to put right
    # now that a name is a file rather than a Traefik of its own.
    _restore_route(meta, emit)

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
    if topology.of_meta(meta) == topology.KUBERNETES:
        # `rc_state` asks docker whether a CONTAINER is running, so `ready` on a
        # Kubernetes workspace answered "Rocket.Chat container is not running" about
        # a workspace whose pods were fine. It also re-establishes the port-forward,
        # which is the whole reason `ready` is the right place to ask: a forward
        # dies with its pod, and this is the command someone runs when the URL is
        # not answering.
        return _wait_serving_kubernetes(meta, emit, timeout)
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
    # Docker spells the transitional one "health: starting" inside the Status
    # string, and a bare "healthy"/"unhealthy" for the settled ones. Taken verbatim
    # the panel rendered "Health: health: starting". The prefix is noise from the
    # string format, not part of the value.
    if health.startswith("health: "):
        health = health[len("health: "):]
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
        # A Kubernetes workspace has no compose project, so asking docker whether
        # its containers exist answered `down` for a workspace that was running --
        # the state column was reporting on the wrong runtime entirely.
        if topology.of_meta(m) == topology.KUBERNETES:
            state = kubernetes_state(m.name, m)
        else:
            state = "?" if not docker_up else repro_state(
                rc_status, bool(states.get(m.project)))
        uptime, health = _uptime_health(rc_status)
        runtime = topology.of_meta(m)
        monitored = bool(isinstance(m.extra, dict) and m.extra.get("monitoring"))
        extra_ = m.extra if isinstance(m.extra, dict) else {}
        owner = extra_.get("owner") or extra_.get("created_by", "")
        out.append({"name": m.name, "created_by": owner,
                    "owner": owner, "made_by": extra_.get("created_by", ""),
                    "rc_version": m.rc_version, "mongo_tag": m.mongo_tag,
                    "host_port": m.host_port, "root_url": m.root_url, "state": state,
                    # Which runtime, so a row can say so. A Compose and a Kubernetes
                    # workspace differ in what every other command will do to them --
                    # which commands refuse, where the data lives, how to reach it --
                    # and the list was the one place both looked identical.
                    "runtime": runtime,
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
    _x = m.extra if isinstance(m.extra, dict) else {}
    d["created_by"] = _x.get("owner") or _x.get("created_by", "")
    d["owner"] = d["created_by"]
    d["made_by"] = _x.get("created_by", "")
    d["owner_history"] = list(_x.get("owner_history") or [])
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
    if topology.of_meta(m) == topology.KUBERNETES:
        # Same reason as `list_repros`. The panel's containers/env/health blocks are
        # compose-shaped and stay empty rather than being faked: an empty list is a
        # readable absence, and invented rows would be a plausible wrong answer.
        d["state"] = kubernetes_state(target, m)
        d["containers"] = []
        d["uptime"] = ""
        d["health"] = ""
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
    # Guarded here rather than in each front-end: the CLI's `stop`/`start`/`restart`
    # and the GUI's always-enabled buttons both arrive through this one function, so
    # one guard covers both and cannot drift between them.
    #
    # Kubernetes has no pause. The plan is to scale the Deployments and the
    # StatefulSet to 0, which maps onto this contract exactly -- but it is not
    # written, and reaching for `docker compose stop` on a workspace with no compose
    # project fails with "no configuration file provided", which tells the user
    # nothing about what to do.
    topology.require_compose(
        target, action,
        instead=f"Kubernetes scale-to-zero is not implemented yet; for now "
                f"`kubectl -n rc-repro-{target} scale deploy --all --replicas=0`.")
    # Looked up by a hashable key: `action` arrives from a JSON body, and a dict or
    # list reached .get() and raised "unhashable type" -- a 500 rather than the
    # "unknown action" this already knows how to say.
    if not isinstance(action, str):
        raise ValidationError(f"action must be a string (want start|stop|restart), "
                              f"got {type(action).__name__}")
    fn = {"start": runner.start, "stop": runner.stop, "restart": runner.restart}.get(action)
    if fn is None:
        raise ValidationError(f"unknown action {action!r} (want start|stop|restart)")
    # Locked like every other mutator. Stop/Restart are always-enabled GUI buttons,
    # so they were clickable straight through a create, backup or upgrade running on
    # the same repro -- and app.js's PENDING map only blocks within ONE tab, so a
    # second tab, a second user or the CLI walked right past it. Stopping mid-backup
    # is the nastiest: _Quiesced.__exit__ restarts the services afterwards, so the
    # stack silently comes back up and the "workspace is stopped" warning never fires.
    # A short timeout because this is interactive: waiting 15 minutes on a click is
    # not a better answer than "something else is working on it, try again".
    with runner.repro_lock(target, timeout=_INTERACTIVE_LOCK_WAIT):
        if fn(target) != 0:
            # A `down`ed repro has no containers, so `compose start` can never
            # revive it. Say what actually works instead of the exit code.
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


#: Who may DESTROY a workspace somebody else owns. `owner` is the default:
#: §3.3's guardrail was "confirms first, naming the owner", and a confirm dialog
#: stops being a guardrail around the twentieth time you click through it for your
#: own workspaces -- by which point the audit log tells you who did it only after
#: the data is gone. `anyone` restores the previous behaviour exactly, for a team
#: that would rather have the confirm.
#:
#: Only DESTRUCTION is gated. Visibility is not, and neither is help: starting,
#: stopping, seeding, load-testing and reading logs on a colleague's workspace all
#: stay open, because covering someone's ticket is the workflow this exists for.
DESTROY_POLICY_KEY = "gui.destroy_policy"

#: Who may set the create fields that decide what CODE runs and where it LISTENS:
#: `rc_image`, `reg_token`, `bind`.
#:
#: DEFAULT: anyone who may create at all, which is member and up. Set it to `admin`
#: to narrow it.
#:
#: The default started as `admin` and was wrong, for a reason worth keeping written
#: down. A member can already create workspaces and tear them down WITH their data.
#: Against that, "you may destroy Maria's customer repro but not choose which
#: interface your own listens on" is not a security boundary -- it is an
#: inconsistent ladder, and every rung it adds is friction for somebody doing the
#: job. A member who needs `--reg-token` to reproduce an Enterprise bug, or `--bind`
#: because the entire point of the box is that teammates reach it, is not escalating
#: anything; they are using the product.
#:
#: What each field actually costs, so narrowing it is an informed choice rather than
#: a reflex:
#:
#:   rc_image   the sharpest. An arbitrary image runs as the user running `serve`.
#:              Anyone with a shell on this box can already do it -- the CLI has no
#:              roles -- so restricting it only binds people who ONLY have the GUI.
#:   reg_token  an EE licence the caller SUPPLIES. Not a secret they extract: the env
#:              tab masks anything key-named like a credential, REG_TOKEN included.
#:   bind       publishes a workspace running fixed admin/admin123. Prefer setting
#:              `bind_host` once for the box; this is the per-workspace override.
#:
#: Narrow it where an account is not the same thing as trust:
#:     rc-repro config set gui.create_policy admin
CREATE_POLICY_KEY = "gui.create_policy"

#: The fields CREATE_POLICY_KEY governs. Named here rather than inline in web/app.py
#: so the check and the message cannot drift apart.
PRIVILEGED_CREATE_FIELDS = ("rc_image", "reg_token", "bind")


def may_set_privileged_fields(actor: str) -> bool:
    """Whether `actor` may set rc_image / reg_token / bind on a create.

    Open unless the box has explicitly narrowed it. Note the asymmetry with
    may_destroy() below, which defaults the OTHER way and should: destroying
    somebody else's workspace loses a colleague's in-progress work, and no create
    field does that.
    """
    if config.load_config().get(CREATE_POLICY_KEY) != "admin":
        return True
    if not actor:                     # no accounts on this box: nothing to bound
        return True
    from rc_repro.services import users as usersvc
    return usersvc.at_least(usersvc.role_of(actor), "admin")


def may_destroy(name: str, actor: str) -> tuple[bool, str]:
    """(allowed, why not). Advisory for a front end, enforced by teardown()."""
    if config.load_config().get(DESTROY_POLICY_KEY) == "anyone":
        return True, ""
    if not actor:                     # no accounts on this box: nothing to bound
        return True, ""
    from rc_repro.services import users as usersvc
    if usersvc.role_of(actor) == "admin":
        return True, ""
    owner = owner_of(name)
    if not owner or owner == actor:
        return True, ""
    return False, (f"{name} belongs to {owner} — ask {owner}, or an admin can "
                   f"force it. (`rc-repro chown -n {name} --to {actor}` hands it "
                   "over for good.)")


def teardown(name: str, *, volumes: bool = False, confirm: bool = False, emit: Emit = null_emit) -> dict:
    require_docker()
    target = resolve_name(name)
    if volumes and not confirm:
        raise ValidationError(f"deleting {target!r}'s data volume and record is irreversible - "
                              "pass confirm=true")
    if volumes:
        allowed, why = may_destroy(target, auditsvc.actor())
        if not allowed:
            auditsvc.record("down-volumes", target, outcome="denied")
            raise ConflictError(why)
    # Recorded BEFORE the work, not after: a teardown that dies half way through
    # has still destroyed containers, and that is exactly the event someone will
    # come looking for. Auditing here rather than at the route/command covers the
    # CLI and the GUI at once -- the single web-side call site missed both of the
    # destructive operations entirely.
    auditsvc.record("down-volumes" if volumes else "down", target)
    # Locked: `down` during a create races `docker compose up` and leaves orphaned
    # containers behind -- the exact corruption repro_lock exists to prevent -- and
    # `down --volumes` during an upgrade destroys the workspace while its
    # pre-upgrade bundle survives, so `upgrade --rollback` can never find it again.
    with runner.repro_lock(target, timeout=_INTERACTIVE_LOCK_WAIT):
        if topology.of_repro(target) == topology.KUBERNETES:
            # Deliberately BELOW the confirmation, the ownership gate and the audit
            # record: all three are about whose data is being destroyed, which does
            # not depend on the runtime, and duplicating them here is how one
            # runtime quietly loses a check the other keeps. Inside the lock for
            # the same reason compose is.
            #
            # Without this dispatch, `down` reached for a compose project that is
            # not there and answered "no configuration file provided: not found" --
            # a workspace that could be CREATED and never removed, which is worse
            # than not being able to create one.
            from rc_repro.services import k8s
            meta = runner.read_meta(target)
            context = str((meta.extra or {}).get("context") or k8s.CONTEXT)
            pid = (meta.extra or {}).get("port_forward_pid")
            if pid:
                _stop_port_forward(int(pid))
            found = k8s.delete_namespace(target, context=context, volumes=volumes,
                                         emit=emit)
            if volumes:
                shutil.rmtree(runner.workspace(target), ignore_errors=True)
                _clear_default_if(target)
            # Docker's nouns are wrong here. "containers, data volume, and
            # record" for a workspace that has a namespace and a PVC says nothing
            # about what actually went away -- and `helm uninstall` does NOT delete
            # a PVC, so the difference between these two paths is precisely the
            # thing the user needs told.
            if volumes:
                info(emit, f"{target!r} removed — namespace "
                           f"{k8s.namespace_for(target)}, its "
                           "PersistentVolumeClaim and the local record",
                     phase="done")
            else:
                info(emit, f"{target!r} down — the {k8s.RELEASE} release is "
                           f"uninstalled; namespace {k8s.namespace_for(target)} "
                           "and its PersistentVolumeClaim are KEPT, so `up` again "
                           "reuses the data. `down --volumes` deletes it.",
                     phase="done")
            return {"name": target, "removed": volumes, "found": found,
                    "runtime": topology.KUBERNETES}
        # BEFORE `down`, not after: compose cannot remove a network that still has
        # an active endpoint, and the edge attached to it is exactly that -- so
        # leaving it attached makes `down` fail with "network has active
        # endpoints" and strands the workspace half torn down.
        edgesvc.detach(target)
        if runner.down(target, volumes=volumes) != 0:
            raise DockerError(f"`docker compose down` failed for {target!r}")
        # A route left behind points the edge at a container that no longer
        # exists, so that hostname would 502 instead of 404 -- and the name could
        # never be reused by anyone else.
        edgesvc.deregister(target)
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
    me = auditsvc.actor()
    return [m.name for m in runner.list_meta()
            if not m.pinned and m.project not in states
            and may_destroy(m.name, me)[0]]


def prune(*, confirm: bool = False, emit: Emit = null_emit) -> dict:
    targets = prunable()
    if not targets:
        return {"targets": [], "removed": []}
    if not confirm:
        raise ValidationError(f"prune deletes {len(targets)} down repro(s) incl. data - pass confirm=true")
    auditsvc.record("prune", ",".join(targets))
    removed = []
    for name in targets:
        # Per repro, not around the whole loop: prune touches every `down` repro,
        # and holding them all would block unrelated work for the length of the
        # sweep. A repro that somebody grabbed in between is skipped rather than
        # waited on -- it is no longer idle, so it no longer qualifies for pruning.
        try:
            with runner.repro_lock(name, timeout=5.0):
                # Detach BEFORE `down`: compose cannot remove a network that still
                # has an active endpoint, and the attached edge is one.
                edgesvc.detach(name)
                if runner.down(name, volumes=True) != 0:
                    warn(emit, f"could not clean up {name!r} - skipping", phase="done")
                    continue
                # prune bypasses teardown(), so it drops the route itself or the
                # edge is left pointing at nothing.
                edgesvc.deregister(name)
                runner.remove(name)
        except ConflictError:
            warn(emit, f"{name!r} is in use by another operation - skipping",
                 phase="done")
            continue
        _clear_default_if(name)
        removed.append(name)
        info(emit, f"pruned {name!r}", phase="done")
    return {"targets": targets, "removed": removed}


def _create_kubernetes(req: CreateReq, emit: Emit = null_emit) -> dict:
    """The Kubernetes half of `_create_repro_locked`.

    Shares everything that is not runtime-specific -- name derivation, version
    resolution, the capacity refusal, host-port allocation, repro.json -- and
    delegates only the sequence that differs. A workspace created here is a
    workspace like any other: `list`, `info`, `logs` and `down` find it because it
    has a repro.json, which is what `runner.exists()` now looks for.
    """
    from rc_repro.services import k8s

    repro_name = _derive_for(req)
    _require_valid_name(repro_name)
    # NO refusal for an existing workspace. `down` tells the user "bring it back:
    # rc-repro up ...", and this raised "already exists, down first or --force" at
    # them -- two messages in the same tool contradicting each other about the same
    # workspace. Compose reuses; so does this. The namespace and its PVC survived a
    # plain `down`, which is exactly what makes bringing it back meaningful.
    reused = runner.exists(repro_name)
    if reused:
        info(emit, f"{repro_name!r} already exists — bringing it back up",
             phase="plan")
    check_capacity(req, req.preset, emit)

    try:
        resolved = versions.resolve(req.version, offline=req.offline)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if req.rc_image:
        resolved.rc_image = req.rc_image
    if req.mongo:
        versions.apply_mongo_override(resolved, req.mongo)

    # A workspace that comes back must come back at the SAME address. Allocating
    # afresh moved it from :3382 to :3000 on the `up` after a `down` -- so every
    # bookmark, every curl in a ticket and the URL the user had just been shown all
    # pointed at nothing. An explicit --port still wins; otherwise the recorded one
    # does, and only a genuinely new workspace allocates.
    recorded = 0
    if reused:
        try:
            recorded = int(runner.read_meta(repro_name).host_port or 0)
        except (OSError, ValueError, TypeError):
            recorded = 0
    host_port = req.port or recorded or pick_host_port(
        0, presets.load("default", {}), exclude=repro_name)
    # Same resolution order Compose uses: the flag, then the box-level config, then
    # loopback. This was dropped entirely on the Kubernetes path.
    cfg = config.load_config()
    bind_host = req.bind or cfg.get("bind_host") or config.DEFAULT_BIND_HOST
    root = f"http://localhost:{host_port}"
    microservices = req.deployment == topology.MICROSERVICES
    out = k8s.create_workspace(
        name=repro_name, resolved=resolved, host_port=host_port,
        microservices=microservices, replicas=req.replicas or 1,
        owner=req.actor, bind_host=bind_host,
        use_operator=req.mongo_operator, emit=emit)

    meta = runner.Metadata(
        name=repro_name, project=out["namespace"], rc_version=resolved.rc_version,
        rc_image=resolved.rc_image, mongo_tag=resolved.mongo_tag,
        mongo_flavor=resolved.mongo_flavor, preset=req.preset, root_url=root,
        host_port=host_port, version_source=resolved.source, pinned=req.pin,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    topology.stamp(meta.extra, topology.KUBERNETES)
    meta.extra[config.EXTRA_DEPLOYMENT] = req.deployment or topology.MICROSERVICES
    meta.extra.update({k: v for k, v in out.items() if k != "microservices"})
    if req.replicas > 1:
        meta.extra["instances"] = req.replicas
    if req.actor:
        meta.extra["created_by"] = req.actor
    # rc-repro keeps its OWN kubeconfig so creating a cluster cannot move the user's
    # current-context. The cost is that a bare `kubectl` sees nothing, which is
    # confusing rather than safe unless we say so -- so the export comes FIRST and
    # every command below it works once pasted.
    kubeconfig = k8s.owned_kubeconfig()
    ns = out["namespace"]
    pods = 9 if microservices else 5
    # The port-forward is the ONLY way in without an ingress, and it dies with its
    # pod -- so the command to re-establish it belongs here rather than in someone's
    # memory. It carries the bind host, because a workspace created with
    # `--bind 0.0.0.0` needs `--address 0.0.0.0` to come back the same way.
    addr = ("" if bind_host in ("", "127.0.0.1", "localhost")
            else f"--address {bind_host} ")
    reach = (f"reachable on this box at {root}" if not addr else
             f"reachable at {root} and, from other machines, at "
             f"http://<this-box>:{host_port} — it publishes on {bind_host} and the "
             "workspace runs admin/admin123, so keep it off untrusted networks")
    # What is IN the namespace versus what is shared by the cluster, stated rather
    # than left to be discovered. The guide installs the MongoDB operator into the
    # Rocket.Chat namespace because it assumes one Rocket.Chat per cluster; rc-repro
    # runs several, its CRDs are cluster-scoped, and a second per-namespace install
    # collides on them. So the deviation is deliberate and has to be visible here --
    # otherwise the first person to run the guide's `kubectl -n <ns> get pods` finds
    # no operator and reasonably concludes it was never installed.
    if out.get("mongo_managed_by") == "operator":
        mongo_note = (
            f"MongoDB {resolved.mongo_tag} via the official operator, with SCRAM "
            f"auth — {out.get('mongo_image', '')}")
        shared = [
            f"the operator itself is SHARED: one install in {k8s.OPERATOR_NAMESPACE} "
            f"watching every namespace, not one per workspace as the official guide "
            f"shows. Its CRDs are cluster-scoped, so a per-workspace install would "
            f"collide at the second workspace. Nothing of it lives in {ns} except "
            f"the database, its Secrets and its ServiceAccount:",
            f"    kubectl -n {k8s.OPERATOR_NAMESPACE} get pods    # the operator",
            f"    kubectl -n {ns} get mongodbcommunity              # this workspace's DB",
        ]
    else:
        mongo_note = (
            f"MongoDB {resolved.mongo_tag} as a plain StatefulSet — "
            f"{out.get('mongo_image', '')}, NO authentication. This path is "
            f"rc-repro's own: the official guide documents only the operator, which "
            f"needs MongoDB "
            f"{'.'.join(str(n) for n in k8s.OPERATOR_MIN_MONGO)}+. Add "
            f"--mongo-operator for the documented path with auth.")
        shared = []
    meta.extra["notes"] = [
        f"{'microservices' if microservices else 'monolith'} on "
        f"{out['context']} — about {pods} pods, namespace {ns}",
        mongo_note,
        *shared,
        reach,
        "the port-forward dies with its pod; bring it back with:",
        f"    kubectl -n {ns} port-forward {addr}"
        f"deployment/{out['release']}-rocketchat {host_port}:3000",
        "rc-repro keeps its own kubeconfig; a bare kubectl will not see this:",
        f"    export KUBECONFIG={kubeconfig}",
        f"    kubectl -n {ns} get pods",
        f"    kubectl -n {ns} logs -l app.kubernetes.io/name=rocketchat -f",
        f"    helm -n {ns} get values {out['release']}",
        "monitoring is shared the same way: `rc-repro monitor --name "
        f"{repro_name}` installs one Prometheus + Grafana in "
        f"{k8s.OPERATOR_NAMESPACE} for the whole cluster, and `--off` leaves it up "
        "while any other workspace still wants it.",
        "stop/start/restart, logs, stats and backup have no Kubernetes path yet — "
        "each refuses and names the kubectl command that does the job.",
    ]
    # No compose document, so `write` is given an empty one rather than a fake:
    # a file that looks like a compose project but is not would be worse than none.
    ws = runner.workspace(repro_name)
    ws.mkdir(parents=True, exist_ok=True)
    runner.atomic_write(ws / "repro.json",
                        json.dumps(asdict(meta), indent=2))
    # --wait and --seed were ignored here, so `up --seed` ran the seeder the instant
    # helm returned and failed with "can't seed - repro not ready". The CLI already
    # forces wait=True when seeding; this path simply never read it.
    result = {"name": repro_name, "meta": asdict(meta), "url": root,
              "reused": reused, "runtime": topology.KUBERNETES}
    if req.wait or req.seed:
        wait_serving(meta, emit, timeout=600.0)
        result["ready"] = True
    if req.seed:
        result["seed"] = run_seed_inline(meta, req.seed_profile, req.stats, emit)
    # `--monitor` last, and only once the workspace is serving: attaching turns on
    # RC's own Prometheus_Enabled over REST, which needs a workspace that answers.
    # Ignoring the flag here is what `--seed` did, and it produced a create that
    # silently did less than it was asked for.
    if req.monitor:
        if not (req.wait or req.seed):
            wait_serving(meta, emit, timeout=600.0)
        from rc_repro.services import monitor as monitorsvc
        # An add-on failing does not un-create the workspace. The first live run of
        # this failed on the monitoring chart and exited 7 with `montest` up,
        # serving, and listed as running -- a create reported as failed while its
        # result was sitting there working. The workspace is the deliverable; say
        # what went wrong with the extra and name the command that retries it.
        try:
            result["monitoring"] = monitorsvc.attach(repro_name, emit)
        except ReproError as exc:
            warn(emit, f"the workspace is up, but monitoring did not attach: {exc}",
                 phase="monitor")
            warn(emit, f"retry it with: rc-repro monitor --name {repro_name}",
                 phase="monitor")
            result["monitoring"] = {"monitoring": False, "error": str(exc)}
    info(emit, f"{root}  admin / {config.ADMIN_PASSWORD}", phase="done", pct=100)
    return result


def _stop_port_forward(pid: int) -> None:
    """Kill a workspace's port-forward, if it is still ours to kill.

    Best-effort and deliberately narrow: a recorded pid can have been recycled by
    the OS, so this checks the process is still a kubectl port-forward before
    signalling it. Killing an unrelated process because a pid was reused is a much
    worse failure than leaving a dead forward recorded.
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return
    if "port-forward" not in cmdline:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def kubernetes_state(name: str, meta) -> str:
    """`running` / `down` for a Kubernetes workspace.

    `repro_state()` asks docker whether the project's containers exist, so it called
    a live Kubernetes workspace `down` -- the state column was reporting on the
    wrong runtime entirely.
    """
    from rc_repro.services import k8s
    context = str((getattr(meta, "extra", None) or {}).get("context") or k8s.CONTEXT)
    if k8s.namespace_for(name) not in k8s.workspace_namespaces(context):
        return "down"
    # The namespace EXISTING is not the workspace running. A plain `down` keeps the
    # namespace and its PVC on purpose and uninstalls the release, so a torn-down
    # workspace reported "starting" -- and would have reported it forever, because
    # nothing was coming. Ask for the workload instead.
    if not k8s.workload_exists(name, context=context):
        return "down"
    return "running" if k8s.workspace_ready(name, context=context) else "starting"


def _wait_serving_kubernetes(meta: runner.Metadata, emit: Emit,
                             timeout: float) -> dict:
    """Wait for a Kubernetes workspace to serve, and make sure it is reachable.

    Two jobs, because on this runtime they are different questions. The pod being
    Ready is Kubernetes' answer; the URL answering also needs a live port-forward,
    and a forward dies with its pod. `ready` is exactly when someone asks "why is
    the URL not answering", so it re-establishes one rather than reporting a
    healthy workspace the caller still cannot reach.
    """
    import time as _time

    from rc_repro.services import k8s

    extra = meta.extra if isinstance(meta.extra, dict) else {}
    context = str(extra.get("context") or k8s.CONTEXT)
    namespace = str(extra.get("namespace") or k8s.namespace_for(meta.name))
    deadline = _time.monotonic() + timeout
    last = 0.0
    while _time.monotonic() < deadline:
        if k8s.workspace_ready(meta.name, context=context):
            pid = k8s.ensure_port_forward(
                meta.name, namespace=namespace, context=context,
                host_port=meta.host_port, pid=extra.get("port_forward_pid"),
                bind_host=str(extra.get("bind_host") or ""), emit=emit)
            if pid and pid != extra.get("port_forward_pid"):
                runner.update_meta(meta.name,
                                   lambda m: m.extra.update({"port_forward_pid": pid}))
            info(emit, f"{meta.name!r} is serving at {meta.root_url}",
                 phase="ready", pct=100)
            return {"ready": True, "url": meta.root_url}
        now = _time.monotonic()
        if now - last > 15:
            last = now
            info(emit, f"waiting for Rocket.Chat in {namespace}", phase="wait")
        _time.sleep(3.0)
    raise NotReadyError(
        f"Rocket.Chat did not become ready within {int(timeout)}s. "
        f"kubectl -n {namespace} get pods")
