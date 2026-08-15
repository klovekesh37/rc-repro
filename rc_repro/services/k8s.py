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
import secrets
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


def why(res, *, limit: int = 400) -> str:
    """The line that explains a failure, not the first 400 characters of noise.

    Written after a `helm install` failure reported two harmless warnings and hid
    its own reason. Every tool here puts its diagnostics FIRST and its error LAST --
    helm emits klog warnings then `Error: ...`, kubectl emits deprecation notices
    then the message -- so taking a prefix reliably shows the least useful part.

    Prefers a line that announces itself as an error; falls back to the last
    non-empty line, which is where these tools put it.
    """
    text = f"{getattr(res, 'stderr', '') or ''}\n{getattr(res, 'stdout', '') or ''}"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "no reason given"
    named = [ln for ln in lines
             if re.match(r"^(error|Error|ERROR|fatal|Fatal)\b", ln)
             or "error:" in ln.lower()]
    # Warnings are not errors, however loudly they are printed. klog prefixes them
    # with I/W and a timestamp, and helm's install failure sits after them.
    named = [ln for ln in named if not re.match(r"^[IWD]\d{4} ", ln)]
    chosen = named[-1] if named else lines[-1]
    return chosen[:limit]


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
        return [], why(res, limit=120)
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
                    raise CreateFailedError(
                        f"could not create the cluster {CLUSTER_NAME}: "
                        + why(res))
    # ALWAYS export, not only after creating. `kind get clusters` reads Docker, so
    # it sees a cluster that rc-repro's OWN kubeconfig knows nothing about -- which
    # happens whenever the cluster outlives the kubeconfig: a different or fresh
    # RC_REPRO_HOME, a deleted config, or a cluster someone made by hand. Then
    # "reusing cluster" is followed by every kubectl call going to localhost:8080
    # and the create failing with "the API server is not answering" about a cluster
    # that is perfectly healthy.
    #
    # PR #3 does this and says why: "refresh the owned kubeconfig for both new and
    # pre-existing clusters". I ported the create and left the export behind.
    export = run(["kind", "export", "kubeconfig", "--name", CLUSTER_NAME,
                  "--kubeconfig", str(kubeconfig)], timeout=APPLY_TIMEOUT, own=True)
    if export.returncode != 0:
        raise CreateFailedError(
            f"cluster {CLUSTER_NAME} exists but its kubeconfig could not be read: "
            + why(export))
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
            raise DockerError(f"could not delete the cluster {CLUSTER_NAME}: "
                              + why(res))
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

#: How mongosh must connect BEFORE the replica set is initiated. Without
#: `directConnection=true` it tries to discover the set's topology, finds no
#: primary, and fails its handshake with "ReadConcernMajorityNotAvailableYet".
#: `compose.py` already knew this -- "directConnection=true lets mongosh reach the
#: node before rs.initiate" -- and the first cut of this module did not.
MONGO_DIRECT_URI = "mongodb://localhost:27017/?directConnection=true"

#: How long to wait for the Rocket.Chat pod to EXIST before forwarding to it.
POD_WAIT_TRIES = 40
POD_WAIT_INTERVAL = 3.0

#: How long to watch a namespace actually go away. Deleting one is not instant:
#: pods drain, finalizers run, and the PVC goes last.
NS_GONE_TRIES = 24
NS_GONE_INTERVAL = 2.5

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
            f"({HELM_REPO_URL}): " + why(res))


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
                                + why(res))
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
  # The same circular dependency as the readiness probe, one layer down. A headless
  # Service does NOT publish DNS for a not-ready pod, and this pod cannot be ready
  # until the replica set is initiated -- so `rs.initiate` could not resolve
  # `{MONGO_SERVICE}-0.{MONGO_SERVICE}` to check the member is itself, and mongod
  # refused with "no host described in new configuration ... maps to this node".
  # Publishing not-ready addresses is the standard bootstrap pattern for a
  # StatefulSet database; the MongoDB operator does the same.
  publishNotReadyAddresses: true
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
            command: ["mongosh", "{MONGO_DIRECT_URI}", "--quiet", "--eval",
                      "db.adminCommand('ping')"]
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
               replicas: int = 1, root_url: str = "", oplog: bool = False,
               mongo_url: str = "", oplog_url: str = "") -> dict:
    """Chart values for one workspace.

    MongoDB is ALWAYS external, never the chart's bundled subchart. PR #3's reason
    holds and is worth keeping: the bundled path is Bitnami, which publishes
    amd64-only images so it cannot work on arm64 at all, and chart 7.0.2 declares
    appVersion 8.6.1 while defaulting MongoDB to 6.0.10, which Rocket.Chat 8.6.1
    rejects outright. One external path that works everywhere beats two where one
    is broken on half the hosts.
    """
    repo, tag = split_image(rc_image, rc_version)
    # THE WORKSPACE HAS TO BE USABLE ON ARRIVAL, which is what these five give and
    # what Kubernetes was missing entirely. `up` printed "Login admin / admin123"
    # for a workspace where that user had never been created and the setup wizard
    # was still waiting -- confidently stated credentials that did not work, which
    # is worse than printing nothing.
    #
    # Same set and same reasoning as compose.py: skip the wizard, auto-provision the
    # first admin.
    #
    # DEPLOY_METHOD and DEPLOY_PLATFORM are NOT here, and were, for one commit. The
    # chart sets them itself -- it knows perfectly well it is helm on Kubernetes --
    # and a second entry for the same key is not an override but a conflict:
    #
    #   Error: INSTALLATION FAILED: server-side apply failed ... .spec.template
    #   .spec.containers[name="rocketchat"].env: duplicate entries for key
    #   [name="DEPLOY_PLATFORM"]
    #
    # So anything the chart already provides must not be repeated here. `extraEnv`
    # is for what the chart does NOT know: rc-repro's fixed admin and the skipped
    # wizard. That is the line to check before adding to this list.
    env: list[dict] = [
        {"name": "OVERWRITE_SETTING_Show_Setup_Wizard", "value": "completed"},
        {"name": "INITIAL_USER", "value": "yes"},
        {"name": "ADMIN_USERNAME", "value": config.ADMIN_USERNAME},
        {"name": "ADMIN_NAME", "value": config.ADMIN_NAME},
        {"name": "ADMIN_EMAIL", "value": config.ADMIN_EMAIL},
        {"name": "ADMIN_PASS", "value": config.ADMIN_PASSWORD},
        {"name": "ALLOW_UNSAFE_QUERY_AND_FIELDS_API_PARAMS", "value": "true"},
    ]
    values: dict = {
        # pullPolicy and the NATS cluster name are the guide's own values.yaml.
        # IfNotPresent also matters here specifically: a repro box re-creates
        # workspaces on the same versions constantly, and Always would re-pull
        # 1.6 GB each time.
        "image": {"repository": repo, "tag": tag, "pullPolicy": "IfNotPresent"},
        "nats": {"cluster": {"name": "rocketchat-nats-cluster"}},
        "replicaCount": max(1, int(replicas or 1)),
        "microservices": {"enabled": bool(microservices)},
        "mongodb": {"enabled": False},
        "externalMongodbUrl": mongo_url or MONGO_URL,
        "extraEnv": env,
    }
    if oplog:
        # Rocket.Chat below 8.x still wants the oplog URL; 8.x deprecates it.
        values["externalMongodbOplogUrl"] = oplog_url or MONGO_OPLOG_URL
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
            + why(res)
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
                                + why(res))


