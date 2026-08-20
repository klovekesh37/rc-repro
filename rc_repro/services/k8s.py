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

import base64
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
                            NotFoundError, NotReadyError, PreflightError)
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
    #: What this cluster IS, and what it can do. The name is for the wording of a
    #: message and never for a branch -- what it CAN do is each of the fields below,
    #: because `k3s --disable traefik` is a real setup and minikube's ingress is an
    #: addon that ships off.
    distribution: str = ""
    node_count: int = 0
    architectures: list[str] = field(default_factory=list)
    metrics: bool = False
    #: Evidence that a LoadBalancer works here, or "" -- see loadbalancer_address().
    loadbalancer: str = ""
    #: The context actually probed, and how that cluster came to exist. On a box
    #: with no kind this is whatever `kubectl` is already pointed at -- the
    #: bring-your-own-cluster case, where rc-repro manages namespaces and never
    #: the cluster.
    context: str = ""
    provider: str = ""
    #: Whether this call would BRING THE CLUSTER INTO EXISTENCE. Reported rather
    #: than inferred from `cluster_reachable`, because "not answering" and "not made
    #: yet" are different sentences and send a reader to different places.
    will_create: bool = False
    #: Why the cluster question could not be ANSWERED, as opposed to answered no.
    #: `kind get clusters` fails when Docker is down, and returns nothing when there
    #: are simply no clusters -- both give an empty list. Reporting the first as
    #: "the cluster does not exist" would send someone to create one that is
    #: already there, so the two are kept apart.
    probe_failed: str = ""

    @property
    def tools_ready(self) -> bool:
        """Whether Kubernetes can be USED. Provisioning is a separate question.

        Keyed on CORE_TOOLS rather than on what happens to be in `self.tools`, because
        `all()` over nothing is True: a Preflight carrying no tool information at all
        claimed every tool was ready, which is "I have no idea" reported as "yes".
        """
        return all(n in self.tools and self.tools[n].present
                   and self.tools[n].new_enough for n in CORE_TOOLS)

    @property
    def can_provision(self) -> bool:
        """Whether rc-repro can CREATE a cluster, as opposed to use one.

        Same reasoning as `tools_ready`: no information is not a yes.
        """
        return all(n in self.tools and self.tools[n].present
                   and self.tools[n].new_enough for n in PROVISION_TOOLS)

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


def metrics_available(context: str = CONTEXT) -> bool:
    """Whether `kubectl top` can answer here, i.e. metrics-server is installed.

    Asked rather than assumed per distribution: kind ships none, k3s ships one, and
    minikube has it as an addon that is off by default -- and any of the three can be
    changed by the person running the cluster. `stats` already discovers this by trying;
    this is the same question asked before anybody needs the answer, so `doctor` can say
    it instead of `stats` being the way you find out.
    """
    return run(["kubectl", "--context", context, "top", "nodes", "--no-headers"],
               own=is_ours(context)).returncode == 0


def loadbalancer_address(context: str = CONTEXT) -> str:
    """An address some LoadBalancer Service actually got, or "".

    EVIDENCE, not a capability claim: "no address" means either no controller or nobody
    asked for one, and those cannot be told apart from outside. Reported as what it is,
    because a cluster that has given a Service a real address has demonstrably got a
    load balancer -- which on k3s is ServiceLB and on kind is nothing at all.
    """
    res = run(["kubectl", "--context", context, "get", "svc", "-A", "-o",
               "jsonpath={range .items[?(@.spec.type=='LoadBalancer')]}"
               "{.metadata.name}={.status.loadBalancer.ingress[0].ip}"
               "{.status.loadBalancer.ingress[0].hostname} {end}"],
              own=is_ours(context))
    if res.returncode != 0:
        return ""
    for pair in (res.stdout or "").split():
        name, _, addr = pair.partition("=")
        if addr:
            return f"{name} has {addr}"
    return ""


def nodes_summary(context: str = CONTEXT) -> tuple[int, list[str]]:
    """(how many nodes, which architectures). Both matter, for different reasons.

    Node COUNT decides whether a node-local default StorageClass can bite: `local-path`
    on kind and k3s alike is `WaitForFirstConsumer`, so on more than one node a MongoDB
    pod rescheduled elsewhere cannot rebind its volume. ARCHITECTURE matters because on
    kind and k3s the node is this machine and on a managed cluster it need not be --
    `bitnamilegacy/mongodb`, which every Rocket.Chat below 8 pairs with, is amd64-only.
    """
    res = run(["kubectl", "--context", context, "get", "nodes", "-o",
               "jsonpath={range .items[*]}{.status.nodeInfo.architecture} {end}"],
              own=is_ours(context))
    if res.returncode != 0:
        return 0, []
    arches = (res.stdout or "").split()
    return len(arches), sorted(set(arches))


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

    # THE SAME FUNCTION `up` USES, and that is the whole point of the call. An
    # earlier version resolved the context itself -- `kind` cluster if one existed,
    # else whatever `kubectl` pointed at -- which agreed with `plan_cluster` on every
    # box that had only one of the two and disagreed on the box that had both: with
    # `kind` installed but no cluster yet and k3s running, `doctor` reported "Using
    # your cluster 'default' (k3s)" while `up` went and created a kind cluster. A
    # preflight whose job is to predict a boot must not be a second opinion about it.
    if context:
        out.context = context
    else:
        try:
            plan = plan_cluster()
        except PreflightError:
            plan = None
        if plan:
            out.context = plan.context
            out.will_create = plan.create
    out.provider = PROVIDER_KIND if out.context == CONTEXT else PROVIDER_EXTERNAL

    if not out.context or out.will_create:
        # Nothing to probe: the cluster this create would use does not exist yet, and
        # asking the OTHER cluster for its storage classes would describe a machine
        # rc-repro is not about to use.
        return out
    out.cluster_reachable = reachable(out.context)
    if not out.cluster_reachable:
        return out
    out.storage_classes, out.default_storage_class = storage_classes(out.context)
    out.ingress_classes = ingress_classes(out.context)
    out.namespaces = workspace_namespaces(out.context)
    out.distribution = distribution(out.context)
    out.node_count, out.architectures = nodes_summary(out.context)
    out.metrics = metrics_available(out.context)
    out.loadbalancer = loadbalancer_address(out.context)
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


#: What `distribution` a cluster is, for the one thing a name may decide: the wording
#: of a message. Never a branch -- `k3s --disable traefik` is a real setup, minikube
#: ships ingress as an addon that is off, and kind gains one when rc-repro installs it,
#: so what a cluster CAN do is probed (see Preflight) and what it IS is only labelled.
#:
#: Signals in order of reliability, each verified against a live cluster: `providerID`
#: (`kind://docker/…`, `k3s://docker-01`), then the kubelet's version suffix (`+k3s`,
#: `+rke2`), then node labels. A managed cluster's `providerID` names its cloud, which
#: is the most reliable "this is not a disposable cluster" signal there is.
_PROVIDER_ID_PREFIX = (("kind://", "kind"), ("k3s://", "k3s"), ("aws://", "eks"),
                       ("gce://", "gke"), ("azure://", "aks"))
_KUBELET_SUFFIX = (("+k3s", "k3s"), ("+rke2", "rke2"))


def distribution(context: str) -> str:
    """What kind of Kubernetes this is, as a label. "unknown" is a fine answer.

    Asks the first node, because every signal lives there and one node is enough to
    identify a distribution. Never raises: an unreachable cluster is "unknown", and a
    label nobody branches on is not worth a failure.
    """
    res = run(["kubectl", "--context", context, "get", "nodes", "-o",
               "jsonpath={.items[0].spec.providerID} {.items[0].status.nodeInfo."
               "kubeletVersion} {.items[0].metadata.labels."
               "node\\.kubernetes\\.io/instance-type} {.items[0].metadata.name}"],
              own=is_ours(context))
    if res.returncode != 0:
        return "unknown"
    parts = (res.stdout or "").split()
    blob = " ".join(parts)
    for prefix, name in _PROVIDER_ID_PREFIX:
        if prefix in blob:
            return name
    for suffix, name in _KUBELET_SUFFIX:
        if suffix in blob:
            return name
    if "minikube" in blob:
        return "minikube"
    if "docker-desktop" in blob:
        return "docker-desktop"
    return "unknown"


