"""The only Kubernetes seam, as `runner.py` is the only Docker one.

Every `kind`, `kubectl` and `helm` invocation in rc-repro goes through `run()`
here, so tests drive the whole Kubernetes surface without a cluster and there is
one place to look when a command needs a flag, a timeout or a kubeconfig.

Three names in this module are decisions rather than settings, and each is
recorded where it is defined: the cluster rc-repro owns, the namespace a
workspace gets, and the label that proves ownership. PR #3 arrived at the first
and third independently, which is some evidence they are right.

**Nothing here creates anything yet.** This is the preflight: it answers "could
this machine run a Kubernetes workspace, and what is missing" without changing a
byte of the machine's state. Every function is a read.

Timeouts are short and mandatory. `kubectl` against an unreachable API server
blocks until its own default gives up, and a preflight that hangs is worse than
one that says "cannot reach it" -- `doctor` exists to answer quickly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from rc_repro import config, runner
from rc_repro.errors import (ConflictError, CreateFailedError, DockerError,
                            PreflightError)
from rc_repro.services.events import Emit, info, null_emit, warn

#: The cluster rc-repro creates and owns. One cluster with a namespace per
#: workspace, not a cluster per workspace: a control plane each would forbid
#: concurrent repros on laptop-scale hardware, which is behaviour rc-repro
#: already has on Compose.
#:
#: rc-repro USES whatever cluster it is pointed at and creates this one only when
#: there is none. An earlier draft always created it, on the argument that kind's
#: `extraPortMappings` are fixed at creation so an adopted cluster could never
#: serve a hostname. Measured on Linux, that is false: kind nodes are directly
#: routable from the host on the `kind` bridge (`curl https://172.19.0.2:6443` ->
#: 200, no mapping), so an ingress in any kind cluster is reachable at the node's
#: :80 and the edge reaches it with `docker network connect`, as it already does
#: for Compose workspaces.
#:
#: What survives is the TEARDOWN asymmetry, which is the part that matters: a
#: cluster rc-repro created it may delete; in one you supplied it owns only the
#: namespaces carrying its label.
CLUSTER_NAME = "rc-repro-local"

#: kind prefixes the kubeconfig context with `kind-`.
CONTEXT = f"kind-{CLUSTER_NAME}"

#: One namespace per workspace. The official guide creates a named namespace and
#: says the name is the operator's choice, so this fits the documented model.
NAMESPACE_PREFIX = "rc-repro-"

#: The Helm release name INSIDE that namespace. Deliberately the same name the
#: official docs use, so every command copied from docs.rocket.chat works with
#: one substitution (the namespace) rather than a translation. PR #3 calls it
#: `rc`, which makes `helm upgrade rocketchat ...` from the docs wrong.
RELEASE = "rocketchat"

#: Ownership. Selection is ALWAYS by this label, never by a name prefix: anyone
#: can create a namespace called `rc-repro-anything`, and a teardown that matched
#: on the prefix would eventually delete one rc-repro never made.
OWNER_LABEL_KEY = "app.kubernetes.io/managed-by"
OWNER_LABEL_VALUE = "rc-repro"
OWNER_SELECTOR = f"{OWNER_LABEL_KEY}={OWNER_LABEL_VALUE}"
WORKSPACE_LABEL = "rc-repro.io/workspace"
OWNER_OF_LABEL = "rc-repro.io/owner"

#: The tools, and the floor each must clear. These two are the official guide's
#: own requirement ("kubectl v1.21+, Helm 3") and they are all that USING
#: Kubernetes needs -- namespaces, helm, PVCs, port-forward and exec are plain
#: Kubernetes and work on k3s, minikube, Docker Desktop or a remote cluster
#: exactly as they work on kind.
CORE_TOOLS: dict[str, tuple[int, int]] = {
    "kubectl": (1, 21),
    "helm": (3, 0),
}

#: Needed only to PROVISION a cluster, never to use one. kind has no documented
#: floor, so 0.20 is where `extraPortMappings` and the config API settled.
#: Without it rc-repro cannot create a cluster; it can still run perfectly well
#: in one you already have.
PROVISION_TOOLS: dict[str, tuple[int, int]] = {
    "kind": (0, 20),
}

TOOLS: dict[str, tuple[int, int]] = {**CORE_TOOLS, **PROVISION_TOOLS}

#: How a cluster came to exist, which is what decides how far rc-repro may go.
PROVIDER_KIND = "kind"          # rc-repro made it, so rc-repro may delete it
PROVIDER_EXTERNAL = "external"  # you made it; rc-repro owns its namespaces only

#: Seconds. Long enough for a loaded laptop, short enough that `doctor` answers.
PROBE_TIMEOUT = 8.0


@dataclass
class Tool:
    """One binary: whether it is there, what version, and whether that is enough."""
    name: str
    path: str = ""
    version: tuple[int, ...] = ()
    raw: str = ""

    @property
    def present(self) -> bool:
        return bool(self.path)

    @property
    def new_enough(self) -> bool:
        floor = TOOLS.get(self.name)
        if not floor or not self.version:
            # Unknown version with the binary present is not a failure: a distro
            # may print something this does not parse, and refusing to proceed
            # over an unrecognised version string would be worse than trying.
            return True
        return self.version[:len(floor)] >= floor

    @property
    def pretty(self) -> str:
        return ".".join(str(n) for n in self.version) if self.version else (self.raw or "?")


@dataclass
class Preflight:
    """What this machine can do, as facts rather than a verdict.

    The verdict belongs to the caller, because it depends on what is being asked:
    a box with no `kind` is perfectly healthy if nobody wants Kubernetes, and
    broken if a Kubernetes workspace already exists on it.
    """
    tools: dict[str, Tool] = field(default_factory=dict)
    cluster_exists: bool = False
    cluster_reachable: bool = False
    storage_classes: list[str] = field(default_factory=list)
    default_storage_class: str = ""
    #: Ingress controllers installed here. Empty is NOT a fault -- a workspace
    #: reached by port-forward needs none. It only blocks `--domain`, so it is
    #: checked at create time against what was asked for, never in `doctor`.
    ingress_classes: list[str] = field(default_factory=list)
    other_clusters: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    #: The context actually probed, and how that cluster came to exist. On a box
    #: with no kind this is whatever `kubectl` is already pointed at -- the
    #: bring-your-own-cluster case, where rc-repro manages namespaces and never
    #: the cluster.
    context: str = ""
    provider: str = ""
    #: Why the cluster question could not be ANSWERED, as opposed to answered no.
    #: `kind get clusters` fails when Docker is down, and returns nothing when there
    #: are simply no clusters -- both give an empty list. Reporting the first as
    #: "the cluster does not exist" would send someone to create one that is
    #: already there, so the two are kept apart.
    probe_failed: str = ""

    @property
    def tools_ready(self) -> bool:
        """Whether Kubernetes can be USED. Provisioning is a separate question."""
        return all(self.tools[n].present and self.tools[n].new_enough
                   for n in CORE_TOOLS if n in self.tools)

    @property
    def can_provision(self) -> bool:
        """Whether rc-repro can CREATE a cluster, as opposed to use one."""
        return all(self.tools[n].present and self.tools[n].new_enough
                   for n in PROVISION_TOOLS if n in self.tools)

    @property
    def missing_tools(self) -> list[str]:
        """Only the ones that stop Kubernetes working. A missing `kind` means
        rc-repro cannot make you a cluster, not that it cannot use yours."""
        return [n for n in CORE_TOOLS if n in self.tools and not self.tools[n].present]

    @property
    def outdated_tools(self) -> list[str]:
        return [n for n, t in self.tools.items() if t.present and not t.new_enough]

    @property
    def usable(self) -> bool:
        """A cluster is reachable and rc-repro could put a workspace in it."""
        return self.tools_ready and self.cluster_reachable


def owned_kubeconfig() -> Path:
    """rc-repro's OWN kubeconfig, under RC_REPRO_HOME.

    `kind create cluster` writes `~/.kube/config` AND switches current-context to
    the cluster it just made. Without this, creating a workspace would silently
    redirect the user's own `kubectl` -- somebody working in their k3s cluster
    runs `rc-repro up` and their next `kubectl get pods` answers from somewhere
    else. That is the one way cluster creation genuinely disturbs existing work,
    and it is not acceptable for a tool that is supposed to be disposable.

    Adopted from PR #3, which also redirects every Helm home: pinning only
    repositories.yaml still leaves cache and data writes in the user's home.
    """
    return config.home() / "clients" / "kubernetes" / "config"


def owned_env() -> dict[str, str]:
    """An environment whose client state cannot reach the user's home.

    Used for anything touching rc-repro's OWN cluster. Discovery -- finding the
    cluster you already have -- deliberately does NOT use this, because the whole
    point there is to read the config you already set up.
    """
    root = config.home() / "clients"
    helm = root / "helm"
    kubeconfig = owned_kubeconfig()
    for directory in (kubeconfig.parent, helm / "cache", helm / "config",
                      helm / "data", helm / "cache" / "repository"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy()
    env.update({
        "KUBECONFIG": str(kubeconfig),
        "HELM_CACHE_HOME": str(helm / "cache"),
        "HELM_CONFIG_HOME": str(helm / "config"),
        "HELM_DATA_HOME": str(helm / "data"),
        "HELM_REPOSITORY_CONFIG": str(helm / "config" / "repositories.yaml"),
        "HELM_REPOSITORY_CACHE": str(helm / "cache" / "repository"),
    })
    return env


def is_ours(context: str) -> bool:
    """Whether a context names the cluster rc-repro created."""
    return context == CONTEXT


def which(tool: str) -> str:
    """Absolute path to a tool, or "" -- the one place PATH is consulted."""
    return shutil.which(tool) or ""


def run(argv: list[str], *, timeout: float = PROBE_TIMEOUT,
        own: bool = False) -> subprocess.CompletedProcess:
    """Run a kind/kubectl/helm command. Never raises; the caller reads returncode.

    `own=True` runs against rc-repro's own client state, so nothing it does can
    reach or rewrite the user's `~/.kube/config` or Helm homes. Never guessed at a
    call site: each function derives it from the context it is targeting, so
    forgetting it is not a thing that can happen.

    A preflight that raises on a missing binary or an unreachable API server would
    have to be wrapped at every call site, and one forgotten wrapper takes down
    `doctor` -- which is the command someone runs precisely because things are
    already wrong.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False,
                              env=owned_env() if own else None)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, returncode=127, stdout="",
                                           stderr=str(exc))


