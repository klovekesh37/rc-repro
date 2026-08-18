"""Upgrade a running repro to another Rocket.Chat version — CLI and web API.

An upgrade is where most "it broke after we upgraded" tickets actually live,
because Rocket.Chat runs its database migrations on boot. Reproducing that means
taking a real workspace with real data to a new version and watching what happens
— which is only meaningful if the data survives the trip.

Requires the repro to be RUNNING. Not a stylistic choice: the pre-upgrade backup
needs Mongo up to dump, and the migrations only run when Rocket.Chat boots. A
`down`ed repro has no containers at all, so there is nothing to upgrade.

The compose file is edited SURGICALLY (image tag + the oplog variable) rather than
regenerated from the spec. Regenerating would rebuild every launch-time decision
from defaults, so an --https repro would quietly lose its TLS and a bound repro
its --bind. Same approach services/envvars.py takes, for the same reason.

A pre-upgrade backup is taken automatically and is what --rollback restores. The
backup is the point: it is what makes an upgrade something you can run twice.
"""

from __future__ import annotations

import time
from pathlib import Path

from packaging.version import InvalidVersion, Version

from rc_repro import compose, rcapi, runner, versions
from rc_repro.errors import (ConflictError, DockerError, NotReadyError,
                             ReproError, ValidationError)
from rc_repro.services import backup as backupsvc
from rc_repro.services import lifecycle
from rc_repro.services.events import Emit, info, null_emit, warn

#: Where the automatic pre-upgrade bundle is recorded, so --rollback can find it
#: without the user having kept the path.
LAST_BACKUP_KEY = "last_upgrade_backup"
UPGRADE_FROM_KEY = "upgraded_from"


def _ver(value: str):
    try:
        return Version(str(value))
    except InvalidVersion:
        return None


def require_running(name: str) -> runner.Metadata:
    """The gate: an upgrade is only offered on a running workspace.

    Checked here in the service layer so the CLI and the GUI cannot disagree about
    when the action is available.
    """
    lifecycle.require_docker()
    target = lifecycle.resolve_name(name)
    # BEFORE the running check, not after. `rc_state` asks docker about containers,
    # so a healthy Kubernetes workspace was reported as "has no containers (it was
    # `down`ed)" -- advice that is wrong twice over: it was never downed, and `up`
    # would not have helped. The guard deeper in `run()` never got the chance.
    from rc_repro.services import topology
    if topology.of_repro(target) == topology.KUBERNETES:
        # Asked of Kubernetes, not Docker. `rc_state` reads `docker compose ps`, so a
        # perfectly healthy workspace answered "has no containers (it was `down`ed)"
        # -- advice wrong twice over, since it was never downed and `up` would not
        # have helped either.
        from rc_repro.services import k8s
        meta = runner.read_meta(target)
        context = str((meta.extra or {}).get("context") or k8s.CONTEXT)
        if k8s.workload_exists(target, context=context):
            return meta
        raise NotReadyError(
            f"{target!r} is not running. Upgrades run Rocket.Chat's migrations on "
            f"boot and the pre-upgrade backup needs MongoDB up: "
            f"`rc-repro start --name {target}`")
    state = runner.rc_state(target)
    if state == "running":
        return runner.read_meta(target)
    # `rc_state` reads `docker compose ps` WITHOUT --all, so a merely stopped repro
    # reports "absent" -- indistinguishable there from a `down`ed one. The two need
    # different advice (`start` vs `up`), and sending someone to `up` when `start`
    # would do is the kind of wrong hint that costs a rebuild, so ask again with a
    # call that does see stopped containers.
    if runner.container_details(target):
        raise NotReadyError(
            f"{target!r} is not running. Upgrades run Rocket.Chat's migrations on boot "
            "and the pre-upgrade backup needs MongoDB up: "
            f"`rc-repro start --name {target}`")
    raise NotReadyError(
        f"{target!r} has no containers (it was `down`ed). Upgrades run Rocket.Chat's "
        f"migrations on boot, so it has to be up first: `rc-repro up --name {target}`")


def can_upgrade(name: str) -> dict:
    """Whether the Upgrade action should be offered, and why not if it should not.

    Read-only; the GUI calls this to decide whether to render the control at all.
    """
    try:
        meta = require_running(name)
    except ReproError as exc:
        # Any expected failure -- missing repro, Docker down, stopped -- is a reason
        # not to offer the action. This is advisory and must never itself raise.
        return {"can_upgrade": False, "reason": str(exc), "current": ""}
    return {"can_upgrade": True, "reason": "", "current": meta.rc_version}