@dataclass
class ClusterPlan:
    """Which cluster this create will use, decided before anything is written.

    The whole Kubernetes/k3s difference is ONE STEP: provisioning. With `kind` here,
    rc-repro creates its own cluster exactly as it always has; without it, that step
    is skipped and the cluster `kubectl` already points at is used instead. Every step
    afterwards is the same code, because every function below takes `context=`.

    `create` is the only thing any caller needs to know beyond the context: it decides
    what a failed create may roll back (never a cluster we did not make), and whether
    `check_capacity` should charge for a control plane that does not exist yet.
    """
    context: str
    distribution: str = ""
    create: bool = False          # this call will bring the cluster into existence


def plan_cluster() -> ClusterPlan:
    """Choose the cluster, probing only -- nothing here writes to the machine.

    Separate from `ensure_cluster` on purpose, and the reason is a measured defect: the
    write-ahead `repro.json` is written before `create_workspace` runs, so a create that
    refuses for want of a cluster left an `incomplete` record that `list` showed as a
    workspace and whose port `used_ports()` reserved. A pure probe can run BEFORE that
    write, so the refusal costs nothing.

    Order, and it is short because it is not a precedence contest:

      1. a cluster rc-repro created, if it still answers  -- ask the cluster, not `kind`
      2. `kind` present                                   -- create ours, as today
      3. whatever `kubectl` points at, if it answers       -- k3s, minikube, anything
      4. refuse

    Step 1 asks the owned kubeconfig rather than `kind get clusters` deliberately: that
    probe needs the kind BINARY, so uninstalling kind while its cluster still ran made
    the cluster invisible -- containers holding memory that nothing in rc-repro could
    see or remove, and resolution quietly falling through to a different cluster.
    """
    if which("kind"):
        # `create` is decided by whether the CLUSTER exists, never by whether this
        # home's kubeconfig happens to know about it. An earlier version required the
        # kubeconfig to name a reachable context, so a fresh RC_REPRO_HOME facing an
        # existing cluster planned to CREATE one -- which charged 600 MB of capacity for
        # a control plane already running and, far worse, told a failed create's
        # rollback that the cluster was its to delete. `ensure_cluster` already
        # re-exports the kubeconfig for exactly this case, so the context is knowable
        # before it has been read.
        return ClusterPlan(context=CONTEXT, distribution="kind",
                           create=CLUSTER_NAME not in clusters()[0])
    # No kind binary. Our own kubeconfig may still name a cluster that is up -- one
    # rc-repro made before kind was uninstalled -- and that is ours to use even though
    # we could no longer create or delete it.
    ours = cluster_context()
    if ours and reachable(ours):
        return ClusterPlan(context=ours, distribution=distribution(ours), create=False)
    active = active_context()
    if active and reachable(active):
        return ClusterPlan(context=active, distribution=distribution(active),
                           create=False)
    raise PreflightError(
        "kind is not installed, so rc-repro cannot create a cluster, and `kubectl` is "
        + ("not pointed at one either" if not active else
           f"pointed at {active!r}, which is not answering")
        + ". Install kind, or point kubectl at a cluster you already have.")


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


def cluster_reclaimable() -> bool:
    """Whether rc-repro's own cluster exists and holds nothing of ours.

    Mirrors `delete_cluster`'s own guard so a caller can ASK before offering to reclaim,
    rather than finding out by being refused. A cluster that exists but does not answer
    counts: `kind delete` talks to Docker, not to the API server, and a control plane
    nobody can reach is the clearest case of memory doing nothing.
    """
    if not which("kind") or CLUSTER_NAME not in clusters()[0]:
        return False
    ctx = cluster_context()
    return not reachable(ctx) or not workspace_namespaces(ctx)


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

#: The image the hand-written StatefulSet runs, and the reasoning, because there is
#: no upstream answer to copy: the official Kubernetes guide documents ONLY the
#: operator, and the chart's bundled option is the Bitnami subchart it tells you to
#: migrate away from. This path is rc-repro's own, so the choice has to defend itself.
#:
#: `mongo` is the Docker Official Image, and it is picked over MongoDB Inc's
#: `mongodb/mongodb-community-server` for one decisive reason: COVERAGE. This
#: StatefulSet exists to serve the versions the operator cannot reach, and
#: community-server has no tag below 4.4 -- 3.6, 4.0 and 4.2 are all 404 -- while
#: rc-repro's map goes down to MongoDB 3.6 for Rocket.Chat < 3.0. Switching to it
#: would break precisely the versions this code was written for.
#:
#: Two lesser reasons that still matter: `mongo` publishes amd64 AND arm64 across
#: that whole range, where `bitnamilegacy` is amd64-only; and it takes ownership of
#: /data/db itself, so this needs none of the fix-permission init container that
#: compose.py carries for community-server's different UID.
#:
#: What it is NOT is a different MongoDB. `mongo:8.0` and
#: `mongodb/mongodb-community-server:8.0-ubi8` are the same source, version, storage
#: engine and wire protocol; they differ in base OS and packaging. That distinction
#: is worth stating because `mongo_flavor` -- a Compose concept -- used to be
#: reported for this path as though it applied.
MONGO_IMAGE_REPO = "mongo"


def mongo_image(tag: str) -> str:
    """The image this runtime's StatefulSet actually runs. See MONGO_IMAGE_REPO."""
    return f"{MONGO_IMAGE_REPO}:{tag}"


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


def container_security_context(chart_version: str) -> dict:
    """Drop `fsGroup` from the chart's CONTAINER securityContext, if it puts one there.

    `fsGroup` is a POD-level field. Chart 6.26.0 -- the exact-match chart for
    Rocket.Chat 7.10.0 -- ships `containerSecurityContext: {{runAsUser: 999,
    fsGroup: 999}}` and renders it onto the container, where the field does not
    exist. Helm 3 applied client-side and silently dropped it; Helm 4 applies
    server-side, and the API server refuses the whole Deployment:

        Error: server-side apply failed ... Kind=Deployment: failed to create typed
        patch object: .spec.template.spec.containers[name="rocketchat"]
        .securityContext.fsGroup: field not declared in schema

    That makes every Rocket.Chat 7.x install fail on this runtime, which went unseen
    because every live run so far used 8.5.1: chart 7.0.0 fixed it upstream.

    `null` rather than a replacement map, because Helm MERGES values: passing
    `{{runAsUser: 999}}` leaves the chart's `fsGroup: 999` in place underneath, and
    the render is unchanged. `null` is Helm's delete.

    And it is applied only where the chart actually has the field, which is why this
    reads the chart's own values first. Injecting `fsGroup: null` unconditionally put
    a literal `fsGroup: null` onto chart 7.0.0's container -- the same undeclared
    field, arrived at from the other direction. The UIDs differ too (999 in 6.26.0,
    65533 in 7.0.0), so nothing about this map is safe to write down here.
    """
    res = run(["helm", "show", "values", CHART, "--version", chart_version],
              timeout=APPLY_TIMEOUT, own=True)
    if res.returncode != 0:
        return {}
    import yaml
    try:
        values = yaml.safe_load(res.stdout or "") or {}
    except yaml.YAMLError:
        return {}
    current = values.get("containerSecurityContext")
    if not isinstance(current, dict) or "fsGroup" not in current:
        return {}
    return {"containerSecurityContext": {"fsGroup": None}}


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
        image: {mongo_image(tag)}
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
               mongo_url: str = "", oplog_url: str = "",
               mongo_secret: str = "", reg_token_secret: str = "",
               preset_env=None) -> dict:
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
    # A scenario's Rocket.Chat settings are the SHARED half of the Scenario
    # contract: `OVERWRITE_SETTING_LDAP_*` is byte-identical on both runtimes, so it
    # rides extraEnv here exactly as it rides the service environment on Compose.
    # Only the backing service differs, and that is the manifests the caller applies.
    #
    # AFTER the base list, so a preset can override a default rather than being
    # overridden by one -- the same precedence compose.py gives it ("Preset env wins
    # over base defaults").
    env.extend({"name": str(k), "value": str(v)} for k, v in (preset_env or {}).items())
    if reg_token_secret:
        # By REFERENCE, so the token is never a value in this document. The chart
        # renders extraEnv verbatim, so valueFrom survives -- verified against the
        # published chart rather than assumed.
        env.append({"name": "REG_TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": reg_token_secret,
                                                   "key": REG_TOKEN_KEY}}})
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
        "extraEnv": env,
    }
    if mongo_secret:
        # The URL goes in a Secret and its NAME goes in the values, so
        # `helm get values rocketchat` no longer prints the password to anyone who
        # can read the release. The chart reads both keys from it itself.
        values["existingMongodbSecret"] = mongo_secret
    else:
        values["externalMongodbUrl"] = mongo_url or MONGO_URL
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