def install(*, namespace: str, context: str, values: dict,
            chart_version: str = "") -> None:
    """`helm install` with values on stdin, so nothing is written to disk.

    The release is `rocketchat` -- the official docs' own name -- so every command
    in them works here by substituting the namespace.
    """
    # `upgrade --install`, not `install`. Bringing a workspace back up re-runs this
    # sequence, and plain `install` fails on a release that is already there --
    # which is every `up` over an existing workspace, and every retry after a
    # partial failure.
    argv = ["helm", "upgrade", "--install", RELEASE, CHART,
            "--kube-context", context, "-n", namespace, "--values", "-"]
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
                                + why(res))


def delete_namespace(name: str, *, context: str, volumes: bool = False,
                     emit: Emit = null_emit, sleep=time.sleep) -> bool:
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
        # Reported as it happens, and WAITED for. `--wait=false` returned instantly
        # and `down` said "removed" while the namespace was still Terminating, the
        # pods still shutting down and the PVC still there -- so `rc-repro up` with
        # the same name could race a half-deleted namespace, and anyone checking
        # with kubectl saw the opposite of what they had just been told.
        pvcs = workspace_pvcs(name, context=context)
        info(emit, f"deleting namespace {ns}: {len(pvcs)} volume(s), "
                   "pods and the release", phase="teardown", pct=10)
        res = run(["kubectl", "--context", context, "delete", "namespace", ns,
                   "--wait=false"], timeout=APPLY_TIMEOUT, own=own)
        if res.returncode != 0:
            raise DockerError(f"could not delete namespace {ns}: "
                              + why(res))
        for attempt in range(NS_GONE_TRIES):
            check = run(["kubectl", "--context", context, "get", "namespace", ns,
                         "-o", "jsonpath={.status.phase}"], own=own)
            phase = (check.stdout or "").strip()
            if check.returncode != 0 or not phase:
                info(emit, f"namespace {ns} and its volume(s) are gone",
                     phase="teardown", pct=100)
                return True
            if attempt % 4 == 0:
                info(emit, f"namespace {ns} is {phase} — waiting for the pods and "
                           "volume(s) to go", phase="teardown",
                     pct=min(90, 20 + attempt * 5))
            sleep(NS_GONE_INTERVAL)
        # Not an error: Kubernetes will finish on its own. Saying so beats either
        # blocking forever or claiming it is done.
        warn(emit, f"namespace {ns} is still terminating. Kubernetes will finish; "
                   f"check with `kubectl get ns {ns}`.", phase="teardown")
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


def _mongo_exec(context: str, namespace: str, script: str, *, tries: int = 5,
               sleep=time.sleep):
    """mongosh inside the MongoDB pod, retried.

    `containerStatuses[0].started` is still true for a pod that is TERMINATING, so
    a re-create over an existing namespace can pass the wait and then exec into a
    pod on its way out -- `unable to upgrade connection: container not found
    ("mongod")`. A one-shot exec turns that race into a failed create; retrying
    turns it into a two-second delay.
    """
    for attempt in range(tries):
        res = _mongo_exec_once(context, namespace, script)
        combined = f"{res.stdout or ''}{res.stderr or ''}".lower()
        if res.returncode == 0 or "container not found" not in combined:
            return res
        sleep(2.0)
    return res


