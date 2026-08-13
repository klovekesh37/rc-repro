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
import re
import shutil
import subprocess
from dataclasses import dataclass, field

#: The cluster rc-repro creates and owns. One cluster with a namespace per
#: workspace, not a cluster per workspace: a control plane each would forbid
#: concurrent repros on laptop-scale hardware, which is behaviour rc-repro
#: already has on Compose.
#:
#: rc-repro NEVER adopts a cluster it finds. A cluster it created it may delete;
#: one it found it may not -- and kind's `extraPortMappings`, which the ingress
#: needs, can only be set at creation, so adopting one would also mean it could
#: never serve a hostname.
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

#: The tools, and the floor each must clear. kubectl and helm floors are the
#: official guide's own ("kubectl v1.21+, Helm 3"); kind has no documented floor,
#: so 0.20 is where `extraPortMappings` and the config API this needs settled.
TOOLS: dict[str, tuple[int, int]] = {
    "kind": (0, 20),
    "kubectl": (1, 21),
    "helm": (3, 0),
}

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
    other_clusters: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    #: Why the cluster question could not be ANSWERED, as opposed to answered no.
    #: `kind get clusters` fails when Docker is down, and returns nothing when there
    #: are simply no clusters -- both give an empty list. Reporting the first as
    #: "the cluster does not exist" would send someone to create one that is
    #: already there, so the two are kept apart.
    probe_failed: str = ""

    @property
    def tools_ready(self) -> bool:
        return all(t.present and t.new_enough for t in self.tools.values())

    @property
    def missing_tools(self) -> list[str]:
        return [n for n, t in self.tools.items() if not t.present]

    @property
    def outdated_tools(self) -> list[str]:
        return [n for n, t in self.tools.items() if t.present and not t.new_enough]


def which(tool: str) -> str:
    """Absolute path to a tool, or "" -- the one place PATH is consulted."""
    return shutil.which(tool) or ""


def run(argv: list[str], *, timeout: float = PROBE_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a kind/kubectl/helm command. Never raises; the caller reads returncode.

    A preflight that raises on a missing binary or an unreachable API server would
    have to be wrapped at every call site, and one forgotten wrapper takes down
    `doctor` -- which is the command someone runs precisely because things are
    already wrong.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
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
    res = run(["kind", "get", "clusters"])
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        return [], (detail[0][:120] if detail else "kind could not list clusters")
    return [ln.strip() for ln in (res.stdout or "").splitlines()
            if ln.strip() and "No kind clusters" not in ln], ""


def reachable(context: str = CONTEXT) -> bool:
    """Whether the API server answers. A cluster can exist and not respond --
    a stopped Docker, a half-deleted cluster, a machine that just woke up."""
    res = run(["kubectl", "--context", context, "get", "--raw", "/readyz"])
    return res.returncode == 0 and "ok" in (res.stdout or "").lower()


def storage_classes(context: str = CONTEXT) -> tuple[list[str], str]:
    """(all storage class names, the default one).

    The guide's step 1, and it opens with the warning that matters here: "Local
    Kubernetes distributions such as Kind, K3s, and Minikube often ship without a
    storage provisioner enabled." Without one, a PVC stays Pending forever and the
    workspace never boots -- with no error that names storage.
    """
    res = run(["kubectl", "--context", context, "get", "storageclass", "-o", "json"])
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


def workspace_namespaces(context: str = CONTEXT) -> list[str]:
    """Namespaces rc-repro owns, selected by LABEL.

    Never by name prefix -- see OWNER_LABEL_KEY.
    """
    res = run(["kubectl", "--context", context, "get", "namespace",
               "-l", OWNER_SELECTOR, "-o", "name"])
    if res.returncode != 0:
        return []
    return [ln.split("/", 1)[-1].strip()
            for ln in (res.stdout or "").splitlines() if ln.strip()]


def preflight() -> Preflight:
    """Everything `doctor` needs, in one pass, changing nothing.

    Ordered so that each step's precondition is already known: there is no point
    asking an API server for storage classes when `kubectl` is not installed, and
    a timeout there would be misreported as "no storage".
    """
    out = Preflight(tools={name: tool(name) for name in TOOLS})
    if not out.tools["kind"].present:
        return out
    found, out.probe_failed = clusters()
    if out.probe_failed:
        return out
    out.cluster_exists = CLUSTER_NAME in found
    out.other_clusters = [c for c in found if c != CLUSTER_NAME]
    if not out.cluster_exists or not out.tools["kubectl"].present:
        return out
    out.cluster_reachable = reachable()
    if not out.cluster_reachable:
        return out
    out.storage_classes, out.default_storage_class = storage_classes()
    out.namespaces = workspace_namespaces()
    return out