def upgrade_image(*, namespace: str, context: str, chart_version: str,
                  image_repo: str, tag: str, oplog: bool) -> None:
    """Move a release to a new Rocket.Chat image, the way the official guide does.

    The guide says: change `image.tag` in your values and run `helm upgrade`. This is
    that, with two differences it explicitly warns about or implies.

    First, the chart version IS pinned. The guide's own note says its command "does
    not pin a chart version, so it installs the latest Rocket.Chat Helm chart" --
    which for a tool whose entire purpose is reproducing a customer's exact version
    would quietly deploy different software than the one asked for.

    Second, `--reuse-values` rather than a re-rendered values file: everything else
    about the workspace -- the admin env, microservices, replica count, whether
    MongoDB comes from a Secret or a URL, a preset's settings -- was decided at
    create time, and rebuilding that set from scratch risks silently dropping a
    piece of it. The one value that must NOT be carried over is the oplog URL:
    Rocket.Chat 8 dropped oplog tailing and chart 7.0.0 removed the key, so it is
    explicitly cleared when the target no longer wants it.
    """
    argv = ["helm", "upgrade", RELEASE, CHART, "--kube-context", context,
            "-n", namespace, "--version", chart_version, "--reuse-values",
            "--set", f"image.repository={image_repo}",
            "--set", f"image.tag={tag}"]
    if not oplog:
        argv += ["--set", "externalMongodbOplogUrl=null"]
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=INSTALL_TIMEOUT, check=False,
                             env=owned_env() if is_ours(context) else None)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerError(f"helm upgrade failed: {exc}") from exc
    if res.returncode != 0:
        raise DockerError("helm upgrade failed: " + why(res))


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


def render_scenario_manifest(manifest: str, name: str) -> str:
    """Bind a scenario's native resources to this namespace-local workspace.

    PR #3's substitution, kept verbatim: a scenario adapter cannot know the
    workspace name when it renders, so it writes a placeholder and the lifecycle
    fills it in. The label it feeds -- `rc-repro.io/repro` -- is what makes the
    resource show up under this workspace's ownership rather than floating in the
    namespace unattributed.
    """
    return manifest.replace("__RC_REPRO_NAME__", name)


#: A scenario Service wearing this label is published on the host port it names.
#: The label rather than a table here, because the adapter that creates the Service
#: is the thing that knows it has a UI -- and a table would be a second place to
#: declare a port, which is how two registries disagree. See presets/ldap.py.
UI_PORT_LABEL = "rc-repro.io/ui-port"


#: Up to two minutes for a scenario's workload to have a ready endpoint. Keycloak
#: imports a realm on first boot and takes about forty seconds of it.
ENDPOINT_WAIT_TRIES = 60
ENDPOINT_WAIT_INTERVAL = 2.0


def scenario_ui_forwards(name: str, *, namespace: str, context: str,
                         bind_host: str = "", emit: Emit = null_emit,
                         sleep=time.sleep) -> dict:
    """Publish every scenario Service that asks to be published. Returns {port: pid}.

    Compose gives a preset's UI a host port for free; here it is a port-forward, for
    the reasons NodePort cannot serve: its range is 30000-32767 so the port would not
    MATCH the Compose one, kind's nodes are unreachable from the host on macOS and
    Windows, and `extraPortMappings` is fixed when the cluster is created -- which
    for a cluster shared by every workspace would mean recreating it to add a preset.

    Forwarded to the SERVICE here, not a Deployment: by this point the workload is
    installed and its endpoints are ready, and a Service is the stable name if the
    pod is later replaced.
    """
    res = run(["kubectl", "--context", context, "-n", namespace, "get", "svc",
               "-l", UI_PORT_LABEL, "-o",
               "jsonpath={range .items[*]}{.metadata.name}{\" \"}"
               "{.metadata.labels.rc-repro\\.io/ui-port}{\" \"}"
               "{.spec.ports[0].targetPort}{\" \"}"
               "{.metadata.labels.rc-repro\\.io/ui-deployment}{\"\\n\"}{end}"],
              own=is_ours(context))
    forwards: dict[int, int] = {}
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) not in (3, 4):
            continue
        svc, host, target = parts[:3]
        deploy = parts[3] if len(parts) == 4 else svc
        try:
            host_p, target_p = int(host), int(target)
        except ValueError:
            continue
        # WAIT FOR A READY ENDPOINT FIRST. `kubectl port-forward svc/...` binds the
        # local socket immediately and only then dials a pod -- so a forward started
        # while the workload is still booting passes a TCP check on the local side,
        # fails upstream, and exits. Keycloak takes ~40s to import its realm, and
        # that is exactly what happened: "keycloak published at http://localhost:8081"
        # followed by a connection refused a minute later.
        #
        # Binding a socket is not the same as the backend answering, which is the
        # same lesson as `svc/` needing ready endpoints -- learned here for the
        # third time, on the third kind of forward.
        for _ in range(ENDPOINT_WAIT_TRIES):
            ep = run(["kubectl", "--context", context, "-n", namespace, "get",
                      "endpoints", svc, "-o",
                      "jsonpath={.subsets[0].addresses[0].ip}"],
                     own=is_ours(context))
            if (ep.stdout or "").strip():
                break
            sleep(ENDPOINT_WAIT_INTERVAL)
        else:
            warn(emit, f"{svc} has no ready endpoint yet; publish it when it is up: "
                       f"kubectl -n {namespace} port-forward svc/{svc} "
                       f"{host_p}:{target_p}", phase="boot")
            continue
        argv = ["kubectl", "--context", context, "-n", namespace, "port-forward"]
        if bind_host and bind_host not in ("127.0.0.1", "localhost"):
            argv += ["--address", bind_host]
        # deployment/, NOT svc/. A Service forward dies when its endpoint churns --
        # which is exactly what happened to a live OIDC workspace: Keycloak was
        # 1/1 Running, the recorded pid was gone, and nothing answered on 8085. The
        # workspace's own forward learned this and uses deployment/; scenarios then
        # reintroduced it. Our adapters name the Deployment and the Service
        # identically, which `test_a_scenario_names_its_deployment_and_service_alike`
        # holds them to.
        argv += [f"deployment/{deploy}", f"{host_p}:{target_p}"]
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True,
                                    env=owned_env() if is_ours(context) else None)
        except (OSError, subprocess.SubprocessError):
            continue
        # Confirmed, not assumed -- the same rule the workspace URL and Grafana both
        # had to learn: port-forward returns a pid before it binds the socket.
        if forward_reachable(host_p, tries=20):
            forwards[host_p] = proc.pid
            info(emit, f"{svc} published at http://localhost:{host_p}", phase="boot")
        else:
            warn(emit, f"{svc} is running but http://localhost:{host_p} is not "
                       f"answering yet; `kubectl -n {namespace} port-forward "
                       f"svc/{svc} {host_p}:{target_p}` re-establishes it",
                 phase="boot")
    return forwards


def record_rendered(name: str, *, values: dict, manifests: dict) -> list[str]:
    """Write what was sent to helm and kubectl into the workspace directory.

    A Compose workspace has a docker-compose.yml you can read; a Kubernetes one had
    nothing but repro.json, because values go to `helm --values -` and manifests to
    `kubectl apply -f -` on stdin. That is good for secrets and bad for answering
    "what did it actually deploy" -- especially once the cluster is gone, when the
    live `helm get values` is no longer there to ask.

    So they are written for READING. Re-running `up` regenerates them, exactly as it
    regenerates docker-compose.yml, and editing them changes nothing -- the same
    contract the Compose file already has.

    SECRETS ARE NOT WRITTEN. The MongoDB URL carries a generated SCRAM password and
    lives only in a Kubernetes Secret; putting it here would undo the whole point of
    moving it off `helm get values`. `values.yaml` names the Secret instead, which is
    what the running release does too.
    """
    import yaml as _yaml
    ws = config.repros_dir() / name
    out = ws / "kubernetes"
    out.mkdir(parents=True, exist_ok=True)
    written = [str(out / "values.yaml")]
    runner.atomic_write(out / "values.yaml",
                        _yaml.safe_dump(values, sort_keys=False))
    for label, manifest in manifests.items():
        if not manifest:
            continue
        path = out / f"{label}.yaml"
        runner.atomic_write(path, manifest)
        written.append(str(path))
    return written