def _mongo_exec_once(context: str, namespace: str, script: str):
    return run(["kubectl", "--context", context, "-n", namespace, "exec",
                f"{MONGO_SERVICE}-0", "--", "mongosh", MONGO_DIRECT_URI,
                "--quiet", "--eval", script],
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
    # RUNNING, not READY -- and the difference is a circular wait. The readiness
    # probe cannot pass until the replica set is initiated, and initiation cannot
    # run until something answers. Waiting on readiness here made the whole create
    # a coin flip: the probe occasionally scraped through and the workspace built,
    # otherwise it timed out at 300s reporting that MongoDB never became ready --
    # while mongod had in fact been up the whole time, logging
    # "ReadConcernMajorityNotAvailableYet".
    for attempt in range(MONGO_READY_TRIES):
        # `started`, not `phase`. A pod reports Running before its container is
        # exec-able, and `kubectl exec` then fails with `container not found
        # ("mongod")` -- which is what `up` after a plain `down` hit: the PVC was
        # still there, the StatefulSet came back, the pod said Running, and the exec
        # raced the container.
        #
        # Not `ready` either: readiness needs the replica set initiated, and this IS
        # the initiation. `started` is the field that means "the container is up",
        # independent of whether it is serving.
        res = run(["kubectl", "--context", context, "-n", namespace, "get", "pod",
                   f"{MONGO_SERVICE}-0", "-o",
                   "jsonpath={.status.containerStatuses[0].started}"],
                  own=is_ours(context))
        if (res.stdout or "").strip() == "true":
            break
        if attempt % 6 == 0:
            info(emit, "waiting for MongoDB", phase="wait")
        sleep(MONGO_READY_INTERVAL)
    else:
        raise CreateFailedError(
            "the MongoDB pod never started, and Rocket.Chat cannot work without it "
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
            "streams cannot work: " + why(ok))
    info(emit, "replica set ready", phase="boot")


def port_forward(name: str, *, namespace: str, context: str, host_port: int,
                 bind_host: str = "", emit: Emit = null_emit,
                 sleep=time.sleep) -> int:
    """Publish a workspace on a host port. Returns the pid, or 0.

    Detached with `start_new_session`, so it outlives the `up` that started it --
    the workspace has to stay reachable after the command returns, exactly as a
    published Compose port does.

    **Forwards to the DEPLOYMENT, not the Service.** `port-forward svc/...` needs a
    ready ENDPOINT, and a Service has none until its pod passes readiness -- so the
    first version started the forward immediately after `helm install`, kubectl
    found nothing to attach to and exited, and the URL `up` printed never answered.
    A deployment target only needs a pod to exist, which is the difference between
    a URL that works and one that lies.

    It still waits for that pod, briefly. A forward started before the ReplicaSet
    has created anything exits just the same.

    A port-forward dies with its pod, which is a real difference from Compose --
    hence `ensure_port_forward`, which `ready` and `start` use to re-establish it.
    """
    # RUNNING, not merely existing. `kubectl port-forward` refuses a pod that is
    # still ContainerCreating ("unable to forward port because pod is not
    # running") and exits, which is precisely what happened when this waited only
    # for a pod NAME: the forward was spawned a second after `helm install`, died
    # at once, and the URL never answered. Existence is not reachability.
    for attempt in range(POD_WAIT_TRIES):
        res = run(["kubectl", "--context", context, "-n", namespace, "get", "pod",
                   "-l", "app.kubernetes.io/name=rocketchat", "-o",
                   "jsonpath={.items[0].status.phase}"], own=is_ours(context))
        if (res.stdout or "").strip() == "Running":
            break
        if attempt % 4 == 0:
            info(emit, "waiting for the Rocket.Chat pod", phase="wait")
        sleep(POD_WAIT_INTERVAL)
    # `--bind` was accepted and then dropped on this path, which is worse than
    # refusing it: a workspace created with `--bind 0.0.0.0` on a shared box was
    # reachable only from localhost, and nothing said so. kubectl binds 127.0.0.1
    # by default and takes --address, so this is a real setting rather than a
    # Compose-only one.
    argv = ["kubectl", "--context", context, "-n", namespace, "port-forward"]
    if bind_host and bind_host not in ("127.0.0.1", "localhost"):
        argv += ["--address", bind_host]
    argv += [f"deployment/{RELEASE}-rocketchat", f"{host_port}:3000"]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                env=owned_env() if is_ours(context) else None)
    except (OSError, subprocess.SubprocessError):
        return 0
    return proc.pid