def _parse_version(text: str) -> tuple[int, ...]:
    """First dotted number in a version string, as a tuple.

    Deliberately loose: `kind v0.32.0 go1.26.3 linux/amd64`,
    `Client Version: v1.36.3` and `v4.2.3+g43e8b7f` are all real output from the
    three tools, and each spells it differently.
    """
    match = re.search(r"v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return ()
    return tuple(int(g) for g in match.groups() if g is not None)


def tool(name: str) -> Tool:
    """Locate one tool and read its version."""
    found = Tool(name=name, path=which(name))
    if not found.present:
        return found
    argv = {
        "kind": [name, "version"],
        "kubectl": [name, "version", "--client"],
        "helm": [name, "version", "--short"],
    }.get(name, [name, "--version"])
    res = run(argv, timeout=PROBE_TIMEOUT)
    found.raw = (res.stdout or res.stderr or "").strip().splitlines()[0] if (
        res.stdout or res.stderr) else ""
    found.version = _parse_version(found.raw)
    return found


def clusters() -> tuple[list[str], str]:
    """(every kind cluster on this machine, why the question could not be answered).

    kind talks to Docker, so this fails when Docker is down -- and it fails by
    printing to stderr and exiting non-zero, which is indistinguishable from "no
    clusters" if only the stdout is read. Returning the reason separately is what
    stops a stopped Docker being reported as an absent cluster.
    """
    res = run(["kind", "get", "clusters"], own=True)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        return [], (detail[0][:120] if detail else "kind could not list clusters")
    return [ln.strip() for ln in (res.stdout or "").splitlines()
            if ln.strip() and "No kind clusters" not in ln], ""


def reachable(context: str = CONTEXT) -> bool:
    """Whether the API server answers. A cluster can exist and not respond --
    a stopped Docker, a half-deleted cluster, a machine that just woke up."""
    res = run(["kubectl", "--context", context, "get", "--raw", "/readyz"],
               own=is_ours(context))
    return res.returncode == 0 and "ok" in (res.stdout or "").lower()


def storage_classes(context: str = CONTEXT) -> tuple[list[str], str]:
    """(all storage class names, the default one).

    The guide's step 1, and it opens with the warning that matters here: "Local
    Kubernetes distributions such as Kind, K3s, and Minikube often ship without a
    storage provisioner enabled." Without one, a PVC stays Pending forever and the
    workspace never boots -- with no error that names storage.
    """
    res = run(["kubectl", "--context", context, "get", "storageclass", "-o", "json"],
              own=is_ours(context))
    if res.returncode != 0:
        return [], ""
    try:
        items = (json.loads(res.stdout or "{}") or {}).get("items") or []
    except (ValueError, TypeError):
        return [], ""
    names, default = [], ""
    for item in items:
        meta = item.get("metadata") or {}
        name = meta.get("name") or ""
        if not name:
            continue
        names.append(name)
        ann = meta.get("annotations") or {}
        if ann.get("storageclass.kubernetes.io/is-default-class") == "true":
            default = name
    return names, default


def ingress_classes(context: str = CONTEXT) -> list[str]:
    """Ingress controllers installed in this cluster.

    kind ships none -- verified on this box, `get ingressclass` returns "No
    resources found" -- which is why rc-repro installs Traefik into its OWN
    cluster and refuses `--domain` against yours rather than installing into it.
    """
    res = run(["kubectl", "--context", context, "get", "ingressclass", "-o", "name"],
              own=is_ours(context))
    if res.returncode != 0:
        return []
    return [ln.split("/", 1)[-1].strip()
            for ln in (res.stdout or "").splitlines() if ln.strip()]


def workspace_namespaces(context: str = CONTEXT) -> list[str]:
    """Namespaces rc-repro owns, selected by LABEL.

    Never by name prefix -- see OWNER_LABEL_KEY.
    """
    res = run(["kubectl", "--context", context, "get", "namespace",
               "-l", OWNER_SELECTOR, "-o", "name"], own=is_ours(context))
    if res.returncode != 0:
        return []
    return [ln.split("/", 1)[-1].strip()
            for ln in (res.stdout or "").splitlines() if ln.strip()]


def active_context() -> str:
    """The context `kubectl` would use right now, or "".

    This is the bring-your-own-cluster entry point: on a box with k3s, minikube,
    Docker Desktop or a kubeconfig pointing at a remote cluster, this is the
    cluster rc-repro would work in.
    """
    res = run(["kubectl", "config", "current-context"])
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def preflight(context: str = "") -> Preflight:
    """Everything `doctor` needs, in one pass, changing nothing.

    **kind is optional.** Creating a cluster needs it; using one does not.
    Namespaces, helm releases, PVCs, port-forwards and exec are plain Kubernetes
    and behave the same on k3s, minikube, Docker Desktop or a remote cluster. So
    a missing `kind` narrows what rc-repro can do -- it cannot make you a cluster
    -- without stopping it working in the cluster you already have.

    Which cluster gets probed, in order: the one asked for, else rc-repro's own if
    it exists, else whatever `kubectl` is already pointed at.

    Ordered so each step's precondition is known. There is no point asking an API
    server for storage classes when `kubectl` is absent or the server is not
    answering, and a timeout there would be misreported as "no storage" -- sending
    someone to fix the wrong thing.
    """
    out = Preflight(tools={name: tool(name) for name in TOOLS})

    if out.can_provision:
        found, out.probe_failed = clusters()
        if out.probe_failed:
            return out
        out.cluster_exists = CLUSTER_NAME in found
        out.other_clusters = [c for c in found if c != CLUSTER_NAME]

    if not out.tools["kubectl"].present:
        return out

    if context:
        out.context = context
    elif out.cluster_exists:
        out.context = CONTEXT
    else:
        out.context = active_context()
    out.provider = PROVIDER_KIND if out.context == CONTEXT else PROVIDER_EXTERNAL

    if not out.context:
        return out
    out.cluster_reachable = reachable(out.context)
    if not out.cluster_reachable:
        return out
    out.storage_classes, out.default_storage_class = storage_classes(out.context)
    out.ingress_classes = ingress_classes(out.context)
    out.namespaces = workspace_namespaces(out.context)
    return out


def storage_blocker(pre: Preflight) -> str:
    """Why a workspace's volume could not bind in this cluster, or "".

    Takes an already-probed Preflight rather than a context, so it cannot be
    called on data from an unreachable cluster -- reporting "no StorageClass" for
    a cluster nobody could reach is the same wrong answer as reporting an absent
    cluster for a stopped Docker.

    This is a REFUSAL, not a warning, for the reason that makes it nasty: with no
    provisioner the PVC stays Pending, the pod stays Pending, and **Rocket.Chat
    never starts** -- so there are no Rocket.Chat logs to find it in. Left to run,
    it costs the full readiness timeout and then reports "Rocket.Chat did not
    become ready", which blames the one component that is innocent.
    """
    if not pre.cluster_reachable:
        return ""          # not our question; the caller reports unreachable first
    if pre.default_storage_class:
        return ""
    where = f"Cluster {pre.context!r}"
    if pre.storage_classes:
        return (f"{where} has StorageClasses ({', '.join(pre.storage_classes)}) but "
                "none is marked default, so the workspace's volume would stay Pending "
                "and Rocket.Chat would never start. Mark one default with: kubectl "
                "patch storageclass <name> -p "
                "'{\"metadata\":{\"annotations\":"
                "{\"storageclass.kubernetes.io/is-default-class\":\"true\"}}}'")
    return (f"{where} has no StorageClass, so the workspace's volume would stay "
            "Pending forever and Rocket.Chat would never start — with nothing in its "
            "logs naming storage, because it never runs. The official guide warns "
            "that local distributions often ship without a provisioner.")


def ingress_blocker(pre: Preflight, *, wants_domain: bool) -> str:
    """Why a hostname could not be served here, or "".

    Conditional on purpose. A workspace reached by port-forward needs no ingress
    controller at all, so an absent one is not a fault -- it is only a fault
    against a request for `--domain`. Checking it unconditionally in `doctor`
    would warn every port-forward user about something that cannot affect them.
    """
    if not wants_domain or not pre.cluster_reachable:
        return ""
    if pre.ingress_classes:
        return ""
    if pre.provider == PROVIDER_KIND:
        # Our own cluster: this is rc-repro's job to fix, not the user's.
        return (f"Cluster {pre.context!r} has no ingress controller yet — rc-repro "
                "installs one into its own cluster; re-create it with "
                "`rc-repro down --name <workspace>` and `up` again.")
    return (f"Cluster {pre.context!r} has no ingress controller, so --domain could "
            "not be served. rc-repro does not install one into a cluster it does not "
            "own. Install one (e.g. helm install traefik traefik/traefik -n traefik "
            "--create-namespace), or drop --domain and use the port-forward.")


# --- provisioning: the first thing here that writes to the machine ---------------

#: Lock name for cluster creation. Not a valid repro name (`sanitize` strips the
#: underscores), so it can never collide with a workspace's own lock.
CLUSTER_LOCK = "__cluster__"

#: Creating a cluster pulls a node image on first use and waits for the control
#: plane. Nothing like the 8s a probe gets.
CREATE_TIMEOUT = 600.0
DELETE_TIMEOUT = 120.0


def cluster_context() -> str:
    """The context rc-repro's own cluster is reachable at.

    Read back from the owned kubeconfig rather than assumed from kind's naming
    convention, so a future kind that names contexts differently does not silently
    point every command at nothing.
    """
    res = run(["kubectl", "config", "current-context"], own=True)
    found = (res.stdout or "").strip() if res.returncode == 0 else ""
    return found or CONTEXT


def ensure_cluster(emit: Emit = null_emit) -> str:
    """Create rc-repro's cluster if it is not there, and return its context.

    Serialised with the SAME lock every other mutating operation uses
    (`runner.repro_lock`), rather than a second lock of its own. Two simultaneous
    `up`s would otherwise both see no cluster and both run `kind create`, and the
    second fails with "node(s) already exist". The re-check inside the lock means
    the loser of the race reuses rather than retries -- concurrent workspaces are
    the entire point of one-cluster-many-namespaces, so the lock has to hold.

    Nothing about this touches `~/.kube/config`: `--kubeconfig` is passed
    explicitly AND the environment is redirected, because kind writes the file it
    is given and also honours KUBECONFIG for the context switch.
    """
    if not which("kind"):
        raise PreflightError(
            "kind is not installed, so rc-repro cannot create a cluster. Install "
            "kind, or point kubectl at a cluster you already have.")
    kubeconfig = owned_kubeconfig()
    with runner.repro_lock(CLUSTER_LOCK, timeout=CREATE_TIMEOUT):
        if CLUSTER_NAME in clusters()[0]:
            info(emit, f"reusing cluster {CLUSTER_NAME}", phase="provision")
        else:
            info(emit, f"creating cluster {CLUSTER_NAME} — first time on this "
                       "machine, so this pulls a node image",
                 phase="provision", pct=5)
            owned_env()          # make the directories before kind writes into them
            res = run(["kind", "create", "cluster", "--name", CLUSTER_NAME,
                       "--kubeconfig", str(kubeconfig)],
                      timeout=CREATE_TIMEOUT, own=True)
            if res.returncode != 0:
                combined = f"{res.stdout or ''}\n{res.stderr or ''}".lower()
                # A create that lost a race to something not holding this lock
                # (a manual `kind create`) is success, not failure.
                if "already exist" not in combined:
                    detail = (res.stderr or res.stdout or "").strip().splitlines()
                    raise CreateFailedError(
                        f"could not create the cluster {CLUSTER_NAME}: "
                        + (detail[-1][:200] if detail else "kind gave no reason"))
    context = cluster_context()
    if not reachable(context):
        raise CreateFailedError(
            f"cluster {CLUSTER_NAME} was created but its API server is not "
            f"answering at {context!r}. `kind delete cluster --name "
            f"{CLUSTER_NAME}` and try again.")
    info(emit, f"cluster {CLUSTER_NAME} ready at {context}", phase="provision")
    return context


def delete_cluster(*, force: bool = False, emit: Emit = null_emit) -> bool:
    """Delete rc-repro's own cluster. Returns False if there was nothing to delete.

    Refuses while any workspace namespace is still in it, unless forced. On a
    shared box the cluster holds other people's workspaces, and `prune` reclaiming
    the cluster out from under a colleague mid-ticket is the failure this guards --
    the namespaces carry `rc-repro.io/owner`, so the refusal can say whose.

    Only ever CLUSTER_NAME. There is no parameter for which cluster to delete,
    because a delete that can be pointed anywhere eventually is.
    """
    if not which("kind"):
        return False
    with runner.repro_lock(CLUSTER_LOCK, timeout=DELETE_TIMEOUT):
        if CLUSTER_NAME not in clusters()[0]:
            return False
        context = cluster_context()
        if not force and reachable(context):
            live = workspace_namespaces(context)
            if live:
                raise ConflictError(
                    f"cluster {CLUSTER_NAME} still holds {len(live)} workspace "
                    f"namespace(s): {', '.join(sorted(live)[:5])}. Remove those "
                    "workspaces first, or pass --force to take the cluster and "
                    "everything in it.")
        info(emit, f"deleting cluster {CLUSTER_NAME}", phase="teardown")
        res = run(["kind", "delete", "cluster", "--name", CLUSTER_NAME],
                  timeout=DELETE_TIMEOUT, own=True)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip().splitlines()
            raise DockerError(f"could not delete the cluster {CLUSTER_NAME}: "
                              + (detail[-1][:200] if detail else "kind gave no reason"))
    return True


# --- a workspace: namespace, MongoDB, the chart ----------------------------------

#: The chart, never vendored -- it is the topology's source of truth.
HELM_REPO = "rocketchat"
HELM_REPO_URL = "https://rocketchat.github.io/helm-charts"
CHART = f"{HELM_REPO}/rocketchat"

#: In-cluster MongoDB. `mongodb` rather than PR #3's `mongo`, so the URL matches
#: Compose's `config.MONGO_URL` and the two runtimes read the same way.
MONGO_SERVICE = "mongodb"
MONGO_URL = f"mongodb://{MONGO_SERVICE}-0.{MONGO_SERVICE}:27017/rocketchat?replicaSet=rs0"
MONGO_OPLOG_URL = f"mongodb://{MONGO_SERVICE}-0.{MONGO_SERVICE}:27017/local?replicaSet=rs0"

#: The volume a workspace's data lives on. PR #3 runs MongoDB with no volume at
#: all, so its data lives in the pod's writable layer and dies with a reschedule --
#: while `--fresh` tells the user it deleted a PVC that never existed. `backup`,
#: `restore` and `upgrade` all assume the data survives a restart.
MONGO_VOLUME_GB = 8

APPLY_TIMEOUT = 120.0
INSTALL_TIMEOUT = 900.0


def namespace_for(name: str) -> str:
    return f"{NAMESPACE_PREFIX}{name}"


def _labels(name: str, owner: str = "") -> dict[str, str]:
    """The labels every resource rc-repro creates carries.

    `managed-by` is what teardown selects on. `workspace` and `owner` are what make
    a refusal specific -- "the cluster still holds 2 namespaces" is actionable only
    if it can say whose.
    """
    out = {OWNER_LABEL_KEY: OWNER_LABEL_VALUE, WORKSPACE_LABEL: name}
    if owner:
        out[OWNER_OF_LABEL] = owner
    return out


def ensure_repo(emit: Emit = null_emit) -> None:
    """Add and refresh the Rocket.Chat chart repo in rc-repro's own Helm home."""
    run(["helm", "repo", "add", HELM_REPO, HELM_REPO_URL, "--force-update"],
        timeout=APPLY_TIMEOUT, own=True)
    res = run(["helm", "repo", "update", HELM_REPO], timeout=APPLY_TIMEOUT, own=True)
    if res.returncode != 0:
        raise CreateFailedError(
            "could not read the Rocket.Chat chart repository "
            f"({HELM_REPO_URL}): " + (res.stderr or res.stdout or "").strip()[:200])


def _version_key(text: str) -> tuple:
    """Sort key for a semver-ish string. Non-numeric parts sort low."""
    out = []
    for part in str(text).split("."):
        digits = re.match(r"(\d+)", part)
        out.append(int(digits.group(1)) if digits else -1)
    return tuple(out)


def resolve_chart_version(rc_version: str, emit: Emit = null_emit) -> str:
    """Pin a chart version for a Rocket.Chat version.

    Taken from PR #3, whose rule is right and whose reasoning the official guide
    does not give: most Rocket.Chat releases have no chart with a matching
    appVersion, so an exact match cannot be required. Exact match if one exists,
    else the newest chart whose appVersion is at or BELOW the request -- a floor,
    so the chart is never newer than the app it deploys -- else the newest, warned.

    An unreadable index is terminal rather than falling back to an unpinned chart.
    For a tool whose whole job is version-matching, `helm install` without
    `--version` deploys different software after the next chart release, which
    quietly destroys the only property the workspace was created to have.
    """
    res = run(["helm", "search", "repo", CHART, "--versions", "-o", "json"],
              timeout=APPLY_TIMEOUT, own=True)
    if res.returncode != 0:
        raise CreateFailedError("could not read the Rocket.Chat chart index: "
                                + (res.stderr or res.stdout or "").strip()[:200])
    try:
        entries = json.loads(res.stdout or "[]")
    except ValueError as exc:
        raise CreateFailedError(
            "the Rocket.Chat chart index was not valid JSON") from exc
    if not isinstance(entries, list) or not entries:
        raise CreateFailedError(f"no versions found for the chart {CHART!r}")
    want = _version_key(rc_version)
    exact = [e for e in entries if str(e.get("app_version") or "") == rc_version]
    if exact:
        return str(exact[0]["version"])
    at_or_below = [e for e in entries
                   if _version_key(str(e.get("app_version") or "0")) <= want]
    if at_or_below:
        best = max(at_or_below, key=lambda e: _version_key(str(e.get("version"))))
        return str(best["version"])
    newest = max(entries, key=lambda e: _version_key(str(e.get("version"))))
    warn(emit, f"no chart deploys Rocket.Chat {rc_version} or older; using "
               f"{newest.get('version')} (appVersion {newest.get('app_version')})",
         phase="plan")
    return str(newest["version"])


def mongo_manifest(name: str, tag: str, *, owner: str = "",
                   storage_class: str = "") -> str:
    """A single-node MongoDB replica set with a PVC.

    A replica set rather than a standalone mongod because Rocket.Chat needs change
    streams. `volumeClaimTemplates` rather than nothing, so the data survives a
    pod reschedule -- see MONGO_VOLUME_GB.

    `storageClassName` is omitted when empty, which makes Kubernetes use the
    cluster's default. That is what keeps this working unchanged on kind
    (`standard`), k3s (`local-path`) and a real cluster.
    """
    labels = _labels(name, owner)

    def at(indent: int) -> str:
        """Label block at a given indent. Every nesting level needs its own: the
        first version reused one 4-space block everywhere, and inside
        `volumeClaimTemplates` that put the labels at the same level as `metadata`,
        so they became keys of the template item. The result is valid YAML and an
        invalid manifest -- which a `yaml.safe_load` test cannot see, and the API
        server rejected with "unknown field
        spec.volumeClaimTemplates[0].app.kubernetes.io/managed-by".
        """
        pad = " " * indent
        return "\n".join(f"{pad}{k}: {v}" for k, v in labels.items())

    label_yaml = at(4)
    pod_labels = at(8)
    pvc_labels = at(8)
    sc_line = f"\n        storageClassName: {storage_class}" if storage_class else ""
    return f"""apiVersion: v1
kind: Service
metadata:
  name: {MONGO_SERVICE}
  labels:
{label_yaml}
spec:
  clusterIP: None
  selector:
    app: {MONGO_SERVICE}
  ports:
  - port: 27017
    name: mongo
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {MONGO_SERVICE}
  labels:
{label_yaml}
spec:
  serviceName: {MONGO_SERVICE}
  replicas: 1
  selector:
    matchLabels:
      app: {MONGO_SERVICE}
  template:
    metadata:
      labels:
        app: {MONGO_SERVICE}
{pod_labels}
    spec:
      containers:
      - name: mongod
        image: mongo:{tag}
        args: ["--replSet", "rs0", "--bind_ip_all"]
        ports:
        - containerPort: 27017
        volumeMounts:
        - name: data
          mountPath: /data/db
        readinessProbe:
          exec:
            command: ["mongosh", "--quiet", "--eval", "db.adminCommand('ping')"]
          initialDelaySeconds: 10
          periodSeconds: 5
  volumeClaimTemplates:
  - metadata:
      name: data
      labels:
{pvc_labels}
    spec:
      accessModes: ["ReadWriteOnce"]{sc_line}
      resources:
        requests:
          storage: {MONGO_VOLUME_GB}Gi
"""


def split_image(rc_image: str, rc_version: str) -> tuple[str, str]:
    """(repository, tag) for the chart, which wants them separately.

    `versions.resolve()` returns the REPOSITORY only -- compose composes it as
    f"{rc_image}:{rc_tag}" -- so a naive rpartition(":") on a tagless string
    returns the whole repository as the tag. That is truthy, so an `or rc_version`
    fallback does not save it, and the chart fed it to `semverCompare`, which
    failed the install with "invalid semantic version" rather than anything naming
    the image.

    A colon only introduces a tag when nothing after it looks like a path segment:
    `registry:5000/org/img` is a host with a port, not a tag. `--rc-image` lets a
    user pass either shape, so both are handled.
    """
    head, sep, tail = rc_image.rpartition(":")
    if sep and "/" not in tail:
        return head, tail
    return rc_image, rc_version


def values_for(*, rc_version: str, rc_image: str, microservices: bool,
               replicas: int = 1, root_url: str = "", oplog: bool = False) -> dict:
    """Chart values for one workspace.

    MongoDB is ALWAYS external, never the chart's bundled subchart. PR #3's reason
    holds and is worth keeping: the bundled path is Bitnami, which publishes
    amd64-only images so it cannot work on arm64 at all, and chart 7.0.2 declares
    appVersion 8.6.1 while defaulting MongoDB to 6.0.10, which Rocket.Chat 8.6.1
    rejects outright. One external path that works everywhere beats two where one
    is broken on half the hosts.
    """
    repo, tag = split_image(rc_image, rc_version)
    values: dict = {
        "image": {"repository": repo, "tag": tag},
        "replicaCount": max(1, int(replicas or 1)),
        "microservices": {"enabled": bool(microservices)},
        "mongodb": {"enabled": False},
        "externalMongodbUrl": MONGO_URL,
        "extraEnv": [],
    }
    if oplog:
        # Rocket.Chat below 8.x still wants the oplog URL; 8.x deprecates it.
        values["externalMongodbOplogUrl"] = MONGO_OPLOG_URL
    if root_url:
        values["host"] = root_url
    return values


def ensure_namespace(name: str, *, context: str, owner: str = "",
                     emit: Emit = null_emit) -> str:
    """Create the workspace's namespace with its ownership labels.

    Labels are applied on every call, not only at creation, so a namespace made by
    an older rc-repro gains them and becomes visible to teardown. A resource that
    exists but cannot be selected is worse than one that does not exist.
    """
    ns = namespace_for(name)
    run(["kubectl", "--context", context, "create", "namespace", ns],
        timeout=APPLY_TIMEOUT, own=is_ours(context))
    labels = [f"{k}={v}" for k, v in _labels(name, owner).items()]
    res = run(["kubectl", "--context", context, "label", "namespace", ns,
               *labels, "--overwrite"], timeout=APPLY_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        raise CreateFailedError(
            f"could not label namespace {ns}: "
            + (res.stderr or res.stdout or "").strip()[:200]
            + " — without the label it would be invisible to teardown")
    info(emit, f"namespace {ns}", phase="provision")
    return ns


def apply(manifest: str, *, namespace: str, context: str) -> None:
    """`kubectl apply` a manifest from stdin, so no temp file is left behind."""
    argv = ["kubectl", "--context", context, "-n", namespace, "apply", "-f", "-"]
    try:
        res = subprocess.run(argv, input=manifest, capture_output=True, text=True,
                             timeout=APPLY_TIMEOUT, check=False,
                             env=owned_env() if is_ours(context) else None)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerError(f"kubectl apply failed: {exc}") from exc
    if res.returncode != 0:
        raise CreateFailedError("kubectl apply failed: "
                                + (res.stderr or res.stdout or "").strip()[:300])


def install(*, namespace: str, context: str, values: dict,
            chart_version: str = "") -> None:
    """`helm install` with values on stdin, so nothing is written to disk.

    The release is `rocketchat` -- the official docs' own name -- so every command
    in them works here by substituting the namespace.
    """
    argv = ["helm", "install", RELEASE, CHART, "--kube-context", context,
            "-n", namespace, "--values", "-"]
    if chart_version:
        argv += ["--version", chart_version]
    try:
        res = subprocess.run(argv, input=json.dumps(values), capture_output=True,
                             text=True, timeout=INSTALL_TIMEOUT, check=False,
                             env=owned_env() if is_ours(context) else None)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerError(f"helm install failed: {exc}") from exc
    if res.returncode != 0:
        raise CreateFailedError("helm install failed: "
                                + (res.stderr or res.stdout or "").strip()[:400])


def delete_namespace(name: str, *, context: str, volumes: bool = False,
                     emit: Emit = null_emit) -> bool:
    """Remove a workspace. Returns False if there was nothing there.

    **`helm uninstall` does not delete the PVCs a StatefulSet created.** Kubernetes
    retains them deliberately. Deleting the NAMESPACE does remove them -- which is
    why `down` without `--volumes` cannot just delete the namespace, or it would
    silently destroy data that Compose's `down` keeps.

    So the two paths differ, matching Compose exactly:
      down            uninstall the release, keep the namespace and its PVCs
      down --volumes  delete the namespace, taking the PVCs with it
    """
    ns = namespace_for(name)
    own = is_ours(context)
    if ns not in workspace_namespaces(context):
        return False
    if volumes:
        info(emit, f"deleting namespace {ns} and its volumes", phase="teardown")
        res = run(["kubectl", "--context", context, "delete", "namespace", ns,
                   "--wait=false"], timeout=APPLY_TIMEOUT, own=own)
        if res.returncode != 0:
            raise DockerError(f"could not delete namespace {ns}: "
                              + (res.stderr or res.stdout or "").strip()[:200])
        return True
    info(emit, f"uninstalling {RELEASE} from {ns} — the volume is kept",
         phase="teardown")
    run(["helm", "uninstall", RELEASE, "--kube-context", context, "-n", ns],
        timeout=APPLY_TIMEOUT, own=own)
    # The hand-written MongoDB is not part of the release, so it is removed by
    # label rather than by helm -- and its PVC is deliberately left behind.
    run(["kubectl", "--context", context, "-n", ns, "delete",
         "statefulset,service", "-l", OWNER_SELECTOR], timeout=APPLY_TIMEOUT, own=own)
    return True


def workspace_pvcs(name: str, *, context: str) -> list[str]:
    """PVCs belonging to a workspace, so `prune` can prove what it reclaimed."""
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "pvc", "-l", OWNER_SELECTOR, "-o", "name"],
              own=is_ours(context))
    if res.returncode != 0:
        return []
    return [ln.split("/", 1)[-1].strip()
            for ln in (res.stdout or "").splitlines() if ln.strip()]