def create_workspace(*, name: str, resolved, host_port: int, microservices: bool,
                     replicas: int = 1, owner: str = "", root_url: str = "",
                     bind_host: str = "", use_operator: bool = False,
                     reg_token: str = "", preset=None, plan: ClusterPlan | None = None,
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
    # The plan was made before the caller wrote its record, so the refusal for "no
    # cluster and no kind" has already happened by here. Provisioning is the ONE step
    # that differs between a kind box and a k3s box; everything below is the same code.
    plan = plan or plan_cluster()
    context = ensure_cluster(emit=emit) if plan.create else plan.context
    if not plan.create:
        info(emit, f"using your {plan.distribution} cluster {context!r} — rc-repro "
                   "creates a namespace in it and never removes the cluster",
             phase="provision", pct=5)
    # Only a cluster THIS call created may be rolled back. `plan.create` carries that;
    # it is not inferred from the cluster's name, which is what made a hand-made
    # `rc-repro-local` look like ours.
    had_cluster = not plan.create
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
        # Everything sent to kubectl, kept so it can be written next to repro.json.
        rendered: dict[str, str] = {}
        # Recorded rather than inferred. `mongo_flavor` is a COMPOSE concept
        # ("official" / "bitnami-legacy") and this runtime honours neither value, so
        # reporting it here made `rc-repro list` print "8.0 (official)" for a
        # workspace running Docker Hub's `mongo:8.0`. What is true is which of the
        # two mechanisms built the database and what it actually runs.
        managed_by = "statefulset"
        image = mongo_image(resolved.mongo_tag)
        if (use_operator or operator_enabled()) and \
                operator_supports(resolved.mongo_tag, forced=use_operator):
            managed_by = "operator"
            # The operator chooses the image from `spec.version`, so this names the
            # version it was asked for rather than guessing a repository that is the
            # operator's to change.
            image = f"mongodb-community-server {operator_version(resolved.mongo_tag)} (operator's choice)"
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
            operator_doc = mongodb_community_manifest(name, resolved.mongo_tag,
                                                      owner=owner)
            rendered["mongodb"] = operator_doc
            apply(operator_doc, namespace=namespace, context=context)
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

        # A scenario's backing services go in BEFORE Rocket.Chat, so its settings
        # point at something that already exists. The chart is what waits for
        # readiness; a Deployment that is still pulling is fine here.
        manifests = list(getattr(preset, "kubernetes_manifests", None) or [])
        if manifests:
            info(emit, f"scenario {getattr(preset, 'scenario', '') or preset.name}: "
                       f"{len(manifests)} resource(s)", phase="provision", pct=25)
            for i, manifest in enumerate(manifests):
                body = render_scenario_manifest(manifest, name)
                rendered[f"scenario-{i}" if i else "scenario"] = body
                apply(body, namespace=namespace, context=context)
        apply(mongo_url_secret_manifest(name, mongo_url=mongo_url,
                                        oplog_url=oplog_url, owner=owner),
              namespace=namespace, context=context)
        if reg_token:
            info(emit, "installing the registration token Secret", phase="boot")
            apply(reg_token_secret_manifest(name, token=reg_token, owner=owner),
                  namespace=namespace, context=context)
        values = values_for(rc_version=resolved.rc_version,
                            rc_image=resolved.rc_image,
                            microservices=microservices, replicas=replicas,
                            root_url=root_url, oplog=resolved.oplog,
                            mongo_secret=MONGO_URL_SECRET,
                            reg_token_secret=(REG_TOKEN_SECRET if reg_token else ""),
                            preset_env=getattr(preset, "env", None))
        values.update(container_security_context(chart_version))
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
    # Written after a successful install, so the directory never describes something
    # that failed halfway.
    record_rendered(name, values=values, manifests=rendered)
    ui = scenario_ui_forwards(name, namespace=namespace, context=context,
                              bind_host=bind_host, emit=emit)
    return {"scenario_forwards": {str(k): v for k, v in ui.items()},
            "context": context, "namespace": namespace, "bind_host": bind_host,
            "chart_version": chart_version, "release": RELEASE,
            "port_forward_pid": pid, "microservices": microservices,
            "mongo_managed_by": managed_by, "mongo_image": image}


#: The workloads whose readiness `ready` is entitled to speak for: the ones the
#: official chart gives a real readinessProbe. `account`, `authorization` and
#: `presence` are deliberately NOT here -- the chart ships them with no probe at all,
#: so their pod-Ready is set the moment the container starts and attests nothing.
#: Waiting on it would add delay and buy no confidence.
READY_SELECTOR = ("app.kubernetes.io/name in "
                  "(rocketchat,rocketchat-ddp-streamer)")


def workspace_ready(name: str, *, context: str) -> bool:
    """Whether every probed Rocket.Chat workload reports itself Ready.

    This used to read `items[0].status.containerStatuses[0].ready`, which over-claimed
    twice, and an audit caught both:

      * `items[0]` is ONE pod. With `--replicas 3` the workspace was called ready on
        the strength of the first pod alone.
      * Only the monolith container was consulted. On microservices, `ddp-streamer`
        carries the WebSocket -- the realtime half of Rocket.Chat -- and it was
        observed Running-but-not-Ready on a workspace `ready` had already reported as
        serving. A caller told "ready" could open the URL and find messages not
        arriving, which is the one failure this tool exists to reproduce, not cause.

    A Pending pod has no containerStatuses at all, so counting pods and comparing is
    what keeps "no statuses yet" from passing as "nothing failed".
    """
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "pod", "-l", READY_SELECTOR, "-o", "json"],
              own=is_ours(context))
    if res.returncode != 0:
        return False
    try:
        pods = json.loads(res.stdout or "{}").get("items") or []
    except ValueError:
        return False
    if not pods:
        return False
    for pod in pods:
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        if not statuses:                      # Pending/ContainerCreating
            return False
        if not all(c.get("ready") for c in statuses):
            return False
    return True


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


#: Rocket.Chat's APPLICATION pods -- every replica of the monolith, and on
#: microservices the account, authorization, ddp-streamer and presence deployments
#: too. `-l` rather than a pod name so this follows a rollout.
#:
#: This was `app.kubernetes.io/name=rocketchat`, which is the MONOLITH deployment
#: alone. On microservices that silently omitted the four services that make the
#: deployment interesting -- ddp-streamer above all, since it carries the WebSocket,
#: so a realtime problem produced logs with nothing about realtime in them. Compose
#: has no service filter at all and shows everything, so the narrow selector was also
#: the two runtimes disagreeing.
#:
#: Selected by RELEASE with nats excluded, rather than by listing the four names: a
#: microservice the chart adds later is then included automatically, which a hardcoded
#: list would quietly miss. nats is the message bus, not Rocket.Chat, and its output
#: would drown the logs somebody opened to read Rocket.Chat's.
LOG_SELECTOR = (f"app.kubernetes.io/instance={RELEASE},"
                "app.kubernetes.io/name!=nats")

#: The MONOLITH Rocket.Chat deployment alone, and its container. Anything asking a
#: question about "Rocket.Chat itself" -- readiness, its environment -- wants this and
#: not the broad log selector: that one matches ddp-streamer and presence too, so
#: `items[0]` under it could answer with a microservice's environment instead of
#: Rocket.Chat's.
APP_SELECTOR = "app.kubernetes.io/name=rocketchat"
LOG_CONTAINER = "rocketchat"


def _pod_count(name: str, *, context: str, selector: str = LOG_SELECTOR) -> int:
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "pods", "-l", selector, "-o", "name"],
              own=is_ours(context))
    if res.returncode != 0:
        return 1
    return len([ln for ln in (res.stdout or "").splitlines() if ln.strip()])


def log_process(name: str, *, context: str, tail: int = 200, follow: bool = True,
                selector: str = LOG_SELECTOR) -> subprocess.Popen:
    """Stream Rocket.Chat's logs out of the cluster, shaped like the Compose one.

    A named function for the same reason `open_log_process` is one: it is the single
    kubectl call the log path makes, so a test can stand here without a cluster.

    `--prefix` matters more here than on Compose: with microservices or several
    replicas the lines come from different pods and are otherwise indistinguishable,
    which is exactly when someone is reading them. `--max-log-requests` is raised
    because kubectl refuses to follow more than five pods by default and a
    microservices workspace has more than five.
    """
    # No `-c`: the pods now selected have DIFFERENT container names (rocketchat,
    # ddp-streamer, presence...), so naming one would fail on the others. Each pod
    # here has a single container, so kubectl's default is the right one.
    argv = ["kubectl", "--context", context, "-n", namespace_for(name), "logs",
            "-l", selector, "--tail", str(int(tail)),
            "--max-log-requests", "20"]
    # `--prefix` ONLY when there is more than one pod to tell apart. It was
    # unconditional and that was wrong: Rocket.Chat pretty-prints its own log lines
    # in columns, and stamping "[pod/rocketchat-rocketchat-<hash>/rocketchat] " onto
    # the front of every one of them destroys exactly the formatting somebody opened
    # the logs to read. With a single pod the prefix names the only thing it could
    # be, so it is pure noise; with several it is the only way to tell them apart.
    if _pod_count(name, context=context, selector=selector) > 1:
        argv.append("--prefix")
    if follow:
        argv.append("-f")
    return subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            env=owned_env() if is_ours(context) else None)


