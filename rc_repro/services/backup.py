"""Back up and restore a repro's Rocket.Chat database — shared by CLI and web API.

Built on `mongodump --archive` / `mongorestore --archive`, the procedure the
official docs describe for Docker deployments, with three deliberate changes:

  --drop   the docs omit it, so a restore MERGES into the existing collections and
           leaves a hybrid of two databases that looks fine and behaves wrongly.
  --db     scopes the dump to Rocket.Chat's own database, so `admin`/`local` never
           travel and the target's replica-set configuration is left alone.
  --gzip   a seeded repro's dump is large; roughly a 10x saving for one flag.

Rocket.Chat is also STOPPED for the duration of both operations. `mongodump` is
not point-in-time consistent across collections, so a dump taken while RC writes
can capture a half-finished state; and dropping collections under a live writer
is what --drop exists to prevent. Mongo itself keeps running throughout.

A backup is a BUNDLE, not a bare dump. Restoring an 8.5 database into a 6.x
workspace fails in ways that are hard to trace, so the manifest travels with the
archive and `restore` refuses or warns before touching anything.

Not a raw volume copy: the two Mongo flavors mount different paths
(bitnami-legacy /bitnami/mongodb, official /data/db), so a tarred volume is
pinned to one flavor and one Mongo major. A BSON archive moves between both,
which is exactly what an upgrade test needs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from packaging.version import InvalidVersion, Version

from rc_repro import config, runner
from rc_repro.errors import (ConflictError, DockerError, NotFoundError,
                             NotReadyError, ReproError, ValidationError)
from rc_repro.services import lifecycle, topology
from rc_repro.services.events import Emit, info, null_emit, warn

#: Bundle layout. `schema` is checked on read: a future rc-repro that changes the
#: layout must not silently misread an old bundle.
SCHEMA = 1
SUFFIX = ".rcbak"
MANIFEST = "manifest.json"
ARCHIVE = "mongo.archive.gz"
COMPOSE = "compose.yml"
RECORD = "repro.json"
FILES_DIR = "files"

#: Rocket.Chat's database. Overridable per repro via MONGO_URL, so it is read
#: from the compose file rather than assumed.
DEFAULT_DATABASE = "rocketchat"
MONGO_SERVICE = "mongodb"

#: mongodump/mongorestore on a large seeded repro is minutes, not seconds, but it
#: must not hang a web worker forever.
DUMP_TIMEOUT = 3600.0
PROBE_TIMEOUT = 30.0


def backups_dir() -> Path:
    d = config.home() / "backups"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def database_of(name: str) -> str:
    """The Mongo database this repro's Rocket.Chat uses.

    Read from MONGO_URL rather than hardcoded: `env --set MONGO_URL=...` is
    supported, and dumping the wrong database would produce an empty backup that
    only looks fine until someone restores it.
    """
    try:
        doc = runner.read_compose(name)
    except (OSError, ValueError):
        return DEFAULT_DATABASE
    svcs = doc.get("services") or {}
    rc = svcs.get("rocketchat") or svcs.get("rocketchat-1") or {}
    env = rc.get("environment") or {}
    if isinstance(env, list):
        env = dict((e.split("=", 1) + [""])[:2] for e in env)
    url = str(env.get("MONGO_URL") or "")
    # mongodb://host:port/<db>?opts  -- take the path segment, drop the query.
    tail = url.split("://", 1)[-1]
    path = tail.split("/", 1)[1] if "/" in tail else ""
    db = path.split("?", 1)[0].strip()
    return db or DEFAULT_DATABASE


def _kube(name: str) -> str:
    """The kube context for a Kubernetes workspace, or "" if it is a Compose one.

    One question asked in one place. backup.py's LOGIC -- the bundle format, the
    manifest, the safety checks -- is runtime-agnostic and stays shared; only the
    five places that actually touch a container differ, and each consults this.
    """
    from rc_repro.services import topology
    if topology.of_repro(name) != topology.KUBERNETES:
        return ""
    meta = runner.read_meta(name)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    from rc_repro.services import k8s
    return str(extra.get("context") or k8s.CONTEXT)


def _exec_capture(name: str, argv: list[str], timeout: float | None = None):
    ctx = _kube(name)
    if ctx:
        from rc_repro.services import k8s
        return k8s.exec_capture(name, argv, context=ctx, timeout=timeout)
    return runner.compose_exec_capture(name, MONGO_SERVICE, argv, timeout=timeout)


def _exec_to_file(name: str, argv: list[str], dest, timeout: float | None = None):
    ctx = _kube(name)
    if ctx:
        from rc_repro.services import k8s
        return k8s.exec_to_file(name, argv, dest, context=ctx, timeout=timeout)
    return runner.compose_exec_to_file(name, MONGO_SERVICE, argv, dest,
                                       timeout=timeout)


def _exec_from_file(name: str, argv: list[str], src, timeout: float | None = None):
    ctx = _kube(name)
    if ctx:
        from rc_repro.services import k8s
        return k8s.exec_from_file(name, argv, src, context=ctx, timeout=timeout)
    return runner.compose_exec_from_file(name, MONGO_SERVICE, argv, src,
                                         timeout=timeout)


def _require_mongo_tools(name: str) -> None:
    """Fail early and clearly if the Mongo image has no database tools.

    Verified present in mongodb/mongodb-community-server; checked rather than
    assumed because the bitnami-legacy flavor is a different image and a missing
    binary would otherwise surface as an empty archive.
    """
    rc, _ = _exec_capture(
        name, ["sh", "-c", "command -v mongodump && command -v mongorestore"],
        timeout=PROBE_TIMEOUT)
    if rc != 0:
        raise NotReadyError(
            f"the mongodb for {name!r} has no mongodump/mongorestore, or is "
            "not running. Start the repro (`rc-repro start`) and try again.")


#: mongod's own recommended minimum. Below this a big restore risks EMFILE.
NOFILE_MIN = 64000


def warn_low_fd_limit(name: str, emit: Emit = null_emit) -> int:
    """Warn when the mongod container still has Docker's default 1024 fd limit.

    Repros created before compose.py set ulimits keep the old value until they are
    recreated. Restore works anyway (see --numParallelCollections), but this is the
    difference between a slow restore and a mongod that panics, so say it.
    """
    rc, out = _exec_capture(name, ["sh", "-c", "ulimit -n"], timeout=PROBE_TIMEOUT)
    try:
        limit = int((out or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0
    if rc == 0 and limit < NOFILE_MIN:
        warn(emit, f"this repro's mongodb can open only {limit} files "
                   f"(MongoDB asks for {NOFILE_MIN}). Restoring anyway, one collection "
                   f"at a time. Recreate it with `rc-repro up --force --name {name}` to "
                   "pick up the raised limit.", phase="restore")
    return limit


def _rc_services(name: str) -> list[str]:
    # On Kubernetes there is no compose file to read service names out of, and none
    # is needed: `_Quiesced` scales by label instead. The list is a Compose concept,
    # so it is empty there rather than faked.
    if _kube(name):
        return []
    svcs = runner.rc_services(name)
    if not svcs:
        raise DockerError(f"{name!r} has no rocketchat service in its compose file")
    return svcs


class _Quiesced:
    """Stop Rocket.Chat for the duration, and start it again no matter what.

    A failed dump that leaves the workspace stopped is a worse outcome than the
    failure itself -- the user came back to a repro that silently is not serving.
    """

    def __init__(self, name: str, services: list[str], emit: Emit, *, skip: bool = False):
        self.name, self.services, self.emit, self.skip = name, services, emit, skip

    def __enter__(self):
        if self.skip:
            warn(self.emit, "--live: Rocket.Chat keeps running, so the dump may catch a "
                            "half-written state. Prefer the default for anything you "
                            "intend to restore.", phase="backup")
            return self
        info(self.emit, "stopping Rocket.Chat so the dump is consistent "
                        "(Mongo keeps running)", phase="backup", pct=10)
        ctx = _kube(self.name)
        if ctx:
            from rc_repro.services import k8s
            # ONLY Rocket.Chat. `stop_workspace` would take MongoDB with it, and a
            # dump needs the database up and only its writers quiesced -- the same
            # distinction runner draws between `stop()` and `stop_services()`.
            k8s.scale_rocketchat(self.name, replicas=0, context=ctx)
        else:
            runner.stop_services(self.name, self.services)
        return self

    def __exit__(self, *exc):
        if self.skip:
            return False
        # The return code is checked, not discarded: a failed start left the
        # workspace stopped while the caller went on to report "✓ backed up", which
        # is precisely the outcome this class exists to prevent. Raising here would
        # mask an in-flight exception, so it warns instead -- loudly, and naming the
        # command that fixes it.
        ctx = _kube(self.name)
        if ctx:
            from rc_repro.services import k8s
            failed = k8s.scale_rocketchat(self.name, replicas=1, context=ctx) != 0
        else:
            failed = runner.start_services(self.name, self.services) != 0
        if failed:
            warn(self.emit,
                 f"Rocket.Chat did not come back up on {self.name!r} - the data is "
                 f"safe, but the workspace is stopped. Start it with "
                 f"`rc-repro start --name {self.name}`.", phase="backup")
        return False


# --- create -------------------------------------------------------------------

def create(name: str, *, out: str = "", note: str = "", live: bool = False,
           emit: Emit = null_emit) -> dict:
    """Dump a repro's database into a bundle. Returns {path, bytes, manifest}.

    The note is called `note`, not `label`: JobManager.submit() consumes a `label`
    keyword of its own, so a service function taking one can never be submitted as
    a job with its note set.
    """
    target = lifecycle.resolve_name(name)
    # Docker is required only when Docker is what runs it. Everything below this is
    # runtime-agnostic: the bundle, the manifest and the safety checks are the same
    # on both, and only the five places that touch a container differ.
    if not _kube(target):
        lifecycle.require_docker()
    with runner.repro_lock(target):
        return _create_locked(target, out=out, note=note, live=live, emit=emit)


def _create_locked(target: str, *, out: str = "", note: str = "", live: bool = False,
                   emit: Emit = null_emit) -> dict:
    meta = runner.read_meta(target)
    _require_mongo_tools(target)
    database = database_of(target)
    services = _rc_services(target)

    dest = Path(out).expanduser() if out else \
        backups_dir() / f"{target}-{_utc_stamp()}{SUFFIX}"
    if dest.is_dir():
        dest = dest / f"{target}-{_utc_stamp()}{SUFFIX}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise ConflictError(f"{dest} already exists; pass a different --out")

    workspace = runner.workspace(target)
    with tempfile.TemporaryDirectory(prefix="rcrepro-backup-") as tmp:
        staging = Path(tmp)
        archive = staging / ARCHIVE
        with _Quiesced(target, services, emit, skip=live):
            info(emit, f"dumping database {database!r}", phase="backup", pct=30)
            started = time.monotonic()
            rc, err = _exec_to_file(
                target, ["mongodump", "--archive", "--gzip", "--db", database],
                archive, timeout=DUMP_TIMEOUT)
        if rc != 0:
            raise DockerError(f"mongodump failed for {target!r}: {err.strip()[:600]}")
        if not archive.exists() or archive.stat().st_size == 0:
            raise DockerError(
                f"mongodump produced an empty archive for database {database!r} - "
                "is this repro seeded, and is MONGO_URL correct?")

        manifest = {
            "schema": SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rc_repro_version": _our_version(),
            "repro": target,
            "label": note,
            "database": database,
            "rc_version": meta.rc_version,
            "rc_image": meta.rc_image,
            "mongo_tag": meta.mongo_tag,
            "mongo_flavor": meta.mongo_flavor,
            "preset": meta.preset,
            "params": (meta.extra or {}).get("params") or {},
            # The axes, so a restore rebuilds the workspace it captured rather
            # than inferring the deployment from the preset name. Absent in
            # archives taken before these keys existed, where the inference is
            # still correct -- see topology.axes_of_meta.
            "runtime": topology.of_meta(meta),
            "deployment": (meta.extra or {}).get(config.EXTRA_DEPLOYMENT) or "",
            "env_overrides": (meta.extra or {}).get("env") or {},
            # Named volumes other than Mongo's: their CONTENT is not captured, and
            # restore warns when this is non-empty. Recording it is what makes that
            # warning possible after the fact.
            "sidecar_volumes": _sidecar_volumes(target),
            "archive_sha256": _sha256(archive),
            "archive_bytes": archive.stat().st_size,
            "dump_seconds": round(time.monotonic() - started, 1),
            "live": bool(live),
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True),
                                        encoding="utf-8")
        for src, dst in ((workspace / "docker-compose.yml", COMPOSE),
                         (workspace / "repro.json", RECORD)):
            if src.exists():
                shutil.copy2(src, staging / dst)
        _copy_preset_files(workspace, staging / FILES_DIR)

        info(emit, "packing the bundle", phase="backup", pct=85)
        _pack(staging, dest)

    os.chmod(dest, 0o600)   # the dump contains every message and credential hash
    size = dest.stat().st_size
    info(emit, f"backup written to {dest} ({_human(size)})", phase="done", pct=100)
    return {"name": target, "path": str(dest), "bytes": size, "manifest": manifest}


def _our_version() -> str:
    try:
        from importlib.metadata import version
        return version("rc-repro")
    except Exception:  # noqa: BLE001 - running from a source tree
        return ""


def _sidecar_volumes(name: str) -> list[str]:
    try:
        doc = runner.read_compose(name)
    except (OSError, ValueError):
        return []
    return sorted(v for v in (doc.get("volumes") or {}) if v != "mongodb_data")


def _copy_preset_files(workspace: Path, dest: Path) -> None:
    """Copy preset-generated workspace files (traefik/dynamic.yml, LDIF, ...).

    Deliberately excludes `import/`: those are customers' uploaded settings dumps,
    transient by design, and a backup is not where they should accumulate.
    """
    skip = {"docker-compose.yml", "repro.json", "import"}
    for item in workspace.iterdir():
        if item.name in skip or item.name.startswith("."):
            continue
        dest.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, dest / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest / item.name)


def _pack(staging: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".partial")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)
        os.replace(tmp, dest)     # atomic: a killed pack leaves no usable-looking bundle
    finally:
        tmp.unlink(missing_ok=True)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# --- read / list ----------------------------------------------------------------

def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Reject absolute paths, traversal and links.

    A bundle can arrive from a colleague, so it is untrusted input; a crafted
    member path would otherwise write outside the extraction directory.
    """
    out = []
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or m.issym() or m.islnk() or m.isdev():
            raise ValidationError(f"bundle contains an unsafe entry: {m.name!r}")
        out.append(m)
    return out