def plan(name: str, to_version: str, *, offline: bool = False) -> dict:
    """Resolve what an upgrade would do, without doing any of it."""
    meta = require_running(name)
    if not to_version:
        raise ValidationError("no target version given, e.g. `--to 8.6.1`")
    try:
        resolved = versions.resolve(to_version, offline=offline)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    cur, new = _ver(meta.rc_version), _ver(resolved.rc_version)
    blocked, direction, warnings = "", "same", []

    if resolved.rc_version == meta.rc_version:
        blocked = f"{name!r} is already at {meta.rc_version}"
    elif cur and new and new < cur:
        direction = "downgrade"
        blocked = (f"downgrading {meta.rc_version} -> {resolved.rc_version} is not "
                   "supported: Rocket.Chat does not migrate a database backwards, so "
                   "the workspace will very likely fail to boot")
    elif cur and new:
        direction = "upgrade"
    else:
        warnings.append(
            f"cannot compare {meta.rc_version!r} with {resolved.rc_version!r}; "
            "proceeding blind")

    cur_mongo, new_mongo = _ver(meta.mongo_tag), _ver(resolved.mongo_tag)
    mongo_change = ""
    if cur_mongo and new_mongo and new_mongo.major != cur_mongo.major:
        # Refused, not attempted. Mongo majors must be stepped one at a time with a
        # featureCompatibilityVersion bump between each; doing it in one move is how
        # the data is lost, and a repro tool guessing here would be indefensible.
        steps = " -> ".join(str(v) for v in range(cur_mongo.major, new_mongo.major + 1))
        mongo_change = (
            f"Rocket.Chat {resolved.rc_version} pairs with MongoDB {resolved.mongo_tag}, "
            f"but {name!r} runs MongoDB {meta.mongo_tag}. MongoDB majors have to be "
            f"stepped one at a time ({steps}) with a featureCompatibilityVersion bump "
            "at each step; rc-repro will not do that in a single move")
        blocked = blocked or mongo_change
    elif cur_mongo and new_mongo and new_mongo != cur_mongo:
        warnings.append(
            f"MongoDB minor moves {meta.mongo_tag} -> {resolved.mongo_tag}; the "
            "container keeps its data volume and is not recreated by this upgrade")

    if isinstance(meta.extra, dict) and meta.extra.get("tls"):
        warnings.append(
            "this repro terminates TLS; its certificate and Traefik configuration are "
            "left exactly as they are")

    return {
        "name": meta.name,
        "from_version": meta.rc_version,
        "to_version": resolved.rc_version,
        "rc_image": resolved.rc_image,
        "from_mongo": meta.mongo_tag,
        "to_mongo": resolved.mongo_tag,
        "mongo_blocked": mongo_change,
        "direction": direction,
        "allowed": not blocked,
        "blocked_reason": blocked,
        "warnings": warnings,
        "oplog": resolved.oplog,
        "source": resolved.source,
    }


def _apply_image(doc: dict, rc_image: str, tag: str, oplog: bool) -> int:
    """Point every Rocket.Chat service at the new image; fix the oplog variable.

    Services are read from the doc rather than reconstructed from an instance
    count, so a multi-instance repro cannot end up with some containers upgraded
    and others left behind.

    MONGO_OPLOG_URL is emitted for RC < 8 and dropped from 8.x, so an upgrade
    across that line has to add or remove it or the new container boots with a
    variable its version does not expect.
    """
    changed = 0
    services = doc.get("services") or {}
    for svc in sorted(s for s in services
                      if s == "rocketchat" or s.startswith("rocketchat-")):
        service = services[svc]
        service["image"] = f"{rc_image}:{tag}"
        env = service.setdefault("environment", {})
        if isinstance(env, list):
            env = dict((e.split("=", 1) + [""])[:2] for e in env)
            service["environment"] = env
        if oplog:
            env.setdefault("MONGO_OPLOG_URL",
                           "mongodb://mongodb:27017/local?replicaSet=rs0")
        else:
            env.pop("MONGO_OPLOG_URL", None)
        changed += 1
    return changed


