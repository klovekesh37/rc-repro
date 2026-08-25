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
import socket
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
    #: Which cluster facts could not be READ, as opposed to read as empty. Same
    #: distinction as `probe_failed` below, one level down: a restricted kubeconfig
    #: can pass `/readyz` and then be refused every actual read, and reporting those
    #: as "no StorageClass, no ingress, 0 nodes" made doctor state three false facts
    #: under a green verdict. Named here so the verdict can see them.
    unreadable: list[str] = field(default_factory=list)
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


def helm_env(context: str) -> dict[str, str]:
    """Helm's own state is ALWAYS rc-repro's; the kubeconfig belongs to whoever owns the
    cluster.

    `owned_env` sets both at once, and that conflation was a real defect. Every helm call
    was either fully owned or not at all, so on a cluster rc-repro ADOPTED the chart repo
    went into rc-repro's Helm home (`ensure_repo`, `own=True`) while the install that
    needed it read the USER's (`is_ours(context)` is False there). Two different
    repositories.yaml, and the create failed at 60% -- after the namespace, the operator
    and MongoDB had all been built -- with

        helm install failed: Error: repo rocketchat not found

    then rolled the namespace back. The MongoDB operator path happened to work only
    because BOTH its halves used the user's home: consistent, and also wrong, because it
    wrote repositories into the home `owned_env` exists to keep rc-repro out of.

    So HELM_* always points at rc-repro's directories and KUBECONFIG is left as the user
    has it unless the cluster is ours. kubectl needs no equivalent: it has no client
    state worth redirecting, and `own=is_ours(context)` already picks its kubeconfig.
    """
    env = owned_env()
    if not is_ours(context):
        outside = os.environ.get("KUBECONFIG")
        if outside is None:
            env.pop("KUBECONFIG", None)
        else:
            env["KUBECONFIG"] = outside
    return env


def is_ours(context: str) -> bool:
    """Whether a context names the cluster rc-repro created."""
    return context == CONTEXT


def which(tool: str) -> str:
    """Absolute path to a tool, or "" -- the one place PATH is consulted."""
    return shutil.which(tool) or ""


def run(argv: list[str], *, timeout: float = PROBE_TIMEOUT,
        own: bool = False,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
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
                              env=env if env is not None
                              else (owned_env() if own else None))
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, returncode=127, stdout="",
                                           stderr=str(exc))


#: The three answers an external tool can give about a thing, and the reason this
#: module has a type for them at all. `namespace_labels` has said it since it was
#: written -- "'I could not ask' and 'it is not there' are different answers and only
#: one of them is safe to act on" -- and it was the ONLY function that implemented it.
#: Five others collapsed the three back to two, each by reading a non-zero exit as a
#: negative answer, and that one habit produced a `down --volumes` that deleted a
#: workspace's record while its namespace was still there, a `prune` that reported
#: namespaces reclaimed that were still Terminating, and a `doctor` that reported "0
#: nodes, no StorageClass, no ingress" for a cluster with all three.
#:
#: MEASURED, not assumed: kubectl exits 1 for BOTH "namespace absent" and "context
#: does not exist", so no threshold on the exit code can separate them. `.docs/design/
#: k8s-answer-contract.md` has the runs.
REFUSED, ABSENT, PRESENT = "refused", "absent", "present"


@dataclass(frozen=True)
class Answer:
    """What the cluster said about one thing: PRESENT, ABSENT or REFUSED.

    `absent` is a fact the cluster asserted and is safe to act on. `refused` means
    nobody knows -- a wrong context, an expired credential, an RBAC denial, an API
    server that is not answering -- and acting on it as though it meant "no" is the
    defect this type exists to make unwriteable.

    There is deliberately no truthiness. `if answer:` would be the very collapse
    being prevented, so a caller has to name which of the three it means.
    """

    state: str
    doc: dict | None = None
    res: subprocess.CompletedProcess | None = None

    @property
    def present(self) -> bool:
        return self.state == PRESENT

    @property
    def absent(self) -> bool:
        return self.state == ABSENT

    @property
    def refused(self) -> bool:
        return self.state == REFUSED

    @property
    def items(self) -> list[dict]:
        """The objects of a list read; empty for a list that legitimately holds none.

        Empty is only ever "the cluster has none of these" -- a refused read never
        reaches here, because `refused` is a separate state and not an empty list.
        That equivalence is exactly what made `doctor` report "0 nodes".
        """
        return list(((self.doc or {}).get("items")) or [])

    def why(self, *, limit: int = 400) -> str:
        return why(self.res, limit=limit) if self.res is not None else ""

    def require(self, what: str, *, context: str) -> "Answer":
        """Raise unless the cluster actually answered. For paths that must not guess.

        The wording is `namespace_labels`' own, because it was right: it names the
        cluster, quotes the tool, and says why a guess is not on offer.
        """
        if self.refused:
            raise DockerError(
                f"could not ask cluster {context!r} about {what}: " + self.why()
                + " — refusing to guess, because 'I cannot ask' and 'it is not "
                  "there' are different answers and only one of them is safe to "
                  "act on")
        return self


def _classify(res: subprocess.CompletedProcess, *, single: bool) -> Answer:
    """Turn a finished kubectl into one of the three answers.

    The rule, and the only rule: **a zero exit means the API server answered.**
    Absence is then read from the PAYLOAD -- empty for a single object fetched with
    `--ignore-not-found` -- and never from the exit code.
    """
    if res.returncode != 0:
        # SECONDARY, AND ONLY EVER SECONDARY: the API server's own reason code.
        # `--ignore-not-found` makes an absent object exit 0 with an empty payload, so
        # reaching here at all means either a kubectl older than the flag or a call
        # site that did not pass it -- and in both cases the server's `(NotFound)` is
        # still a real assertion of absence, so throwing it away would be its own bug.
        #
        # The flag stays the primary test because THIS test was once the only one and
        # got it wrong: the first cut matched `"not found"`, which also matches
        # `Error in configuration: context was not found for specified context: ...`
        # -- a kubeconfig problem read as an absent namespace, the exact confusion
        # this module exists to prevent. Only the API server says
        # `Error from server (NotFound)`; a client-side configuration error never does.
        if single and "fromserver(notfound)" in why(res).lower().replace(" ", ""):
            return Answer(ABSENT, None, res)
        return Answer(REFUSED, None, res)
    raw = (res.stdout or "").strip()
    if single and not raw:
        # `--ignore-not-found` turns "not there" into exit 0 with nothing on stdout,
        # which is the whole reason it is passed. `-o json` rather than a jsonpath
        # because a jsonpath can be legitimately empty for an object that EXISTS --
        # a namespace with no labels reads identically to no namespace at all, and
        # that collision would rebuild the bug inside the fix.
        return Answer(ABSENT, None, res)
    try:
        doc = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        # The server answered and we cannot parse it. That is not absence.
        return Answer(REFUSED, None, res)
    return Answer(PRESENT, doc if isinstance(doc, dict) else {}, res)


def ask_object(kind: str, name: str, *, context: str, namespace: str = "",
               timeout: float = PROBE_TIMEOUT) -> Answer:
    """Ask about ONE named object: PRESENT with its document, ABSENT, or REFUSED."""
    argv = ["kubectl", "--context", context]
    if namespace:
        argv += ["-n", namespace]
    argv += ["get", kind, name, "--ignore-not-found", "-o", "json"]
    return _classify(run(argv, own=is_ours(context), timeout=timeout), single=True)


def ask_list(kind: str, *, context: str, namespace: str = "",
             selector: str = "", timeout: float = PROBE_TIMEOUT) -> Answer:
    """Ask for a COLLECTION: PRESENT with `.items` (possibly empty), or REFUSED.

    There is no ABSENT for a list. A cluster that holds no StorageClass answers with
    an empty collection and exit 0; a cluster nobody could reach is REFUSED. Reading
    those as the same empty list is finding F-010.
    """
    argv = ["kubectl", "--context", context]
    if namespace:
        argv += ["-n", namespace]
    argv += ["get", kind, "-o", "json"]
    if selector:
        argv += ["-l", selector]
    return _classify(run(argv, own=is_ours(context), timeout=timeout), single=False)


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
    raw = [ln for ln in text.splitlines() if ln.strip()]
    lines = [ln.strip() for ln in raw]
    if not lines:
        return "no reason given"
    named = [i for i, ln in enumerate(lines)
             if re.match(r"^(error|Error|ERROR|fatal|Fatal)\b", ln)
             or "error:" in ln.lower()]
    # Warnings are not errors, however loudly they are printed. klog prefixes them
    # with I/W and a timestamp, and helm's install failure sits after them.
    named = [i for i in named if not re.match(r"^[IWD]\d{4} ", lines[i])]
    at = named[-1] if named else len(lines) - 1
    # THE CONTINUATION LINES ARE THE REASON, and they were being thrown away. This
    # docstring's premise -- diagnostics first, error last -- holds for klog and
    # inverts for a Helm TEMPLATE error, which announces the location on the `Error:`
    # line and puts the cause on the indented lines after it:
    #
    #     Error: UPGRADE FAILED: rocketchat/templates/podmonitor.yaml:24:18
    #       executing "..." at <.Values.podMonitor.enabled>:
    #         nil pointer evaluating interface {}.enabled
    #
    # So the file and line survived and "nil pointer" did not, which is why an
    # upgrade that failed for a knowable reason read as unexplained. Indentation is
    # the signal helm itself uses, and a line that starts a new diagnostic is never
    # indented.
    chosen = [lines[at]]
    for nxt in range(at + 1, len(raw)):
        if not raw[nxt][:1].isspace():
            break
        chosen.append(lines[nxt])
    return " ".join(chosen)[:limit]


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