def container_env(name: str, *, context: str,
                  selector: str = APP_SELECTOR) -> dict[str, str]:
    """Rocket.Chat's EFFECTIVE environment, read out of the running container.

    Compose answers this from the generated compose file. There is no such document
    here, and the helm values are only what rc-repro asked for -- the chart adds its
    own, so the values are not the answer to "what is Rocket.Chat actually running
    with". The container is, so it is asked directly.
    """
    pod = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "pod", "-l", selector, "-o",
               "jsonpath={.items[0].metadata.name}"], own=is_ours(context))
    target = (pod.stdout or "").strip()
    if not target:
        raise NotReadyError(
            f"no Rocket.Chat pod in {namespace_for(name)} to read the environment "
            f"from — is {name!r} running? (`rc-repro start --name {name}`)")
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "exec", target, "-c", LOG_CONTAINER, "--", "env"],
              timeout=APPLY_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        raise NotReadyError(f"could not read the environment from {target}: "
                            + why(res))
    out: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value
    return out


def _parse_quantity(text: str) -> float:
    """A Kubernetes CPU or memory quantity as a plain number.

    CPU comes as millicores ("142m") or cores ("1"); memory as Ki/Mi/Gi. Returned as
    millicores and BYTES respectively, so the caller does the same arithmetic it does
    for Compose.
    """
    t = text.strip()
    if t.endswith("m"):
        return float(t[:-1] or 0)
    for suffix, mult in (("Ki", 1024), ("Mi", 1024 ** 2), ("Gi", 1024 ** 3),
                         ("K", 1000), ("M", 1000 ** 2), ("G", 1000 ** 3)):
        if t.endswith(suffix):
            return float(t[:-len(suffix)] or 0) * mult
    try:
        return float(t)
    except ValueError:
        return 0.0


def pod_metrics(name: str, *, context: str,
                selector: str = LOG_SELECTOR) -> list[dict]:
    """Per-pod CPU and memory, via `kubectl top`.

    Every Rocket.Chat application pod, which on microservices means the four services
    as well as the monolith deployment -- the same set Compose sums when it adds up
    `rocketchat`, `rocketchat-1`, `rocketchat-2`. Scoped to the monolith alone it
    would report two pods out of six and call it the workspace's resource use, which
    is the quietly-wrong number this function exists to avoid.

    Needs metrics-server, which kind does NOT ship. When it is absent the refusal
    says so and how to install it, rather than reporting zero -- a resource figure
    that is quietly wrong is worse than one that is missing, which is the same
    reasoning `stats` already applies to a container it cannot find.
    """
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "top", "pods", "-l", selector, "--no-headers"],
              timeout=APPLY_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        blob = (res.stderr or "") + (res.stdout or "")
        if "metrics" in blob.lower() or "not available" in blob.lower():
            # Both commands, because the second is not optional on kind: its
            # kubelet serves a self-signed certificate that metrics-server refuses
            # by default, so the install succeeds and never reports a metric. The
            # first version of this message mixed the helm flag into the kubectl
            # route, which is syntax that does not apply to what it just told the
            # user to run.
            raise NotReadyError(
                "this cluster has no metrics-server, so there is nothing to read "
                "CPU and memory from. Install it with:\n"
                "  kubectl apply -f https://github.com/kubernetes-sigs/"
                "metrics-server/releases/latest/download/components.yaml\n"
                "and on kind, let it accept the kubelet's self-signed certificate:\n"
                "  kubectl -n kube-system patch deploy metrics-server --type=json "
                "-p '[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0"
                "/args/-\",\"value\":\"--kubelet-insecure-tls\"}]'")
        raise NotReadyError("could not read pod metrics: " + why(res))
    rows = []
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append({"pod": parts[0],
                         "cpu_millicores": _parse_quantity(parts[1]),
                         "mem_bytes": _parse_quantity(parts[2])})
    return rows


def pod_rows(name: str, *, context: str) -> list[dict]:
    """Every pod in this workspace's namespace, in the shape the panel's containers
    tab already renders: [{service, state, status, health, restarts, started}].

    A pod is not a container, but the question is the one Compose's list answers --
    what is running, and is any of it unhappy -- so the tab renders the same three
    columns rather than growing a second Kubernetes-only view. ALL pods, not just
    Rocket.Chat's: the Compose tab lists Mongo and every sidecar too, and the pod
    that is wrong is usually not the one you went looking for.

    A waiting REASON outranks the phase in the status column. "Pending" says only
    that something has not happened; `ImagePullBackOff`, `CrashLoopBackOff` and
    `CreateContainerConfigError` say what, and that is the answer to "why is this
    workspace not up" -- which the GUI previously had no way to show at all, because
    this block was empty and the tab said "No containers -- this repro is down."
    under a workspace that was running.
    """
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "pods", "-o", "json"], own=is_ours(context))
    if res.returncode != 0:
        # An unreachable cluster is the panel's "docker is unavailable": an empty
        # list, never a raise. detail() is on the path of every panel open, and a
        # workspace whose cluster is asleep still has to render.
        return []
    try:
        doc = json.loads(res.stdout or "{}")
    except ValueError:
        return []
    rows = []
    for item in doc.get("items") or []:
        meta = item.get("metadata") or {}
        st = item.get("status") or {}
        # initContainerStatuses too: the MongoDB fix-permission and init containers
        # are exactly where a Kubernetes workspace gets stuck, and a pod whose init
        # container is in CrashLoopBackOff reports phase Pending and an EMPTY
        # containerStatuses -- so reading only the main list showed "0/0 ready" and
        # named nothing.
        cs = list(st.get("initContainerStatuses") or []) + list(st.get("containerStatuses") or [])
        main = list(st.get("containerStatuses") or [])
        ready = sum(1 for c in main if c.get("ready"))
        restarts = sum(int(c.get("restartCount") or 0) for c in cs)
        reason = ""
        for c in cs:
            state = c.get("state") or {}
            waiting = state.get("waiting") or {}
            terminated = state.get("terminated") or {}
            # A terminated init container that SUCCEEDED is not a reason -- that is
            # the normal end of an init container, and reporting "Completed" as the
            # pod's status hid a Rocket.Chat container that was crash-looping
            # behind it.
            if terminated.get("reason") == "Completed":
                continue
            reason = str(waiting.get("reason") or terminated.get("reason") or "")
            if reason:
                break
        phase = str(st.get("phase") or "")
        status = reason or f"{ready}/{len(main)} ready"
        if restarts:
            status += f" · {restarts} restart" + ("s" if restarts != 1 else "")
        rows.append({"service": str(meta.get("name") or ""),
                     "state": phase.lower(), "status": status,
                     "health": "healthy" if main and ready == len(main) else "",
                     # WHICH POD IS ROCKET.CHAT ITSELF, decided by the label the
                     # APP_SELECTOR above already names -- not by a substring of the
                     # pod name, which is a naming rule the browser would then hold a
                     # second copy of. The panel needs it to report Rocket.Chat's own
                     # restart count on a runtime where nine pods have one each.
                     "app": (meta.get("labels") or {}).get(
                         "app.kubernetes.io/name") == "rocketchat",
                     # Decided HERE, next to the list that defines it, because the
                     # browser needs the answer and must not keep a second copy of
                     # the policy: `ContainerCreating` and `PodInitializing` are
                     # waiting reasons too, and a panel that treated every reason as
                     # a failure would report a booting workspace as broken. The
                     # create path spends the same list -- see terminal_pod_failure.
                     "blocked": reason in TERMINAL_POD_REASONS,
                     "restarts": restarts,
                     "started": str(st.get("startTime") or "")})
    # Named order, so the tab does not reshuffle itself between two panel opens.
    rows.sort(key=lambda r: r["service"])
    return rows