def run(name: str, to_version: str, *, offline: bool = False, force: bool = False,
        no_backup: bool = False, rollback_on_failure: bool = True,
        emit: Emit = null_emit) -> dict:
    """Upgrade a running repro, backing it up first."""
    meta = require_running(name)
    target = meta.name
    with runner.repro_lock(target):
        return _run_locked(target, to_version, offline=offline, force=force,
                           no_backup=no_backup,
                           rollback_on_failure=rollback_on_failure, emit=emit)


def _run_locked(target: str, to_version: str, *, offline: bool, force: bool,
                no_backup: bool, rollback_on_failure: bool, emit: Emit) -> dict:
    p = plan(target, to_version, offline=offline)
    if not p["allowed"] and not force:
        raise ValidationError(p["blocked_reason"] + ". Pass --force to try anyway.")
    if p["mongo_blocked"] and not force:
        raise ValidationError(p["mongo_blocked"])
    for line in p["warnings"]:
        warn(emit, line, phase="upgrade")

    from rc_repro.services import topology
    kube = topology.of_repro(target) == topology.KUBERNETES

    meta = runner.read_meta(target)
    workspace = runner.workspace(target)
    # A Kubernetes workspace has no compose document to keep a copy of; its previous
    # state is the image tag, which the helm release carries.
    previous_compose = ("" if kube
                        else (workspace / "docker-compose.yml").read_text(encoding="utf-8"))
    previous = {"rc_version": meta.rc_version, "rc_image": meta.rc_image}

    bundle = ""
    if no_backup:
        warn(emit, "--no-backup: there will be nothing to roll back to if the "
                   "migrations fail", phase="upgrade")
    else:
        info(emit, "taking a pre-upgrade backup", phase="backup", pct=5)
        # _create_locked, not create(): the repro lock is already held here and it is
        # not reentrant.
        made = backupsvc._create_locked(
            target, note=f"pre-upgrade {p['from_version']} -> {p['to_version']}",
            emit=emit)
        bundle = made["path"]

    info(emit, f"upgrading {target!r}: {p['from_version']} -> {p['to_version']}",
         phase="upgrade", pct=25)
    if not kube:
        services = runner.rc_services(target)
        runner.stop_services(target, services)
        doc = runner.read_compose(target)
        if not _apply_image(doc, p["rc_image"], p["to_version"], p["oplog"]):
            raise DockerError(f"{target!r} has no rocketchat service to upgrade")

    meta.rc_version = p["to_version"]
    meta.rc_image = p["rc_image"]
    meta.extra[UPGRADE_FROM_KEY] = p["from_version"]
    if bundle:
        meta.extra[LAST_BACKUP_KEY] = bundle
    if not kube:
        # Compose has to be written BEFORE `up`, because `up` reads it. That window
        # is inherent there, and its rollback restores the previous document.
        runner.write(target, compose.to_yaml(doc), meta)

    info(emit, "pulling the new image and recreating Rocket.Chat "
               "(MongoDB and its data are untouched)", phase="boot", pct=40)
    started = time.monotonic()
    try:
        if kube:
            # APPLY FIRST, then record. The record used to move ahead of the cluster,
            # and when the helm call then failed the workspace was left running the
            # OLD version while `list`, `info` and the GUI card all reported the new
            # one -- reported from real use, after a failure that never reached helm
            # at all (`helm list` still showed revision 1). A record that has to be
            # corrected by hand is worse than an upgrade that simply did not happen.
            _apply_kubernetes(target, p, emit)
            runner.update_meta(target, lambda m: m.__dict__.update(
                rc_version=meta.rc_version, rc_image=meta.rc_image,
                extra=meta.extra))
        elif runner.up(target) != 0:
            raise DockerError("`docker compose up` failed applying the new image")
        info(emit, "waiting for Rocket.Chat to run its migrations", phase="wait", pct=60)
        lifecycle.wait_and_finalize(runner.read_meta(target), emit)
    except Exception as exc:  # noqa: BLE001 - any failure past this point is rollback-worthy
        if rollback_on_failure and bundle:
            warn(emit, f"upgrade failed ({exc}); rolling back to {p['from_version']}",
                 phase="upgrade")
            try:
                if kube:
                    _rollback_kubernetes(target, previous, bundle, emit)
                else:
                    _rollback(target, previous_compose, previous, bundle, emit)
            except Exception as rexc:  # noqa: BLE001 - the worst case, and it must be said
                # Letting this propagate would report the ROLLBACK's error and hide
                # why the upgrade failed -- while leaving the workspace in a state
                # that is neither version. Name both, and the bundle to recover from.
                raise DockerError(
                    f"upgrade to {p['to_version']} failed ({exc}) AND the rollback "
                    f"failed ({rexc}). {target!r} is now in an indeterminate state; "
                    f"its pre-upgrade backup is at {bundle} - restore it with "
                    f"`rc-repro restore {bundle} --force`.") from exc
            raise DockerError(
                f"upgrade to {p['to_version']} failed and was rolled back to "
                f"{p['from_version']}: {exc}") from exc
        raise

    elapsed = round(time.monotonic() - started, 1)
    running = (rcapi.api_info(meta.root_url) or {}).get("version", "")
    if running and not str(p["to_version"]).startswith(str(running).split("-")[0]) \
            and not str(running).startswith(str(p["to_version"])):
        warn(emit, f"Rocket.Chat reports version {running}, expected {p['to_version']}",
             phase="upgrade")
    errors = _migration_errors(target)
    for line in errors:
        warn(emit, f"migration log: {line}", phase="upgrade")

    info(emit, f"{target!r} upgraded to {p['to_version']} in {elapsed}s",
         phase="done", pct=100)
    return {"name": target, "from_version": p["from_version"],
            "to_version": p["to_version"], "running_version": running,
            "boot_seconds": elapsed, "backup": bundle,
            "migration_errors": errors, "warnings": p["warnings"]}