def reachable(context: str = CONTEXT,
              timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether the API server answers. A cluster can exist and not respond --
    a stopped Docker, a half-deleted cluster, a machine that just woke up."""
    res = run(["kubectl", "--context", context, "get", "--raw", "/readyz"],
               own=is_ours(context), timeout=timeout)
    return res.returncode == 0 and "ok" in (res.stdout or "").lower()


def storage_classes(context: str = CONTEXT) -> tuple[list[str], str] | None:
    """(all storage class names, the default one).

    The guide's step 1, and it opens with the warning that matters here: "Local
    Kubernetes distributions such as Kind, K3s, and Minikube often ship without a
    storage provisioner enabled." Without one, a PVC stays Pending forever and the
    workspace never boots -- with no error that names storage.
    """
    # `None`, NOT `[], ""`. A cluster that could not be asked was reporting the same
    # answer as a cluster with no storage provisioner, so `doctor` told somebody with
    # a perfectly good default StorageClass that their volume would stay Pending --
    # and gave it a green verdict, because a warning about missing storage is a
    # warning and "I could not read your cluster" is not something it could say.
    seen = ask_list("storageclass", context=context)
    if seen.refused:
        return None
    names, default = [], ""
    items = seen.items
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


def loadbalancer_service(context: str = CONTEXT) -> tuple[str, str]:
    """`(service name, address)` for the first LoadBalancer Service that has one.

    Split out from `loadbalancer_address`, which returns a SENTENCE for the report --
    "traefik has 172.16.0.2". That was the only caller for a while, and the first
    caller that needed the address itself got the sentence and silently did nothing
    with it: `_is_local_address("traefik has 172.16.0.2")` is False, so a `doctor`
    check written against it never fired. A value shaped for a screen is not a value.

    This is also the address the Kubernetes HTTPS work needs -- the endpoint an edge
    passes TLS through to -- so it is a seam rather than a helper for one check.
    """
    res = run(["kubectl", "--context", context, "get", "svc", "-A", "-o",
               "jsonpath={range .items[?(@.spec.type=='LoadBalancer')]}"
               "{.metadata.name}={.status.loadBalancer.ingress[0].ip}"
               "{.status.loadBalancer.ingress[0].hostname} {end}"],
              own=is_ours(context))
    if res.returncode != 0:
        return "", ""
    for pair in (res.stdout or "").split():
        name, _, addr = pair.partition("=")
        if addr:
            return name, addr
    return "", ""


def loadbalancer_address(context: str = CONTEXT) -> str:
    """Evidence that a LoadBalancer works here, phrased for the report, or "".

    EVIDENCE, not a capability claim: "no address" means either no controller or nobody
    asked for one, and those cannot be told apart from outside. Reported as what it is,
    because a cluster that has given a Service a real address has demonstrably got a
    load balancer -- which on k3s is ServiceLB and on kind is nothing at all.

    For the address itself, call `loadbalancer_service`.
    """
    name, addr = loadbalancer_service(context)
    return f"{name} has {addr}" if addr else ""


@dataclass
class PortClaim:
    """A cluster holding one of THIS host's ports through a LoadBalancer Service.

    The failure this exists to name is silent by construction. k3s's ServiceLB
    (klipper) claims a host port with a `hostPort`, which is CNI portmap DNAT and
    not a socket bind -- so nothing conflicts, no pod fails, `ss` shows only
    docker-proxy, and `docker ps` shows nothing at all because k3s runs on
    containerd under systemd. rc-repro's edge then binds :80 and :443 perfectly
    happily and never receives a packet on either: kube-proxy's chain gets them
    first. Measured on the box where it took a manager's GUI dark for two days --
    every name answered the cluster's 404 and the cluster's certificate, while
    `rc-repro edge status` said the edge was running, because it was.

    `ports` is what the Service asks for, so both :80 and :443 are visible. That
    distinction is the whole diagnosis on an ACME failure: :443 taken breaks
    TLS-ALPN and serving, :80 taken breaks HTTP-01 -- and a check that only knew
    about :443 would have called the second one healthy.
    """
    context: str
    service: str
    address: str
    ports: list[int] = field(default_factory=list)


def is_local_address(ip: str) -> bool:
    """Whether `ip` is one of THIS host's own addresses.

    Bind is the test, because bind is the question: a socket can only bind an
    address the host holds. Cheaper and more portable than parsing `ip addr`, and
    it is the same check the kernel would make.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((ip, 0))
        return True
    except OSError:
        return False


def host_port_claim(context: str,
                    timeout: float = PROBE_TIMEOUT) -> PortClaim | None:
    """The LoadBalancer Service in `context` claiming ports on this host, or None.

    `-o json` rather than a jsonpath because three facts are wanted per Service --
    the address, the ports and where it lives -- and a jsonpath that emits three
    lists leaves the caller re-associating them by position.
    """
    res = run(["kubectl", "--context", context, "get", "svc", "-A", "-o", "json"],
              own=is_ours(context), timeout=timeout)
    if res.returncode != 0:
        return None
    try:
        items = json.loads(res.stdout or "{}").get("items") or []
    except (ValueError, AttributeError, TypeError):
        return None
    for svc in items:
        spec = svc.get("spec") or {}
        if spec.get("type") != "LoadBalancer":
            continue
        ingress = ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        addr = next((i.get("ip") or i.get("hostname") or "" for i in ingress), "")
        # A LoadBalancer with no address, or one on an address this host does not
        # hold, takes nothing from anybody -- that is a cloud provider's job or a
        # controller that never answered.
        if not addr or not is_local_address(addr):
            continue
        ports = sorted({p["port"] for p in spec.get("ports") or []
                        if isinstance(p.get("port"), int)})
        if not ports:
            continue
        meta = svc.get("metadata") or {}
        where = f"{meta.get('namespace', 'default')}/{meta.get('name', '?')}"
        return PortClaim(context=context, service=where, address=addr, ports=ports)
    return None


def port_claiming_cluster(ports: tuple[int, ...] = (80, 443),
                          contexts: tuple[str, ...] = (),
                          timeout: float = PROBE_TIMEOUT) -> PortClaim | None:
    """The first reachable cluster claiming any of `ports` on this host, or None.

    Both candidates are checked, and the one rc-repro USES is not the interesting
    one: on the box where this was measured rc-repro was on kind, which has no
    LoadBalancer at all, while the k3s alongside it held the ports. A cluster
    rc-repro is not using can still take the edge's ports away.

    `timeout` exists because this also runs on the `edge start` path, where it is a
    WARNING and must cost nearly nothing. Four probes at the 8s default is half a
    minute added to starting a proxy -- on a box whose kubeconfig points at an
    unreachable remote cluster, which is a normal laptop. The no-kubectl exit comes
    first for the same reason: a Docker-only box must not pay for this at all.

    LIMIT, STATED RATHER THAN LEFT TO BE DISCOVERED: only LoadBalancer Services are
    inspected. The DNAT mechanism this whole function exists to catch is a hostPort,
    and a DaemonSet with a hostPort on 80/443 -- an ingress-nginx installed that way,
    for instance -- would claim the ports and not be found here. Default k3s uses a
    LoadBalancer, which is the case that took a manager's GUI dark for two days and is
    covered; the gap is real and narrower than it sounds.
    """
    if not shutil.which("kubectl"):
        return None
    for ctx in dict.fromkeys(contexts or (CONTEXT, active_context())):
        if not ctx or not reachable(ctx, timeout=timeout):
            continue
        claim = host_port_claim(ctx, timeout=timeout)
        if claim and set(claim.ports) & set(ports):
            return claim
    return None


def nodes_summary(context: str = CONTEXT) -> tuple[int, list[str]] | None:
    """(how many nodes, which architectures). Both matter, for different reasons.

    Node COUNT decides whether a node-local default StorageClass can bite: `local-path`
    on kind and k3s alike is `WaitForFirstConsumer`, so on more than one node a MongoDB
    pod rescheduled elsewhere cannot rebind its volume. ARCHITECTURE matters because on
    kind and k3s the node is this machine and on a managed cluster it need not be --
    `bitnamilegacy/mongodb`, which every Rocket.Chat below 8 pairs with, is amd64-only.
    """
    # "0 nodes" was the answer for a cluster nobody could read, and it is a
    # sentence doctor then printed next to a tick.
    seen = ask_list("nodes", context=context)
    if seen.refused:
        return None
    arches = [str((((it.get("status") or {}).get("nodeInfo") or {}).get("architecture")) or "")
              for it in seen.items]
    return len(seen.items), sorted({a for a in arches if a})


def ingress_classes(context: str = CONTEXT) -> list[str] | None:
    """Ingress controllers installed in this cluster.

    kind ships none -- verified on this box, `get ingressclass` returns "No
    resources found" -- which is why rc-repro installs Traefik into its OWN
    cluster and refuses `--domain` against yours rather than installing into it.
    """
    seen = ask_list("ingressclass", context=context)
    if seen.refused:
        return None
    return [str(((it.get("metadata") or {}).get("name")) or "")
            for it in seen.items
            if ((it.get("metadata") or {}).get("name"))]


def workspace_namespaces(context: str = CONTEXT) -> list[str] | None:
    """Namespaces rc-repro owns, selected by LABEL. `None` if it could not be asked.

    Never by name prefix -- see OWNER_LABEL_KEY.

    THIS RETURNED `[]` FOR A REFUSED READ, and `[]` is what "this cluster holds no
    rc-repro workspaces" looks like -- so a namespace-scoped RBAC identity, an 8s
    PROBE_TIMEOUT under load or a control-plane restart all read as an empty
    cluster. Seven callers act on that, three of them destructively:
    `remove_monitoring` uninstalls the SHARED Prometheus/Grafana stack while another
    workspace is using it, `mongodb_resources` does the same for the shared operator
    -- leaving other workspaces' MongoDBCommunity finalizers with nothing to clear,
    so THEIR teardown hangs -- and `create_workspace`'s `had_namespace` goes False,
    which makes a failed create delete a namespace `down` had kept, PVC and all.
    That last one is data loss, and the guard's own comment says it was observed.

    So the third answer exists and every caller has to say which way it leans. The
    Answer-type migration reached `delete_namespace`, `wait_namespace_gone`,
    `namespace_labels` and doctor's reads, and stopped just short of this one.
    """
    seen = ask_list("namespace", context=context, selector=OWNER_SELECTOR)
    if seen.refused:
        return None
    return sorted(str(((it.get("metadata") or {}).get("name")) or "")
                  for it in seen.items
                  if ((it.get("metadata") or {}).get("name")))


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
        else:
            # THE REASON WAS THROWN AWAY WITH THE EXCEPTION. `plan_cluster` raises
            # both for "no kind and no kubeconfig" and for "kubectl is pointed at
            # 'X', which is not answering", and dropping it left `context` empty --
            # so doctor fell through to "No Kubernetes cluster configured, install
            # kind or point kubectl at an existing cluster" for somebody whose
            # cluster IS configured and whose credentials are what need fixing. It
            # sent them to configure a second one.
            #
            # Naming what kubectl is pointed at restores doctor's own correct
            # branch, and where there genuinely is nothing this is still "" and the
            # message that was wrong before is right.
            out.context = active_context()
    out.provider = PROVIDER_KIND if out.context == CONTEXT else PROVIDER_EXTERNAL

    if not out.context or out.will_create:
        # Nothing to probe: the cluster this create would use does not exist yet, and
        # asking the OTHER cluster for its storage classes would describe a machine
        # rc-repro is not about to use.
        return out
    out.cluster_reachable = reachable(out.context)
    if not out.cluster_reachable:
        return out
    # EACH ANSWER READ, not assumed. `reachable()` probes /readyz, which a
    # restricted credential can usually still get -- so passing it is not evidence
    # that any of the reads below will be permitted.
    sc = storage_classes(out.context)
    if sc is None:
        out.unreadable.append("storage")
    else:
        out.storage_classes, out.default_storage_class = sc
    ic = ingress_classes(out.context)
    if ic is None:
        out.unreadable.append("ingress")
    else:
        out.ingress_classes = ic
    ns_seen = workspace_namespaces(out.context)
    if ns_seen is None:
        out.unreadable.append("namespaces")
    else:
        out.namespaces = ns_seen
    out.distribution = distribution(out.context)
    ns_ = nodes_summary(out.context)
    if ns_ is None:
        out.unreadable.append("nodes")
    else:
        out.node_count, out.architectures = ns_
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
    if "storage" in pre.unreadable:
        # NOT "has no StorageClass". `preflight` records that the read was refused;
        # this refused the create anyway and blamed the customer's cluster, which on a
        # namespace-scoped RBAC identity made rc-repro unusable AND sent the engineer
        # to ask a platform team to install a provisioner that was already there
        # (`standard (default) rancher.io/local-path`, measured). The lesson reached
        # doctor and stopped one function short of the blocker that refuses.
        #
        # Still a refusal, because a create whose volume cannot be predicted is not a
        # create worth starting -- but a refusal that names the right thing.
        return (f"Cluster {pre.context!r} would not let rc-repro read its "
                f"StorageClasses, so whether the workspace's volume can bind is "
                f"unknown. This is a permissions problem on the credential in your "
                f"kubeconfig, not a missing provisioner: `kubectl get storageclass` "
                f"as yourself says which.")
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
    if "ingress" in pre.unreadable:
        # Same as storage above: unknown is not absent.
        return (f"Cluster {pre.context!r} would not let rc-repro read its "
                f"IngressClasses, so whether --domain can be served is unknown. "
                f"Check the credential in your kubeconfig rather than the cluster.")
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
        # AND THE PROBE'S SECOND RETURN VALUE IS NOT DISCARDED. `clusters()` returns
        # (names, why_it_could_not_be_answered) precisely so a stopped Docker is not
        # read as an absent cluster -- and this read `[0]` alone. With kind installed,
        # Docker down and a healthy k3s beside it, `clusters()` answers ([], "...") so
        # `create` became True, `ensure_cluster` then failed on `kind create cluster`,
        # and steps 3 and 4 below -- which would have USED the cluster that works --
        # were never reached. `preflight()` inherited the same answer and reported
        # will_create=True.
        #
        # Not tested live, and said so: proving it means stopping the Docker daemon,
        # which on this box would take down an edge somebody else is serving from. The
        # mechanism is the same conflation `namespace_labels` was fixed for in v0.70.9,
        # and the remedy is the same -- do not act on "I could not ask" as though it
        # were "it is not there".
        names, unasked = clusters()
        if unasked:
            ours_now = cluster_context()
            if ours_now and reachable(ours_now):
                return ClusterPlan(context=ours_now,
                                   distribution=distribution(ours_now), create=False)
            active_now = active_context()
            if active_now and reachable(active_now):
                return ClusterPlan(context=active_now,
                                   distribution=distribution(active_now), create=False)
            raise PreflightError(
                f"kind is installed but could not be asked what exists ({unasked}), so "
                f"rc-repro cannot tell whether its cluster is there. `kubectl` is "
                + ("not pointed at a working cluster either" if not active_now else
                   f"pointed at {active_now!r}, which is not answering")
                + ". Usually this is Docker not running: start it, or point kubectl at "
                  "a cluster you already have.")
        return ClusterPlan(context=CONTEXT, distribution="kind",
                           create=CLUSTER_NAME not in names)
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


def ensure_kubeconfig(plan, emit: Emit = null_emit) -> str:
    """The context to use, with rc-repro's OWN kubeconfig guaranteed to know it.

    `ensure_cluster` was called only when a cluster had to be created -- and its own
    docstring says the export must happen either way, naming this exact case: "a
    different or fresh RC_REPRO_HOME". So the comment described the fix and the call
    site defeated it. With a kind cluster already up and a fresh home, every command
    failed with `context was not found for specified context: kind-rc-repro-local`
    and could not self-heal, because the only thing that writes that context ran
    later -- or, when the cluster existed, never.

    CLAUDE.md tells anyone writing a script to `export RC_REPRO_HOME=$(mktemp -d)`,
    so this was reachable by following our own instructions once a cluster existed.

    For a cluster rc-repro did not create there is nothing to export: the user's own
    kubeconfig already names it, and rewriting it is not ours to do.
    """
    if plan.create or plan.context == CONTEXT:
        # ALREADY DONE IS DONE. Two call sites reach this per create -- the preflight
        # in `lifecycle` needs the kubeconfig before it asks about the namespace, and
        # `create_workspace` needs the context -- so the banner appeared twice and,
        # worse, `ensure_cluster` took `repro_lock(CLUSTER_LOCK)` twice, doubling the
        # window a concurrent create waits on provisioning. Cheap to skip: if our own
        # kubeconfig already names this context and the API server answers, there is
        # nothing left for the export to do. A fresh RC_REPRO_HOME fails both halves
        # and still exports, which is the case that made this function necessary.
        if not plan.create and cluster_context() == plan.context \
                and reachable(plan.context):
            return plan.context
        return ensure_cluster(emit=emit)
    return plan.context


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
    # `None` (could not ask) must NOT read as "empty and therefore reclaimable".
    held = workspace_namespaces(ctx)
    return not reachable(ctx) or (held is not None and not held)


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
            if live is None:
                raise ConflictError(
                    f"cannot tell what cluster {CLUSTER_NAME} still holds: the "
                    f"namespace list could not be read. Refusing to delete a "
                    f"cluster that may hold somebody's workspace — check the "
                    f"cluster, or pass --force to take it and everything in it.")
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
#: Consecutive refused reads before the pod wait gives up and says so. A single
#: unlucky probe is not evidence of anything; nine of them in a row, which is what a
#: destroyed cluster produced, are.
POD_WAIT_REFUSALS = 4
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
    """Add and refresh the Rocket.Chat chart repo in rc-repro's own Helm home.

    `own=True` UNCONDITIONALLY, and that is correct rather than an oversight -- it has
    been read as a bug twice, so: every helm call that needs this repo reads it through
    `helm_env`, which always points HELM_* at rc-repro's directories whatever cluster is
    targeted. Writer and readers therefore agree by construction.

    The obvious-looking alternative, `own=is_ours(context)`, would fix the older
    mismatch by writing `rocketchat` into the USER's repositories.yaml -- mutating their
    Helm configuration as a side effect of running a repro tool, which is the one thing
    `owned_env` exists to prevent.

    No context is threaded in because none is needed: `repo add`, `repo update` and
    `search repo` contact no cluster at all, so KUBECONFIG is irrelevant to them.
    """
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
    # `own=True` for the same reason as `ensure_repo`, which is what populated this
    # index: the chart index lives in rc-repro's Helm home, and reading it touches no
    # cluster.
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


def _refuse_foreign_namespace(ns: str, name: str, labels: dict[str, str],
                              owner: str) -> None:
    """Raise unless `labels` prove this namespace is this workspace's."""
    if labels.get(OWNER_LABEL_KEY) != OWNER_LABEL_VALUE:
        raise ConflictError(
            f"namespace {ns} already exists and is not managed by rc-repro, so it "
            f"will not be adopted -- `down --volumes` deletes a namespace and its "
            f"volumes, and rc-repro cannot tell a namespace an older rc-repro made "
            f"from one somebody else made. Use a different --name, or delete it "
            f"yourself first: kubectl delete namespace {ns}")
    theirs = labels.get(WORKSPACE_LABEL, "")
    if theirs and theirs != name:
        raise ConflictError(
            f"namespace {ns} belongs to rc-repro workspace {theirs!r}, not {name!r}. "
            f"Two workspaces cannot share a namespace; use a different --name")
    held_by = labels.get(OWNER_OF_LABEL, "")
    if held_by and held_by != owner:
        # `owner` EMPTY IS NOT A MATCH. This was `held_by and owner and ...`, so a box
        # with no accounts -- where `_cli_actor()` returns "" because team mode is
        # opt-in -- skipped the comparison entirely and adopted a namespace stamped
        # with somebody else's name. Found by doing it on a live cluster: the label
        # survived (`--overwrite` only sets the keys it is given) and Rocket.Chat was
        # installed into their namespace anyway.
        #
        # Not knowing who you are is not evidence that you are the owner. An owner
        # label can only have been written by a box with accounts, so meeting one
        # while unable to identify yourself is exactly the case to refuse -- and
        # RC_REPRO_USER is the way to answer it.
        # An EMPTY owner means RC_REPRO_USER was not set, which is not the same as
        # "this box has no accounts" -- on a box with adm/mem/ro this said there were
        # none, and pointed at creating an account rather than at the variable the very
        # next sentence recommends. The refusal was right; the reason was not.
        mine = f"you are {owner!r}" if owner else (
            "rc-repro cannot tell who you are, because RC_REPRO_USER is not set")
        raise ConflictError(
            f"namespace {ns} belongs to {held_by!r} on this cluster and {mine}. "
            f"Adopting it would make `down --volumes` delete their data. Use a "
            f"different --name, or identify yourself with "
            f"`RC_REPRO_USER={held_by} rc-repro ...` if it is yours")


def namespace_labels(ns: str, *, context: str) -> dict[str, str] | None:
    """A namespace's labels, `None` if it does not exist -- and RAISES if the cluster
    could not be asked.

    The three answers are different and were being collapsed into two. "I asked and
    it is not there" is safe to act on; "I could not ask" is not, and a wrong
    kube-context, an expired credential or an RBAC denial all produce the second
    while looking like the first.
    """
    # NO LONGER MATCHED ON THE SERVER'S PROSE. This read `Error from server
    # (NotFound)` out of stderr, which worked and was still the wrong shape: the
    # comment it replaces records that the first attempt matched `"not found"` and
    # classified `Error in configuration: context was not found for specified
    # context: ...` as an absent namespace. Both versions depend on how kubectl
    # words itself, which is not a contract -- the skill file tells callers never to
    # branch on our prose for the same reason. `--ignore-not-found` makes the API
    # server state absence in the PAYLOAD, so there is no wording to get wrong.
    seen = ask_object("namespace", ns, context=context).require(
        f"namespace {ns}", context=context)
    if seen.absent:
        return None
    labels = ((seen.doc or {}).get("metadata") or {}).get("labels") or {}
    if not isinstance(labels, dict):
        return {}
    return {str(k): str(v) for k, v in labels.items()}


def assert_namespace_available(name: str, *, context: str, owner: str = "") -> None:
    """Refuse now if this workspace's namespace belongs to somebody else.

    A PREFLIGHT, so it runs before the write-ahead `repro.json`. `ensure_namespace`
    makes the same check -- it has to, since it is the thing that would do the
    labelling -- but by then a provisional record exists, and a refusal that created
    nothing should leave nothing behind. Observed: a refused create left an
    `incomplete` record that `prune` then offered to delete.

    IT ALSO READS THE PHASE, because ownership was the only thing it looked at. A
    namespace that is `Terminating` still carries its labels, so this passed, and the
    create then died several steps later on `unable to create new content in namespace
    X because it is being terminated` -- at exit 7, after emitting "namespace X" as a
    completed step for a namespace it had not created, and leaving exactly the record
    this preflight exists to prevent, holding a host port. The everyday trigger is
    `down --volumes` followed by re-creating under the same ticket name.

    Refused rather than waited out. Kubernetes finishes on its own and the wait is
    usually seconds, but a finalizer can wedge indefinitely, and a preflight that
    sometimes blocks for a minute is a worse contract than one that says retry.
    """
    seen = ask_object("namespace", namespace_for(name), context=context).require(
        f"namespace {namespace_for(name)}", context=context)
    if seen.absent:
        return
    meta = (seen.doc or {}).get("metadata") or {}
    phase = ((seen.doc or {}).get("status") or {}).get("phase") or ""
    if phase == "Terminating":
        raise ConflictError(
            f"namespace {namespace_for(name)} is still Terminating, so nothing can be "
            f"created in it yet. Kubernetes will finish on its own -- `kubectl get ns "
            f"{namespace_for(name)}` shows when -- then run this again. A workspace "
            f"just torn down with `--volumes` takes a few seconds to go.")
    labels = meta.get("labels") or {}
    _refuse_foreign_namespace(namespace_for(name), name,
                             {str(k): str(v) for k, v in labels.items()}
                             if isinstance(labels, dict) else {}, owner)


def ensure_namespace(name: str, *, context: str, owner: str = "",
                     emit: Emit = null_emit) -> str:
    """Create the workspace's namespace with its ownership labels.

    REFUSES A NAMESPACE THAT IS NOT OURS. This used to `create` (ignoring the result,
    so an existing one was fine) and then `label --overwrite` whatever was there --
    which stamped rc-repro's ownership onto a namespace it did not make, after which
    `down --volumes` would delete it and its PVCs.

    The realistic trigger is not a stranger: it is two rc-repro users on one adopted
    cluster. Name collisions are guarded through the local `repro.json`, and another
    user's home is invisible from here, so the namespace itself is the only evidence
    there is -- and overwriting the labels destroyed it.

    An UNLABELLED namespace is refused too, and that is a deliberate change of intent.
    The old docstring adopted one so "a namespace made by an older rc-repro gains
    them"; rc-repro cannot tell that from somebody else's namespace, and being wrong
    deletes data. The refusal names the manual step instead.
    """
    ns = namespace_for(name)
    existing = namespace_labels(ns, context=context)
    if existing is not None:
        _refuse_foreign_namespace(ns, name, existing, owner)
    else:
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
            chart_version: str = "", emit: Emit = null_emit) -> None:
    """`helm install` with values on stdin, so nothing is written to disk.

    The release is `rocketchat` -- the official docs' own name -- so every command
    in them works here by substituting the namespace.
    """
    # `upgrade --install`, not `install`. Bringing a workspace back up re-runs this
    # sequence, and plain `install` fails on a release that is already there --
    # which is every `up` over an existing workspace, and every retry after a
    # partial failure.
    # A RELEASE LEFT PENDING BY A KILLED OPERATION BLOCKS EVERY LATER ATTEMPT, and
    # helm's refusal says "another operation (install/upgrade/rollback) is in
    # progress" -- which is not true and sends the reader looking for a process that
    # does not exist. Freed here rather than reported, because the state is not
    # something the person asking for a workspace did or can be expected to fix, and
    # `up` retrying identically was the only thing rc-repro offered.
    try:
        clear_pending_release(RELEASE, namespace, context, emit=emit)
    except DockerError:
        # Could not even list the releases. Let the install below fail on its own
        # terms; its message is about the thing actually being attempted.
        pass
    # THE SAME UNDO THE SHARED RELEASES GET. `helm_rollback_on_failure` was applied
    # to the operator and the monitoring stack and not to the workspace's own release
    # -- the one a workspace actually is -- so a partly-applied Rocket.Chat upgrade
    # stayed partly applied. `clear_pending_release` above recovers it on the next
    # `up`, so this closes a gap rather than a dead end.
    argv = ["helm", "upgrade", "--install", RELEASE, CHART,
            "--kube-context", context, "-n", namespace, "--values", "-",
            *helm_rollback_on_failure()]
    if chart_version:
        argv += ["--version", chart_version]
    try:
        res = subprocess.run(argv, input=json.dumps(values), capture_output=True,
                             text=True, timeout=INSTALL_TIMEOUT, check=False,
                             env=helm_env(context))
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerError(f"helm install failed: {exc}") from exc
    if res.returncode != 0:
        raise CreateFailedError("helm install failed: "
                                + why(res))


