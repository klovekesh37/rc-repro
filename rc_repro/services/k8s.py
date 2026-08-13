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
from dataclasses import dataclass, field
from pathlib import Path

from rc_repro import config, runner
from rc_repro.errors import (ConflictError, CreateFailedError, DockerError,
                            PreflightError)
from rc_repro.services.events import Emit, info, null_emit

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
