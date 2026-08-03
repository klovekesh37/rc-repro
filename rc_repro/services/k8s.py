"""Kubernetes topology: the `microservices` preset's create/ready/teardown.

A parallel path to services/lifecycle.py rather than a refactor of it. lifecycle.py
is Compose-shaped throughout and two front-ends depend on it, so the Docker default
stays byte-identical and this module owns the Kubernetes lifecycle instead. Naming,
version resolution, metadata, and readiness are shared, not reimplemented.

Design notes worth knowing before changing anything here:

* **MongoDB is always external, never the chart's bundled subchart.** The chart
  ships Bitnami MongoDB, and Bitnami publishes amd64-only images, so the bundled
  path cannot work on arm64 at all. Its default tag is also wrong: chart 7.0.2
  declares appVersion 8.6.1 and defaults MongoDB to 6.0.10, which Rocket.Chat
  8.6.1 rejects outright. One external path that works everywhere beats two paths
  where one is broken on half the hosts.
* **MongoDB runs as a single-node replica set**, not a standalone mongod, because
  Rocket.Chat needs change streams.
* **MongoDB 8.0 cannot start on Linux kernel 6.19 or newer** (SERVER-121912). With
  Rocket.Chat 8.2+ requiring MongoDB 8.0, that combination is impossible rather
  than slow, so preflight refuses it instead of timing out.

All external commands go through `_Runner`, so tests drive this module without a
cluster.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from rc_repro import config, runner, versions
from rc_repro.errors import (ConflictError, CreateFailedError, DockerError,
                             NotReadyError, ValidationError)
from rc_repro.services import events
from rc_repro.services.events import Emit, info, null_emit

#: The official chart. Never vendored: the chart is the topology's source of truth.
HELM_REPO_NAME = "rocketchat"
HELM_REPO_URL = "https://rocketchat.github.io/helm-charts"
CHART = "rocketchat/rocketchat"

#: The rc-repro-owned cluster. One cluster, a namespace per repro: a control plane
#: per repro would forbid concurrent repros on laptop-scale hardware, which is
#: behaviour rc-repro already has.
CLUSTER_NAME = "rc-repro-local"

#: Ownership labels. Teardown selects by these, never by name prefix, so a
#: namespace that merely looks like rc-repro's is left alone.
OWNER_LABEL = "app.kubernetes.io/managed-by=rc-repro"
REPRO_LABEL = "rc-repro.io/repro"
CLUSTER_OWNER_CONFIGMAP = "rc-repro-cluster-owner"

#: Measured floor for the microservices topology (see the #12 findings): peak
#: working set 3.49 GiB and ~3.5 of 4 cores during convergence. CPU is the binding
#: constraint, so a memory-only floor would miss it.
FLOOR_MEMORY_GIB = 6.0
FLOOR_CPUS = 4

#: MongoDB majors that cannot start on a 6.19+ kernel.
_KERNEL_BROKEN_MONGO_MAJOR = 8
_KERNEL_FIRST_BROKEN = (6, 19)

#: Kubernetes Secret that carries REG_TOKEN into the workload. The token never
#: appears in values.yaml, repro metadata, or helm argv: only this Secret and the
#: chart's secretKeyRef reference hold the value at rest in the cluster.
REG_TOKEN_SECRET = "rc-repro-reg-token"
REG_TOKEN_SECRET_KEY = "token"


@dataclass(frozen=True)
class ClientState:
    """Kubernetes and Helm client state owned by rc-repro.

    kind owns the cluster, so its client configuration must not be redirected by
    an ambient KUBECONFIG. Helm has three XDG homes plus two repository paths;
    pinning only repositories.yaml still leaves cache/data writes in the user's
    home. All of them therefore live below RC_REPRO_HOME.
    """
    kubeconfig: Path
    helm_cache_home: Path
    helm_config_home: Path
    helm_data_home: Path
    helm_repository_config: Path
    helm_repository_cache: Path


def client_state() -> ClientState:
    root = config.home() / "clients"
    helm = root / "helm"
    return ClientState(
        kubeconfig=root / "kubernetes" / "config",
        helm_cache_home=helm / "cache",
        helm_config_home=helm / "config",
        helm_data_home=helm / "data",
        helm_repository_config=helm / "config" / "repositories.yaml",
        helm_repository_cache=helm / "cache" / "repository",
    )


def prepare_client_state() -> ClientState:
    """Create and prove the rc-repro-owned client directories are writable."""
    state = client_state()
    directories = (
        state.kubeconfig.parent,
        state.helm_cache_home,
        state.helm_config_home,
        state.helm_data_home,
        state.helm_repository_cache,
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Directory ownership alone is insufficient: Helm needs to create lock and
        # index files. A real write probe catches root-owned migrated directories.
        with tempfile.NamedTemporaryFile(dir=directory):
            pass
    for path in (state.kubeconfig, state.helm_repository_config):
        if path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            os.close(fd)
    return state


def _client_env() -> dict[str, str]:
    """Return a process environment that cannot fall back to ambient client homes."""
    state = prepare_client_state()
    env = os.environ.copy()
    env.update({
        "KUBECONFIG": str(state.kubeconfig),
        "HELM_CACHE_HOME": str(state.helm_cache_home),
        "HELM_CONFIG_HOME": str(state.helm_config_home),
        "HELM_DATA_HOME": str(state.helm_data_home),
        "HELM_REPOSITORY_CONFIG": str(state.helm_repository_config),
        "HELM_REPOSITORY_CACHE": str(state.helm_repository_cache),
    })
    return env


def _helm_flags() -> list[str]:
    state = prepare_client_state()
    return [
        "--kubeconfig", str(state.kubeconfig),
        "--repository-config", str(state.helm_repository_config),
        "--repository-cache", str(state.helm_repository_cache),
    ]


def _failure_detail(res: subprocess.CompletedProcess, limit: int = 1200) -> str:
    """Keep the terminal cause when a tool emits more than the display limit."""
    detail = "\n".join(
        part.strip() for part in (res.stdout or "", res.stderr or "") if part.strip()
    )
    if not detail:
        return f"command exited {res.returncode} without an error message"
    if len(detail) <= limit:
        return detail
    head = min(300, limit // 3)
    return f"{detail[:head]}\n... output truncated ...\n{detail[-(limit - head - 28):]}"


@dataclass
class _Runner:
    """Injectable command seam, so the whole module is testable offline."""

    def which(self, tool: str) -> str | None:
        return shutil.which(tool)

    def docker_server_platform(self) -> str | None:
        return runner.docker_server_platform()

    def run(self, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        env = _client_env() if argv and argv[0] in {"kind", "kubectl", "helm"} else None
        return subprocess.run(argv, capture_output=True, text=True, check=check,
                              env=env)

    def apply(self, ctx: str, ns: str, manifest: str) -> subprocess.CompletedProcess:
        """kubectl apply from stdin. Part of the seam so tests can capture it."""
        return subprocess.run(
            _kubectl_argv(ctx, "-n", ns, "apply", "-f", "-"),
            input=manifest, capture_output=True, text=True, check=True,
            env=_client_env())

    def sleep(self, seconds: float) -> None:
        import time
        time.sleep(seconds)

    def port_forward(self, ctx: str, ns: str, host_port: int) -> int:
        """Start kubectl port-forward detached and return its pid.

        Part of the seam so tests never spawn a real forward.
        """
        proc = subprocess.Popen(
            _kubectl_argv(ctx, "-n", ns, "port-forward", "svc/rc-rocketchat",
                          f"{host_port}:80"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=_client_env())
        return proc.pid

    def install(self, ctx: str, ns: str, values: dict,
                chart_version: str = "") -> subprocess.CompletedProcess:
        """helm install with values on stdin, so no temp file is left behind."""
        argv = ["helm", "install", "rc", CHART, "--kube-context", ctx,
                "-n", ns, "--values", "-"] + _helm_flags()
        if chart_version:
            # Pinned: an unpinned install silently changes behaviour the next time
            # the chart is released, which defeats a version-matched repro.
            argv += ["--version", chart_version]
        return subprocess.run(argv, input=yaml.safe_dump(values),
                              capture_output=True, text=True, check=False,
                              env=_client_env())


@dataclass
class Plan:
    """Everything resolved before anything is created, so a dry run is possible."""
    name: str
    namespace: str
    rc_version: str
    rc_image: str
    mongo_tag: str
    chart_version: str = ""
    values: dict = field(default_factory=dict)
    scenario_manifests: list[str] = field(default_factory=list)


def namespace_for(name: str) -> str:
    return f"rc-repro-{name}"


def require_tools(run: _Runner | None = None) -> None:
    """kind, kubectl, and helm must all be present before anything is attempted."""
    run = run or _Runner()
    missing = [t for t in ("kind", "kubectl", "helm") if not run.which(t)]
    if missing:
        raise DockerError(
            "the microservices preset needs " + ", ".join(missing) +
            " on PATH (kind provisions the cluster, helm installs the chart)")


def _kernel_version(run: _Runner) -> tuple[int, int] | None:
    """The engine VM's kernel, which is what MongoDB actually runs on.

    On macOS the host kernel is irrelevant: containers run in the Podman/Docker
    VM, so the VM's kernel is the one SERVER-121912 applies to.
    """
    try:
        res = run.run(["docker", "info", "--format", "{{.KernelVersion}}"], check=False)
    except OSError:
        return None
    m = re.match(r"(\d+)\.(\d+)", (res.stdout or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def check_mongo_kernel_support(mongo_tag: str, run: _Runner | None = None) -> None:
    """Refuse the impossible combination rather than letting it time out.

    MongoDB 8.0 hard-exits on kernel 6.19+, and Rocket.Chat 8.2+ requires MongoDB
    8.0, so on such a host that Rocket.Chat line simply cannot run. Saying so up
    front is the whole point of preflight.
    """
    run = run or _Runner()
    try:
        major = int(str(mongo_tag).split(".")[0])
    except ValueError:
        return
    if major < _KERNEL_BROKEN_MONGO_MAJOR:
        return
    kernel = _kernel_version(run)
    if kernel and kernel >= _KERNEL_FIRST_BROKEN:
        raise ValidationError(
            f"MongoDB {mongo_tag} cannot start on engine kernel "
            f"{kernel[0]}.{kernel[1]} (SERVER-121912), and this Rocket.Chat "
            f"version requires it. Use an older Rocket.Chat line (7.x pairs with "
            f"MongoDB 7.0) or an engine on a kernel below 6.19.")


#: How long to wait for the MongoDB pod, and how often to look.
_MONGO_READY_TRIES = 60
_MONGO_READY_INTERVAL = 5.0

_RS_INITIATE = ('rs.initiate({_id:"rs0",'
                'members:[{_id:0,host:"mongo-0.mongo:27017"}]})')


def init_replica_set(run: _Runner, ctx: str, ns: str, emit: Emit = null_emit) -> None:
    """Wait for MongoDB, then initiate the single-node replica set, and verify it.

    Rocket.Chat needs change streams, which need a replica set, so an uninitiated
    MongoDB produces a repro that never becomes ready. This used to swallow its own
    failures: `kubectl wait` was called the instant after `apply`, before the pod
    existed, so it failed immediately and rs.initiate then ran against nothing.
    Both errors were discarded and the repro was reported as created.

    So: poll for the pod (it does not exist yet right after apply), initiate,
    tolerate an already-initiated set, and *verify* rather than assume. A genuine
    failure raises CreateFailedError, which is exit 7: known dead, stop now, rather
    than letting the caller wait out a timeout.
    """
    for attempt in range(_MONGO_READY_TRIES):
        res = _kubectl(run, ctx, "-n", ns, "get", "pod", "mongo-0",
                       "-o", "jsonpath={.status.containerStatuses[0].ready}",
                       check=False)
        if (res.stdout or "").strip() == "true":
            break
        if attempt % 6 == 0:
            events.info(emit, "waiting for MongoDB to be ready", phase="wait")
        run.sleep(_MONGO_READY_INTERVAL)
    else:
        raise CreateFailedError(
            "MongoDB did not become ready; the repro cannot work without it "
            f"(kubectl -n {ns} describe pod mongo-0)")

    events.info(emit, "initiating the replica set", phase="boot", pct=45)
    res = _kubectl(run, ctx, "-n", ns, "exec", "mongo-0", "--", "mongosh",
                   "--quiet", "--eval", _RS_INITIATE, check=False)
    combined = f"{res.stdout or ''}{res.stderr or ''}"
    if res.returncode != 0 and "already initialized" not in combined.lower():
        raise CreateFailedError(f"could not initiate the MongoDB replica set: {combined.strip()[:400]}")

    # Verify rather than trust the exit code: this is the step whose silent failure
    # produced a repro that looked created and could never become ready.
    ok = _kubectl(run, ctx, "-n", ns, "exec", "mongo-0", "--", "mongosh",
                  "--quiet", "--eval", "rs.status().ok", check=False)
    if (ok.stdout or "").strip() != "1":
        raise CreateFailedError(
            "the MongoDB replica set is not initiated, so Rocket.Chat's change "
            "streams cannot work: " + (ok.stdout or ok.stderr or "").strip()[:300])


def _extra_env(*, reg_token_supplied: bool = False) -> list[dict]:
    """Chart extraEnv entries for the shared first-admin contract and optional token.

    When a registration token is supplied, REG_TOKEN is referenced from a
    Kubernetes Secret via valueFrom rather than inlined. The Secret is applied
    separately, so the rendered values.yaml and helm stdin never contain the
    token value.
    """
    env: list[dict] = [
        {"name": key, "value": value}
        for key, value in config.first_admin_env().items()
    ]
    if reg_token_supplied:
        env.append({
            "name": "REG_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": REG_TOKEN_SECRET,
                    "key": REG_TOKEN_SECRET_KEY,
                },
            },
        })
    return env


def _reg_token_secret_manifest(name: str, token: str) -> str:
    """Opaque Secret applied via stdin so the token never appears in process argv."""
    owner_k, owner_v = OWNER_LABEL.split("=", 1)
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": REG_TOKEN_SECRET,
            "labels": {
                owner_k: owner_v,
                REPRO_LABEL: name,
            },
        },
        "type": "Opaque",
        "stringData": {REG_TOKEN_SECRET_KEY: token},
    }
    return yaml.safe_dump(body, sort_keys=False)


def build_values(rc_version: str, *, offline: bool = False,
                 rc_image: str = "", mongo: str = "",
                 reg_token_supplied: bool = False, preset=None) -> Plan:
    """Resolve versions and render the Helm values for one repro.

    Reuses versions.resolve unchanged: it already returns everything the chart
    override needs (rc_version -> image.tag, rc_image -> image.repository,
    mongo_tag -> the external MongoDB tag).
    """
    r = versions.resolve(rc_version, offline=offline)
    tag = mongo or r.mongo_tag
    values = {
        "image": {"repository": rc_image or r.rc_image, "tag": rc_version},
        "microservices": {"enabled": True},
        # Match the Compose lifecycle contract: every repro has the advertised
        # first admin and opens at the login screen rather than Rocket.Chat's
        # setup wizard. The chart applies extraEnv to the main Rocket.Chat
        # deployment, which owns first-user creation in microservices mode.
        "extraEnv": _extra_env(reg_token_supplied=reg_token_supplied),
        # Never the bundled subchart: Bitnami is amd64-only and the chart's
        # default MongoDB tag is rejected by its own appVersion.
        "mongodb": {"enabled": False},
        "externalMongodbUrl":
            "mongodb://mongo-0.mongo:27017/rocketchat?replicaSet=rs0",
    }
    if r.oplog:
        # Rocket.Chat below 8.x still wants the oplog URL; 8.x deprecates it.
        values["externalMongodbOplogUrl"] = \
            "mongodb://mongo-0.mongo:27017/local?replicaSet=rs0"
    scenario_manifests: list[str] = []
    if preset is not None and preset.scenario:
        # A built-in scenario's RC settings use the existing Preset.env field;
        # only its concrete native resources travel through this Kubernetes path.
        values["extraEnv"].extend(
            {"name": key, "value": str(value)}
            for key, value in preset.env.items())
        scenario_manifests = list(preset.kubernetes_manifests or [])
    return Plan(name="", namespace="", rc_version=rc_version,
                rc_image=rc_image or r.rc_image, mongo_tag=tag, values=values,
                scenario_manifests=scenario_manifests)


def _version_key(v: str) -> tuple:
    """Sort key for a semver-ish string. Non-numeric parts sort low."""
    out = []
    for part in str(v).split("."):
        digits = re.match(r"(\d+)", part)
        out.append(int(digits.group(1)) if digits else -1)
    return tuple(out)


def resolve_chart_version(rc_version: str, run: _Runner | None = None,
                          emit: Emit = null_emit) -> str:
    """Pick a pinned chart version for a Rocket.Chat version.

    Most Rocket.Chat releases have no chart with a matching appVersion, so an exact
    match cannot be required. The rule is: exact appVersion match if one exists,
    otherwise the newest chart whose appVersion is at or below the requested
    version (a floor, so the chart is never newer than the app it deploys),
    otherwise the newest chart with a warning.

    A missing or unreadable index is terminal. Falling back to an unpinned chart
    would make the same command deploy different software after a chart release.
    """
    run = run or _Runner()
    res = run.run(["helm", "search", "repo", CHART, "--versions", "-o", "json"]
                  + _helm_flags(),
                  check=False)
    if res.returncode != 0:
        raise CreateFailedError(
            "could not read the Rocket.Chat Helm chart index: " +
            _failure_detail(res))
    try:
        entries = json.loads(res.stdout or "[]")
    except ValueError as exc:
        raise CreateFailedError(
            "the Rocket.Chat Helm chart index was not valid JSON: " +
            _failure_detail(res)) from exc
    if not isinstance(entries, list):
        raise CreateFailedError(
            "the Rocket.Chat Helm chart index had an unexpected JSON shape")
    charts = [(e.get("version", ""), e.get("app_version", "")) for e in entries
              if e.get("version")]
    if not charts:
        raise CreateFailedError(
            "the Rocket.Chat Helm chart index contained no chart versions; "
            "refusing an unpinned install")

    exact = [c for c, app in charts if app == rc_version]
    if exact:
        return max(exact, key=_version_key)

    want = _version_key(rc_version)
    floor = [(app, c) for c, app in charts if app and _version_key(app) <= want]
    if floor:
        # Newest *appVersion* at or below the request, and the newest chart among
        # ties. Sorting by chart version alone could pick a chart that packages an
        # older Rocket.Chat just because its own version number is higher.
        chart = max(floor, key=lambda ac: (_version_key(ac[0]), _version_key(ac[1])))[1]
        events.info(emit, f"no chart declares appVersion {rc_version}; using the "
                          f"newest at or below it ({chart})", phase="resolve")
        return chart

    chart = max((c for c, _ in charts), key=_version_key)
    events.warn(emit, f"no chart at or below appVersion {rc_version}; using the "
                      f"newest chart {chart}, which may not match", phase="resolve")
    return chart


import contextlib


@contextlib.contextmanager
def _cluster_lock():
    """A cross-process lock so only one `up` creates the shared cluster at a time.

    A file lock under RC_REPRO_HOME via flock. On a platform without flock the lock
    degrades to a no-op rather than failing: the race window returns, but a hard
    dependency on flock would be worse, and the create is idempotent-tolerant above
    regardless. The lock file is never deleted, so there is no unlink race.
    """
    from rc_repro import config
    lock_path = config.home() / ".cluster.lock"
    try:
        config.home().mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w", encoding="utf-8")
    except OSError:
        yield
        return
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass          # no flock (e.g. Windows): degrade to no-op
        yield
    finally:
        fh.close()


def cluster_exists(run: _Runner | None = None) -> bool:
    run = run or _Runner()
    res = run.run(["kind", "get", "clusters"], check=False)
    return CLUSTER_NAME in (res.stdout or "").split()


def _export_kubeconfig(run: _Runner) -> str:
    """Refresh the owned kubeconfig for both new and pre-existing clusters."""
    state = prepare_client_state()
    res = run.run(["kind", "export", "kubeconfig", "--name", CLUSTER_NAME,
                   "--kubeconfig", str(state.kubeconfig)], check=False)
    if res.returncode != 0:
        raise CreateFailedError(
            f"could not export kubeconfig for cluster {CLUSTER_NAME}: " +
            _failure_detail(res))
    # kind's context is stable today. Read back the owned file when possible so a
    # future naming change is detected without consulting ambient kubectl state.
    current = run.run(_kubectl_argv("", "config", "current-context"), check=False)
    ctx = (current.stdout or "").strip()
    if current.returncode != 0 or not ctx:
        raise CreateFailedError(
            f"the exported kubeconfig for cluster {CLUSTER_NAME} is not usable: " +
            _failure_detail(current))
    return ctx


def _wait_cluster_ready(run: _Runner, ctx: str) -> subprocess.CompletedProcess:
    """Prove the API and at least one node are usable before reuse."""
    return _kubectl(run, ctx, "wait", "--for=condition=Ready", "node", "--all",
                    "--timeout=60s", check=False)


def _cluster_owner_manifest() -> str:
    owner_key, owner_value = OWNER_LABEL.split("=", 1)
    return yaml.safe_dump({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CLUSTER_OWNER_CONFIGMAP,
            "namespace": "kube-system",
            "labels": {owner_key: owner_value},
        },
        "data": {"cluster": CLUSTER_NAME},
    })


def _mark_cluster_owned(ctx: str, run: _Runner) -> None:
    """Create the in-cluster proof that permits later reuse and deletion."""
    try:
        result = run.apply(ctx, "kube-system", _cluster_owner_manifest())
    except (OSError, subprocess.SubprocessError) as exc:
        raise CreateFailedError(
            f"cluster {CLUSTER_NAME} was created but its ownership marker could "
            f"not be recorded: {exc}") from exc
    if result is not None and result.returncode != 0:
        raise CreateFailedError(
            f"cluster {CLUSTER_NAME} was created but its ownership marker could "
            f"not be recorded: {_failure_detail(result)}")


def _rollback_unmarked_cluster(run: _Runner) -> str:
    """Remove a just-created cluster that failed before ownership was provable."""
    try:
        result = run.run(
            ["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)
    except OSError as exc:
        return f"; rollback also failed: {exc}"
    if result.returncode != 0:
        return f"; rollback also failed: {_failure_detail(result)}"
    return "; the unmarked cluster was rolled back"


def ensure_cluster(emit: Emit = null_emit, run: _Runner | None = None) -> str:
    """Create the rc-repro-owned cluster if it isn't there, and return its context.

    The context name is read from kubectl rather than assumed from kind's naming
    convention, and then passed explicitly on every call. That is the enforcement
    point for never selecting the ambient kubectl context implicitly.
    """
    run = run or _Runner()
    create_failure = ""
    created_here = False
    # Serialise creation across concurrent `up`s. Without this, two simultaneous
    # creates both see no cluster and both run `kind create cluster`, and the second
    # fails ("node(s) already exist"). The lock makes the check-then-create atomic,
    # and the re-check inside it means the loser of the race reuses rather than
    # retries. This is the concurrency the one-cluster-many-namespaces design exists
    # to support, so it has to actually hold.
    with _cluster_lock():
        if cluster_exists(run):
            events.info(emit, f"reusing cluster {CLUSTER_NAME}", phase="provision")
        else:
            events.info(emit, f"creating cluster {CLUSTER_NAME}", phase="provision", pct=10)
            # Tolerate a create that lost a race to a process not holding this lock
            # (e.g. a manual `kind create`): "already exist" is success, not failure.
            state = prepare_client_state()
            res = run.run(["kind", "create", "cluster", "--name", CLUSTER_NAME,
                           "--kubeconfig", str(state.kubeconfig)], check=False)
            if res.returncode == 0:
                created_here = True
            else:
                combined = f"{res.stdout or ''}\n{res.stderr or ''}".lower()
                if "already exist" not in combined:
                    create_failure = _failure_detail(res)
                    # kind can create the node and then fail while exporting its
                    # host-side kubeconfig. Preserve the cluster and reconcile it
                    # rather than returning a false terminal failure immediately.
                    if not cluster_exists(run):
                        raise CreateFailedError(
                            f"could not create the cluster {CLUSTER_NAME}: "
                            f"{create_failure}")
                    created_here = True
    try:
        ctx = _export_kubeconfig(run)
    except CreateFailedError as exc:
        rollback = _rollback_unmarked_cluster(run) if created_here else ""
        if create_failure:
            raise CreateFailedError(
                f"kind create failed for {CLUSTER_NAME}: {create_failure}; "
                f"owned-kubeconfig recovery also failed: {exc}{rollback}") from exc
        raise CreateFailedError(f"{exc}{rollback}") from exc

    if created_here:
        try:
            _mark_cluster_owned(ctx, run)
        except CreateFailedError as exc:
            raise CreateFailedError(
                f"{exc}{_rollback_unmarked_cluster(run)}") from exc
    elif not cluster_is_ours(ctx, run):
        raise ConflictError(
            f"a Kind cluster named {CLUSTER_NAME!r} exists without rc-repro's "
            "ownership marker; refusing to reuse or delete it. Rename or remove "
            "that cluster yourself, then retry.")

    ready = _wait_cluster_ready(run, ctx)
    if ready.returncode != 0:
        prefix = (f"kind create failed for {CLUSTER_NAME}: {create_failure}; "
                  if create_failure else
                  f"existing cluster {CLUSTER_NAME} is not usable; ")
        raise CreateFailedError(
            prefix + "API/node readiness reconciliation failed: " +
            _failure_detail(ready))
    if create_failure:
        events.warn(
            emit,
            "kind returned an error, but the owned kubeconfig was recovered and "
            f"cluster {CLUSTER_NAME} reached Ready; continuing",
            phase="provision")
    return ctx


def _kubectl_argv(ctx: str, *args: str) -> list[str]:
    argv = ["kubectl", "--kubeconfig", str(prepare_client_state().kubeconfig)]
    if ctx:
        argv += ["--context", ctx]
    return argv + list(args)


def _kubectl(run: _Runner, ctx: str, *args: str, check: bool = True):
    return run.run(_kubectl_argv(ctx, *args), check=check)


def setup_helm_repository(run: _Runner) -> None:
    """Idempotently configure and refresh the official chart repository."""
    commands = (
        (["helm", "repo", "add", HELM_REPO_NAME, HELM_REPO_URL,
          "--force-update"] + _helm_flags(), "configure"),
        (["helm", "repo", "update", HELM_REPO_NAME] + _helm_flags(), "update"),
    )
    for argv, action in commands:
        res = run.run(argv, check=False)
        if res.returncode != 0:
            raise CreateFailedError(
                f"could not {action} the Rocket.Chat Helm repository: " +
                _failure_detail(res))


def owns_namespace(ns: str, ctx: str, run: _Runner | None = None) -> bool:
    """Whether rc-repro created this namespace, asserted from its label.

    Name-prefix matching is not ownership: a namespace called rc-repro-foo without
    the label belongs to somebody else and must never be deleted.
    """
    run = run or _Runner()
    res = _kubectl(run, ctx, "get", "namespace", ns, "-o",
                   "jsonpath={.metadata.labels}", check=False)
    if res.returncode != 0:
        return False
    try:
        labels = json.loads(res.stdout or "{}")
    except ValueError:
        return False
    return labels.get("app.kubernetes.io/managed-by") == "rc-repro"


#: MongoDB as a single-node replica set. Not a standalone mongod: Rocket.Chat needs
#: change streams. The official image is used rather than Bitnami's because it is
#: the only one published for arm64.
_MONGO_MANIFEST = """\
apiVersion: v1
kind: Service
metadata:
  name: mongo
  labels: {{{owner}, {repro}: {name}}}