def helm_rollback_on_failure() -> list[str]:
    """The flag that makes a failed install undo itself, named for the helm installed.

    **`--atomic` DOES NOT EXIST IN HELM 4** -- it is `--rollback-on-failure`, checked
    against the 4.2.3 on this box. So the usual advice for a wedged release ("just
    add --atomic") would itself fail here, and a flag helm does not recognise turns a
    working install into a broken one. `CORE_TOOLS` admits helm 3, which has
    `--atomic` and not the new name, so the flag is chosen rather than written down.

    Empty for a helm too old to be sure about: no flag is the behaviour we already
    had, and guessing wrong costs the install itself.
    """
    found = tool("helm")
    if not found.present or not found.version:
        return []
    return ["--rollback-on-failure"] if found.version >= (4, 0) else ["--atomic"]


def release_values(release: str, namespace: str, context: str) -> dict:
    """The values a release was installed with -- its `values.yaml`, from helm itself.

    `helm get values` returns exactly the user-supplied values, which is what a values
    file IS: the chart's computed defaults are its own business and come back only
    with `--all`. So this is the guide's "your values.yaml", read from the live
    release rather than rebuilt from our own records -- it cannot drift from what is
    actually deployed, and it needs no assumption that the copy `record_rendered`
    wrote is still current.
    """
    res = run(["helm", "get", "values", release, "--kube-context", context,
               "-n", namespace, "-o", "json"],
              timeout=APPLY_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        raise DockerError(
            f"could not read the values of release {release!r} in {namespace}: "
            + why(res))
    try:
        doc = json.loads(res.stdout or "null")
    except (ValueError, TypeError):
        doc = None
    # `helm get values` answers `null` for a release installed with no values of its
    # own. That is an empty values file, not a failure.
    return doc if isinstance(doc, dict) else {}


def upgrade_image(*, namespace: str, context: str, chart_version: str,
                  image_repo: str, tag: str, oplog: bool) -> None:
    """Move a release to a new Rocket.Chat image, the way the official guide does.

    The guide says: change `image.tag` in your values and run `helm upgrade`. This is
    that, with two differences it explicitly warns about or implies.

    First, the chart version IS pinned. The guide's own note says its command "does
    not pin a chart version, so it installs the latest Rocket.Chat Helm chart" --
    which for a tool whose entire purpose is reproducing a customer's exact version
    would quietly deploy different software than the one asked for.

    Second, the release's own values are carried over rather than a values file being
    re-rendered: everything else about the workspace -- the admin env, microservices,
    replica count, whether MongoDB comes from a Secret or a URL, a preset's settings
    -- was decided at create time, and rebuilding that set from scratch risks
    silently dropping a piece of it. The one value that must NOT be carried over is
    the oplog URL: Rocket.Chat 8 dropped oplog tailing and chart 7.0.0 removed the
    key, so it is explicitly cleared when the target no longer wants it.

    THIS IS THE GUIDE'S PROCEDURE LITERALLY: read the release's values file, change
    the image tag in it, `helm upgrade` with it. It was `--reuse-values`, which is the
    one thing the guide does not do, and two separate failures came of that.

    First, `--reuse-values` never applies the NEW chart's defaults -- helm's own
    words: "reuse the last release's values and merge in any overrides". A chart that
    ADDS a templated value then renders against a map with no such key. RC 7.4.0 ->
    7.10.15 crosses chart 6.23.2 -> 6.26.0, which added `podMonitor`, and it died on
    `nil pointer evaluating interface {}.enabled`.

    Second, and worse, it carries values that were only ever correct for the chart
    they were computed against. `container_security_context` exists because chart
    6.26.0 renders a pod-level `fsGroup` onto a CONTAINER and needs it deleted, while
    7.0.0 fixed that upstream and BREAKS if handed the deletion -- the same undeclared
    field from the other direction, as that docstring records. Carrying `fsGroup:
    null` across that upgrade reintroduces the bug it was written to fix.

    Note that this defeats `--reset-then-reuse-values` as well: that flag still
    re-applies the last release's values on top of the new defaults, so the stale
    per-chart workaround still arrives. Only recomputing it against the TARGET chart
    is correct, and supplying a whole values file is what makes that possible. So no
    reuse flag is passed here at all -- the file is complete, which is the guide's
    whole model.
    """
    values = release_values(RELEASE, namespace, context)
    # RECOMPUTED FOR THE TARGET CHART, never carried -- see the second paragraph.
    values.pop("containerSecurityContext", None)
    values.update(container_security_context(chart_version))
    image = values.get("image")
    values["image"] = {**(image if isinstance(image, dict) else {}),
                       "repository": image_repo, "tag": tag}
    if not oplog:
        # Rocket.Chat 8 dropped oplog tailing and chart 7.0.0 removed the key. With a
        # whole values file supplied, DELETING the key is how you say "not this any
        # more"; `--set ...=null` was the way to say it through a flag.
        values.pop("externalMongodbOplogUrl", None)
    argv = ["helm", "upgrade", RELEASE, CHART, "--kube-context", context,
            "-n", namespace, "--version", chart_version, "--values", "-"]
    try:
        res = subprocess.run(argv, input=json.dumps(values), capture_output=True,
                             text=True, timeout=INSTALL_TIMEOUT, check=False,
                             env=helm_env(context))
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
    # `namespace_labels`, not `workspace_namespaces`: that one returns [] both when
    # there is nothing there and when the cluster could not be asked, and this used
    # the second as proof of the first. A wrong kube-context, an expired credential
    # or an RBAC denial therefore reported "nothing to remove" -- after which the
    # caller deleted the local record and told the reader the namespace and its
    # PersistentVolumeClaim were gone, while all of it went on running with the only
    # record that knew about it destroyed. `namespace_labels` raises instead.
    try:
        labels = namespace_labels(ns, context=context)
    except DockerError:
        # A DELETED CLUSTER IS NOT AN UNREACHABLE ONE, and this is the difference the
        # refusal above cannot see. When the cluster is gone its kubeconfig entry
        # survives, so `kubectl get ns` fails with "connection refused" and
        # `namespace_labels` raises -- correctly, for a cluster that might be merely
        # down. But a namespace cannot outlive the cluster it lived in, so if the
        # cluster is PROVABLY absent the namespace is gone and there is nothing to
        # guess about.
        #
        # Without this there was no way to delete the record of a workspace whose
        # cluster had been removed: the namespace query cannot answer, so `teardown`
        # refused, so `doctor` went on reporting "Cluster 'rc-repro-local' is gone and
        # its workspaces cannot be reached" for ever. doctor already knew; this is the
        # same question asked by the delete path.
        #
        # Only on a cluster rc-repro owns, and only when the kind probe SUCCEEDED and
        # came back without it. A failed probe (no Docker, no kind) re-raises: that is
        # "I could not ask" a second time, and it stays a refusal.
        if is_ours(context):
            names, probe_failed = clusters()
            if not probe_failed and CLUSTER_NAME not in names:
                info(emit, f"cluster {CLUSTER_NAME!r} no longer exists, so namespace "
                           f"{ns} went with it — removing the local record",
                     phase="teardown")
                return True
        raise
    if labels is None:
        # ABSENT IS THE MOST CONFIRMED-GONE STATE THERE IS, and this returned False for
        # it -- so `teardown` read "not confirmed gone", kept the local record, and said
        # so. The consequence is that a Kubernetes record whose namespace has already
        # been removed could NEVER be deleted: `down --volumes` refused it every time,
        # for ever. The commonest way to reach that is `prune` reclaiming the cluster,
        # or the cluster being recreated -- after which every record from the old one is
        # immortal.
        #
        # Introduced by the change that gave `namespace_labels` three answers instead of
        # two. Splitting "I could not ask" from "it is not there" was right; wiring the
        # second one into the failure branch beside it was not. "I could not ask" still
        # raises, which is the case that mattered.
        return True
    if labels.get(OWNER_LABEL_KEY) != OWNER_LABEL_VALUE:
        # Refused rather than deleted. Reaching a namespace rc-repro does not own is
        # either the adoption bug above leaving one half-labelled, or the wrong
        # cluster -- and neither is a reason to delete somebody's namespace.
        raise ConflictError(
            f"namespace {ns} exists but is not labelled as rc-repro's, so it will "
            f"not be deleted. Check you are on the right cluster "
            f"({context!r}), then remove it yourself if you meant to")
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
        # `check.returncode != 0` USED TO MEAN "GONE" HERE. It does not: kubectl
        # exits 1 for an absent namespace AND for a context that does not exist, so
        # one transient API error, one expired credential or one wrong context made
        # `down --volumes` announce a deletion that had not happened and drop the
        # local record -- leaving a namespace nobody could reach through rc-repro
        # again. `ask_object` reads absence from the payload instead, which is the
        # only place it can honestly come from.
        seen = None
        for attempt in range(NS_GONE_TRIES):
            seen = ask_object("namespace", ns, context=context)
            if seen.absent:
                info(emit, f"namespace {ns} and its volume(s) are gone",
                     phase="teardown", pct=100)
                return True
            if seen.present:
                phase = ((seen.doc or {}).get("status") or {}).get("phase") or "Terminating"
                if attempt % 4 == 0:
                    info(emit, f"namespace {ns} is {phase} — waiting for the pods and "
                               "volume(s) to go", phase="teardown",
                         pct=min(90, 20 + attempt * 5))
            elif attempt % 4 == 0:
                # Kept polling rather than failing at the first refusal, because a
                # single unlucky probe is not evidence of anything either.
                info(emit, f"cannot reach cluster {context!r} to confirm {ns} is "
                           f"gone — still trying", phase="teardown",
                     pct=min(90, 20 + attempt * 5))
            sleep(NS_GONE_INTERVAL)
        if seen is not None and seen.refused:
            # A DIFFERENT SENTENCE, because it is a different problem with a
            # different fix. "Still terminating" says wait; this says the cluster
            # could not be asked at all, and waiting will not change that.
            warn(emit, f"could not confirm that namespace {ns} is gone: "
                       f"{seen.why()} — the local record is KEPT. Check the cluster "
                       f"({context!r}), then `rc-repro down --name {name} --volumes` "
                       f"again.", phase="teardown")
            return False
        # FALSE, because it is not gone. This returned True, and the caller reads
        # True as "confirmed absent" -- so it deleted the local record and tore down
        # the shared operator and monitoring stack while the namespace was still
        # Terminating. Finalizers can wedge indefinitely, and the workspace was then
        # an orphan with no rc-repro path left to it. Kubernetes will still finish on
        # its own; the point is not to act as though it already had.
        warn(emit, f"namespace {ns} is still terminating after "
                   f"{int(NS_GONE_TRIES * NS_GONE_INTERVAL)}s — Kubernetes will "
                   f"finish on its own. The local record is KEPT so you can retry: "
                   f"`kubectl get ns {ns}`, then `rc-repro down --name {name} "
                   f"--volumes` again once it is gone.", phase="teardown")
        return False
    info(emit, f"uninstalling {RELEASE} from {ns} — the volume is kept",
         phase="teardown")
    # CHECKED. Both of these had their return codes dropped, so an RBAC denial or an
    # unreachable API server left the release installed and the pods running while
    # `down` reported success. "already uninstalled" is not a failure -- a repeated
    # `down` is normal -- so that one case is allowed through by name.
    rel = run(["helm", "uninstall", RELEASE, "--kube-context", context, "-n", ns],
              timeout=APPLY_TIMEOUT, own=own)
    if rel.returncode != 0 and "not found" not in why(rel).lower():
        raise DockerError(f"could not uninstall {RELEASE} from {ns}: " + why(rel))
    # The hand-written MongoDB is not part of the release, so it is removed by
    # label rather than by helm -- and its PVC is deliberately left behind.
    gone = run(["kubectl", "--context", context, "-n", ns, "delete",
                "statefulset,service", "-l", OWNER_SELECTOR],
               timeout=APPLY_TIMEOUT, own=own)
    if gone.returncode != 0:
        raise DockerError(f"could not remove MongoDB from {ns}: " + why(gone))
    return True


def workspace_pvcs(name: str, *, context: str) -> list[str]:
    """PVCs belonging to a workspace, so `prune` can prove what it reclaimed.

    Still `[]` on a refused read, and deliberately: the one caller uses this to say
    how many volumes are about to go, so being wrong understates a count in a
    message. Nothing branches on it. Named here so the next reader knows it was
    considered rather than missed.
    """
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
    # THE EXIT CODE WAS NEVER READ HERE, and empty stdout from a refused call is
    # identical to "the pod is not Running yet". So this waited the full 40 x 3s
    # against a cluster that had been destroyed: measured at 94 seconds and nine
    # "waiting for the Rocket.Chat pod" messages after the control plane was killed.
    # And it never asked `terminal_pod_failure`, though the readiness loop in
    # `lifecycle` does -- so an ErrImagePull burned the whole two minutes before
    # anything named it.
    refused = 0
    for attempt in range(POD_WAIT_TRIES):
        seen = ask_list("pod", context=context, namespace=namespace,
                        selector="app.kubernetes.io/name=rocketchat")
        if seen.refused:
            refused += 1
            if refused >= POD_WAIT_REFUSALS:
                raise DockerError(
                    f"could not ask cluster {context!r} about the Rocket.Chat pod in "
                    f"{namespace} ({refused} attempts): {seen.why()}. Waiting longer "
                    f"will not help while the cluster cannot be reached.")
            sleep(POD_WAIT_INTERVAL)
            continue
        refused = 0
        phases = [((it.get("status") or {}).get("phase") or "") for it in seen.items]
        if "Running" in phases:
            break
        # Stop waiting for something Kubernetes has already decided cannot happen --
        # the same check, and the same reasoning, as the readiness loop.
        doomed = terminal_pod_failure(name, context=context)
        if doomed:
            pod, reason, message = doomed
            raise CreateFailedError(
                f"{name!r} cannot start: pod {pod} is {reason}"
                + (f" — {message}" if message else "")
                + f". Nothing will change by waiting. `kubectl -n {namespace} "
                  f"describe pod {pod}` has the detail.")
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
    # CONFIRMED, not assumed: `return proc.pid` recorded a pid for a kubectl that had
    # already exited because the port was taken. See `forward_bound`.
    if not forward_bound(proc, host_port, sleep=sleep):
        warn(emit, f"the port-forward for {name!r} did not come up on port "
                   f"{host_port}, so nothing is published there. Something else "
                   f"probably holds that port — `ss -ltnp | grep {host_port}` says "
                   f"what, and `rc-repro down --name {name}` then `rc-repro up` "
                   f"picks a free one.", phase="wait")
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
            # EndpointSlice, not the deprecated `endpoints`: kubectl warns on every
            # call to the old one ("v1 Endpoints is deprecated in v1.33+") and this is
            # a WAIT LOOP, so the day it stops being served this would spin to its
            # timeout rather than fail visibly. `discovery.k8s.io/v1` is GA from 1.21,
            # this module's kubectl floor.
            counts = service_ready_addresses(namespace, context=context)
            if counts and counts.get(svc, 0) > 0:
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
        # had to learn: port-forward returns a pid before it binds the socket. And
        # `forward_bound` rather than `forward_reachable`, because the port answering
        # is not evidence that OUR kubectl is the thing answering.
        if forward_bound(proc, host_p):
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
    context = ensure_kubeconfig(plan, emit=emit)
    # `is_ours`, NOT `not plan.create`. Those are different questions and the second
    # one is true of every create after the first -- so rc-repro announced "using YOUR
    # kind cluster 'kind-rc-repro-local' ... and never removes the cluster" about its
    # own cluster, which `prune` removes by design. The two lines above it say
    # "reusing cluster rc-repro-local", so the tool already knew.
    if not plan.create and not is_ours(context):
        info(emit, f"using your {plan.distribution} cluster {context!r} — rc-repro "
                   "creates a namespace in it and never removes the cluster",
             phase="provision", pct=5)
    # Only a cluster THIS call created may be rolled back. `plan.create` carries that;
    # it is not inferred from the cluster's name, which is what made a hand-made
    # `rc-repro-local` look like ours.
    had_cluster = not plan.create
    # SAID, BECAUSE A WARM DOCKER CACHE DOES NOT HELP HERE AND NOTHING ADMITTED IT.
    # Docker, kind's containerd and k3s's containerd are three separate image stores,
    # so the ~1.6 GB Rocket.Chat image is pulled again per runtime even on a box that
    # has run that exact version on Compose minutes earlier. Measured at ~90s of a
    # k3s create's 1m43s. `kind load docker-image` would move it from the local
    # daemon instead and is not done here: it is a 1.6 GB copy with its own disk and
    # failure modes, it does not help k3s at all, and choosing to spend that belongs
    # with somebody who asked for it rather than in a fix for a silence.
    if not had_cluster or plan.distribution != "kind":
        info(emit, f"pulling images into the {plan.distribution} cluster's own store — "
                   f"separate from Docker's, so a version already pulled for Compose "
                   f"is fetched again (about 1.6 GB the first time)",
             phase="provision", pct=6)
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
    # REFUSED READS COUNT AS "IT WAS ALREADY THERE". This decides whether a failed
    # create may delete the namespace, and the branch below deletes the PVC with it.
    # Never destroy data we could not prove was ours to destroy.
    _held = workspace_namespaces(context)
    had_namespace = _held is None or namespace_for(name) in _held
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
            # THE STATED REASON WAS WRONG FOR EVERY VERSION THIS TOOL ACTUALLY RUNS.
            # This branch is reached for two unrelated causes -- the operator was not
            # asked for (it is opt-in), or it was and cannot manage that MongoDB --
            # and it always blamed the second. `OPERATOR_MIN_MONGO` is 6.0 and every
            # supported Rocket.Chat resolves to 6.0, 7.0, 8.0 or 8.2, so "no operator
            # below 6.0" was never the real reason on any workspace anyone booted.
            # Someone who wanted authentication was sent to raise a MongoDB version
            # that was not the obstacle, while the workspace notes printed moments
            # later said the true thing -- so the two outputs contradicted each other.
            floor = ".".join(str(n) for n in OPERATOR_MIN_MONGO)
            why_no_auth = (
                "the MongoDB operator is opt-in — add `--mongo-operator` for a "
                "database with SCRAM authentication"
                if not (use_operator or operator_enabled()) else
                f"the operator cannot manage MongoDB below {floor}, so this falls "
                f"back to a plain StatefulSet")
            info(emit, f"MongoDB {resolved.mongo_tag}, {MONGO_VOLUME_GB}Gi volume "
                       f"(no authentication: {why_no_auth})",
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
    if forward_alive(pid, namespace=namespace, host_port=host_port):
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


def service_ready_addresses(namespace: str, *, context: str) -> dict[str, int] | None:
    """Ready endpoint count per Service in a namespace. `None` if it could not be read.

    **EndpointSlice, not Endpoints.** kubectl says it plainly on every call to the old
    one -- "v1 Endpoints is deprecated in v1.33+; use discovery.k8s.io/v1
    EndpointSlice" -- and `discovery.k8s.io/v1` has been GA since Kubernetes 1.21,
    which is this module's declared kubectl floor. So there is no version this tool
    supports where the new API is unavailable.

    A Service with pods behind it but no READY endpoints is the case worth having
    this for: readiness reads pod status and the port-forward targets the Deployment,
    so nothing on either path ever traverses the Service. A selector that matches
    nothing therefore looked exactly like a healthy workspace -- and the Kubernetes
    default deployment is microservices, where in-cluster Service traffic is what
    carries the product.
    """
    seen = ask_list("endpointslices", context=context, namespace=namespace)
    if seen.refused:
        return None
    out: dict[str, int] = {}
    for slice_ in seen.items:
        svc = ((slice_.get("metadata") or {}).get("labels")
               or {}).get("kubernetes.io/service-name")
        if not svc:
            continue
        ready = sum(1 for e in (slice_.get("endpoints") or [])
                    if ((e.get("conditions") or {}).get("ready")))
        out[str(svc)] = out.get(str(svc), 0) + ready
    return out


def services_without_endpoints(name: str, *, context: str) -> list[str]:
    """Rocket.Chat Services in this workspace that no ready pod is behind.

    REPORTED, NOT GATED, and that is deliberate. Endpoints churn during a rollout, so
    a transient empty set is normal; making readiness depend on this would turn an
    ordinary restart into a workspace that never comes ready. And a false negative
    here -- a Service this cannot see, a chart that renames one -- would block every
    boot, which is a far worse failure than the one being detected.

    Empty list on any doubt: a namespace whose slices could not be read, or one with
    no Services at all, says nothing.
    """
    namespace = namespace_for(name)
    counts = service_ready_addresses(namespace, context=context)
    if counts is None:
        return []
    svcs = ask_list("svc", context=context, namespace=namespace)
    if svcs.refused:
        return []
    bad = []
    for svc in svcs.items:
        meta = svc.get("metadata") or {}
        svc_name = str(meta.get("name") or "")
        spec = svc.get("spec") or {}
        # A Service with no selector is wired by hand (or is a headless alias) and has
        # no pods to be missing. ExternalName has no endpoints by definition.
        if not svc_name or not (spec.get("selector") or {}):
            continue
        if str(spec.get("type") or "") == "ExternalName":
            continue
        # THE SELECTOR, NOT THE LABELS, and this was wrong first time round: the
        # first version filtered on the Service's own
        # `app.kubernetes.io/instance` LABEL, and the chart does not set it on most
        # of them -- `rocketchat-ddp-streamer` carries only
        # `app.kubernetes.io/managed-by: Helm`. So the filter excluded every Service
        # it was meant to judge and the whole check was a silent no-op: it returned
        # [] for a workspace whose ddp-streamer Service had been deliberately broken.
        # Caught by breaking one on a live cluster, which is the only way it could
        # have been caught -- a stub would have answered whatever the test put in it.
        #
        # The SELECTOR carries the instance on every one of them, verified against a
        # real microservices workspace, and it excludes our hand-rolled `mongodb`
        # (selector `{"app": "mongodb"}`) without naming it.
        if (spec.get("selector") or {}).get(
                "app.kubernetes.io/instance") != RELEASE:
            continue
        if counts.get(svc_name, 0) == 0:
            bad.append(svc_name)
    return sorted(bad)


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


def forward_alive(pid: int | None, *, namespace: str = "",
                  host_port: int | None = None) -> bool:
    """Whether `pid` is OUR port-forward -- not merely someone's.

    A pid alone is not enough: the OS recycles them. This used to accept any process
    whose command line mentioned `port-forward`, which is a liveness check dressed as
    an identity check. A recycled pid belonging to a different workspace's forward --
    or another user's, on a shared box -- passed it, so rc-repro could believe a
    workspace was reachable when it was not, decline to start the forward it needed,
    and signal a stranger's process at teardown.

    The argv already carries the proof: `kubectl --context X -n <namespace>
    port-forward deployment/... <host>:<container>`. So identity comes from the
    cmdline and needs no extra bookkeeping. Matched as whole NUL-separated tokens,
    because a substring test lets `3000` match `13000`.

    `namespace`/`host_port` are optional so the check degrades to what it did before
    where a caller genuinely has neither; every caller in rc-repro passes both.
    """
    if not pid:
        return False
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, ValueError, TypeError):
        return False
    argv = [t for t in raw.decode("utf-8", "replace").split("\0") if t]
    # AN ARGV OF ONE IS NOT AN ARGV THAT DISAGREES. On a host where `kubectl` is a
    # symlink to `k3s` -- which is every k3s host, and this is how k3s ships kubectl --
    # the multi-call binary rewrites its own command line when it dispatches, so
    # /proc/<pid>/cmdline reads `kubectl` and then nothing. Measured: `ss -ltnp` showed
    # that very pid holding both :3000 sockets while this returned False.
    #
    # Reading that as "the forward is gone" made `ensure_port_forward` respawn onto a
    # port its own live forward already held, on every call, and told the reader the
    # URL was not published while it answered 200. So a command line that carries no
    # arguments to check is "I cannot identify it this way", not a mismatch -- the
    # distinction this module is otherwise built on.
    if len(argv) <= 1:
        return _forward_alive_without_argv(pid, host_port=host_port)
    if "port-forward" not in argv:
        return False
    if namespace and namespace not in argv:
        return False
    if host_port is not None and not any(
            t.startswith(f"{host_port}:") for t in argv):
        return False
    return True


def _forward_alive_without_argv(pid: int, *, host_port: int | None) -> bool:
    """Fallback identity when the process hid its own arguments.

    Three facts together, and none of them alone would do: the pid came from OUR
    record, the process is still a `kubectl`, and the port it was started for is
    actually held. Pid recycling is what the argv check exists to defeat, and a
    recycled pid that is also a kubectl that is also listening on exactly the port we
    recorded is a coincidence worth accepting -- the alternative is calling every
    healthy forward on every k3s host dead.
    """
    try:
        comm = Path(f"/proc/{int(pid)}/comm").read_text(encoding="utf-8").strip()
    except (OSError, ValueError, TypeError):
        return False
    if comm not in ("kubectl", "k3s"):
        return False
    if host_port is None:
        return True
    # `pid_owns_port`, not `forward_reachable`: "the port answers" is satisfied by
    # whoever holds it, which is exactly the confusion this fallback exists inside.
    return pid_owns_port(int(pid), int(host_port))


def ensure_port_forward(name: str, *, namespace: str, context: str, host_port: int,
                        pid: int | None = None, bind_host: str = "",
                        emit: Emit = null_emit) -> int:
    """Return a live port-forward pid, starting one if the recorded one is gone.

    A forward dies with its pod, so `ready` and `start` call this rather than
    assuming the one written at create time is still there. Idempotent: an
    already-live forward is left alone rather than duplicated onto a busy port.
    """
    if forward_alive(pid, namespace=namespace, host_port=host_port):
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
    # An EMPTY selector means the whole namespace, and the flag is dropped rather than
    # passed empty -- `stats` asks for that, because Compose sums every container in
    # the workspace and reporting only the Rocket.Chat pods made the two runtimes
    # measure different sets, omitting the database on the one where it is usually the
    # largest consumer.
    argv = ["kubectl", "--context", context, "-n", namespace_for(name),
            "top", "pods", "--no-headers"]
    if selector:
        argv[-1:-1] = ["-l", selector]
    res = run(argv, timeout=APPLY_TIMEOUT, own=is_ours(context))
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
        timeout=APPLY_TIMEOUT, env=helm_env(context))
    run(["helm", "repo", "update", OPERATOR_REPO], timeout=APPLY_TIMEOUT,
        env=helm_env(context))
    info(emit, f"MongoDB operator in {OPERATOR_NAMESPACE} (once per cluster)",
         phase="provision", pct=15)
    # A KILLED INSTALL WEDGES THIS FOR EVERY WORKSPACE ON THE CLUSTER, which is why
    # it matters more here than for a workspace's own release: `rc-repro-system` is
    # shared, so one Ctrl-C inside the five-minute `--wait` leaves every later
    # `--mongo-operator` failing with "another operation is in progress".
    # `--rollback-on-failure` does not help -- a killed helm is not there to perform
    # the rollback -- so the release has to be freed the same way the workspace's is.
    clear_pending_release(OPERATOR_RELEASE, OPERATOR_NAMESPACE, context, emit=emit)
    res = run(["helm", "upgrade", "--install", OPERATOR_RELEASE, OPERATOR_CHART,
               "--kube-context", context, "-n", OPERATOR_NAMESPACE,
               "--create-namespace", "--set", "operator.watchNamespace=*",
               "--wait", "--timeout", "5m", *helm_rollback_on_failure()],
              timeout=INSTALL_TIMEOUT, env=helm_env(context))
    if res.returncode != 0:
        raise CreateFailedError("could not install the MongoDB operator: " + why(res))