#: Waiting-state reasons a pod will never recover from on its own. Taken from PR #3,
#: whose note is the important part: `ImagePullBackOff` alone is NOT terminal -- a slow
#: or rate-limited registry looks identical while it is still making progress -- so it
#: is the REASON that discriminates, not the phase.
TERMINAL_POD_REASONS = (
    "ErrImagePull", "ImagePullBackOff", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
)


def terminal_pod_failure(name: str, *, context: str) -> tuple[str, str, str] | None:
    """The first pod condition that cannot succeed, as (pod, reason, message).

    Without this, a create waits out its whole timeout on something already decided:
    a mistyped version, an image the registry does not have, an unreachable registry.
    Ten minutes of "waiting for Rocket.Chat" for an answer Kubernetes had in seconds,
    and the eventual failure names the timeout rather than the cause.

    Returns None when nothing is terminal, so the caller keeps waiting -- the default
    has to be patience, or a slow pull becomes a failed create.
    """
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "pods", "-o", "json"], own=is_ours(context))
    if res.returncode != 0:
        return None
    try:
        items = json.loads(res.stdout or "{}").get("items") or []
    except ValueError:
        return None
    for item in items:
        pod = item.get("metadata", {}).get("name", "")
        for status in (item.get("status", {}).get("containerStatuses") or []) + \
                      (item.get("status", {}).get("initContainerStatuses") or []):
            waiting = (status.get("state") or {}).get("waiting") or {}
            reason = str(waiting.get("reason") or "")
            if reason in TERMINAL_POD_REASONS:
                return pod, reason, str(waiting.get("message") or "").strip()
    return None


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
#: 6.0 was a guess, and this is what came of checking it rather than leaving the
#: guess in place:
#:
#:   - The operator's own documentation states NO minimum server version. Its
#:     supported-features list covers replica sets, SCRAM, TLS and scaling with no
#:     version window attached, so there is nothing there to copy.
#:   - Its image repository, quay.io/mongodb/mongodb-community-server, publishes
#:     tags back to 4.4 -- so the pullable floor is 4.4, not 6.0.
#:   - 6.0 is verified live, via the operator, end to end.
#:
#: So 6.0 stays: deliberately conservative, now evidenced rather than invented.
#: Nothing rc-repro supports goes near it -- Rocket.Chat 7.x and 8.x pair with
#: MongoDB 7.0, 8.0 and 8.2, and all three are verified live -- so the cost of being
#: conservative here is zero and the cost of being wrong the other way is a
#: workspace that never starts.
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
    # clusterMonitor is what makes MONGO_OPLOG_URL work, and it is what Rocket.Chat's
    # OWN chart grants: templates/mongodb-init-configmap.yaml runs
    #   db.getSiblingDB('<db>').grantRolesToUser('<user>',
    #       [{{ role: 'clusterMonitor', db: 'admin' }}])
    # against its bundled MongoDB, commenting "having clusterMonitor role shouldn't
    # hurt". readWrite on the app database alone cannot read `local`, so Rocket.Chat
    # below 8.x -- the only versions that still tail the oplog -- could authenticate
    # and then fail to tail. This is upstream's answer, not an invention.
    - name: clusterMonitor
      db: admin
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


#: The Secret the chart reads its MongoDB URL from, instead of the URL sitting in
#: the release's values where `helm get values rocketchat` prints it in full --
#: password included -- to anyone with read access to the namespace. The workspace
#: credentials are deliberately weak, but the operator's are generated per workspace
#: and are the thing SCRAM auth exists to protect.
MONGO_URL_SECRET = "rocketchat-mongodb-url"


#: The EE registration token travels in its own Opaque Secret, referenced from the
#: container env by `valueFrom`. NEVER in helm values or `extraEnv` as a literal:
#: values are readable with `helm get values` by anyone who can reach the release, and
#: rc-repro also writes them to `repros/<n>/kubernetes/values.yaml` for a human to
#: read -- which is exactly the exposure the MongoDB password was moved out of. The
#: manifest goes to `kubectl apply -f -` on stdin so the token never appears in argv
#: either, where `ps` would show it to every user on the box.
REG_TOKEN_SECRET = "rc-repro-reg-token"
REG_TOKEN_KEY = "token"


def reg_token_secret_manifest(name: str, *, token: str, owner: str = "") -> str:
    """An Opaque Secret holding the cloud registration token."""
    import yaml as _yaml
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": REG_TOKEN_SECRET, "labels": _labels(name, owner)},
        "type": "Opaque",
        "stringData": {REG_TOKEN_KEY: token},
    }
    return _yaml.safe_dump(body, sort_keys=False)


def mongo_url_secret_manifest(name: str, *, mongo_url: str, oplog_url: str,
                              owner: str = "") -> str:
    """A Secret holding the connection strings, for `existingMongodbSecret`.

    BOTH keys, always. Chart 6.26.0 reads `mongo-uri` and `mongo-oplog-uri` from
    this Secret unconditionally once `existingMongodbSecret` is set -- there is no
    `if oplog` around the second one -- so omitting it on a chart that wants it
    leaves the pod unable to start on a missing key.
    """
    labels = _labels(name, owner)
    lab = "\n".join(f"    {k}: {v}" for k, v in labels.items())
    return f"""apiVersion: v1
kind: Secret
metadata:
  name: {MONGO_URL_SECRET}
  labels:
{lab}
type: Opaque
stringData:
  mongo-uri: {mongo_url}
  mongo-oplog-uri: {oplog_url}
"""


def mongo_tool_auth(name: str, *, context: str) -> list[str]:
    """`mongodump`/`mongorestore` flags for an operator-managed MongoDB.

    The operator enables SCRAM, so an unauthenticated `mongodump` fails with
    "Command listCollections requires authentication" -- which is exactly what it did:
    backup was broken on the operator path, the path the official guide recommends,
    while working perfectly on the hand-written StatefulSet that has no auth. The
    audit found it because it ran both.

    The password is read back out of the Secret rather than kept anywhere: it is
    generated at create time and deliberately never written to disk, which is a
    property worth keeping (`repros/<n>/kubernetes/` is checked for exactly this).

    Empty list when there is no operator, so the StatefulSet path is unchanged.
    """
    res = run(["kubectl", "--context", context, "-n", namespace_for(name),
               "get", "secret", "mongodb-rocketchat-password",
               "-o", "jsonpath={.data.password}"], own=is_ours(context))
    encoded = (res.stdout or "").strip()
    if res.returncode != 0 or not encoded:
        return []
    try:
        password = base64.b64decode(encoded).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError):
        return []
    if not password:
        return []
    # authenticationDatabase is MONGO_APP_DB, not `admin`: the app user is DEFINED
    # in the application database, and authenticating against admin looks for a user
    # that was never created there. The same mistake, on authSource, is recorded in
    # operator_mongo_url's docstring.
    return ["--username", MONGO_APP_USER, "--password", password,
            "--authenticationDatabase", MONGO_APP_DB]


def operator_mongo_url(namespace: str, password: str, *, oplog: bool = False) -> str:
    """The authenticated URI, built as the guide builds it.

    The operator names its service `<name>-svc`, not `<name>` -- a detail worth
    stating, because the hand-written path uses the bare name and the two are not
    interchangeable.

    `authSource` is on BOTH forms, and the oplog one is where it was missing. The
    oplog URL addresses the `local` DATABASE, but the user is defined in
    `{MONGO_APP_DB}` -- and without authSource the driver authenticates against the
    database in the path, so it looked for a `{MONGO_APP_USER}` in `local` that has
    never existed there. Two faults in one line: even once authentication succeeded,
    reading `local` needs clusterMonitor, which the user now carries.

    Only Rocket.Chat below 8.x uses this at all; 8.x dropped oplog tailing. That is
    also why it survived so long unnoticed -- every live run of the operator path
    used 8.5.1, where the URL is never emitted.
    """
    db = "local" if oplog else MONGO_APP_DB
    return (f"mongodb://{MONGO_APP_USER}:{password}@"
            f"{MONGO_SERVICE}-0.{MONGO_SERVICE}-svc.{namespace}.svc.cluster.local:27017/"
            f"{db}?replicaSet={MONGO_SERVICE}&authSource={MONGO_APP_DB}")


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