def _kube_target(target: str):
    from rc_repro.services import k8s
    meta = runner.read_meta(target)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    return (k8s, str(extra.get("context") or k8s.CONTEXT),
            str(extra.get("namespace") or k8s.namespace_for(target)))


def _apply_kubernetes(target: str, p: dict, emit: Emit) -> None:
    """`helm upgrade` to the new image, with the chart pinned for that version.

    The chart moves WITH the app, which the official guide's own command does not do
    -- it warns that it "installs the latest Rocket.Chat Helm chart". Here the chart
    is resolved for the target release by the same floor rule `up` uses, so an
    upgrade lands on the chart that release was matched with rather than whatever is
    newest today.
    """
    k8s, context, namespace = _kube_target(target)
    chart_version = k8s.resolve_chart_version(p["to_version"], emit)
    info(emit, f"helm upgrade to chart {chart_version} "
               f"(image {p['to_version']})", phase="boot", pct=45)
    repo, _tag = k8s.split_image(p["rc_image"], p["to_version"])
    k8s.upgrade_image(namespace=namespace, context=context,
                      chart_version=chart_version, image_repo=repo,
                      tag=p["to_version"], oplog=bool(p["oplog"]))
    # The recorded values.yaml exists so a human can read what was deployed -- and
    # after an upgrade it still said the old tag, which is precisely the kind of
    # record that answers a question wrongly. Best-effort: the upgrade itself has
    # already succeeded, and a stale note is not worth failing it over.
    try:
        _record_new_tag(target, repo, p["to_version"], chart_version)
    except Exception as exc:  # noqa: BLE001
        warn(emit, f"upgraded, but the recorded values.yaml still shows the old "
                   f"image ({exc})", phase="upgrade")