def create_workspace(*, name: str, resolved, host_port: int, microservices: bool,
                     replicas: int = 1, owner: str = "", root_url: str = "",
                     bind_host: str = "", use_operator: bool = False,
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
    had_cluster = CLUSTER_NAME in clusters()[0]
    context = ensure_cluster(emit=emit)
    ensure_repo(emit=emit)
    chart_version = resolve_chart_version(resolved.rc_version, emit=emit)
    info(emit, f"chart {chart_version} for Rocket.Chat {resolved.rc_version}",
         phase="plan")

    pre = preflight(context)
    blocked = storage_blocker(pre)
    if blocked:
        raise PreflightError(blocked)

    # Whether the namespace was already here decides what a failure may undo. A
    # re-create over a namespace kept by `down` must NOT have its rollback delete
    # that namespace: the PVC goes with it, and `down` had just promised the data
    # would be there. Observed exactly that way -- a marker document written before
    # `down` was gone after a failed `up`.
    had_namespace = namespace_for(name) in workspace_namespaces(context)
    namespace = ensure_namespace(name, context=context, owner=owner, emit=emit)
    try:
        # Which MongoDB, keyed on the VERSION -- the same shape `mongo_flavor`
        # already uses on the Compose side ("official" >= 8, "bitnami-legacy"
        # below). The operator brings SCRAM auth and owns the bootstrap that cost
        # four live failures by hand; it does not reach the old half of the twelve
        # versions rc-repro pairs, so the hand-written StatefulSet stays for those.
        mongo_url = MONGO_URL
        oplog_url = MONGO_OPLOG_URL
        if (use_operator or operator_enabled()) and \
                operator_supports(resolved.mongo_tag, forced=use_operator):
            ensure_operator(context=context, emit=emit)
            # Generated per workspace, never reused, never written to disk here:
            # the manifest goes to `kubectl apply` on stdin.
            app_password = secrets.token_urlsafe(24)
            info(emit, f"MongoDB {resolved.mongo_tag} via the operator, SCRAM auth, "
                       f"{MONGO_VOLUME_GB}Gi volume", phase="provision", pct=20)
            apply(mongo_rbac_manifest(name, owner=owner),
                  namespace=namespace, context=context)
            apply(mongo_secret_manifest(name, admin_password=secrets.token_urlsafe(24),
                                        app_password=app_password, owner=owner),
                  namespace=namespace, context=context)
            apply(mongodb_community_manifest(name, resolved.mongo_tag, owner=owner),
                  namespace=namespace, context=context)
            wait_for_mongodb(namespace=namespace, context=context, emit=emit)
            mongo_url = operator_mongo_url(namespace, app_password)
            oplog_url = operator_mongo_url(namespace, app_password, oplog=True)
        else:
            info(emit, f"MongoDB {resolved.mongo_tag}, {MONGO_VOLUME_GB}Gi volume "
                       "(no operator below "
                       f"{'.'.join(str(n) for n in OPERATOR_MIN_MONGO)}, so no auth)",
                 phase="provision", pct=20)
            apply(mongo_manifest(name, resolved.mongo_tag, owner=owner),
                  namespace=namespace, context=context)
            init_replica_set(namespace=namespace, context=context, emit=emit)

        values = values_for(rc_version=resolved.rc_version,
                            rc_image=resolved.rc_image,
                            microservices=microservices, replicas=replicas,
                            root_url=root_url, oplog=resolved.oplog,
                            mongo_url=mongo_url, oplog_url=oplog_url)
        info(emit, f"installing {CHART} as {RELEASE}", phase="boot", pct=60)
        install(namespace=namespace, context=context, values=values,
                chart_version=chart_version)
    except Exception:
        # A failed create must not leave anything a user cannot see. No repro.json
        # is written until this returns, so a surviving namespace would be
        # invisible to `list` and to `down` -- and on Compose a failed `up` at
        # least leaves a workspace directory you can find. The cluster goes too if
        # this call created it and nothing else is using it: `delete_cluster`
        # refuses while any other workspace namespace is there, so a colleague's
        # concurrent `up` is safe from this.
        if had_namespace:
            # Leave it. The data in it predates this call and is not ours to
            # discard on a failure -- `down --volumes` is how a user asks for that,
            # deliberately and with a confirmation.
            warn(emit, f"create failed — namespace {namespace} and its volume are "
                       "left as they were", phase="teardown")
            raise
        warn(emit, f"create failed — removing namespace {namespace}",
             phase="teardown")
        try:
            run(["kubectl", "--context", context, "delete", "namespace", namespace,
                 "--wait=false"], timeout=APPLY_TIMEOUT, own=is_ours(context))
            if not had_cluster:
                delete_cluster(emit=emit)
        except Exception:  # noqa: BLE001 - the original failure is what matters
            pass
        raise

    pid = port_forward(name, namespace=namespace, context=context,
                       host_port=host_port, bind_host=bind_host, emit=emit)
    # Confirmed, not assumed. A port-forward that died on spawn leaves a URL that
    # looks like an address and answers nothing, which is worse than no URL -- it
    # sends someone to debug Rocket.Chat when the forward is what failed. If it is
    # not alive, say so and hand over the command that establishes one.
    time.sleep(1.0)
    if forward_alive(pid):
        info(emit, f"http://localhost:{host_port}", phase="boot", pct=90)
    else:
        pid = 0
        warn(emit, f"the port-forward did not stay up, so "
                   f"http://localhost:{host_port} is not reachable yet. Once the "
                   f"pod is running: kubectl -n {namespace} port-forward "
                   f"deployment/{RELEASE}-rocketchat {host_port}:3000 "
                   f"— or `rc-repro ready --name {name}` to re-establish it.",
             phase="boot")
    return {"context": context, "namespace": namespace, "bind_host": bind_host,
            "chart_version": chart_version, "release": RELEASE,
            "port_forward_pid": pid, "microservices": microservices}


def workspace_ready(name: str, *, context: str) -> bool:
    """Whether the Rocket.Chat pod reports itself Ready."""
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "pod", "-l", "app.kubernetes.io/name=rocketchat", "-o",
               "jsonpath={.items[0].status.containerStatuses[0].ready}"],
              own=is_ours(context))
    return (res.stdout or "").strip() == "true"


def forward_alive(pid: int | None) -> bool:
    """Whether a recorded port-forward is still running and still ours.

    A pid alone is not enough: the OS recycles them, so this confirms the process
    is still a kubectl port-forward before believing it. The same check keeps
    teardown from signalling an unrelated process.
    """
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().decode("utf-8", "replace")
    except (OSError, ValueError, TypeError):
        return False
    return "port-forward" in cmdline


def ensure_port_forward(name: str, *, namespace: str, context: str, host_port: int,
                        pid: int | None = None, bind_host: str = "",
                        emit: Emit = null_emit) -> int:
    """Return a live port-forward pid, starting one if the recorded one is gone.

    A forward dies with its pod, so `ready` and `start` call this rather than
    assuming the one written at create time is still there. Idempotent: an
    already-live forward is left alone rather than duplicated onto a busy port.
    """
    if forward_alive(pid):
        return int(pid)
    return port_forward(name, namespace=namespace, context=context,
                        host_port=host_port, bind_host=bind_host, emit=emit)


def workload_exists(name: str, *, context: str) -> bool:
    """Whether the Rocket.Chat deployment is present in the workspace's namespace.

    Distinct from the namespace existing, which survives a plain `down` along with
    the PVC. Without this a torn-down workspace reported "starting" indefinitely,
    because the only thing being asked was whether the namespace was there.
    """
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "deployment", f"{RELEASE}-rocketchat", "-o", "name"],
              own=is_ours(context))
    return res.returncode == 0 and bool((res.stdout or "").strip())


# --- MongoDB via the official operator -------------------------------------------

#: The operator lives ONCE per cluster, in rc-repro's own namespace, watching all
#: namespaces. The official guide installs it into the workspace namespace because
#: it assumes one Rocket.Chat per cluster; its CRDs are cluster-scoped, so a second
#: per-namespace install collides on them rather than giving you a second operator.
OPERATOR_NAMESPACE = "rc-repro-system"
OPERATOR_RELEASE = "mongodb-kubernetes-operator"
OPERATOR_REPO = "mongodb"
OPERATOR_REPO_URL = "https://mongodb.github.io/helm-charts"
OPERATOR_CHART = f"{OPERATOR_REPO}/mongodb-kubernetes"