spec:
  clusterIP: None
  selector: {{app: mongo}}
  ports: [{{port: 27017, name: mongo}}]
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: mongo
  labels: {{{owner}, {repro}: {name}}}
spec:
  serviceName: mongo
  replicas: 1
  selector: {{matchLabels: {{app: mongo}}}}
  template:
    metadata:
      labels: {{app: mongo, {owner}, {repro}: {name}}}
    spec:
      containers:
      - name: mongod
        image: mongo:{tag}
        args: ["--replSet", "rs0", "--bind_ip_all"]
        ports: [{{containerPort: 27017}}]
"""


def _mongo_manifest(name: str, tag: str) -> str:
    # The labels are inline YAML flow maps, so they need the key: value form the
    # label constants already carry as "k=v".
    owner_k, owner_v = OWNER_LABEL.split("=", 1)
    return _MONGO_MANIFEST.format(
        owner=f"{owner_k}: {owner_v}", repro=REPRO_LABEL, name=name, tag=tag)


def _render_scenario_manifest(manifest: str, name: str) -> str:
    """Bind a built-in scenario resource to this namespace-local repro."""
    return manifest.replace("__RC_REPRO_NAME__", name)


def create_repro(name: str, rc_version: str, *, offline: bool = False,
                 rc_image: str = "", mongo: str = "", port: int = 0,
                 reg_token: str = "", preset=None,
                 emit: Emit = null_emit, run: _Runner | None = None) -> dict:
    """Create a Kubernetes microservices repro. Returns the result payload.

    ``reg_token`` is delivered through a Kubernetes Secret and a chart
    ``valueFrom`` reference. It must never land in values.yaml, repro.json, or
    helm/kubectl argv. ``preset`` is the resolved internal aggregate; omitting it
    retains the legacy ``microservices`` behaviour.
    """
    run = run or _Runner()
    if preset is not None and preset.topology != "kubernetes":
        raise ValidationError(
            f"preset {preset.name!r} cannot run on the Kubernetes topology")
    require_tools(run)
    try:
        prepare_client_state()
    except OSError as exc:
        raise DockerError(
            "rc-repro's Kubernetes and Helm client state is not writable under "
            f"{config.home()}: {exc}") from exc

    # A repeat over an existing repro would fail deep inside helm ("cannot re-use a
    # name that is still in use") with a raw error. Refuse early and clearly instead,
    # naming how to proceed. The Kubernetes path has no --force (see #15), so recreate
    # is an explicit down-then-up, not a silent clobber.
    if runner.exists(name):
        raise ConflictError(
            f"a repro named {name!r} already exists. The Kubernetes topology does not "
            f"recreate in place; run `rc-repro down --name {name} --volumes` first, or "
            f"choose another --name.")

    token = (reg_token or "").strip()
    token_supplied = bool(token)

    events.info(emit, "checking engine capacity", phase="preflight", pct=2)
    check_capacity(run, emit)

    events.info(emit, "resolving versions and chart", phase="resolve", pct=5)
    plan = build_values(rc_version, offline=offline, rc_image=rc_image, mongo=mongo,
                        reg_token_supplied=token_supplied, preset=preset)
    plan.name, plan.namespace = name, namespace_for(name)
    # Fail on the impossible combination now rather than after a long wait.
    check_mongo_kernel_support(plan.mongo_tag, run)

    setup_helm_repository(run)
    plan.chart_version = resolve_chart_version(plan.rc_version, run, emit)
    if plan.chart_version:
        events.info(emit, f"chart {plan.chart_version} for Rocket.Chat "
                          f"{plan.rc_version}", phase="resolve", pct=8)

    ctx = ensure_cluster(emit, run)

    events.info(emit, f"creating namespace {plan.namespace}", phase="provision", pct=20)
    _kubectl(run, ctx, "create", "namespace", plan.namespace, check=False)
    # Ownership is asserted at creation, so teardown can prove what it may delete.
    _kubectl(run, ctx, "label", "namespace", plan.namespace,
             OWNER_LABEL, f"{REPRO_LABEL}={name}", "--overwrite")

    for manifest in plan.scenario_manifests:
        events.info(emit, "applying scenario services", phase="provision")
        run.apply(ctx, plan.namespace, _render_scenario_manifest(manifest, name))

    if token_supplied:
        # Apply via stdin (run.apply), never as argv, so process listings cannot
        # observe the token. The helm values only reference the Secret by name.
        events.info(emit, "installing the registration token Secret",
                    phase="boot", pct=25)
        run.apply(ctx, plan.namespace, _reg_token_secret_manifest(name, token))

    events.info(emit, f"starting MongoDB {plan.mongo_tag}", phase="boot", pct=30)
    run.apply(ctx, plan.namespace, _mongo_manifest(name, plan.mongo_tag))
    events.info(emit, "waiting for MongoDB", phase="wait", pct=40)
    init_replica_set(run, ctx, plan.namespace, emit)

    events.info(emit, "installing the Rocket.Chat chart", phase="boot", pct=55)
    installed = run.install(ctx, plan.namespace, plan.values, plan.chart_version)
    if installed.returncode != 0:
        raise CreateFailedError(
            "could not install the Rocket.Chat Helm chart: " +
            _failure_detail(installed))

    events.info(emit, "chart installed", phase="wait", pct=70)

    # Reachability, and the metadata that makes this repro visible to list/info/
    # resolve_name exactly like a Compose one. Metadata is shared deliberately: a
    # second record format would be a second thing to keep in sync.
    host_port = port or runner.pick_port()
    pid = start_port_forward(ctx, plan.namespace, host_port, run)
    resolved = versions.resolve(plan.rc_version, offline=offline)
    extra = {_TOPOLOGY: "kubernetes", _NAMESPACE: plan.namespace,
             _CONTEXT: ctx, _FORWARD_PID: pid,
             "chart_version": plan.chart_version}
    # Additive selector provenance. Existing consumers still read
    # ``preset=\"microservices\"``; agents that used the new public selectors can
    # inspect the deployment/scenario choice without parsing Helm values.
    extra["deployment"] = "microservices"
    if preset is not None and preset.scenario:
        extra["scenarios"] = [preset.scenario]
    # Boolean only: evidence and info report whether a token was consumed, never
    # the value. Absent means not supplied, so consumers do not invent True.
    if token_supplied:
        extra["reg_token_supplied"] = True
    meta = runner.Metadata(
        name=name, project=plan.namespace, rc_version=plan.rc_version,
        rc_image=plan.rc_image, mongo_tag=plan.mongo_tag,
        mongo_flavor=resolved.mongo_flavor, preset="microservices",
        root_url=f"http://localhost:{host_port}", host_port=host_port,
        version_source=resolved.source,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        extra=extra,
    )
    # The workspace holds the rendered artifact, values.yaml here instead of
    # docker-compose.yml, so evidence hashes the same kind of thing either way.
    # plan.values holds only a secretKeyRef when a token was supplied: no token
    # value is written to disk.
    runner.write(name, yaml.safe_dump(plan.values, sort_keys=False), meta,
                 artifact_name="values.yaml")
    events.info(emit, f"forwarding localhost:{host_port}", phase="wait", pct=80)

    # Report what the forward is actually doing, not what was intended. Started
    # this early it often dies immediately, because the chart's Service has no
    # ready endpoints yet and kubectl port-forward exits when it cannot bind to
    # one. That is exactly why the forward is reconcilable state: `ready`, `info`,
    # and anything else needing HTTP call ensure_port_forward first and revive it.
    # Claiming "up" here would be the kind of confident-but-wrong status that
    # sends someone debugging their network instead of waiting for a pod.
    from rc_repro.services import access
    access_info = access.handoff(host_port, meta.root_url)
    return {"name": name, "namespace": plan.namespace, "context": ctx,
            "topology": "kubernetes", "rc_version": plan.rc_version,
            "mongo_tag": plan.mongo_tag, "chart": CHART,
            "chart_version": plan.chart_version,
            "root_url": meta.root_url, "host_port": host_port,
            "port_forward": forward_state(meta),
            "access": access_info,
            "reg_token_supplied": token_supplied}


def teardown(name: str, *, volumes: bool = False, emit: Emit = null_emit,
             run: _Runner | None = None) -> dict:
    """Remove a repro's namespace, reporting anything left behind.

    `residual` is the point: a partial teardown must not claim success. A tool that
    says "removed" while a volume survives is how a retained repro goes unnoticed
    for days.
    """
    run = run or _Runner()
    require_tools(run)
    ns = namespace_for(name)
    ctx = f"kind-{CLUSTER_NAME}"
    removed: list[str] = []
    residual: list[str] = []

    if not owns_namespace(ns, ctx, run):
        # Either it never existed (already gone, which is fine and idempotent) or
        # it is not ours, which is never ours to delete.
        return {"name": name, "removed": [], "residual": [], "volumes_removed": False}

    try:
        meta = runner.read_meta(name)
    except Exception:  # noqa: BLE001 - record may already be gone
        meta = None
    if meta is not None and stop_port_forward(meta):
        events.info(emit, "stopped the port-forward", phase="teardown", pct=20)

    events.info(emit, f"deleting namespace {ns}", phase="teardown", pct=50)
    res = _kubectl(run, ctx, "delete", "namespace", ns, "--wait=true", check=False)
    if res.returncode == 0:
        removed.append(f"namespace/{ns}")
    else:
        residual.append(f"namespace/{ns}")

    if volumes:
        pv = _kubectl(run, ctx, "get", "pv", "-o",
                      "jsonpath={range .items[*]}{.metadata.name} ", check=False)
        for vol in (pv.stdout or "").split():
            if name in vol:
                residual.append(f"pv/{vol}")

    if volumes:
        # --volumes means forget the repro entirely, matching the Compose path.
        runner.remove(name)          # rmtree, ignore_errors: idempotent by design
        removed.append(f"record/{name}")

    return {"name": name, "removed": removed, "residual": residual,
            "volumes_removed": bool(volumes)}


# --- reachability: the port-forward is reconcilable state ----------------------
#
# A port-forward is a child process that dies with the CLI. Rather than pretend
# otherwise, it is treated as state that any operation may re-establish: `up`
# starts it and records the pid, anything needing HTTP probes it first and revives
# it if dead, and `down` kills it. Without that, every verb inherits a flaky
# precondition.
#
# Why a forward at all: kind fixes extraPortMappings at cluster creation, but one
# warm cluster outlives many repros, so a NodePort block would cap concurrency at
# whatever was reserved up front. A forward is the only per-repro option, and it
# keeps host_port and root_url meaning exactly what they mean on the Docker path.

_FORWARD_PID = "k8s_forward_pid"
_NAMESPACE = "k8s_namespace"
_CONTEXT = "k8s_context"
_TOPOLOGY = "topology"


def _pid_is_our_forward(pid: int) -> bool:
    """Whether `pid` is alive AND is a kubectl port-forward we could have started.

    os.kill(pid, 0) alone is not enough: pids are recycled, so a pid recorded in
    repro.json can, after a reboot or enough churn, belong to an unrelated process.
    Trusting it would report a stranger as our forward; killing it (stop_port_forward)
    would SIGTERM that stranger, which is a far worse failure than a leaked forward.

    So the liveness check is paired with an identity check: read the process command
    line and require it to be a `kubectl ... port-forward`. Where the command line
    cannot be read (no /proc, e.g. macOS), fall back to `ps`, and if neither is
    available treat the pid as NOT ours, because the safe default when identity is
    unknowable is to never signal it.
    """
    try:
        os.kill(pid, 0)          # liveness; raises if the pid is gone
    except (OSError, ProcessLookupError):
        return False
    return _cmdline_is_kubectl_forward(pid)


def _cmdline_is_kubectl_forward(pid: int) -> bool:
    # Linux: /proc/<pid>/cmdline is NUL-separated argv, the cheapest reliable read.
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = fh.read().decode("utf-8", "replace").split("\x00")
        return "kubectl" in " ".join(argv) and "port-forward" in argv
    except OSError:
        pass
    # Fallback (macOS/BSD): ask ps for the command of that pid.
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, check=False).stdout
    except OSError:
        return False          # no ps either: identity unknowable -> not ours
    return "kubectl" in out and "port-forward" in out


def _pid_alive(pid: int) -> bool:
    """Back-compat alias: liveness AND identity, so no caller can regress to a bare
    existence check that would trust or kill a recycled pid."""
    return _pid_is_our_forward(pid)


def start_port_forward(ctx: str, ns: str, host_port: int,
                       run: _Runner | None = None) -> int:
    """Start `kubectl port-forward` detached and return its pid."""
    return (run or _Runner()).port_forward(ctx, ns, host_port)


def _port_accepting(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_for_forward(pid: int, port: int, timeout: float = 5.0) -> None:
    """Wait until a replacement forward is actually accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            raise NotReadyError(
                f"the replacement Kubernetes port-forward for localhost:{port} "
                "exited before it became ready")
        if _port_accepting(port):
            return
        time.sleep(0.1)
    raise NotReadyError(
        f"the replacement Kubernetes port-forward for localhost:{port} did not "
        f"become ready within {timeout:g}s")