def _bundle_path(bundle: str | Path) -> Path:
    """A caller-supplied bundle path, as a Path that is safe to stat.

    The path reaches here from a CLI argument or a JSON body, so every filesystem
    call on it has to be guarded: a 300-character name raises ENAMETOOLONG and an
    unreadable file raises EPERM, and both used to escape as a 500 rather than the
    "that is not a bundle" this is.
    """
    try:
        path = Path(bundle).expanduser()
        if not path.exists():
            raise NotFoundError(f"no backup bundle at {path}")
    except NotFoundError:
        raise
    except OSError as exc:
        raise ValidationError(f"{str(bundle)[:120]!r} is not a usable path: {exc}") from exc
    return path


def read_manifest(bundle: str | Path) -> dict:
    """The manifest of a bundle, without unpacking the archive."""
    path = _bundle_path(bundle)
    try:
        with tarfile.open(path, "r:gz") as tar:
            _safe_members(tar)
            try:
                fh = tar.extractfile(MANIFEST)
            except KeyError:
                fh = None       # extractfile RAISES for a missing name, not returns None
            if fh is None:
                raise ValidationError(f"{path} has no {MANIFEST}; is it an rc-repro backup?")
            data = json.loads(fh.read().decode("utf-8"))
    except tarfile.TarError as exc:
        raise ValidationError(f"{path} is not a readable backup bundle: {exc}") from exc
    except OSError as exc:
        # Unreadable, a directory, a device node -- all "not a bundle", not a bug.
        raise ValidationError(f"{path} cannot be read as a backup bundle: {exc}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError(f"{path} has a malformed {MANIFEST}: {exc}") from exc
    schema = data.get("schema")
    if schema != SCHEMA:
        raise ValidationError(
            f"{path} uses bundle schema {schema!r}, this rc-repro reads {SCHEMA}. "
            "Use the version of rc-repro that wrote it.")
    return data


def list_backups(name: str = "") -> list[dict]:
    """Bundles in the managed directory, newest first.

    A bundle whose manifest cannot be read is still listed, marked with `error`:
    silently hiding it is how a corrupt backup gets discovered at the worst moment.
    """
    out = []
    for path in backups_dir().glob(f"*{SUFFIX}"):
        row = {"path": str(path), "bytes": path.stat().st_size,
               "mtime": path.stat().st_mtime}
        try:
            m = read_manifest(path)
            row.update(repro=m.get("repro", ""), label=m.get("label", ""),
                       rc_version=m.get("rc_version", ""), created_at=m.get("created_at", ""),
                       mongo_tag=m.get("mongo_tag", ""), preset=m.get("preset", ""),
                       error="")
        except (ValidationError, NotFoundError) as exc:
            row.update(repro="", label="", rc_version="", created_at="",
                       mongo_tag="", preset="", error=str(exc))
        if name and row.get("repro") != name:
            continue
        out.append(row)
    return sorted(out, key=lambda r: r["mtime"], reverse=True)


# --- compatibility ---------------------------------------------------------------

def _ver(value: str):
    try:
        return Version(str(value))
    except InvalidVersion:
        return None


def compatibility(manifest: dict, meta) -> dict:
    """Whether this bundle may be restored into this repro.

    Returns {allowed, requires_flag, blocked_reason, warnings, direction}. The web
    UI calls this to decide what to enable BEFORE the user commits to anything.
    """
    warnings: list[str] = []
    src, dst = str(manifest.get("rc_version", "")), str(meta.rc_version)
    sv, dv = _ver(src), _ver(dst)
    direction, requires_flag, blocked = "same", "", ""

    if not sv or not dv:
        warnings.append(f"cannot compare versions {src!r} and {dst!r}; proceeding blind")
    elif dv > sv:
        direction = "upgrade"
        requires_flag = "allow_upgrade"
        warnings.append(
            f"{src} data into a {dst} workspace: Rocket.Chat will run migrations on "
            "boot. That is an upgrade test, not a plain restore.")
    elif dv < sv:
        direction = "downgrade"
        blocked = (f"{src} data cannot be restored into an older {dst} workspace: "
                   "Rocket.Chat does not support downgrading a migrated database.")

    smt, dmt = _ver(manifest.get("mongo_tag", "")), _ver(meta.mongo_tag)
    if smt and dmt and smt.major != dmt.major:
        warnings.append(
            f"MongoDB major differs (dump from {smt}, target runs {dmt}); a restore "
            "across majors can fail on feature compatibility.")

    if manifest.get("preset") and manifest["preset"] != meta.preset:
        warnings.append(
            f"the dump came from preset {manifest['preset']!r} but this repro is "
            f"{meta.preset!r}; the data may reference services that do not exist here.")

    if manifest.get("sidecar_volumes"):
        warnings.append(
            "sidecar data is NOT in this bundle "
            f"({', '.join(manifest['sidecar_volumes'])}); uploads and IdP state will "
            "be missing even though the database references them.")

    if manifest.get("live"):
        warnings.append("this dump was taken with --live, so it may be inconsistent.")

    return {"allowed": not blocked, "blocked_reason": blocked, "direction": direction,
            "requires_flag": requires_flag, "warnings": warnings,
            "from_version": src, "to_version": dst}


# --- restore -----------------------------------------------------------------------

def restore(bundle: str | Path, *, name: str = "", new: bool = False,
            allow_upgrade: bool = False, force: bool = False,
            emit: Emit = null_emit) -> dict:
    """Restore a bundle into a repro.

    Three target modes:
      in place   restore(b)                 -- same repro the bundle came from
      new        restore(b, new=True)       -- create a fresh repro from the manifest
      other      restore(b, name="other")   -- an existing, different repro
    """
    # Docker only when Docker is what runs the TARGET. `new=True` has no target yet,
    # so it is checked below once the name is known -- and restoring into a fresh
    # workspace still creates a Compose one, because a bundle carries no runtime.
    existing = lifecycle.resolve_name(name) if name and not new else ""
    if not (existing and _kube(existing)):
        lifecycle.require_docker()
    path = Path(bundle).expanduser()
    manifest = read_manifest(path)

    if new:
        target = lifecycle.sanitize(name) if name else _derive_new_name(manifest)
        with runner.repro_lock(target):
            _create_from_manifest(target, manifest, emit)
            try:
                return _restore_locked(target, path, manifest, emit,
                                       allow_upgrade=allow_upgrade, force=force,
                                       created=True)
            except ReproError as exc:
                # The workspace was created before the load failed, so it exists and
                # is EMPTY. Left unsaid, that reads as "the command did nothing" and
                # the next --new run picks a different name, quietly accumulating
                # empty repros.
                raise type(exc)(
                    f"{exc}\n  note: {target!r} was created and is running, but empty. "
                    f"Retry with `rc-repro restore <bundle> --name {target}`, or remove "
                    f"it with `rc-repro down --name {target} --volumes --yes`.") from exc

    wanted = name or manifest.get("repro") or ""
    try:
        target = lifecycle.resolve_name(wanted)
    except NotFoundError as exc:
        # The common case for a bundle from a colleague, or after a `down --volumes`.
        # resolve_name's message says the repro is missing; it cannot know that this
        # caller has a bundle capable of recreating it.
        raise NotFoundError(
            f"{exc} - pass --new to create it from the bundle instead") from exc
    with runner.repro_lock(target):
        return _restore_locked(target, path, manifest, emit,
                               allow_upgrade=allow_upgrade, force=force, created=False)


def _drop_database(name: str, database: str, emit: Emit = null_emit) -> None:
    """Empty the target database so the restore is exact, not a merge.

    Uses the shell the repro's own Mongo image ships (mongosh from 5.0; `mongo`
    before that), so this works on both flavors.
    """
    meta = runner.read_meta(name)
    shell = "mongosh"
    try:
        major = int(str(meta.mongo_tag).split(".")[0])
        shell = "mongosh" if major >= 5 else "mongo"
    except (TypeError, ValueError):
        pass
    info(emit, f"dropping database {database!r} so the restore is exact",
         phase="restore", pct=40)
    rc, out = _exec_capture(
        name, [shell, database, "--quiet", "--eval", "db.dropDatabase()"],
        timeout=PROBE_TIMEOUT)
    if rc != 0:
        # Not fatal: --drop below still clears every collection the bundle carries,
        # which is the common case. Say so rather than pretending it was exact.
        warn(emit, f"could not drop {database!r} first ({out.strip()[:200]}); "
                   "collections absent from the bundle may survive", phase="restore")


def _derive_new_name(manifest: dict) -> str:
    base = lifecycle.sanitize(f"{manifest.get('repro') or 'restored'}-restored")
    candidate, n = base, 2
    while runner.exists(candidate):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _create_from_manifest(target: str, manifest: dict, emit: Emit) -> None:
    """Materialise a workspace matching the bundle, then leave it ready to load."""
    if runner.exists(target):
        raise ConflictError(
            f"{target!r} already exists; drop --new to restore into it, or pass "
            "--name for a different one")
    info(emit, f"creating {target!r} at Rocket.Chat {manifest.get('rc_version')} "
               f"(preset {manifest.get('preset') or 'default'})", phase="create", pct=5)
    # An archive that recorded its axes rebuilds from them; an older one falls
    # back to inferring them from the preset name, which is what that name meant.
    axes = ({"runtime": str(manifest.get("runtime") or ""),
             "deployment": str(manifest.get("deployment") or ""),
             "preset": "default"}
            if manifest.get("deployment") else
            {"preset": str(manifest.get("preset") or "default")})
    req = lifecycle.CreateReq(
        version=str(manifest.get("rc_version") or ""),
        name=target,
        **axes,
        mongo=str(manifest.get("mongo_tag") or ""),
        params=dict(manifest.get("params") or {}),
        env=dict(manifest.get("env_overrides") or {}),
        wait=True)
    lifecycle.create_repro(req, emit)


def _restore_locked(target: str, path: Path, manifest: dict, emit: Emit, *,
                    allow_upgrade: bool, force: bool, created: bool) -> dict:
    meta = runner.read_meta(target)
    compat = compatibility(manifest, meta)
    if not compat["allowed"] and not force:
        raise ValidationError(compat["blocked_reason"] + " Pass --force to try anyway.")
    if compat["requires_flag"] == "allow_upgrade" and not (allow_upgrade or force):
        raise ValidationError(
            compat["warnings"][0] + " Pass --allow-upgrade if that is what you want.")
    for line in compat["warnings"]:
        warn(emit, line, phase="restore")

    _require_mongo_tools(target)
    warn_low_fd_limit(target, emit)
    database = database_of(target)
    services = _rc_services(target)

    with tempfile.TemporaryDirectory(prefix="rcrepro-restore-") as tmp:
        staging = Path(tmp)
        info(emit, "unpacking the bundle", phase="restore", pct=20)
        with tarfile.open(path, "r:gz") as tar:
            members = _safe_members(tar)
            try:
                # Defence in depth on top of _safe_members, and the default from
                # Python 3.14 -- asking for it now keeps the behaviour identical
                # across versions instead of changing under us at an upgrade.
                tar.extractall(staging, members=members, filter="data")
            except TypeError:   # pragma: no cover - Python < 3.11.4 has no `filter`
                tar.extractall(staging, members=members)
        archive = staging / ARCHIVE
        if not archive.exists():
            raise ValidationError(f"{path} has no {ARCHIVE}; is it an rc-repro backup?")
        expected = manifest.get("archive_sha256") or ""
        if expected and _sha256(archive) != expected:
            raise ValidationError(
                f"{path} is corrupt: the archive does not match its recorded checksum")

        source_db = str(manifest.get("database") or DEFAULT_DATABASE)
        # --numParallelCollections=1: the default of 4 builds four collections'
        # indexes at once, and on a repro whose mongod still has Docker's default
        # 1024 open-file limit that hit EMFILE inside WiredTiger -- which PANICS and
        # aborts mongod rather than failing the one operation, leaving the restore
        # half-applied. New repros get ulimits.nofile=64000 from compose.py; this
        # keeps restore working on every repro created before that.
        argv = ["mongorestore", "--archive", "--gzip", "--drop",
                "--numParallelCollections=1"]
        if source_db != database:
            # Only when they genuinely differ (a repro whose MONGO_URL was
            # overridden). Passing an identity mapping on every restore would add a
            # flag combination to the common path for no benefit.
            argv += [f"--nsFrom={source_db}.*", f"--nsTo={database}.*"]
            info(emit, f"remapping database {source_db!r} -> {database!r}",
                 phase="restore")
        with _Quiesced(target, services, emit):
            # --drop is NOT enough on its own: mongorestore drops each collection it
            # is ABOUT TO restore, so a collection that exists here but is absent
            # from the bundle survives. Verified against a real workspace -- a
            # collection created after the dump was still there afterwards. That is
            # how restoring a pre-upgrade backup into an upgraded workspace would
            # leave the newer version's collections behind and produce exactly the
            # hybrid this is meant to prevent. Drop the database first for a real
            # point-in-time restore; the checksum is already verified above and
            # Rocket.Chat is stopped, so there is nothing racing this.
            _drop_database(target, database, emit)
            info(emit, f"restoring into database {database!r}", phase="restore", pct=50)
            started = time.monotonic()
            rc, out = _exec_from_file(target, argv, archive, timeout=DUMP_TIMEOUT)
            elapsed = round(time.monotonic() - started, 1)
        if rc != 0:
            raise DockerError(f"mongorestore failed for {target!r}: {out.strip()[-800:]}")

    info(emit, f"restored in {elapsed}s; waiting for Rocket.Chat", phase="restore", pct=85)
    result = lifecycle.wait_and_finalize(runner.read_meta(target), emit)
    info(emit, f"{target!r} restored from {path.name}", phase="done", pct=100)
    return {"name": target, "bundle": str(path), "created": created,
            "database": database, "restore_seconds": elapsed,
            "from_version": compat["from_version"], "to_version": compat["to_version"],
            "direction": compat["direction"], "warnings": compat["warnings"],
            "url": result.get("url") if isinstance(result, dict) else ""}


def delete(bundle: str | Path) -> dict:
    path = _bundle_path(bundle)
    # Compared UNRESOLVED on purpose. Traversal (`<backups>/../../x`) is rejected by
    # this either way, and resolving would additionally refuse a symlink sitting in
    # the backups directory -- which is a reasonable way to keep bundles on another
    # disk, and whose unlink() removes only the link, never the target.
    if path.parent != backups_dir():
        raise ValidationError(
            f"{path} is not in the managed backup directory; delete it yourself")
    try:
        path.unlink()
    except OSError as exc:
        raise ValidationError(f"could not delete {path}: {exc}") from exc
    return {"deleted": str(path)}