def mongodb_resources(context: str) -> list[str] | None:
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
        # `None`, not `[]`: this is the SHARED operator's reference count, and an
        # empty one means "uninstall it". A read nobody could perform is not
        # evidence that nothing needs it.
        return None
    return sorted({ns for ns in (res.stdout or "").split() if ns})


#: Helm release statuses that mean "an operation died part-way and the release is
#: LOCKED". Helm refuses the next install/upgrade with "another operation
#: (install/upgrade/rollback) is in progress" -- a sentence whose central claim is
#: false, because nothing is in progress; the process that was doing it is gone.
PENDING_STATES = ("pending-install", "pending-upgrade", "pending-rollback")


def release_state(release: str, namespace: str, context: str) -> tuple[str, int]:
    """A release's STATUS and revision: `("", 0)` when there is no such release.

    `helm list -q` was the whole answer here, and from helm's own `--help`: "By
    default, it lists all releases in any status." So a release wedged in
    `pending-install` was reported present exactly as a healthy one was, `up` went on
    to `helm upgrade --install`, and helm refused it -- for every subsequent attempt,
    including `--force`, with no path out that rc-repro knew about.

    A release has a STATE, not a presence. Same correction as the kubectl reads in
    this module: an external tool's several answers must not be collapsed into two.
    """
    res = run(["helm", "list", "--kube-context", context, "-n", namespace,
               "-o", "json"], timeout=APPLY_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        # Not "absent". The caller decides what to do about not knowing; guessing
        # "there is nothing there" here is what would delete a record for a release
        # that exists.
        raise DockerError(
            f"could not list helm releases in {namespace} on {context!r}: "
            + why(res))
    try:
        items = json.loads(res.stdout or "[]") or []
    except (ValueError, TypeError):
        return "", 0
    for item in items if isinstance(items, list) else []:
        if (item or {}).get("name") == release:
            try:
                rev = int((item or {}).get("revision") or 0)
            except (TypeError, ValueError):
                rev = 0
            return str((item or {}).get("status") or ""), rev
    return "", 0


def release_installed(release: str, namespace: str, context: str) -> bool:
    """Whether a helm release is in this cluster. Used before uninstalling one, so
    "there was nothing there" is reported as nothing rather than as a failure.

    Presence really is the question at those call sites -- a wedged release still
    needs uninstalling -- so this stays a bool and reads it from `release_state`.
    """
    try:
        return bool(release_state(release, namespace, context)[0])
    except DockerError:
        # These callers ask before an uninstall. Not knowing is not "absent", and the
        # uninstall itself reports its own failure honestly, so let it try.
        return True


def clear_pending_release(release: str, namespace: str, context: str, *,
                          emit: Emit = null_emit) -> str:
    """Free a release stuck in a pending state. Returns what was done, or "".

    The remedy DEPENDS ON WHETHER THERE IS ANYTHING TO GO BACK TO, and getting that
    wrong is why "just run helm rollback" is not the whole answer: a
    `pending-install` at revision 1 never had a successful revision, so a rollback
    has no target and fails. That one is uninstalled instead -- nothing it created
    was ever serving.
    """
    state, revision = release_state(release, namespace, context)
    if state not in PENDING_STATES:
        return ""
    own = is_ours(context)
    if revision <= 1:
        warn(emit, f"the {release!r} release is stuck in {state} at revision "
                   f"{revision} — it never completed once, so it is being removed "
                   f"rather than rolled back", phase="provision")
        res = run(["helm", "uninstall", release, "--kube-context", context,
                   "-n", namespace], timeout=APPLY_TIMEOUT, own=own)
        action = "uninstalled"
    else:
        warn(emit, f"the {release!r} release is stuck in {state} — rolling it back "
                   f"to revision {revision - 1} before continuing", phase="provision")
        res = run(["helm", "rollback", release, str(revision - 1),
                   "--kube-context", context, "-n", namespace],
                  timeout=INSTALL_TIMEOUT, own=own)
        action = f"rolled back to revision {revision - 1}"
    if res.returncode != 0:
        raise CreateFailedError(
            f"the {release!r} release in {namespace} is stuck in {state} and could "
            f"not be freed: {why(res)}. Nothing is actually in progress — the "
            f"operation that was holding it has gone. Free it by hand with "
            f"`helm uninstall {release} -n {namespace}` and try again")
    info(emit, f"{release!r} {action}", phase="provision")
    return action


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
    using = mongodb_resources(context)
    if using is None:
        warn(emit, "not removing the shared MongoDB operator: its MongoDBCommunity "
                   "resources could not be listed, so whether another workspace "
                   "still needs it is unknown. Removing it would leave their "
                   "finalizers with nothing to clear.", phase="teardown")
        return False
    still = [ns for ns in using if ns != excluding]
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
#: The namespace label that says a workspace wants the shared stack. One name, in
#: one place: it was a literal at the setter AND a literal in the reference count,
#: which is two chances to disagree about the thing that decides whether the shared
#: stack gets uninstalled.
MONITORING_LABEL = "rc-repro.io/monitoring"

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


#: cert-manager's namespace is baked into its own manifests and its CRDs are
#: cluster-scoped, so it is not one of ours to place.
CERT_MANAGER_NAMESPACE = "cert-manager"


def cert_manager_installed(context: str) -> bool:
    """Whether cert-manager can issue in this cluster.

    Keyed on the CRD rather than on a Deployment name or a helm release: the release
    can be named anything, and `installCRDs` versus `crds.enabled` differ by
    cert-manager version -- but nothing can issue a Certificate without the CRD, and
    its absence is exactly the failure that reads as "no matches for kind
    ClusterIssuer".
    """
    res = run(["kubectl", "--context", context, "get", "crd",
               "certificates.cert-manager.io", "-o", "name"], own=is_ours(context))
    return res.returncode == 0 and "certificates.cert-manager.io" in (res.stdout or "")


def monitoring_installed(context: str) -> bool:
    """Whether this cluster already has a WORKING shared stack.

    `helm status` exits 0 for a release in any state, including one a killed install
    left in `pending-install` -- so this returned True for a namespace holding no
    Prometheus and no Grafana, `ensure_monitoring` took its early return, skipped
    both the install and `wait_for_grafana`, and `monitor` reported the stack
    attached. The dashboards did not exist.

    That is the exact collapse `release_state` was written for, in a function that
    was not migrated with the others. Only `deployed` means there is something to
    leave alone; pending, failed and absent all mean the install below should run,
    and it is `upgrade --install`, so running it is the repair.
    """
    try:
        state, _revision = release_state(MONITORING_RELEASE, MONITORING_NAMESPACE,
                                         context)
    except DockerError:
        # Could not ask. Not "installed" -- the caller's next step is an idempotent
        # `upgrade --install`, which is the safer way to be wrong here.
        return False
    return state == "deployed"


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
        timeout=APPLY_TIMEOUT, env=helm_env(context))
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
    # Same as the operator above, and same blast radius: one shared release.
    clear_pending_release(MONITORING_RELEASE, MONITORING_NAMESPACE, context,
                          emit=emit)
    res = run(["helm", "upgrade", "--install", MONITORING_RELEASE, MONITORING_CHART,
               "--kube-context", context, "-n", MONITORING_NAMESPACE,
               "--create-namespace",
               "--set", "operator.prometheus.prometheusSpec."
                        "serviceMonitorSelectorNilUsesHelmValues=false",
               "--set", "operator.prometheus.prometheusSpec."
                        "podMonitorSelectorNilUsesHelmValues=false",
               "--timeout", "9m", *helm_rollback_on_failure()],
              timeout=INSTALL_TIMEOUT, env=helm_env(context))
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