def ensure_port_forward(meta, emit: Emit = null_emit,
                        run: _Runner | None = None, *,
                        wait_for_listener: bool = True) -> int | None:
    """Make sure the forward is alive, restarting it if not. Returns its pid.

    Idempotent and cheap, so callers can invoke it unconditionally instead of
    tracking whether it is needed.
    """
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    ns, ctx = extra.get(_NAMESPACE), extra.get(_CONTEXT)
    if not ns or not ctx:
        return None
    pid = extra.get(_FORWARD_PID)
    if isinstance(pid, int) and _pid_alive(pid):
        return pid
    events.info(emit, "re-establishing the port-forward", phase="wait")
    pid = start_port_forward(ctx, ns, meta.host_port, run)
    if wait_for_listener:
        _wait_for_forward(pid, meta.host_port)
    return pid


def stop_port_forward(meta) -> bool:
    """Kill the recorded forward. True if one was running."""
    import os
    import signal
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    pid = extra.get(_FORWARD_PID)
    if not isinstance(pid, int) or not _pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def forward_state(meta) -> str:
    """"up" or "down". A repro whose forward died is still running in the cluster,
    so reporting it as broken would be wrong; it is reported separately instead."""
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    pid = extra.get(_FORWARD_PID)
    return "up" if isinstance(pid, int) and _pid_alive(pid) else "down"