def _record_new_tag(target: str, repo: str, tag: str, chart_version: str) -> None:
    import yaml as _yaml
    path = runner.workspace(target) / "kubernetes" / "values.yaml"
    if not path.exists():
        return
    doc = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    image = doc.setdefault("image", {})
    image["repository"], image["tag"] = repo, tag
    doc["_chart_version"] = chart_version
    path.write_text(_yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _rollback_kubernetes(target: str, previous: dict, bundle: str,
                         emit: Emit) -> None:
    """Put the old image back, then restore the pre-upgrade data.

    Same order as the Compose rollback and for the same reason: the OLD Rocket.Chat
    has to be the one that opens the restored database, or it meets a schema its
    migrations have already moved past.
    """
    k8s, context, namespace = _kube_target(target)
    chart_version = k8s.resolve_chart_version(previous["rc_version"], emit)
    repo, _tag = k8s.split_image(previous["rc_image"], previous["rc_version"])
    k8s.upgrade_image(namespace=namespace, context=context,
                      chart_version=chart_version, image_repo=repo,
                      tag=previous["rc_version"], oplog=_ver(previous["rc_version"])
                      is not None and _ver(previous["rc_version"]).major < 8)
    runner.update_meta(target, lambda m: m.__dict__.update(
        rc_version=previous["rc_version"], rc_image=previous["rc_image"]))
    lifecycle.wait_and_finalize(runner.read_meta(target), emit)
    if bundle:
        backupsvc._restore_locked(
            target, Path(bundle), backupsvc.read_manifest(Path(bundle)), emit,
            allow_upgrade=False, force=True, created=False)

def _migration_errors(name: str, tail: int = 400) -> list[str]:
    """Migration-shaped errors from the boot logs.

    Best-effort and deliberately narrow: the value is naming the failure, not
    reproducing the whole log, which `rc-repro logs` already does.
    """
    try:
        text = runner.compose_logs_capture(name, tail=tail)
    except Exception:  # noqa: BLE001 - logs are a nicety, never a failure
        return []
    hits = []
    for line in text.splitlines():
        low = line.lower()
        if "migration" in low and any(k in low for k in ("error", "failed", "fatal")):
            hits.append(line.strip()[:300])
    return hits[:10]


def _rollback(target: str, previous_compose: str, previous: dict, bundle: str,
              emit: Emit) -> None:
    """Put the workspace back: old compose, old metadata, pre-upgrade data."""
    meta = runner.read_meta(target)
    meta.rc_version = previous["rc_version"]
    meta.rc_image = previous["rc_image"]
    meta.extra.pop(UPGRADE_FROM_KEY, None)
    runner.write(target, previous_compose, meta)
    runner.up(target)
    backupsvc._restore_locked(target, Path(bundle), backupsvc.read_manifest(bundle),
                              emit, allow_upgrade=False, force=True, created=False)


def rollback(name: str, *, bundle: str = "", emit: Emit = null_emit) -> dict:
    """Undo the last upgrade of this repro from its automatic pre-upgrade backup."""
    lifecycle.require_docker()
    target = lifecycle.resolve_name(name)
    meta = runner.read_meta(target)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    path = bundle or str(extra.get(LAST_BACKUP_KEY) or "")
    if not path:
        raise ValidationError(
            f"no pre-upgrade backup recorded for {target!r}; pass one explicitly "
            "with --bundle, or see `rc-repro backups`")
    if not Path(path).expanduser().exists():
        raise ConflictError(
            f"the recorded pre-upgrade backup is gone ({path}); pass another "
            "with --bundle")
    manifest = backupsvc.read_manifest(path)
    info(emit, f"rolling {target!r} back to {manifest.get('rc_version')}",
         phase="upgrade", pct=5)
    from rc_repro.services import topology
    kube = topology.of_repro(target) == topology.KUBERNETES
    resolved = versions.resolve(str(manifest.get("rc_version") or ""), offline=True)
    previous = {"rc_version": str(manifest.get("rc_version") or meta.rc_version),
                "rc_image": str(manifest.get("rc_image") or resolved.rc_image)}
    with runner.repro_lock(target):
        if kube:
            # This is the EXPLICIT `--rollback`, and it reached for a compose
            # document a Kubernetes workspace does not have -- a bare
            # FileNotFoundError naming a path, which is the same contract break the
            # `env` read had. The automatic rollback-on-failure inside _run_locked
            # was already runtime-aware; this entry point was not, and only running
            # it live showed that. It leaves the workspace on the NEW version with
            # the old data untouched, which is the worst of both.
            _rollback_kubernetes(target, previous, path, emit)
            result = {"restored": True}
        else:
            doc = runner.read_compose(target)
            _apply_image(doc, previous["rc_image"], previous["rc_version"],
                         resolved.oplog)
            meta.rc_version = previous["rc_version"]
            meta.rc_image = previous["rc_image"]
            meta.extra.pop(UPGRADE_FROM_KEY, None)
            runner.write(target, compose.to_yaml(doc), meta)
            if runner.up(target) != 0:
                raise DockerError("`docker compose up` failed rolling back the image")
            result = backupsvc._restore_locked(target, Path(path), manifest, emit,
                                               allow_upgrade=False, force=True,
                                               created=False)
    runner.update_meta(target, lambda m: m.extra.pop(UPGRADE_FROM_KEY, None))
    return {"name": target, "rolled_back_to": previous["rc_version"], "bundle": path,
            "restore": result}