#: The grafana-operator custom resources this chart creates, in the order they must be
#: deleted -- children before the Grafana instance that owns them.
GRAFANA_KINDS: tuple[str, ...] = ("grafanadashboard", "grafanadatasource",
                                  "grafanafolder", "grafana")
#: How long ONE kind's delete may block. Short on purpose: a delete that has not
#: finished in this long is waiting on a finalizer nothing will clear, and the useful
#: response is to clear it, not to keep waiting. It was 60s per kind, and four of them
#: ran before a five-minute `helm uninstall --wait` that was also doomed.
GRAFANA_DELETE_WAIT = 15


def grafana_leftovers(context: str) -> list[tuple[str, str]]:
    """`(kind, name)` for every grafana-operator resource still present.

    Read AFTER a delete has been asked for and given a deadline, so a non-empty
    result means "these are not going to go on their own".
    """
    out: list[tuple[str, str]] = []
    for kind in GRAFANA_KINDS:
        res = run(["kubectl", "--context", context, "-n", MONITORING_NAMESPACE,
                   "get", kind, "-o", "jsonpath={.items[*].metadata.name}"],
                  own=is_ours(context))
        if res.returncode != 0:
            continue                      # the CRD is not installed; nothing to find
        out.extend((kind, n) for n in (res.stdout or "").split())
    return out