# --- inspection ----------------------------------------------------------------

def pods(name: str, run: _Runner | None = None) -> list[dict]:
    """The repro's pods, mapped to the same shape Compose reports for services.

    Deliberately identical to `{service, state, status}` so a caller reads `info`
    the same way on both topologies. That mapping is most of what makes the
    Kubernetes path invisible to consumers.
    """
    run = run or _Runner()
    meta = runner.read_meta(name)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    ns, ctx = extra.get(_NAMESPACE), extra.get(_CONTEXT)
    if not ns or not ctx:
        return []
    res = _kubectl(run, ctx, "-n", ns, "get", "pods", "-o", "json", check=False)
    try:
        items = json.loads(res.stdout or "{}").get("items", [])
    except ValueError:
        return []
    out = []
    for item in items:
        status = item.get("status", {})
        conts = status.get("containerStatuses") or []
        ready = sum(1 for c in conts if c.get("ready"))
        restarts = sum(int(c.get("restartCount") or 0) for c in conts)
        out.append({
            "service": item.get("metadata", {}).get("name", ""),
            "state": (status.get("phase") or "unknown").lower(),
            "status": f"{ready}/{len(conts)} ready" +
                      (f", {restarts} restart(s)" if restarts else ""),
        })
    return sorted(out, key=lambda p: p["service"])