#: Rocket.Chat needs change streams, which need a replica set. `--replSet rs0`
#: only puts mongod IN replica-set mode; without `rs.initiate()` there is no
#: primary, so nothing can write and Rocket.Chat waits forever. Compose has a
#: `mongo-init` service for exactly this; the first cut of this module had no
#: equivalent, and a real workspace sat at 5 pods for 540s with Rocket.Chat never
#: becoming ready and nothing in its logs naming MongoDB.
RS_INITIATE = ('rs.initiate({_id:"rs0",members:[{_id:0,'
               f'host:"{MONGO_SERVICE}-0.{MONGO_SERVICE}:27017"}}]}})')
MONGO_READY_TRIES = 60
MONGO_READY_INTERVAL = 5.0


def _mongo_exec(context: str, namespace: str, script: str):
    return run(["kubectl", "--context", context, "-n", namespace, "exec",
                f"{MONGO_SERVICE}-0", "--", "mongosh", "--quiet", "--eval", script],
               timeout=APPLY_TIMEOUT, own=is_ours(context))


def init_replica_set(*, namespace: str, context: str, emit: Emit = null_emit,
                     sleep=time.sleep) -> None:
    """Wait for MongoDB, initiate the single-node replica set, and VERIFY it.

    Structure taken from PR #3, whose docstring records the failure this shape
    exists to avoid: `kubectl wait` called the instant after `apply`, before the
    pod existed, so it failed immediately and `rs.initiate` then ran against
    nothing -- with both errors discarded and the repro reported as created.

    So: poll for the pod, initiate, tolerate an already-initiated set, and check
    `rs.status().ok` rather than trusting an exit code. A failure here is
    CreateFailedError -- exit 7, known dead -- because every second spent waiting
    afterwards is spent waiting for something that cannot happen.
    """
    for attempt in range(MONGO_READY_TRIES):
        res = run(["kubectl", "--context", context, "-n", namespace, "get", "pod",
                   f"{MONGO_SERVICE}-0", "-o",
                   "jsonpath={.status.containerStatuses[0].ready}"],
                  own=is_ours(context))
        if (res.stdout or "").strip() == "true":
            break
        if attempt % 6 == 0:
            info(emit, "waiting for MongoDB", phase="wait")
        sleep(MONGO_READY_INTERVAL)
    else:
        raise CreateFailedError(
            "MongoDB did not become ready, and Rocket.Chat cannot work without it "
            f"(kubectl -n {namespace} describe pod {MONGO_SERVICE}-0)")

    info(emit, "initiating the replica set", phase="boot", pct=45)
    res = _mongo_exec(context, namespace, RS_INITIATE)
    combined = f"{res.stdout or ''}{res.stderr or ''}".lower()
    if res.returncode != 0 and "already initialized" not in combined:
        raise CreateFailedError("could not initiate the MongoDB replica set: "
                                + combined.strip()[:300])
    ok = _mongo_exec(context, namespace, "rs.status().ok")
    if (ok.stdout or "").strip() != "1":
        raise CreateFailedError(
            "the MongoDB replica set is not initiated, so Rocket.Chat's change "
            "streams cannot work: " + (ok.stdout or ok.stderr or "").strip()[:300])
    info(emit, "replica set ready", phase="boot")