def mongodb_resources(context: str) -> list[str]:
    """Namespaces that still hold a `MongoDBCommunity`, i.e. still need the operator.

    Empty when the CRD is not installed at all, which is the answer, not an error --
    `--ignore-not-found` keeps a cluster that never had the operator from looking like
    a failure.
    """
    res = run(["kubectl", "--context", context, "get", "mongodbcommunity", "-A",
               "--ignore-not-found", "-o",
               "jsonpath={range .items[*]}{.metadata.namespace}{\"\\n\"}{end}"],
              timeout=APPLY_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        return []
    return sorted({ns for ns in (res.stdout or "").split() if ns})


def release_installed(release: str, namespace: str, context: str) -> bool:
    """Whether a helm release is in this cluster. Used before uninstalling one, so
    "there was nothing there" is reported as nothing rather than as a failure."""
    res = run(["helm", "list", "--kube-context", context, "-n", namespace, "-q"],
              timeout=APPLY_TIMEOUT, own=is_ours(context))
    return release in (res.stdout or "").split()


def operator_installed(context: str) -> bool:
    """Whether the shared MongoDB operator release is in this cluster."""
    return release_installed(OPERATOR_RELEASE, OPERATOR_NAMESPACE, context)


def remove_operator(*, context: str, excluding: str = "",
                    emit: Emit = null_emit) -> bool:
    """Uninstall the shared MongoDB operator once nothing is using it any more.

    `down --volumes` means delete everything, so leaving an operator running afterwards
    is the wrong answer -- but three things about it are shared, and the ORDER is what
    makes removing it safe rather than destructive.

    **The custom resources go first, and they already have.** This is called after the
    workspace's namespace is deleted, which takes its `MongoDBCommunity` with it while
    the operator is still alive to clear the finalizer. Removing the operator first
    deadlocks on a resource nothing is left to finalise, and the namespace then sits in
    Terminating forever -- the identical failure `remove_monitoring` records for a
    GrafanaFolder, observed there on the first real uninstall.

    **The reference count is a real check, not bureaucracy.** With a second workspace
    still holding a `MongoDBCommunity`, removing the operator leaves its finalizer with
    nothing to clear it, so a later `down --volumes` on THAT workspace hangs. In the
    normal one-workspace case the check passes immediately and the operator goes.

    **The CRD stays.** `helm uninstall` does not remove it anyway, and deleting it would
    delete every `MongoDBCommunity` in the cluster -- every other workspace's database,
    instantly. It is inert without the operator and the next `--mongo-operator` reuses
    it. Same for the namespace: `rc-repro-system` is shared with the monitoring stack.
    """
    if not operator_installed(context):
        return False
    # `excluding` for the same reason `remove_monitoring` takes it: the workspace being
    # destroyed must not be counted as still needing the operator. Its MongoDBCommunity
    # is normally gone by the time the namespace finishes, but "normally" is a race, and
    # losing it leaves the operator running with nothing at all using it.
    still = [ns for ns in mongodb_resources(context) if ns != excluding]
    if still:
        info(emit, "leaving the MongoDB operator up — still used by "
                   + ", ".join(n.removeprefix(NAMESPACE_PREFIX) for n in still),
             phase="teardown")
        return False
    info(emit, f"uninstalling the MongoDB operator from {OPERATOR_NAMESPACE} — "
               "nothing is using it now", phase="teardown")
    res = run(["helm", "uninstall", OPERATOR_RELEASE, "--kube-context", context,
               "-n", OPERATOR_NAMESPACE, "--wait", "--timeout", "5m"],
              timeout=INSTALL_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        # Not an error worth failing a teardown for: the workspace is already gone and
        # the next `--mongo-operator` runs `upgrade --install`, which repairs it.
        warn(emit, "the MongoDB operator could not be uninstalled and is still "
                   f"running in {OPERATOR_NAMESPACE}: " + why(res), phase="teardown")
        return False
    return True


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


def remove_monitoring(*, context: str, excluding: str = "",
                     emit: Emit = null_emit) -> bool:
    """Uninstall the shared stack. Returns False if another workspace still wants it.

    Shared, so `--off` on one workspace must not blind the others. This is the whole
    behavioural difference from Compose, where the stack belongs to a project and
    detaching it is unambiguous.

    `excluding` is the namespace of a workspace being DESTROYED, and it exists because
    the count reads a label on the namespace while `workspace_namespaces` does not
    filter by phase: a namespace still Terminating is still listed and still labelled,
    so a teardown asking "does anyone else want this?" would be told yes by the
    workspace it is in the middle of deleting.
    """
    # Nothing installed is nothing to report. Without this, `monitor --off` on a
    # workspace that never had monitoring ran the uninstall anyway, helm answered
    # "Release not loaded: monitoring: release: not found", and the fallback path
    # announced that "the monitoring stack needed its finalizers cleared by hand" --
    # a frightening sentence about wreckage that did not exist. Measured on a live
    # detach after an attach that had failed.
    if not release_installed(MONITORING_RELEASE, MONITORING_NAMESPACE, context):
        return False
    others = [n for n in workspace_namespaces(context)
              if n != excluding and monitoring_wanted(n, context=context)]
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


# --- stop / start: scale to zero and back ----------------------------------------

#: Where the replica count lives while a workspace is stopped. Kubernetes has no
#: "stopped" state -- scaling to 0 is the whole mechanism -- so the number that was
#: there has to be written down or `start` cannot put it back. An annotation on the
#: namespace rather than repro.json, because `kubectl scale` by hand is a legitimate
#: thing for someone to do and the cluster should carry its own truth.
SCALE_ANNOTATION = "rc-repro.io/replicas-before-stop"


def _scalables(namespace: str, context: str) -> list[str]:
    """Every workload in the namespace that has a replica count, as `kind/name`.

    The MongoDB StatefulSet is included deliberately. `docker compose stop` stops the
    database too, and a workspace that keeps 8Gi of MongoDB resident while claiming
    to be stopped is not stopped in the sense anyone means -- the whole reason to
    stop one on a laptop is to get the memory back.
    """
    res = run(["kubectl", "--context", context, "-n", namespace, "get",
               "deployment,statefulset", "-o", "name"], own=is_ours(context))
    return [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]


def scale_workspace(name: str, *, replicas: int, context: str,
                    emit: Emit = null_emit) -> int:
    """Scale every workload in a workspace to `replicas`. Returns how many it moved.

    `--all` is not used: it would also catch anything a preset or a person added,
    and scaling something back up to a number it never had is worse than leaving it.
    """
    namespace = namespace_for(name)
    targets = _scalables(namespace, context)
    if not targets:
        raise NotFoundError(
            f"{name!r} has no workloads to scale -- it was `down`ed, so there is "
            f"nothing to stop or start. Recreate it with `rc-repro up --name {name}`")
    res = run(["kubectl", "--context", context, "-n", namespace, "scale",
               f"--replicas={replicas}", *targets],
              timeout=APPLY_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        raise DockerError(f"could not scale {name!r}: " + why(res))
    return len(targets)


def desired_replicas(name: str, *, context: str) -> int:
    """The Rocket.Chat Deployment's DESIRED replica count, or -1 if unknown.

    `.spec.replicas`, not `.status`: the question is what was asked for, so that a
    workspace scaled to zero reads as stopped the instant `stop` returns rather than
    once the pods have finished terminating.
    """
    res = run(["kubectl", "--context", context, "-n", namespace_for(name), "get",
               "deployment", f"{RELEASE}-rocketchat", "-o",
               "jsonpath={.spec.replicas}"], own=is_ours(context))
    try:
        return int((res.stdout or "").strip())
    except ValueError:
        return -1


#: Up to a minute for pods to terminate. MongoDB gets a grace period and uses it.
POD_GONE_TRIES = 30
POD_GONE_INTERVAL = 2.0


def stop_workspace(name: str, *, context: str, emit: Emit = null_emit,
                   sleep=time.sleep) -> int:
    """Scale to zero, remembering what to come back to, and wait for it."""
    namespace = namespace_for(name)
    # Recorded BEFORE scaling, or the number read back is the zero we just wrote.
    before = {}
    for target in _scalables(namespace, context):
        res = run(["kubectl", "--context", context, "-n", namespace, "get", target,
                   "-o", "jsonpath={.spec.replicas}"], own=is_ours(context))
        count = (res.stdout or "").strip()
        if count and count != "0":
            before[target] = count
    if before:
        run(["kubectl", "--context", context, "-n", namespace, "annotate",
             "--overwrite", "namespace", namespace,
             f"{SCALE_ANNOTATION}={json.dumps(before)}"], own=is_ours(context))
    moved = scale_workspace(name, replicas=0, context=context, emit=emit)
    # An operator-managed MongoDB CANNOT be scaled to zero: the operator reconciles
    # its StatefulSet straight back, and the Community operator has no way to pause
    # reconciliation. So its pods are expected to survive `stop`, and two things
    # follow that an audit caught this doing wrong.
    #
    # First, the wait below must not require them to go, or every stop and restart on
    # the operator path burns POD_GONE_TRIES * POD_GONE_INTERVAL seconds waiting for
    # something that will never happen, then continues anyway.
    #
    # Second and worse, `stop` documents itself as giving the memory back. On this
    # path it gives back Rocket.Chat's and not MongoDB's, and said nothing -- so the
    # user who ran `stop` to free the box was told it had worked. Say it instead.
    survivors = mongo_pods_the_operator_keeps(name, context=context)
    if survivors:
        warn(emit, f"MongoDB stays up: it is operator-managed, and the operator "
                   f"recreates it as fast as it is scaled down (there is no way to "
                   f"pause its reconciliation). Rocket.Chat's memory is freed, "
                   f"MongoDB's is not — `rc-repro down --name {name} --volumes` is "
                   f"what reclaims it.", phase="done")
    # WAIT for the pods to actually go, rather than returning on the API call.
    # `restart` is stop-then-start, so returning early scales back up while the old
    # pods are still Terminating -- and the readiness check then finds one of THOSE,
    # reports ready, and the port-forward attaches to a pod on its way out. The URL
    # answers nothing. This is the fourth time on this runtime that trusting a
    # readiness signal over the actual state has produced a workspace that looks up
    # and is not.
    for _ in range(POD_GONE_TRIES):
        res = run(["kubectl", "--context", context, "-n", namespace, "get", "pods",
                   "-o", "name"], own=is_ours(context))
        left = [p for p in (res.stdout or "").split() if p.strip()]
        if not [p for p in left if p not in survivors]:
            break
        sleep(POD_GONE_INTERVAL)
    info(emit, f"scaled {moved} workload(s) to zero", phase="done")
    return moved


def mongo_pods_the_operator_keeps(name: str, *, context: str) -> set[str]:
    """Pod names (as `pod/<n>`) that an operator-managed MongoDB will not give up.

    Empty for the hand-written StatefulSet, which scales to zero like anything else.
    """
    namespace = namespace_for(name)
    res = run(["kubectl", "--context", context, "-n", namespace,
               "get", "mongodbcommunity", "-o", "name"], own=is_ours(context))
    if res.returncode != 0 or not (res.stdout or "").strip():
        return set()
    pods = run(["kubectl", "--context", context, "-n", namespace, "get", "pods",
                "-l", f"app={MONGO_SERVICE}-svc", "-o", "name"],
               own=is_ours(context))
    return {p for p in (pods.stdout or "").split() if p.strip()}


def start_workspace(name: str, *, context: str, emit: Emit = null_emit) -> int:
    """Scale back to whatever was recorded, or to 1 if nothing was.

    A workspace stopped by hand (`kubectl scale ... --replicas=0`) carries no
    annotation, and refusing to start it would be pedantry -- 1 is what `up` would
    have given it.
    """
    namespace = namespace_for(name)
    # Read as JSON and pick the key in Python, rather than through jsonpath.
    #
    # jsonpath was how this went wrong: the key is `rc-repro.io/replicas-before-stop`
    # and the code escaped the SLASH while leaving the DOT in `rc-repro.io`
    # unescaped -- so jsonpath read `rc-repro` and `io/...` as separate path segments,
    # matched nothing, and returned empty. Measured against a live cluster: the
    # slash-escaped form returns "" while the dot-escaped form returns the
    # annotation. Every stop/start therefore restored the DEFAULT of 1, silently
    # turning a `--replicas 2` workspace into a one-replica one, and the annotation
    # written to prevent exactly that was never once read back.
    #
    # No escaping at all is a rule that cannot be got subtly wrong.
    res = run(["kubectl", "--context", context, "-n", namespace, "get", "namespace",
               namespace, "-o", "json"], own=is_ours(context))
    before = {}
    try:
        annotations = (json.loads(res.stdout or "{}").get("metadata", {})
                       .get("annotations") or {})
        before = json.loads(annotations.get(SCALE_ANNOTATION) or "{}")
    except ValueError:
        before = {}
    if not isinstance(before, dict):
        before = {}
    targets = _scalables(namespace, context)
    if not targets:
        raise NotFoundError(
            f"{name!r} has no workloads to start -- it was `down`ed. Recreate it "
            f"with `rc-repro up --name {name}`")
    for target in targets:
        count = str(before.get(target, "1"))
        res = run(["kubectl", "--context", context, "-n", namespace, "scale",
                   f"--replicas={count}", target],
                  timeout=APPLY_TIMEOUT, own=is_ours(context))
        if res.returncode != 0:
            raise DockerError(f"could not scale {target}: " + why(res))
    info(emit, f"scaled {len(targets)} workload(s) back up", phase="done")
    return len(targets)


# --- backup/restore: the same five shapes, through kubectl ------------------------
#
# `backup.py` needs exactly five things from a runtime, and its logic -- the bundle
# format, the manifest, the safety checks -- is runtime-agnostic. So these mirror
# runner's compose_exec_* signatures rather than inventing a shape, and backup.py
# picks between them. Anything else there stays shared.

def exec_capture(name: str, argv: list[str], *, context: str,
                 timeout: float | None = None) -> tuple[int, str]:
    """Run a command in the MongoDB pod, capturing stdout. Mirrors
    runner.compose_exec_capture, including its (1, "") on failure."""
    res = run(["kubectl", "--context", context, "-n", namespace_for(name), "exec",
               f"{MONGO_SERVICE}-0", "-c", "mongod", "--", *argv],
              timeout=timeout or APPLY_TIMEOUT, own=is_ours(context))
    return res.returncode, (res.stdout or "")


def exec_to_file(name: str, argv: list[str], dest: Path, *, context: str,
                 timeout: float | None = None) -> tuple[int, str]:
    """Run a command in the MongoDB pod, writing its RAW stdout to `dest`.

    Binary-safe, and that is the whole point: `mongodump --archive` emits BSON, and
    a text-mode decode would corrupt it silently -- the dump would restore with
    errors nobody could trace back here. So stdout goes straight to the file handle
    and never through Python's text layer.

    `kubectl exec` without `-t`, deliberately: a TTY translates newlines and would
    do the same damage from the other end.
    """
    argv_full = ["kubectl", "--context", context, "-n", namespace_for(name), "exec",
                 f"{MONGO_SERVICE}-0", "-c", "mongod", "--", *argv]
    try:
        with dest.open("wb") as handle:
            proc = subprocess.run(argv_full, stdout=handle, stderr=subprocess.PIPE,
                                  timeout=timeout or INSTALL_TIMEOUT, check=False,
                                  env=owned_env() if is_ours(context) else None)
        return proc.returncode, (proc.stderr or b"").decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def exec_from_file(name: str, argv: list[str], src: Path, *, context: str,
                   timeout: float | None = None) -> tuple[int, str]:
    """Run a command in the MongoDB pod with `src` piped to its stdin.

    `-i` is required and easy to forget: without it kubectl closes stdin
    immediately and `mongorestore --archive` reads an empty archive, reports
    success, and restores nothing at all.
    """
    argv_full = ["kubectl", "--context", context, "-n", namespace_for(name), "exec",
                 "-i", f"{MONGO_SERVICE}-0", "-c", "mongod", "--", *argv]
    try:
        with src.open("rb") as handle:
            proc = subprocess.run(argv_full, stdin=handle,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=timeout or INSTALL_TIMEOUT, check=False,
                                  env=owned_env() if is_ours(context) else None)
        return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def scale_rocketchat(name: str, *, replicas: int, context: str) -> int:
    """Scale ONLY Rocket.Chat, leaving MongoDB up.

    What `stop_workspace` does is wrong for a backup: a dump needs the database
    running and only its writers quiesced. This is the Kubernetes counterpart of
    runner.stop_services, and the same distinction runner draws between `stop()` and
    `stop_services()`.
    """
    namespace = namespace_for(name)
    targets = [ln.strip() for ln in
               (run(["kubectl", "--context", context, "-n", namespace, "get",
                     "deployment", "-l", "app.kubernetes.io/name=rocketchat",
                     "-o", "name"], own=is_ours(context)).stdout or "").splitlines()
               if ln.strip()]
    if not targets:
        return 0
    res = run(["kubectl", "--context", context, "-n", namespace, "scale",
               f"--replicas={replicas}", *targets],
              timeout=APPLY_TIMEOUT, own=is_ours(context))
    return res.returncode