def aggregate_state(pod_list: list[dict]) -> str:
    """One word for the whole repro, matching how Compose aggregates services."""
    if not pod_list:
        return "down"
    if all(p["status"].startswith(tuple(f"{n}/{n}" for n in range(1, 6)))
           for p in pod_list):
        return "running"
    if any(p["state"] == "running" for p in pod_list):
        return "starting"
    return "stopped"


def detail(name: str, run: _Runner | None = None) -> dict:
    """The same detail record the Compose path returns, for a Kubernetes repro.

    `port_forward` is reported separately rather than folded into `state`: a repro
    whose forward died is still running in the cluster, so conflating them would
    report a healthy repro as broken.
    """
    meta = runner.read_meta(name)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    pod_list = pods(name, run)
    from rc_repro.services import access
    result = {
        "name": meta.name,
        "preset": meta.preset,
        "rc_version": meta.rc_version,
        "mongo_tag": meta.mongo_tag,
        "root_url": meta.root_url,
        "host_port": meta.host_port,
        "topology": "kubernetes",
        "namespace": extra.get(_NAMESPACE, ""),
        "context": extra.get(_CONTEXT, ""),
        "state": aggregate_state(pod_list),
        "containers": pod_list,
        "port_forward": forward_state(meta),
        "links": [{"label": "Rocket.Chat", "url": meta.root_url}],
        "access": access.handoff(meta.host_port, meta.root_url),
    }
    if extra.get("reg_token_supplied"):
        result["reg_token_supplied"] = True
    return result