#: The oldest MongoDB the operator will manage. Below this rc-repro falls back to
#: the hand-written StatefulSet, which is why that code stays: rc-repro pairs twelve
#: MongoDB versions (3.0 through 8.2) because "the customer's exact version" is the
#: product's promise, and the operator's window does not reach the old half.
#:
#: 6.0 is deliberately conservative and SHOULD BE VERIFIED against the operator's own
#: documentation before anyone relies on the boundary -- picking it too low means a
#: workspace that never starts, which is a worse failure than falling back.
OPERATOR_MIN_MONGO = (6, 0)

#: SCRAM users, matching the guide's names so its commands transfer.
MONGO_ADMIN_USER = "admin"
MONGO_APP_USER = "rocketchat"
MONGO_APP_DB = "rocketchat"

#: The ServiceAccount the operator puts on the database pod. Not configurable and
#: not namespaced to the operator: the StatefulSet the operator writes into OUR
#: namespace names this account, so it has to exist THERE, not where the operator
#: runs. `kubectl -n <ns> get sts mongodb -o jsonpath='{.spec.template.spec.serviceAccountName}'`
#: is where the name comes from.
MONGO_DB_SERVICE_ACCOUNT = "mongodb-kubernetes-appdb"


#: The operator is OPT-IN until it is proven, and the reason is a regression I
#: caused. The hand-written StatefulSet was verified end to end on a live cluster --
#: admin login, PVC Bound, data surviving a down/up cycle. Routing MongoDB 6.0+ to
#: the operator by default replaced that with a path whose PVC never binds:
#:
#:     it reports: Pending ReplicaSet is not yet ready, retrying in 10 seconds
#:     data-volume-mongodb-0   Pending
#:
#: A default that breaks the working case is worse than a missing feature, so the
#: operator waits behind this flag until a live run says otherwise. Auth is the
#: thing it buys and auth is still absent without it -- that is the honest trade,
#: and it is recorded here rather than in a plan nobody reads.
USE_OPERATOR_ENV = "RC_REPRO_MONGO_OPERATOR"


def operator_enabled() -> bool:
    """Whether to use the operator at all. Off by default -- see USE_OPERATOR_ENV."""
    return os.environ.get(USE_OPERATOR_ENV, "").strip().lower() in ("1", "true", "yes")


def operator_supports(mongo_tag: str, *, forced: bool = False) -> bool:
    """Whether the operator will manage this MongoDB version.

    `forced` is `--mongo-operator`: the user asked for it explicitly, so the opt-in
    switch is satisfied and only the VERSION floor still applies. Asking for the
    operator on MongoDB 5.0 still falls back, because the operator cannot manage it
    -- silently ignoring the flag would be worse than the fallback.
    """
    if not forced and not operator_enabled():
        return False
    try:
        parts = tuple(int(n) for n in str(mongo_tag).split(".")[:2])
    except (ValueError, TypeError):
        return False
    return len(parts) == 2 and parts >= OPERATOR_MIN_MONGO