def port_forward(name: str, *, namespace: str, context: str, host_port: int) -> int:
    """Publish a workspace on a host port. Returns the pid, or 0.

    Detached with `start_new_session`, so it outlives the `up` that started it --
    the workspace has to stay reachable after the command returns, exactly as a
    published Compose port does.

    A port-forward dies with its pod, which is a real difference from Compose and
    is why `start` re-establishes it rather than assuming.
    """
    argv = ["kubectl", "--context", context, "-n", namespace, "port-forward",
            f"svc/{RELEASE}-rocketchat", f"{host_port}:80"]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                env=owned_env() if is_ours(context) else None)
    except (OSError, subprocess.SubprocessError):
        return 0
    return proc.pid


def create_workspace(*, name: str, resolved, host_port: int, microservices: bool,
                     replicas: int = 1, owner: str = "", root_url: str = "",
                     emit: Emit = null_emit) -> dict:
    """Build a Kubernetes workspace, and return what repro.json needs.

    A PARALLEL path to lifecycle's compose one rather than a refactor of it, which
    is PR #3's call and the right one: lifecycle.py is compose-shaped throughout
    and two front-ends depend on it, so the Docker default stays byte-identical
    and this owns the Kubernetes sequence instead. Naming, version resolution and
    metadata are shared, not reimplemented.

    Ordered so nothing waits on something that cannot happen: the replica set is
    initiated BEFORE the chart goes in, because Rocket.Chat needs change streams
    and would otherwise sit at not-ready with nothing in its logs naming MongoDB.
    """
    context = ensure_cluster(emit=emit)
    ensure_repo(emit=emit)
    chart_version = resolve_chart_version(resolved.rc_version, emit=emit)
    info(emit, f"chart {chart_version} for Rocket.Chat {resolved.rc_version}",
         phase="plan")

    pre = preflight(context)
    blocked = storage_blocker(pre)
    if blocked:
        raise PreflightError(blocked)

    namespace = ensure_namespace(name, context=context, owner=owner, emit=emit)
    info(emit, f"MongoDB {resolved.mongo_tag}, {MONGO_VOLUME_GB}Gi volume",
         phase="provision", pct=20)
    apply(mongo_manifest(name, resolved.mongo_tag, owner=owner),
          namespace=namespace, context=context)
    init_replica_set(namespace=namespace, context=context, emit=emit)

    values = values_for(rc_version=resolved.rc_version, rc_image=resolved.rc_image,
                        microservices=microservices, replicas=replicas,
                        root_url=root_url, oplog=resolved.oplog)
    info(emit, f"installing {CHART} as {RELEASE}", phase="boot", pct=60)
    install(namespace=namespace, context=context, values=values,
            chart_version=chart_version)

    pid = port_forward(name, namespace=namespace, context=context,
                       host_port=host_port)
    info(emit, f"http://localhost:{host_port}", phase="boot", pct=90)
    return {"context": context, "namespace": namespace,
            "chart_version": chart_version, "release": RELEASE,
            "port_forward_pid": pid, "microservices": microservices}


def workspace_ready(name: str, *, context: str) -> bool:
    """Whether the Rocket.Chat pod reports itself Ready."""
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "pod", "-l", "app.kubernetes.io/name=rocketchat", "-o",
               "jsonpath={.items[0].status.containerStatuses[0].ready}"],
              own=is_ours(context))
    return (res.stdout or "").strip() == "true"