def logs(name: str, *, follow: bool = False, tail: int | None = None,
         run: _Runner | None = None) -> int:
    """Stream the Rocket.Chat deployment's logs, mirroring `compose logs`."""
    run = run or _Runner()
    meta = runner.read_meta(name)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    argv = _kubectl_argv(extra.get(_CONTEXT, ""), "-n",
                         extra.get(_NAMESPACE, ""), "logs",
                         "deployment/rc-rocketchat")
    if follow:
        argv.append("--follow")
    if tail is not None:
        argv.append(f"--tail={tail}")
    return subprocess.call(argv, env=_client_env())


# --- capacity preflight ---------------------------------------------------------

def engine_capacity(run: _Runner | None = None) -> tuple[float, int]:
    """(memory GiB, CPUs) the container engine can give a cluster.

    Read from the engine rather than the host: on macOS the cluster runs inside the
    Podman or Docker VM, so the host's 16 GiB is irrelevant if the VM has 2.
    """
    run = run or _Runner()
    res = run.run(["docker", "info", "--format", "{{.MemTotal}} {{.NCPU}}"], check=False)
    parts = (res.stdout or "").split()
    if len(parts) < 2:
        return (0.0, 0)
    try:
        return (int(parts[0]) / (1024 ** 3), int(parts[1]))
    except ValueError:
        return (0.0, 0)


def _resize_command(mib: int) -> str:
    """The exact command that raises the engine's memory, for a human to run."""
    return (f"podman machine stop && podman machine set --memory {mib} "
            f"&& podman machine start")


def engine_resize_supported(run: _Runner | None = None) -> bool:
    """Whether the active Docker-compatible endpoint is a running Podman machine.

    Finding a separate ``podman`` binary is not enough: resizing that machine while
    rc-repro is connected to Docker Engine would stop the wrong container runtime.
    """
    run = run or _Runner()
    if not run.which("podman"):
        return False
    try:
        if "podman" not in (run.docker_server_platform() or "").lower():
            return False
        machine_result = run.run(
            ["podman", "machine", "inspect", "--format", "{{.State}}"],
            check=False)
    except OSError:
        return False
    return (machine_result.returncode == 0 and
            (machine_result.stdout or "").strip().lower() == "running")