def mongo_rbac_manifest(name: str, *, owner: str = "") -> str:
    """The database pod's ServiceAccount, Role and RoleBinding, in OUR namespace.

    The guide never mentions these because it installs the operator into the same
    namespace as MongoDB, where the operator's own chart creates them. rc-repro
    cannot do that -- the chart owns cluster-scoped CRDs, so a per-workspace install
    collides on them at the second workspace -- so the operator lives once in
    `rc-repro-system` and watches everything. That works for reconciliation and
    silently does not work for the database pod, which needs an account the chart
    only created next to itself.

    Missing, the failure names none of this. The StatefulSet appears, the claims
    appear, and then:

        Warning  FailedCreate  statefulset/mongodb  Create Pod mongodb-0 ... failed
        error: pods "mongodb-0" is forbidden: error looking up service account
        <ns>/mongodb-kubernetes-appdb: serviceaccount ... not found

    is an event on the StatefulSet, while the MongoDBCommunity resource -- the thing
    anyone would look at -- reports only "Pending ReplicaSet is not yet ready,
    retrying in 10 seconds", forever. No pod is ever created, so `WaitForFirstConsumer`
    keeps both PVCs Pending, which is the symptom that shows up first and points at
    storage, which is not the problem.

    The rules are copied from the Role the operator's chart creates for itself; they
    are what a member pod needs and nothing more -- read its own password Secret, and
    patch/delete/get its own Pod to mark readiness and roll itself. Everything here
    is namespaced, so `down` removing the namespace removes it.
    """
    labels = _labels(name, owner)
    lab = "\n".join(f"    {k}: {v}" for k, v in labels.items())
    return f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: {MONGO_DB_SERVICE_ACCOUNT}
  labels:
{lab}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {MONGO_DB_SERVICE_ACCOUNT}
  labels:
{lab}
rules:
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["patch", "delete", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {MONGO_DB_SERVICE_ACCOUNT}
  labels:
{lab}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {MONGO_DB_SERVICE_ACCOUNT}
subjects:
- kind: ServiceAccount
  name: {MONGO_DB_SERVICE_ACCOUNT}
"""


def mongo_secret_manifest(name: str, *, admin_password: str, app_password: str,
                          owner: str = "") -> str:
    """The guide's five Secrets, with generated passwords rather than placeholders.

    `stringData` so Kubernetes does the base64; writing it ourselves is one more
    place to get wrong. The passwords never touch disk on this side -- the manifest
    goes to `kubectl apply` on stdin.
    """
    labels = _labels(name, owner)
    lab = "\n".join(f"    {k}: {v}" for k, v in labels.items())
    def secret(sname: str, data: dict) -> str:
        body = "\n".join(f"  {k}: {v}" for k, v in data.items())
        return (f"---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: {sname}\n"
                f"  labels:\n{lab}\ntype: Opaque\nstringData:\n{body}\n")
    return "".join([
        secret("mongodb-admin-password", {"password": admin_password}),
        secret("mongodb-rocketchat-password", {"password": app_password}),
        secret("admin-scram-credentials",
               {"username": MONGO_ADMIN_USER, "password": admin_password}),
        secret("rocketchat-scram-credentials",
               {"username": MONGO_APP_USER, "password": app_password}),
    ])


def operator_version(tag: str) -> str:
    """A full MongoDB release version for the operator's `spec.version`.

    rc-repro carries a Docker TAG -- "8.0" -- because that is what pulls an image.
    The operator wants a RELEASE version and the guide shows "8.0.0"; given "8.0" it
    accepts the resource and then never reconciles it, so the workspace sits at no
    `.status.phase` until the wait gives up. The failure names nothing: the resource
    exists, the operator is healthy, and MongoDB simply never appears.

    A tag that already has three parts is passed through, so an explicit
    `--mongo 8.0.4` still means 8.0.4.
    """
    parts = str(tag).split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def mongodb_community_manifest(name: str, tag: str, *, owner: str = "",
                               storage_gb: int = MONGO_VOLUME_GB) -> str:
    """The guide's MongoDBCommunity resource.

    `type: ReplicaSet` with `members: 1` is what makes this worth doing: the
    operator owns initiation, readiness and the bootstrap DNS -- the three things
    that produced four separate live failures when this module did them by hand.
    """
    labels = _labels(name, owner)
    lab = "\n".join(f"    {k}: {v}" for k, v in labels.items())
    return f"""apiVersion: mongodbcommunity.mongodb.com/v1
kind: MongoDBCommunity
metadata:
  name: {MONGO_SERVICE}
  labels:
{lab}
spec:
  members: 1
  type: ReplicaSet
  version: "{operator_version(tag)}"
  security:
    authentication:
      modes: ["SCRAM"]
    tls:
      enabled: false
  users:
  - name: {MONGO_ADMIN_USER}
    db: admin
    passwordSecretRef:
      name: mongodb-admin-password
    scramCredentialsSecretName: admin-scram-credentials
    roles:
    - name: root
      db: admin
  - name: {MONGO_APP_USER}
    db: {MONGO_APP_DB}
    passwordSecretRef:
      name: mongodb-rocketchat-password
    scramCredentialsSecretName: rocketchat-scram-credentials
    roles:
    - name: readWrite
      db: {MONGO_APP_DB}
  statefulSet:
    spec:
      volumeClaimTemplates:
      - metadata:
          name: data-volume
        spec:
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: {storage_gb}Gi
      # `logs-volume` as well as `data-volume`, because overriding
      # volumeClaimTemplates REPLACES the operator's list rather than merging with
      # it -- confirmed by watching a run that declared only data-volume produce
      # exactly one PVC -- while both of the pod's containers mount both volumes:
      #
      #   mongod:         data-volume ... logs-volume ...
      #   mongodb-agent:  data-volume ... logs-volume ...
      #
      # so a template list that drops one leaves the pod referencing a claim nothing
      # creates. The guide shows only data-volume because its example is the whole
      # override, and it is reproduced here in full for the same reason.
      - metadata:
          name: logs-volume
        spec:
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 2Gi
"""


def operator_mongo_url(namespace: str, password: str, *, oplog: bool = False) -> str:
    """The authenticated URI, built as the guide builds it.

    The operator names its service `<name>-svc`, not `<name>` -- a detail worth
    stating, because the hand-written path uses the bare name and the two are not
    interchangeable.
    """
    db = "local" if oplog else MONGO_APP_DB
    auth = "" if oplog else f"&authSource={MONGO_APP_DB}"
    return (f"mongodb://{MONGO_APP_USER}:{password}@"
            f"{MONGO_SERVICE}-0.{MONGO_SERVICE}-svc.{namespace}.svc.cluster.local:27017/"
            f"{db}?replicaSet={MONGO_SERVICE}{auth}")


def ensure_operator(*, context: str, emit: Emit = null_emit) -> None:
    """Install the MongoDB operator once for the cluster, watching all namespaces.

    Cluster-scoped by necessity, not by preference: the chart manages CRDs, and CRDs
    are not namespaced. Installing it per workspace collides on them at the second
    workspace, which is why the guide's placement -- inside the Rocket.Chat namespace
    -- cannot be followed literally by a tool that runs several at once.

    `upgrade --install` so a cluster that already has it is not an error, which is
    every workspace after the first.
    """
    run(["helm", "repo", "add", OPERATOR_REPO, OPERATOR_REPO_URL, "--force-update"],
        timeout=APPLY_TIMEOUT, own=is_ours(context))
    run(["helm", "repo", "update", OPERATOR_REPO], timeout=APPLY_TIMEOUT,
        own=is_ours(context))
    info(emit, f"MongoDB operator in {OPERATOR_NAMESPACE} (once per cluster)",
         phase="provision", pct=15)
    res = run(["helm", "upgrade", "--install", OPERATOR_RELEASE, OPERATOR_CHART,
               "--kube-context", context, "-n", OPERATOR_NAMESPACE,
               "--create-namespace", "--set", "operator.watchNamespace=*",
               "--wait", "--timeout", "5m"],
              timeout=INSTALL_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        raise CreateFailedError("could not install the MongoDB operator: " + why(res))


def wait_for_mongodb(*, namespace: str, context: str, emit: Emit = null_emit,
                     sleep=time.sleep) -> None:
    """Wait for the operator to report the MongoDBCommunity Running.

    The operator owns initiation and readiness, so this asks IT rather than poking
    mongod -- which is the whole point of using it. `.status.phase` is the resource's
    own answer; there is no exec, no rs.initiate and no readiness probe to race.
    """
    for attempt in range(MONGO_READY_TRIES):
        res = run(["kubectl", "--context", context, "-n", namespace, "get",
                   "mongodbcommunity", MONGO_SERVICE, "-o",
                   "jsonpath={.status.phase}"], own=is_ours(context))
        if (res.stdout or "").strip() == "Running":
            info(emit, "MongoDB ready (operator-managed, SCRAM)", phase="boot")
            return
        if attempt % 6 == 0:
            info(emit, "waiting for the operator to bring MongoDB up", phase="wait")
        sleep(MONGO_READY_INTERVAL)
    # Say what the resource itself reports. An empty phase means the operator never
    # reconciled it at all -- which is what an unusable `spec.version` looks like,
    # and "did not come up" sent me to the wrong place for it.
    detail = run(["kubectl", "--context", context, "-n", namespace, "get",
                  "mongodbcommunity", MONGO_SERVICE, "-o",
                  "jsonpath={.status.phase}{\" \"}{.status.message}"],
                 own=is_ours(context))
    said = (detail.stdout or "").strip()
    raise CreateFailedError(
        "the operator did not bring MongoDB up"
        + (f" — it reports: {said}" if said else
           " — and reports no status at all, which means it never reconciled the "
           "resource (usually an unusable spec.version)")
        + f". kubectl -n {namespace} describe mongodbcommunity {MONGO_SERVICE}")


# --- monitoring: Prometheus + Grafana, once per cluster ---------------------------

#: The official chart, and it is a STACK: kube-prometheus-stack, grafana-operator
#: and Loki as dependencies. Eleven pods and about 1.0 GB measured on kind.
MONITORING_RELEASE = "monitoring"
MONITORING_CHART = f"{HELM_REPO}/monitoring"

#: Installed ONCE per cluster, next to the MongoDB operator, for the same reason and
#: then one more. The reason it shares: kube-prometheus-stack owns cluster-scoped
#: CRDs (Prometheus, PodMonitor, ...), so a per-workspace install collides on them
#: at the second workspace. The extra reason: even with every dependency disabled
#: the chart still renders a ClusterRole and ClusterRoleBinding at FIXED names
#: (`monitoring-otel-collector-role`) plus a log-collector DaemonSet -- so the
#: per-namespace install that looks like the compose shape does not compose either,
#: and would put one collector per node per workspace on the same host paths.
#:
#: Sharing is also what makes this affordable: one 1.0 GB stack for every workspace
#: on the box rather than 1.0 GB each, on a tool whose capacity preflight exists
#: because it runs on laptops.
MONITORING_NAMESPACE = OPERATOR_NAMESPACE

#: Measured: 7554 MB free before `helm install`, 6524 MB with all eleven pods
#: Running. Rounded up, and charged only to the FIRST monitored workspace on a
#: cluster, because that is the one that pays for it.
MONITORING_MB = 1100

#: grafana-operator names the Grafana instance's Service this; `monitoring-grafana`
#: is the operator itself and serves nothing useful. Forwarding the wrong one gives
#: a page that loads and has no dashboards.
GRAFANA_SERVICE = "monitoring-grafana-service"
GRAFANA_DEPLOYMENT = "monitoring-grafana-deployment"
GRAFANA_PORT = 3000

#: Up to four minutes. The operator has to reconcile a Grafana CR into a Deployment
#: and then pull its image, which is well past what helm's own wait allows for.
GRAFANA_WAIT_TRIES = 48
GRAFANA_WAIT_INTERVAL = 5.0


def monitoring_installed(context: str) -> bool:
    """Whether this cluster already has the shared stack."""
    res = run(["helm", "status", MONITORING_RELEASE, "--kube-context", context,
               "-n", MONITORING_NAMESPACE], own=is_ours(context))
    return res.returncode == 0


def ensure_monitoring(*, context: str, emit: Emit = null_emit) -> None:
    """Install the shared stack, or leave the existing one alone.

    The two `...SelectorNilUsesHelmValues=false` overrides are the whole reason a
    shared stack works. kube-prometheus-stack defaults them to true, which restricts
    Prometheus to monitors carrying its OWN release label -- so a workspace's
    PodMonitor, in a different namespace and a different release, is ignored and
    Grafana shows a dashboard with no data. Nothing errors; the graphs are simply
    empty, which is the kind of failure that gets read as "monitoring is broken"
    rather than "the selector excluded it".

    Rocket.Chat's own chart needs nothing: `prometheusScraping.enabled` and
    `podMonitor.enabled` are already its defaults, so the workspace publishes a
    PodMonitor as soon as it is installed. The scrape starts because the shared
    Prometheus is willing to look outside its own release, not because `--monitor`
    changed anything about the workspace.
    """
    run(["helm", "repo", "add", HELM_REPO, HELM_REPO_URL, "--force-update"],
        timeout=APPLY_TIMEOUT, own=is_ours(context))
    if monitoring_installed(context):
        info(emit, "monitoring stack already on this cluster (shared)",
             phase="monitor")
        return
    info(emit, f"installing the monitoring stack in {MONITORING_NAMESPACE} "
               "(once per cluster, ~1 GB)", phase="monitor")
    # Deliberately NOT `--wait`. This chart creates grafana-operator custom
    # resources in the same release that installs the operator, and helm's readiness
    # check reaches them before the operator has reconciled anything:
    #
    #     Error: resource GrafanaDashboard/rc-repro-system/monitoring-node-exporter-full
    #     not ready. status: NotFound, message: Resource not found
    #
    # which is a race, not a failure -- the stack came up perfectly well anyway, and
    # ten pods were Running while helm reported it broken. So the release is applied
    # without waiting and the thing that actually matters is waited for below.
    res = run(["helm", "upgrade", "--install", MONITORING_RELEASE, MONITORING_CHART,
               "--kube-context", context, "-n", MONITORING_NAMESPACE,
               "--create-namespace",
               "--set", "operator.prometheus.prometheusSpec."
                        "serviceMonitorSelectorNilUsesHelmValues=false",
               "--set", "operator.prometheus.prometheusSpec."
                        "podMonitorSelectorNilUsesHelmValues=false",
               "--timeout", "9m"],
              timeout=INSTALL_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        raise CreateFailedError("could not install the monitoring stack: " + why(res))
    wait_for_grafana(context=context, emit=emit)


def wait_for_grafana(*, context: str, emit: Emit = null_emit,
                     sleep=time.sleep) -> None:
    """Wait for the Grafana the operator builds, in two steps, because it arrives late.

    `monitoring-grafana-deployment` does not exist when helm returns: the
    grafana-operator has to see the Grafana custom resource and create it. So this
    waits for the Deployment to EXIST and only then for it to be available --
    `kubectl rollout status` on a missing Deployment is an error, not a wait, which
    would turn "not yet" into "failed".
    """
    for attempt in range(GRAFANA_WAIT_TRIES):
        res = run(["kubectl", "--context", context, "-n", MONITORING_NAMESPACE,
                   "get", "deployment", GRAFANA_DEPLOYMENT, "-o",
                   "jsonpath={.status.availableReplicas}"], own=is_ours(context))
        if (res.stdout or "").strip() not in ("", "0"):
            return
        if attempt % 5 == 0:
            info(emit, "waiting for Grafana", phase="monitor")
        sleep(GRAFANA_WAIT_INTERVAL)
    warn(emit, "Grafana has not come up yet; the stack is installed and it should "
               f"appear shortly — kubectl -n {MONITORING_NAMESPACE} get pods",
         phase="monitor")


def remove_monitoring(*, context: str, emit: Emit = null_emit) -> bool:
    """Uninstall the shared stack. Returns False if another workspace still wants it.

    Shared, so `--off` on one workspace must not blind the others. This is the whole
    behavioural difference from Compose, where the stack belongs to a project and
    detaching it is unambiguous.
    """
    others = [n for n in workspace_namespaces(context)
              if monitoring_wanted(n, context=context)]
    if others:
        info(emit, "leaving the monitoring stack up — still used by "
                   + ", ".join(sorted(n.removeprefix(NAMESPACE_PREFIX) for n in others)),
             phase="monitor")
        return False
    # The grafana-operator's own resources go FIRST, while the operator that
    # processes their finalizers is still running. Letting helm delete everything at
    # once deadlocks: it removes the operator Deployment and then waits on a
    # GrafanaFolder whose finalizer nothing is left to clear --
    #
    #     resource GrafanaFolder/rc-repro-system/monitoring-rocketchat still exists.
    #     status: Terminating ...
    #     context deadline exceeded
    #
    # -- after which the release is gone, the CR is wedged, and the next install
    # fails on an object that no longer belongs to any release. Observed on the
    # first real uninstall.
    for kind in ("grafanadashboard", "grafanadatasource", "grafanafolder", "grafana"):
        run(["kubectl", "--context", context, "-n", MONITORING_NAMESPACE, "delete",
             kind, "--all", "--ignore-not-found", "--timeout=60s"],
            timeout=APPLY_TIMEOUT, own=is_ours(context))
    res = run(["helm", "uninstall", MONITORING_RELEASE, "--kube-context", context,
               "-n", MONITORING_NAMESPACE, "--wait", "--timeout", "5m"],
              timeout=INSTALL_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        # The release is removed either way; a stuck finalizer must not leave the
        # cluster in a state the next `--monitor` cannot install into.
        for kind in ("grafanafolder", "grafanadashboard", "grafanadatasource"):
            for name in (run(["kubectl", "--context", context, "-n",
                              MONITORING_NAMESPACE, "get", kind, "-o",
                              "jsonpath={.items[*].metadata.name}"],
                             own=is_ours(context)).stdout or "").split():
                run(["kubectl", "--context", context, "-n", MONITORING_NAMESPACE,
                     "patch", kind, name, "--type=merge", "-p",
                     '{"metadata":{"finalizers":[]}}'], own=is_ours(context))
        warn(emit, "the monitoring stack needed its finalizers cleared by hand: "
                   + why(res), phase="monitor")
    return True


def monitoring_wanted(namespace: str, *, context: str) -> bool:
    """Whether a workspace namespace is marked as wanting monitoring.

    A label on the namespace rather than a lookup in repro.json: `remove_monitoring`
    has to answer "does anyone else still want this?" about workspaces it is not
    holding the record for, and possibly ones created by a different user of the
    same cluster.
    """
    res = run(["kubectl", "--context", context, "get", "namespace", namespace,
               "-o", "jsonpath={.metadata.labels.rc-repro\\.io/monitoring}"],
              own=is_ours(context))
    return (res.stdout or "").strip() == "true"


def set_monitoring_label(namespace: str, *, context: str, wanted: bool) -> None:
    """Mark (or unmark) a namespace as wanting the shared stack."""
    value = "true" if wanted else "-"
    run(["kubectl", "--context", context, "label", "--overwrite", "namespace",
         namespace, f"rc-repro.io/monitoring={value}" if wanted
         else "rc-repro.io/monitoring-"], own=is_ours(context))


def forward_reachable(host_port: int, *, tries: int = 20, interval: float = 0.5,
                      sleep=time.sleep) -> bool:
    """Whether something is actually listening on the host port yet.

    `kubectl port-forward` returns a pid long before it has bound the socket, so a
    URL printed straight after spawning it is a guess. The workspace path learned
    this the hard way; the Grafana path repeated it and the matrix caught it --
    `monitor` reported attached and exit 0, and a curl a moment later got nothing.
    """
    import socket

    for _ in range(tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex(("127.0.0.1", int(host_port))) == 0:
                return True
        sleep(interval)
    return False


def grafana_forward(*, context: str, host_port: int, bind_host: str = "") -> int:
    """Publish Grafana on a host port. Returns the pid, or 0.

    Same deployment-not-Service rule as the workspace forward, for the same reason:
    a Service with no ready endpoint makes kubectl exit immediately.
    """
    argv = ["kubectl", "--context", context, "-n", MONITORING_NAMESPACE,
            "port-forward"]
    if bind_host and bind_host not in ("127.0.0.1", "localhost"):
        argv += ["--address", bind_host]
    argv += [f"deployment/{GRAFANA_DEPLOYMENT}", f"{host_port}:{GRAFANA_PORT}"]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                env=owned_env() if is_ours(context) else None)
    except (OSError, subprocess.SubprocessError):
        return 0
    return proc.pid