def clear_grafana_finalizers(context: str) -> int:
    """Strip the operator finalizer from whatever is left, so the delete completes.

    A last resort, and safe as one: the operator that put the finalizer there is gone,
    so there is nothing whose cleanup we are skipping -- and the alternative is a
    namespace that sits in Terminating for ever and a release the next `--monitor`
    cannot install into.
    """
    cleared = 0
    for kind, name in grafana_leftovers(context):
        res = run(["kubectl", "--context", context, "-n", MONITORING_NAMESPACE,
                   "patch", kind, name, "--type=merge", "-p",
                   '{"metadata":{"finalizers":[]}}'], own=is_ours(context))
        cleared += 1 if res.returncode == 0 else 0
    return cleared


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
    held = workspace_namespaces(context)
    if held is None:
        warn(emit, "not removing the shared monitoring stack: the cluster's "
                   "namespace list could not be read, so whether another workspace "
                   "still wants it is unknown", phase="monitor")
        return False
    others = []
    for ns in held:
        if ns == excluding:
            continue
        wants = monitoring_wanted(ns, context=context)
        if wants is None:
            warn(emit, f"not removing the shared monitoring stack: could not read "
                       f"whether {ns} still wants it", phase="monitor")
            return False
        if wants:
            others.append(ns)
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
    #
    # The ordering above is right and it is not sufficient, because it assumes the
    # operator is ALIVE. Measured on a live workspace: the Grafana operator was
    # already gone -- `rc-repro-system` held only the MongoDB operator while nine
    # GrafanaDashboards, one GrafanaFolder and two GrafanaDatasources remained, the
    # folder carrying `operator.grafana.com/finalizer` and a deletionTimestamp ten
    # minutes old, with the release stuck in `uninstalling`. Every step then paid its
    # full timeout for nothing: 4 x 60s on deletes that could never finish, then 5
    # MINUTES on `helm uninstall --wait`, and only after all of it did the fallback
    # below clear the finalizers -- which worked instantly. Nine minutes of silence,
    # and the fix was the last thing tried.
    #
    # So the deletes get a SHORT deadline and are then CHECKED. Whether the operator
    # is running is deliberately not the test: this chart has renamed its operator
    # Deployment between versions, and what actually matters is whether the resources
    # went. If they are still there after the deadline, nothing is going to clear
    # them, so clear the finalizers now rather than in nine minutes' time.
    info(emit, "removing the Grafana resources", phase="monitor")
    for kind in GRAFANA_KINDS:
        run(["kubectl", "--context", context, "-n", MONITORING_NAMESPACE, "delete",
             kind, "--all", "--ignore-not-found", f"--timeout={GRAFANA_DELETE_WAIT}s"],
            timeout=APPLY_TIMEOUT, own=is_ours(context))
    stuck = grafana_leftovers(context)
    if stuck:
        warn(emit, f"{len(stuck)} Grafana resource(s) will not delete — nothing is left "
                   "to clear their finalizers, so clearing them now", phase="monitor")
        clear_grafana_finalizers(context)
    info(emit, "uninstalling the monitoring release", phase="monitor")
    res = run(["helm", "uninstall", MONITORING_RELEASE, "--kube-context", context,
               "-n", MONITORING_NAMESPACE, "--wait", "--timeout", "5m"],
              timeout=INSTALL_TIMEOUT, own=is_ours(context))
    if res.returncode != 0:
        # The release is removed either way; a stuck finalizer must not leave the
        # cluster in a state the next `--monitor` cannot install into.
        clear_grafana_finalizers(context)
        warn(emit, "the monitoring stack needed its finalizers cleared by hand: "
                   + why(res), phase="monitor")
    return True