def check_capacity(run: _Runner | None = None, emit: Emit = null_emit,
                   cfg: dict | None = None) -> None:
    """Refuse, or silently fix, an engine too small for the microservices topology.

    The floor is measured, not guessed: peak working set 3.49 GiB and ~3.5 of 4
    cores during convergence, so 6 GiB and 4 CPUs is the lowest defensible floor and
    CPU is the binding constraint. A memory-only check would pass a 2-core host that
    then crawls and trips its own readiness probes.

    Resizing stops the engine, which stops unrelated containers, so it happens only
    under the standing grant a human gave once at onboarding. Without that grant this
    is a preflight failure (exit 3) naming the exact command, and it does not re-ask:
    re-asking a settled question is what onboarding exists to prevent.
    """
    run = run or _Runner()
    mem_gib, cpus = engine_capacity(run)
    if mem_gib == 0.0 and cpus == 0:
        # Engine unreachable: require_tools and require_docker report that better
        # than a capacity check can.
        return
    if mem_gib >= FLOOR_MEMORY_GIB and cpus >= FLOOR_CPUS:
        return

    shortfall = []
    if mem_gib < FLOOR_MEMORY_GIB:
        shortfall.append(f"{mem_gib:.1f} GiB memory (need {FLOOR_MEMORY_GIB:g})")
    if cpus < FLOOR_CPUS:
        shortfall.append(f"{cpus} CPUs (need {FLOOR_CPUS})")
    detail = " and ".join(shortfall)

    if cpus < FLOOR_CPUS:
        # CPU cannot be raised by the memory resize, and guessing a CPU count for
        # someone's machine is not rc-repro's call.
        raise ValidationError(
            f"the microservices preset needs {FLOOR_CPUS} CPUs and "
            f"{FLOOR_MEMORY_GIB:g} GiB; this engine has {detail}. Raise the engine's "
            f"CPU allocation, or use a Compose preset instead.")

    if not engine_resize_supported(run):
        raise ValidationError(
            f"the microservices preset needs {FLOOR_MEMORY_GIB:g} GiB; this engine "
            f"has {detail}, but the active endpoint is not a running Podman machine "
            "that rc-repro can resize. Raise Docker Desktop's memory, increase the "
            "Docker host's memory, or use a Compose preset instead.")

    from rc_repro.services import onboarding
    granted = onboarding.state(cfg)["grants"].get("engine_resize")

    if not granted:
        grant_command = onboarding.grant_command("engine-resize", cfg)
        raise ValidationError(
            f"the microservices preset needs {FLOOR_MEMORY_GIB:g} GiB; this engine "
            f"has {detail}. Either raise it yourself with "
            f"`{_resize_command(int(FLOOR_MEMORY_GIB * 1024))}`, or grant rc-repro "
            f"permission to do it with `{grant_command}` "
            f"(note that restarting the engine stops unrelated containers).")

    # Granted: act, but report it as an event rather than doing it silently. The
    # grant covers the action, not hiding it.
    events.warn(emit, f"engine has {detail}; resizing it now, which restarts the "
                      f"engine and stops unrelated containers", phase="preflight")
    target = str(int(FLOOR_MEMORY_GIB * 1024))
    run.run(["podman", "machine", "stop"], check=False)
    run.run(["podman", "machine", "set", "--memory", target], check=False)
    run.run(["podman", "machine", "start"], check=False)
    mem_gib, cpus = engine_capacity(run)
    if mem_gib < FLOOR_MEMORY_GIB:
        raise CreateFailedError(
            f"resized the engine but it still reports {mem_gib:.1f} GiB; "
            f"raise it manually with `{_resize_command(int(FLOOR_MEMORY_GIB * 1024))}`")
    events.info(emit, f"engine now has {mem_gib:.1f} GiB", phase="preflight")


#: Pod conditions that no amount of waiting will fix, mapped to a stable error code.
#: These are exactly the two the earlier fail-fast decision named and the measurement
#: work observed on arm64, plus the scheduling case. Each is a fact about the request
#: or the environment, so classifying it terminal and aborting (exit 7) is right where
#: waiting out the timeout (exit 5) is the waste the decision existed to remove.
_TERMINAL_POD_PATTERNS: tuple[tuple[str, str], ...] = (
    ("no match for platform", "IMAGE_PLATFORM_MISMATCH"),
    ("no matching manifest", "IMAGE_PLATFORM_MISMATCH"),
    ("manifest unknown", "IMAGE_NOT_FOUND"),
    ("not found", "IMAGE_NOT_FOUND"),
    ("denied", "IMAGE_PULL_AUTH"),
    ("unauthorized", "IMAGE_PULL_AUTH"),
    ("forbidden", "IMAGE_PULL_AUTH"),
)


def detect_terminal_pod_failure(name: str, run: _Runner | None = None) -> tuple[str, str] | None:
    """Scan a repro's pods for a condition that can never succeed.

    Returns (pod, message) on the first terminal condition, else None. Reads the
    waiting-state reason from each container status, which is where a pull failure
    surfaces: ImagePullBackOff alone is not terminal (a slow registry looks the
    same), so it is the reason string that discriminates, exactly as the decision
    said.
    """
    run = run or _Runner()
    meta = runner.read_meta(name)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    ctx, ns = extra.get(_CONTEXT, ""), extra.get(_NAMESPACE, "")
    if not ctx or not ns:
        return None
    res = _kubectl(run, ctx, "-n", ns, "get", "pods", "-o", "json", check=False)
    try:
        items = json.loads(res.stdout or "{}").get("items", [])
    except ValueError:
        return None
    for item in items:
        pod = item.get("metadata", {}).get("name", "")
        status = item.get("status", {})
        # Unschedulable with no matching node is terminal for a fixed cluster.
        for cond in status.get("conditions", []):
            if cond.get("reason") == "Unschedulable":
                return pod, f"IMAGE/SCHEDULE: {cond.get('message', 'unschedulable')}"
        for cs in status.get("containerStatuses", []) or []:
            waiting = (cs.get("state", {}) or {}).get("waiting") or {}
            blob = f"{waiting.get('reason', '')} {waiting.get('message', '')}".lower()
            for needle, code in _TERMINAL_POD_PATTERNS:
                if needle in blob:
                    return pod, f"{code}: {waiting.get('message', '').strip()[:200]}"
    return None


def wait_ready(name: str, *, timeout: float = 600.0, emit: Emit = null_emit,
               run: _Runner | None = None) -> dict:
    """Block until the repro serves, aborting early on a terminal pod condition.

    Readiness is an HTTP fact, so success is Rocket.Chat answering, not pods being
    Ready. But a stuck image pull would make an HTTP-only wait sit out the whole
    timeout, so each tick also asks whether a pod has hit a condition that can never
    succeed, and raises CreateFailedError (exit 7) if so, distinct from the exit 5 a
    real timeout gives. That distinction is the entire point of the decision.
    """
    from rc_repro import rcapi
    run = run or _Runner()
    meta = runner.read_meta(name)
    deadline_ticks = max(1, int(timeout / _MONGO_READY_INTERVAL))
    for i in range(deadline_ticks):
        # A forward started before the Service has an endpoint can exit immediately.
        # Reconcile on every tick, not just once before the loop: otherwise that
        # early second death leaves every remaining HTTP probe aimed at a dead port.
        # This loop already owns retries while the Service is converging, so an
        # early forward death is non-terminal here. HTTP consumers outside this
        # readiness loop use the default listener wait and never race login.
        pid = ensure_port_forward(meta, emit, run, wait_for_listener=False)
        if pid and pid != (meta.extra or {}).get(_FORWARD_PID):
            meta.extra = {**(meta.extra or {}), _FORWARD_PID: pid}
            runner.write_meta(name, meta)
        info_doc = rcapi.api_info(meta.root_url)
        if info_doc:
            booted = int(i * _MONGO_READY_INTERVAL)
            # HTTP is only the first readiness fact: the shared lifecycle still
            # has to prove the first admin and complete the setup wizard.
            info(emit, "Rocket.Chat is serving", phase="post_ready", pct=95)
            return {"name": name, "booted_s": booted,
                    "version": info_doc.get("version", "?"),
                    "port_forward": forward_state(meta)}
        terminal = detect_terminal_pod_failure(name, run)
        if terminal:
            pod, msg = terminal
            raise CreateFailedError(
                f"{name!r} cannot become ready: pod {pod} hit a terminal condition "
                f"that waiting will not fix ({msg})")
        info(emit, f"waiting for Rocket.Chat ({int(i * _MONGO_READY_INTERVAL)}s)",
             phase="wait", pct=min(99.0, i / deadline_ticks * 100))
        run.sleep(_MONGO_READY_INTERVAL)
    raise NotReadyError(
        f"{name!r} did not serve within {int(timeout)}s "
        f"(`rc-repro logs --name {name}` to see why)")


def exec_in(name: str, service: str, args: list[str],
            run: _Runner | None = None) -> int:
    """Run a command inside a repro's pod, mirroring `compose exec <service>`.

    `service` is a deployment name without the release prefix ("rocketchat",
    "account"), so a caller uses the same word it would on the Compose path rather
    than needing to know the chart's naming.
    """
    run = run or _Runner()
    meta = runner.read_meta(name)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    ctx, ns = extra.get(_CONTEXT, ""), extra.get(_NAMESPACE, "")
    if not ctx or not ns:
        raise ValidationError(f"{name!r} has no Kubernetes context recorded")
    target = service if service.startswith("rc-") else f"rc-{service}"
    return subprocess.call(
        _kubectl_argv(ctx, "-n", ns, "exec", "-i", f"deployment/{target}",
                      "--", *args),
        env=_client_env())


def owned_namespaces(ctx: str, run: _Runner | None = None) -> list[str]:
    """Namespaces rc-repro created, found by label rather than by name pattern."""
    run = run or _Runner()
    res = _kubectl(run, ctx, "get", "namespaces", "-l", OWNER_LABEL,
                   "-o", "jsonpath={range .items[*]}{.metadata.name} ", check=False)
    if res.returncode != 0:
        raise DockerError(
            f"could not verify whether cluster {CLUSTER_NAME} still has rc-repro "
            f"namespaces; refusing to delete it: {_failure_detail(res)}")
    return sorted((res.stdout or "").split())


def cluster_is_ours(ctx: str, run: _Runner | None = None) -> bool:
    """Whether rc-repro created this cluster, so it may be deleted.

    A fixed Kind name is not ownership: another operator can create the same name.
    rc-repro therefore writes a marker only after it creates the cluster and requires
    that marker before either reuse or deletion.
    """
    run = run or _Runner()
    result = _kubectl(
        run, ctx, "-n", "kube-system", "get", "configmap",
        CLUSTER_OWNER_CONFIGMAP, "-o", "json", check=False)
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout or "")
    except (json.JSONDecodeError, TypeError):
        return False
    labels = payload.get("metadata", {}).get("labels", {})
    owner_key, owner_value = OWNER_LABEL.split("=", 1)
    return (labels.get(owner_key) == owner_value and
            payload.get("data", {}).get("cluster") == CLUSTER_NAME)


def cluster_prune_status(run: _Runner | None = None) -> dict:
    """Describe whether the shared owned cluster can be deleted safely.

    Absence and ambiguity are different. A missing cluster is simply not a prune
    target; an existing cluster whose namespaces cannot be enumerated must be retained.
    That distinction prevents an API or kubeconfig failure from becoming permission to
    destroy a cluster.
    """
    run = run or _Runner()
    base = {"cluster": CLUSTER_NAME, "exists": False, "prunable": False,
            "namespaces": []}
    if not run.which("kind"):
        return {**base, "reason": "kind is unavailable; cluster state was not inspected"}
    try:
        clusters = run.run(["kind", "get", "clusters"], check=False)
    except OSError as exc:
        return {**base, "reason": f"could not inspect kind clusters: {exc}"}
    if clusters.returncode != 0:
        return {**base, "reason": "could not inspect kind clusters: " +
                _failure_detail(clusters)}
    if CLUSTER_NAME not in (clusters.stdout or "").split():
        return {**base, "reason": "no rc-repro-owned cluster"}

    base["exists"] = True
    if not run.which("kubectl"):
        return {**base, "reason": "kubectl is unavailable; refusing to delete the cluster"}
    try:
        ctx = _export_kubeconfig(run)
    except (CreateFailedError, OSError) as exc:
        return {**base, "reason": f"could not inspect cluster ownership: {exc}"}
    if not cluster_is_ours(ctx, run):
        return {**base, "reason": "rc-repro ownership marker is absent or unreadable; "
                                   "refusing to delete the cluster"}
    try:
        remaining = owned_namespaces(ctx, run)
    except (DockerError, OSError) as exc:
        return {**base, "reason": str(exc)}
    if remaining:
        return {**base, "namespaces": remaining, "reason": "repros still present"}
    return {**base, "prunable": True, "reason": "empty rc-repro-owned cluster"}


def prune_cluster(emit: Emit = null_emit, run: _Runner | None = None) -> dict:
    """Delete the rc-repro-owned cluster once no owned namespaces remain.

    Matches how `prune` already reclaims down repros on the Compose path. Refuses
    while any owned namespace is left, because deleting the cluster would take
    running repros with it.
    """
    run = run or _Runner()
    status = cluster_prune_status(run)
    if not status["prunable"]:
        return {**status, "deleted": False}
    events.info(emit, f"deleting the empty cluster {CLUSTER_NAME}", phase="teardown")
    res = run.run(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)
    if res.returncode != 0:
        raise DockerError("could not delete the empty rc-repro cluster: " +
                          _failure_detail(res))
    try:
        verify = run.run(["kind", "get", "clusters"], check=False)
    except OSError as exc:
        raise DockerError(f"could not verify cluster deletion: {exc}") from exc
    if verify.returncode != 0:
        raise DockerError("could not verify cluster deletion: " +
                          _failure_detail(verify))
    if CLUSTER_NAME in (verify.stdout or "").split():
        raise DockerError("kind reported successful deletion but the rc-repro cluster "
                          "is still present")
    return {**status, "exists": False, "prunable": False, "deleted": True,
            "reason": "deleted"}


def collect_logs(name: str, run: _Runner | None = None,
                 tail: int = 2000) -> dict[str, str]:
    """Per-pod logs, for an evidence bundle. {pod_name: text}.

    Bounded by `tail` on purpose: an evidence bundle someone attaches to a case
    should not be unboundedly large, and the tail is where a failure shows.

    A pod whose logs cannot be read contributes an explicit note rather than being
    omitted, so a reader can tell "nothing was logged" from "this was not collected".
    """
    run = run or _Runner()
    meta = runner.read_meta(name)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    ctx, ns = extra.get(_CONTEXT, ""), extra.get(_NAMESPACE, "")
    if not ctx or not ns:
        return {}
    out: dict[str, str] = {}
    for pod in pods(name, run):
        pod_name = pod["service"]
        if not pod_name:
            continue
        res = _kubectl(run, ctx, "-n", ns, "logs", pod_name, "--all-containers=true",
                       f"--tail={tail}", check=False)
        if res.returncode == 0 and (res.stdout or "").strip():
            out[pod_name] = res.stdout
        else:
            out[pod_name] = ("[no logs collected: " +
                             ((res.stderr or "").strip()[:200] or "empty") + "]\n")
    return out


def restart(name: str, emit: Emit = null_emit, run: _Runner | None = None) -> int:
    """Roll the repro's deployments, the Kubernetes analogue of `compose restart`."""
    run = run or _Runner()
    meta = runner.read_meta(name)
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    ctx, ns = extra.get(_CONTEXT, ""), extra.get(_NAMESPACE, "")
    if not ctx or not ns:
        raise ValidationError(f"{name!r} has no Kubernetes context recorded")
    events.info(emit, "rolling the deployments", phase="boot")
    res = _kubectl(run, ctx, "-n", ns, "rollout", "restart", "deployment", "--all",
                   check=False)
    return res.returncode