def monitoring_wanted(namespace: str, *, context: str) -> bool | None:
    """Whether a workspace namespace is marked as wanting monitoring.

    A label on the namespace rather than a lookup in repro.json: `remove_monitoring`
    has to answer "does anyone else still want this?" about workspaces it is not
    holding the record for, and possibly ones created by a different user of the
    same cluster.
    """
    # `None` WHEN THE LABEL COULD NOT BE READ. This returned False for any failed
    # call, and False here means "this workspace does not want monitoring" -- so it is
    # the INNER test of `remove_monitoring`'s reference count. Fixing
    # `workspace_namespaces` to stop claiming an empty cluster does not help if the
    # per-namespace question then answers "no" for a namespace nobody could read: the
    # count still comes back empty and the SHARED stack is still uninstalled while
    # another workspace is using it. Same defect, one level down.
    #
    # `-o json` rather than a jsonpath, because an empty jsonpath result cannot say
    # whether the LABEL is absent or the NAMESPACE is.
    seen = ask_object("namespace", namespace, context=context)
    if seen.refused:
        return None
    if seen.absent:
        return False
    labels = ((seen.doc or {}).get("metadata") or {}).get("labels") or {}
    return str(labels.get(MONITORING_LABEL, "")).strip() == "true" \
        if isinstance(labels, dict) else False


def set_monitoring_label(namespace: str, *, context: str, wanted: bool) -> None:
    """Mark (or unmark) a namespace as wanting the shared stack."""
    value = "true" if wanted else "-"
    run(["kubectl", "--context", context, "label", "--overwrite", "namespace",
         namespace, f"{MONITORING_LABEL}={value}" if wanted
         else f"{MONITORING_LABEL}-"], own=is_ours(context))


def forward_reachable(host_port: int, *, tries: int = 20, interval: float = 0.5,
                      sleep=time.sleep) -> bool:
    """Whether SOMETHING is listening on the host port yet. Occupancy, not identity.

    `kubectl port-forward` returns a pid long before it has bound the socket, so a
    URL printed straight after spawning it is a guess. The workspace path learned
    this the hard way; the Grafana path repeated it and the matrix caught it --
    `monitor` reported attached and exit 0, and a curl a moment later got nothing.

    **It cannot tell you WHOSE socket it is.** A foreign process holding the port
    answers this exactly as our own forward does, so nothing may conclude "the
    workspace is serving" from this alone -- `forward_bound` answers the process
    question and `rcapi.api_info` the "is it actually Rocket.Chat" one.
    """
    import socket

    for _ in range(tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex(("127.0.0.1", int(host_port))) == 0:
                return True
        sleep(interval)
    return False


def _listening_inodes(port: int) -> set[str]:
    """Socket inodes LISTENING on `port`, from /proc/net/tcp{,6}."""
    out: set[str] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            # 0A is TCP_LISTEN; anything else is a connection, not a bind.
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                if int(fields[1].split(":")[1], 16) == int(port):
                    out.add(fields[9])
            except (ValueError, IndexError):
                continue
    return out


def pid_owns_port(pid: int, port: int) -> bool:
    """Whether THIS pid holds a listening socket on `port`.

    Ownership, and it is answered rather than timed. Every other way of asking --
    "is the port up?", "is the child still alive?" -- is a race against a kubectl
    that has not failed yet, and `forward_bound` lost that race 3 times out of 3
    against a squatter: `poll()` is None for a few hundred milliseconds after
    `Popen` while the socket is answered instantly by whoever already holds it.
    Matching the process's own socket inodes against the listeners on the port has
    no such window.

    An unreadable /proc entry (a process that is not ours) yields no inodes and so
    answers False, which is the safe direction.
    """
    inodes = _listening_inodes(port)
    if not inodes:
        return False
    try:
        fds = list((Path(f"/proc/{int(pid)}") / "fd").iterdir())
    except (OSError, ValueError, TypeError):
        return False
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith("socket:[") and target[8:-1] in inodes:
            return True
    return False


def forward_bound(proc: subprocess.Popen, host_port: int, *, tries: int = 20,
                  interval: float = 0.5, sleep=time.sleep) -> bool:
    """Whether THIS child actually got the socket. Waits for one answer or the other.

    `kubectl port-forward` cannot share a port: if something already holds it, kubectl
    prints `Unable to listen on port N: bind: address already in use` and exits within
    milliseconds. Returning `proc.pid` regardless recorded a pid that was already
    dead -- and because `forward_reachable` only asks whether the port ANSWERS, and
    the squatter answers, `ready` then certified the workspace's URL while a different
    program served it. Every conclusion drawn from that URL was about the wrong
    software, which is the worst thing this module can do.

    The child exiting is a definite no; the socket answering while the child lives is
    a yes; neither yet means keep waiting.

    stderr stays on DEVNULL rather than a pipe. This process is detached and outlives
    the command, so nothing would be reading that pipe -- kubectl writes a line per
    dropped connection and would eventually fill the buffer and block the forward
    itself. `proc.poll()` answers the only question needed here.
    """
    for _ in range(tries):
        if proc.poll() is not None:
            return False
        # OUR CHILD'S SOCKET, not "a socket". Asking whether the PORT answers made
        # this return True against a squatter every time, in a tenth of a
        # millisecond: the port was answered by the process already holding it while
        # our kubectl had not yet got as far as failing to bind. The warning this
        # function exists to produce therefore never fired in the one scenario its
        # docstring describes, and the dead pid was recorded after all.
        if pid_owns_port(proc.pid, host_port):
            return True
        sleep(interval)
    return proc.poll() is None and pid_owns_port(proc.pid, host_port)


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
    # Same rule as the workspace forward. This path has form: it is where "reported
    # attached and exit 0, and a curl a moment later got nothing" was measured.
    if not forward_bound(proc, host_port):
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


def wait_namespace_gone(ns: str, *, context: str, emit: Emit = null_emit,
                        sleep=time.sleep) -> bool:
    """Block until namespace `ns` is actually gone. False if it is still terminating.

    Extracted for `prune --orphans`, which swept namespaces with `--wait=false` and
    then asked `_reclaim_cluster` for the control plane in the same call -- so
    `delete_cluster` refused, correctly, because the namespaces prune had just deleted
    were still Terminating. Reclaiming the cluster took a SECOND prune twenty seconds
    later, and about 600 MB survived the prune that had emptied it.

    Same bound as `delete_namespace`'s own loop, and the same reasoning: a namespace
    that has not gone has not gone, and saying otherwise is what left an orphan once.
    """
    # SAME CORRECTION AS `delete_namespace`, and it mattered more here: on a cluster
    # rc-repro did not create, the caller passed a context that does not exist in the
    # kubeconfig, kubectl exited non-zero, and this returned True -- so `prune
    # --orphans` printed "removed 1 orphaned namespace(s)" with exit 0 over a
    # namespace still Terminating, which is the exact regression this function's
    # docstring says it was written to prevent.
    seen = None
    for attempt in range(NS_GONE_TRIES):
        seen = ask_object("namespace", ns, context=context)
        if seen.absent:
            return True
        if seen.present:
            phase = ((seen.doc or {}).get("status") or {}).get("phase") or "Terminating"
            if attempt % 4 == 0:
                info(emit, f"namespace {ns} is {phase} — waiting for it to go before "
                           f"the cluster can be reclaimed", phase="done")
        elif attempt % 4 == 0:
            info(emit, f"cannot reach cluster {context!r} to confirm {ns} is gone — "
                       f"still trying", phase="done")
        sleep(NS_GONE_INTERVAL)
    if seen is not None and seen.refused:
        warn(emit, f"could not confirm that namespace {ns} is gone: {seen.why()} — "
                   f"the cluster is left alone.", phase="done")
        return False
    warn(emit, f"namespace {ns} is still terminating; the cluster is left alone. "
               f"`rc-repro prune` again once `kubectl get ns` is clear.", phase="done")
    return False
