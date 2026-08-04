"""One-time onboarding and the preferences it persists.

Onboarding exists so a question is asked once and never again. Two front doors,
one writer: a human answers interactively, or the same answers arrive as flags so
a machine-driven run is possible without a TTY. An agent that finds no onboarding
does not guess a baseline for itself; it stops with an authority gate naming the
exact command a human should run.

Config layout, added to the existing flat config.yaml:

    onboarding:
      completed_at: "2026-07-31T09:14:02+00:00"
      rc_repro_version: "0.18.0"
    grants:
      owned_cluster: true
      engine_resize: false
      clusters: ["rc-repro-local"]
    preferences:
      retain_runs: false
      kubernetes_target: "owned-local"
      seed_profile: "small"
    default_deployment: "default"     # selector default (not a parallel catalog)
    default_scenarios: []

Namespaced and additive only. Existing keys are never renamed or retyped, unknown
keys are ignored on read, and absent means default, so a config written before
onboarding existed stays valid rather than being "version 0". That is why there is
no config_version and no migration code: the change is purely additive, and a buggy
migration corrupting a working config is a worse failure than the one it guards
against. If the additive-only promise is ever broken, config_version gets added
then, and its absence will correctly mean pre-versioning.

Secrets are never written here. A registration token keeps its existing ephemeral
route (RC_REPRO_REG_TOKEN or --reg-token); if onboarding ever needs to remember
that one exists, it stores a boolean, not the value.

The shared setup contract (``setup_snapshot`` / ``apply_setup_patch``) is the
read/write surface both the terminal flow and the GUI use. Static
``capabilities`` stays offline and deterministic; dynamic environment and
capacity facts live only on the setup contract.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from rc_repro import __version__, config, versions
from rc_repro.errors import AuthorityGateError, ValidationError

#: The grants a human can hand over. Each exists because some action needs
#: authority rc-repro does not otherwise have.
GRANTS: dict[str, str] = {
    "owned-cluster": "create and delete the rc-repro-owned local Kind cluster "
                     "and the repro namespaces it owns",
    "engine-resize": "stop, resize, and restart the container engine VM when a "
                     "preset needs more capacity than it currently has",
}

#: Preferences with a safe default, so they are settings rather than questions.
DEFAULT_PREFERENCES: dict[str, object] = {
    # Teardown by default: a retained run costs disk and leaves a workspace behind.
    "retain_runs": False,
    # An rc-repro-owned local cluster. An existing named cluster is an explicit
    # opt-in, and the ambient kubectl context is never selected implicitly.
    "kubernetes_target": "owned-local",
    # Seed Dataset size applied by the first-run command when not "none".
    "seed_profile": "small",
}

#: Goal-first setup sections. Reconfiguration targets one section rather than
#: replaying the whole wizard.
SETUP_SECTIONS: tuple[str, ...] = (
    "deployment",
    "scenarios",
    "seed",
    "cleanup",
    "authority",
    "capacity",
)

SEED_PROFILE_CHOICES: tuple[str, ...] = ("none", "small", "standard", "large")
DEFAULT_FIRST_RUN_VERSION = "8.6.1"
SETUP_SCHEMA = "rc-repro.setup.v1"

#: A gate always sends a human through the interactive authority flow. Automation
#: may use ``--accept-defaults`` with explicit grants, but an agent must never be
#: instructed to approve its own baseline.
ONBOARD_COMMAND = "rc-repro onboard"

#: A settled denial must be changed deliberately, while a newly introduced,
#: unanswered grant belongs in the normal interactive flow.
RECONFIGURE_COMMAND = "rc-repro onboard --reconfigure"

#: The human path ends with one exact, proven lifecycle command rather than a
#: placeholder the operator has to assemble. Kept as the classic microservices
#: form so existing newcomer docs and tests stay stable when that path is ready.
FIRST_RUN_COMMAND = (
    "rc-repro up --preset microservices --version 8.6.1 "
    "--name first-repro --wait"
)

#: Stable codes for capacity and authority setup gates. Preflight and the GUI
#: route recovery from these rather than from prose.
CAPACITY_OK = "CAPACITY_OK"
CAPACITY_INSUFFICIENT_CPU = "CAPACITY_INSUFFICIENT_CPU"
CAPACITY_INSUFFICIENT_MEMORY = "CAPACITY_INSUFFICIENT_MEMORY"
CAPACITY_RESIZE_UNSUPPORTED = "CAPACITY_RESIZE_UNSUPPORTED"
CAPACITY_ENGINE_UNAVAILABLE = "CAPACITY_ENGINE_UNAVAILABLE"
CAPACITY_TOOLS_MISSING = "CAPACITY_TOOLS_MISSING"
CAPACITY_GRANT_REQUIRED = "CAPACITY_GRANT_REQUIRED"
COMPATIBILITY_OK = "COMPATIBILITY_OK"
COMPATIBILITY_MONGODB_KERNEL_UNSUPPORTED = "COMPATIBILITY_MONGODB_KERNEL_UNSUPPORTED"


def grant_key(name: str) -> str:
    """Persist a CLI grant name under its single canonical config key."""
    return name.replace("-", "_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state(cfg: dict | None = None) -> dict:
    """Onboarding state, safe to call on a config that predates onboarding."""
    cfg = cfg if cfg is not None else config.load_config()
    ob = cfg.get("onboarding") if isinstance(cfg.get("onboarding"), dict) else {}
    grants = cfg.get("grants") if isinstance(cfg.get("grants"), dict) else {}
    prefs = dict(DEFAULT_PREFERENCES)
    stored = cfg.get("preferences")
    if isinstance(stored, dict):
        # Absent means default, so only known keys are honoured and an unknown one
        # is ignored rather than treated as a setting.
        prefs.update({k: v for k, v in stored.items() if k in DEFAULT_PREFERENCES})
    return {
        "completed": bool(ob.get("completed_at")),
        "completed_at": ob.get("completed_at", ""),
        "rc_repro_version": ob.get("rc_repro_version", ""),
        "grants": {grant_key(g): bool(grants.get(grant_key(g)))
                   for g in GRANTS},
        "answered_grants": {
            grant_key(g): grant_key(g) in grants for g in GRANTS
        },
        "clusters": list(grants.get("clusters") or []),
        "preferences": prefs,
        "answered_preferences": {
            key: isinstance(stored, dict) and key in stored
            for key in DEFAULT_PREFERENCES
        },
    }


def complete(*, grants: list[str] | None = None,
             denied_grants: list[str] | None = None,
             preferences: dict | None = None,
             clusters: list[str] | None = None) -> dict:
    """Record onboarding. Idempotent: re-running updates rather than duplicating.

    The single writer both front doors call, so the interactive flow and the flags
    cannot drift apart.
    """
    granted = set(grants or [])
    denied = set(denied_grants or [])
    unknown = sorted((granted | denied) - set(GRANTS))
    if unknown:
        raise ValidationError(
            f"unknown grant(s) {', '.join(unknown)}; available: {', '.join(sorted(GRANTS))}")
    bad_prefs = sorted(set(preferences or {}) - set(DEFAULT_PREFERENCES))
    if bad_prefs:
        raise ValidationError(
            f"unknown preference(s) {', '.join(bad_prefs)}; "
            f"available: {', '.join(sorted(DEFAULT_PREFERENCES))}")

    # with_env=False: never persist an ephemeral env value (a reg token!) into the
    # file. That distinction already exists in config.load_config and matters here.
    cfg = config.load_config(with_env=False)
    cfg["onboarding"] = {"completed_at": _now(), "rc_repro_version": __version__}
    stored_grants = cfg.get("grants") if isinstance(cfg.get("grants"), dict) else {}
    overlap = sorted(granted & denied)
    if overlap:
        raise ValidationError(
            f"grant(s) cannot be both granted and denied: {', '.join(overlap)}")
    # Update only answers supplied by this run. This is what makes onboarding
    # additive across releases: a newly introduced grant remains unanswered while
    # every earlier settled answer survives byte-for-byte.
    for g in granted:
        stored_grants[grant_key(g)] = True
    for g in denied:
        stored_grants[grant_key(g)] = False
    if clusters is not None:
        stored_grants["clusters"] = list(clusters)
    elif "owned-cluster" in granted:
        from rc_repro.services import k8s
        stored_grants["clusters"] = [k8s.CLUSTER_NAME]
    elif "owned-cluster" in denied:
        stored_grants["clusters"] = []
    cfg["grants"] = stored_grants
    stored_prefs = cfg.get("preferences") if isinstance(cfg.get("preferences"), dict) else {}
    stored_prefs.update(preferences or {})
    cfg["preferences"] = stored_prefs
    config.save_config(cfg)
    return state(cfg)


def require_onboarded(cfg: dict | None = None) -> None:
    """Stop with an authority gate when onboarding has not happened.

    Exit 6, never retryable, never auto-approvable. `approve_with` is what to ask a
    human to run, not something the caller may run itself.
    """
    if state(cfg)["completed"]:
        return
    raise AuthorityGateError(
        "rc-repro has not been onboarded on this machine",
        kind="onboarding", subject="machine",
        approve_with=ONBOARD_COMMAND, code="GATE_NOT_ONBOARDED")


def require_grant(name: str, cfg: dict | None = None) -> None:
    """Stop with an authority gate when a specific grant is missing.

    A missing grant is an unanswered question, which is different from a settled
    one: onboarding exists to stop re-asking what was answered, not to make
    rc-repro silent about authority it was never given.
    """
    key = grant_key(name)
    current = state(cfg)
    if current["grants"].get(key):
        return
    approve_with = (RECONFIGURE_COMMAND
                    if current["answered_grants"].get(key) else ONBOARD_COMMAND)
    raise AuthorityGateError(
        f"rc-repro is not authorised to {GRANTS.get(name, name)}",
        kind="grant", subject=name,
        approve_with=approve_with,
        code=f"GATE_{key.upper()}")


def grant_command(name: str, cfg: dict | None = None) -> str:
    """Return the interactive command that can grant or change ``name``."""
    key = grant_key(name)
    current = state(cfg)
    return (RECONFIGURE_COMMAND
            if current["answered_grants"].get(key) else ONBOARD_COMMAND)


def _os_release() -> tuple[str, str]:
    """Human-readable OS name/version without adding a distro dependency."""
    values: dict[str, str] = {}
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in raw or raw.lstrip().startswith("#"):
                continue
            key, value = raw.split("=", 1)
            values[key] = value.strip().strip('"')
    except OSError:
        pass
    return (
        values.get("PRETTY_NAME") or platform.system() or "unknown",
        values.get("VERSION_ID") or platform.release() or "unknown",
    )


def _version_line(tool: str, *args: str) -> str:
    if not shutil.which(tool):
        return "missing"
    try:
        result = subprocess.run(
            [tool, *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "present (version unavailable)"
    line = next((line.strip() for line in (result.stdout + result.stderr).splitlines()
                 if line.strip()), "")
    return line or "present (version unavailable)"


def _host_memory_gib() -> float:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (pages * page_size) / (1024 ** 3)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0.0


def classify_engine_provider(platform_name: str | None,
                             endpoint: str | None = None,
                             components: Iterable[str] | None = None) -> str:
    """Classify the active Docker-compatible server from product and endpoint facts."""
    lower = (platform_name or "").strip().lower()
    endpoint_lower = (endpoint or "").strip().lower()
    component_lower = " ".join(str(item).strip().lower()
                               for item in (components or ()))
    if ("podman" in lower or "podman" in component_lower or
            "podman.sock" in endpoint_lower or "/podman/" in endpoint_lower):
        return "podman"
    if not lower:
        return "docker-compatible"
    if ("docker" in lower or "moby" in lower or
            "docker" in component_lower or "moby" in component_lower):
        return "docker"
    # Colima, Rancher Desktop, etc. remain docker-compatible, never podman.
    return "docker-compatible"


def detect_engine_provider() -> str:
    """Classify the active container endpoint without guessing Podman from Docker.

    A Docker-compatible socket is not Podman. Resize is only offered when the
    *active* server platform is a running Podman machine (see k8s.engine_resize_supported).
    """
    from rc_repro import runner

    if not runner.docker_available():
        return "unavailable"
    return classify_engine_provider(
        runner.docker_server_platform(), runner.docker_endpoint(),
        runner.docker_server_components())


def detect_environment() -> dict:
    """Facts shown before a human is asked for authority.

    These are deliberately transient and are never persisted. A later run rechecks
    the machine while keeping only the human's settled grants and preferences.
    """
    from rc_repro import runner
    from rc_repro.services import k8s

    os_name, os_version = _os_release()
    docker_cli_present = shutil.which("docker") is not None
    tools = {
        "docker": (runner.docker_cli_version()
                   or ("present (version unavailable)" if docker_cli_present else "missing")),
        "compose": (runner.compose_version_line()
                    or ("present (version unavailable)" if docker_cli_present else "missing")),
        "kind": _version_line("kind", "version"),
        "kubectl": _version_line("kubectl", "version", "--client"),
        "helm": _version_line("helm", "version", "--short"),
    }
    docker_ready = runner.docker_available()
    engine_provider = detect_engine_provider() if docker_ready else "unavailable"
    engine_memory, engine_cpus = k8s.engine_capacity() if docker_ready else (0.0, 0)
    engine_kernel = runner.docker_kernel_version() if docker_ready else None
    host_memory = _host_memory_gib()
    cpus = os.cpu_count() or 0
    try:
        disk_free = shutil.disk_usage(config.home().parent).free / (1024 ** 3)
    except OSError:
        disk_free = 0.0
    missing = [tool for tool in ("kind", "kubectl", "helm")
               if tools[tool] == "missing"]
    capacity_ready = (
        engine_memory >= k8s.FLOOR_MEMORY_GIB and engine_cpus >= k8s.FLOOR_CPUS
    )
    ready = docker_ready and not missing and capacity_ready
    resize_supported = bool(docker_ready and k8s.engine_resize_supported())
    resize_relevant = (
        docker_ready and engine_memory < k8s.FLOOR_MEMORY_GIB and
        engine_cpus >= k8s.FLOOR_CPUS and resize_supported
    )
    return {
        "os": os_name,
        "os_version": os_version,
        "architecture": platform.machine() or "unknown",
        "cpus": cpus,
        "memory_gib": host_memory,
        "disk_free_gib": disk_free,
        "tools": tools,
        "docker_ready": docker_ready,
        "engine_provider": engine_provider,
        "engine_memory_gib": engine_memory,
        "engine_cpus": engine_cpus,
        "engine_kernel_version": engine_kernel,
        "missing_kubernetes_tools": missing,
        "microservices_ready": ready,
        "engine_resize_supported": resize_supported,
        "engine_resize_relevant": resize_relevant,
    }


# --- shared setup snapshot / patch contract ------------------------------------


def _normalise_grant_name(name: str) -> str:
    """Accept either CLI (owned-cluster) or config (owned_cluster) grant names."""
    key = str(name).strip().replace("_", "-")
    if key not in GRANTS:
        raise ValidationError(
            f"unknown grant {name!r}; available: {', '.join(sorted(GRANTS))}")
    return key


def _selector_state(cfg: Mapping | None = None) -> dict[str, Any]:
    """Durable selector defaults from the existing config keys (not a new catalog)."""
    from rc_repro import presets

    cfg = cfg if cfg is not None else config.load_config()
    deployment, scenarios = presets._saved_selectors(cfg)
    return {
        "deployment": deployment or "default",
        "scenarios": list(scenarios),
        "answered_deployment": bool(
            (cfg.get("default_deployment") or cfg.get("deployment") or
             (isinstance(cfg.get("defaults"), Mapping) and
              cfg["defaults"].get("deployment")))),
        "answered_scenarios": (
            "default_scenarios" in cfg or "scenarios" in cfg or
            (isinstance(cfg.get("defaults"), Mapping) and
             "scenarios" in cfg["defaults"])
        ),
    }


def _topology_for(deployment: str) -> str:
    return "kubernetes" if deployment == "microservices" else "compose"


def _scenario_choices(deployment: str) -> list[dict[str, str]]:
    from rc_repro import presets

    matrix = presets.compatibility_matrix()
    names = list(matrix.get(deployment, ()))
    return [{"id": name, "label": name} for name in names]


def _deployment_choices() -> list[dict[str, str]]:
    from rc_repro import presets

    labels = {
        "default": "Compose (single instance)",
        "multi-instance": "Compose multi-instance",
        "microservices": "Kubernetes microservices",
    }
    return [{"id": name, "label": labels.get(name, name)}
            for name in presets.deployment_names()]


def _validated_selection(deployment: str,
                         scenarios: Iterable[str] | None) -> tuple[str, tuple[str, ...]]:
    """Normalize and validate selectors before they can be persisted or rendered."""
    from rc_repro import presets

    dep = presets._normalise_deployment(deployment) or "default"
    if dep not in presets.DEPLOYMENT_PRESETS:
        valid = ", ".join(presets.deployment_names())
        raise ValidationError(f"unknown deployment {deployment!r}; valid: {valid}")
    selected = presets._normalise_scenarios(scenarios)
    if len(selected) > 1:
        requested = ", ".join(selected)
        raise ValidationError(
            f"scenario set [{requested}] is not supported yet; use zero or one scenario")
    matrix = presets.compatibility_matrix()
    unsupported = [scenario for scenario in selected
                   if scenario not in matrix.get(dep, ())]
    if unsupported:
        supported = ", ".join(matrix.get(dep, ())) or "none"
        raise ValidationError(
            f"deployment {dep!r} does not support scenario {unsupported[0]!r}; "
            f"supported: {supported}")
    return dep, selected


def build_first_run_command(*, deployment: str = "default",
                            scenarios: Iterable[str] | None = None,
                            seed_profile: str = "small",
                            version: str = DEFAULT_FIRST_RUN_VERSION,
                            microservices_ready: bool = False,
                            seed_allowed: bool = True) -> str:
    """One exact, runnable next command reflecting the applied setup choices."""
    deployment, scenarios = _validated_selection(deployment, scenarios)
    if deployment == "microservices" and not scenarios and microservices_ready:
        # Preserve the documented classic first-run form for the ready path.
        cmd = FIRST_RUN_COMMAND
    else:
        parts = ["rc-repro", "up", "--version", version, "--name", "first-repro",
                 "--wait"]
        if deployment and deployment != "default":
            parts.extend(["--deployment", deployment])
        for scenario in scenarios:
            parts.extend(["--scenario", scenario])
        if deployment == "microservices" and not scenarios:
            # Explicit deployment form when microservices is not yet ready so the
            # operator still sees the selection they made.
            if "--deployment" not in parts:
                parts.extend(["--deployment", "microservices"])
        cmd = " ".join(parts)
    if seed_allowed and seed_profile and seed_profile != "none":
        if "--seed" not in cmd:
            cmd = f"{cmd} --seed --seed-profile {seed_profile}"
    return cmd


def compatibility_assessment(environment: Mapping | None = None, *,
                             version: str = DEFAULT_FIRST_RUN_VERSION) -> dict[str, Any]:
    """Whether the setup contract's proposed first run can start on this engine."""
    from rc_repro.services import doctor

    env = dict(environment or {})
    if not env:
        env = detect_environment()
    kernel_text = str(env.get("engine_kernel_version") or "")
    kernel = doctor._kernel_major_minor(kernel_text)
    resolved = versions.resolve(version, offline=True)
    try:
        mongo_major = int(str(resolved.mongo_tag).split(".")[0])
    except ValueError:
        mongo_major = 0
    result = {
        "applicable": bool(kernel_text),
        "version": version,
        "mongo_version": resolved.mongo_tag,
        "engine_kernel_version": kernel_text,
        "status": "ok" if kernel_text else "unknown",
        "ready": True,
        "code": COMPATIBILITY_OK,
        "supported_action": None,
        "side_effects": [],
        "remediation": "",
        "verification": "docker info --format '{{.KernelVersion}}'",
    }
    if (mongo_major >= 8 and kernel and
            kernel >= doctor.MONGO8_BAD_KERNEL):
        result.update({
            "status": "blocked",
            "ready": False,
            "code": COMPATIBILITY_MONGODB_KERNEL_UNSUPPORTED,
            "supported_action": "change_engine_or_version",
            "remediation": (
                f"Rocket.Chat {version} requires MongoDB {resolved.mongo_tag}, which "
                f"cannot start on engine kernel {kernel_text} (SERVER-121912). "
                "Use an engine kernel below 6.19, or choose an older Rocket.Chat "
                "line that pairs with MongoDB 7.0."),
        })
    return result


def capacity_assessment(environment: Mapping | None = None, *,
                        deployment: str = "microservices",
                        cfg: dict | None = None) -> dict[str, Any]:
    """Provider-aware engine capacity facts for Kubernetes setup and preflight.

    Never assumes Podman solely from a Docker-compatible endpoint. Compose
    deployments do not need this assessment; callers pass a non-kubernetes
    deployment to get a not-applicable report.
    """
    from rc_repro.services import k8s

    env = dict(environment or {})
    if not env:
        env = detect_environment()
    topology = _topology_for(deployment)
    provider = str(env.get("engine_provider") or "unavailable")
    observed_cpu = int(env.get("engine_cpus") or 0)
    observed_mem = float(env.get("engine_memory_gib") or 0.0)
    required_cpu = int(k8s.FLOOR_CPUS)
    required_mem = float(k8s.FLOOR_MEMORY_GIB)
    resize_supported = bool(env.get("engine_resize_supported",
                                    env.get("engine_resize_relevant", False)
                                    and provider == "podman"))
    if "engine_resize_supported" not in env and provider == "podman":
        resize_supported = bool(env.get("engine_resize_relevant") or
                                k8s.engine_resize_supported())

    base = {
        "applicable": topology == "kubernetes",
        "provider": provider,
        "observed_cpu": observed_cpu,
        "observed_memory_gib": round(observed_mem, 2),
        "required_cpu": required_cpu,
        "required_memory_gib": required_mem,
        "supported_action": None,
        "side_effects": [],
        "remediation": "",
        "verification": "",
        "code": CAPACITY_OK,
        "status": "ok",
        "ready": True,
    }
    if topology != "kubernetes":
        base["applicable"] = False
        base["status"] = "not_applicable"
        return base

    missing = list(env.get("missing_kubernetes_tools") or [])
    if not env.get("docker_ready", False) or provider == "unavailable":
        base.update({
            "status": "blocked",
            "ready": False,
            "code": CAPACITY_ENGINE_UNAVAILABLE,
            "supported_action": "start_engine",
            "remediation": "Start Docker or Podman so the container engine answers "
                           "`docker info`, then re-run setup.",
            "verification": "rc-repro doctor",
        })
        return base

    if missing:
        base.update({
            "status": "blocked",
            "ready": False,
            "code": CAPACITY_TOOLS_MISSING,
            "supported_action": "install_tools",
            "remediation": (
                "Install missing Kubernetes tools: " + ", ".join(missing)
                + ". Compose deployments do not need them."),
            "verification": "kind version && kubectl version --client && helm version",
        })
        return base

    if observed_cpu >= required_cpu and observed_mem >= required_mem:
        base["verification"] = (
            f"Confirm engine capacity reports at least {required_cpu} CPUs and "
            f"{required_mem:g} GiB (docker info).")
        return base

    shortfall = []
    if observed_mem < required_mem:
        shortfall.append(f"{observed_mem:.1f} GiB memory (need {required_mem:g})")
    if observed_cpu < required_cpu:
        shortfall.append(f"{observed_cpu} CPUs (need {required_cpu})")
    detail = " and ".join(shortfall)

    if observed_cpu < required_cpu:
        base.update({
            "status": "insufficient",
            "ready": False,
            "code": CAPACITY_INSUFFICIENT_CPU,
            "supported_action": "manual_cpu",
            "side_effects": [
                "Raising CPU usually requires restarting the container engine VM.",
            ],
            "remediation": (
                f"The microservices deployment needs {required_cpu} CPUs and "
                f"{required_mem:g} GiB; this engine has {detail}. Raise the engine's "
                "CPU allocation manually, or choose a Compose deployment instead."),
            "verification": "docker info --format '{{.NCPU}} {{.MemTotal}}'",
        })
        return base

    # Memory shortfall only.
    if resize_supported and provider == "podman":
        resize_cmd = k8s._resize_command(int(required_mem * 1024))
        current = state(cfg)
        granted = bool(current["grants"].get("engine_resize"))
        answered = bool(current["answered_grants"].get("engine_resize"))
        if granted:
            base.update({
                "status": "insufficient",
                "ready": False,
                "code": CAPACITY_INSUFFICIENT_MEMORY,
                "supported_action": "engine_resize",
                "side_effects": [
                    "Stops the Podman machine",
                    "Restarts the engine",
                    "Stops unrelated containers on that machine",
                ],
                "remediation": (
                    f"Engine has {detail}. Standing engine-resize grant is present; "
                    f"rc-repro will resize via `{resize_cmd}` during preflight."),
                "verification": (
                    f"After resize, `docker info` should report >= {required_mem:g} GiB."),
            })
            return base
        approve = (RECONFIGURE_COMMAND if answered else ONBOARD_COMMAND)
        base.update({
            "status": "insufficient",
            "ready": False,
            "code": CAPACITY_GRANT_REQUIRED,
            "supported_action": "engine_resize",
            "side_effects": [
                "Stops the Podman machine",
                "Restarts the engine",
                "Stops unrelated containers on that machine",
            ],
            "remediation": (
                f"Engine has {detail}. Either raise memory yourself with "
                f"`{resize_cmd}`, or grant engine-resize via `{approve}` "
                f"(note that restarting the engine stops unrelated containers)."),
            "verification": (
                f"After resize or grant, re-run setup; memory must be "
                f">= {required_mem:g} GiB."),
            "approve_with": approve,
        })
        return base

    # Docker Desktop / other docker-compatible endpoints: never invent a Podman
    # resize command.
    if provider == "podman":
        remediation = (
            f"Engine has {detail}, but no running Podman machine is available to "
            "resize. Start `podman machine`, raise its memory, or use Compose.")
    elif provider == "docker":
        remediation = (
            f"Engine has {detail}. Raise Docker Desktop's memory (Settings → "
            "Resources), increase the Docker host's memory, or use a Compose "
            "deployment instead. rc-repro will not treat this endpoint as Podman.")
    else:
        remediation = (
            f"Engine has {detail}. Raise this Docker-compatible engine's memory "
            "allocation, or use a Compose deployment. A Docker-compatible socket "
            "is never assumed to be Podman.")

    base.update({
        "status": "unsupported",
        "ready": False,
        "code": CAPACITY_RESIZE_UNSUPPORTED,
        "supported_action": "manual_memory",
        "side_effects": [
            "Manual engine restart may stop unrelated containers.",
        ],
        "remediation": remediation,
        "verification": "docker info --format '{{.NCPU}} {{.MemTotal}}'",
    })
    return base


def _question(*, qid: str, section: str, prompt: str, kind: str,
              choices: list | None = None, value: Any = None,
              answered: bool = False, applicable: bool = True,
              consequences: list[str] | None = None,
              grant: str | None = None) -> dict[str, Any]:
    return {
        "id": qid,
        "section": section,
        "prompt": prompt,
        "kind": kind,
        "choices": list(choices or []),
        "value": value,
        "answered": answered,
        "applicable": applicable,
        "consequences": list(consequences or []),
        "grant": grant,
    }


def _merge_draft(persisted: Mapping, draft: Mapping | None) -> dict[str, Any]:
    """Overlay in-progress answers for snapshot rendering without writing config."""
    out = {
        "deployment": persisted.get("deployment") or "default",
        "scenarios": list(persisted.get("scenarios") or []),
        "seed_profile": persisted.get("seed_profile") or "small",
        "retain_runs": bool(persisted.get("retain_runs", False)),
        "kubernetes_target": persisted.get("kubernetes_target") or "owned-local",
        "grants": dict(persisted.get("grants") or {}),
        "answered_grants": dict(persisted.get("answered_grants") or {}),
        "answered_preferences": dict(persisted.get("answered_preferences") or {}),
        "answered_deployment": bool(persisted.get("answered_deployment")),
        "answered_scenarios": bool(persisted.get("answered_scenarios")),
    }
    if not draft:
        return out
    deployment_changed = False
    if "deployment" in draft and draft["deployment"] is not None:
        selected, _ = _validated_selection(str(draft["deployment"]), ())
        deployment_changed = selected != out["deployment"]
        out["deployment"] = selected
        out["answered_deployment"] = True
    if "scenarios" in draft and draft["scenarios"] is not None:
        if isinstance(draft["scenarios"], str):
            out["scenarios"] = [s.strip() for s in draft["scenarios"].split(",")
                                if s.strip()]
        else:
            out["scenarios"] = [str(s).strip() for s in draft["scenarios"] if str(s).strip()]
        out["answered_scenarios"] = True
    elif deployment_changed:
        # A saved scenario is a dependent selector. Reconfiguration must not carry
        # it into a deployment where it cannot run or render an invalid command.
        try:
            _validated_selection(out["deployment"], out["scenarios"])
        except ValidationError:
            out["scenarios"] = []
            out["answered_scenarios"] = False
    if "seed_profile" in draft and draft["seed_profile"] is not None:
        out["seed_profile"] = str(draft["seed_profile"]).strip().lower()
        out["answered_preferences"] = {
            **out["answered_preferences"], "seed_profile": True}
    if "retain_runs" in draft and draft["retain_runs"] is not None:
        out["retain_runs"] = bool(draft["retain_runs"])
        out["answered_preferences"] = {
            **out["answered_preferences"], "retain_runs": True}
    if "kubernetes_target" in draft and draft["kubernetes_target"] is not None:
        out["kubernetes_target"] = str(draft["kubernetes_target"])
        out["answered_preferences"] = {
            **out["answered_preferences"], "kubernetes_target": True}
    if "grants" in draft and isinstance(draft["grants"], Mapping):
        grants = dict(out["grants"])
        answered = dict(out["answered_grants"])
        for raw_name, raw_val in draft["grants"].items():
            name = _normalise_grant_name(str(raw_name))
            key = grant_key(name)
            grants[key] = bool(raw_val)
            answered[key] = True
        out["grants"] = grants
        out["answered_grants"] = answered
    return out


def setup_snapshot(cfg: dict | None = None, *,
                   environment: Mapping | None = None,
                   draft: Mapping | None = None,
                   section: str | None = None,
                   probe_environment: bool = True) -> dict[str, Any]:
    """Structured setup readout shared by the CLI and GUI.

    Separates durable configuration from per-run environment facts, and lists
    only questions and actions applicable to the current selection. Never
    includes secret values.
    """
    cfg = cfg if cfg is not None else config.load_config()
    # Never surface secrets that may live in config.yaml.
    secret_keys = {"reg_token"}
    st = state(cfg)
    selectors = _selector_state(cfg)
    persisted = {
        "completed": st["completed"],
        "completed_at": st["completed_at"],
        "rc_repro_version": st["rc_repro_version"],
        "grants": dict(st["grants"]),
        "answered_grants": dict(st["answered_grants"]),
        "clusters": list(st["clusters"]),
        "preferences": dict(st["preferences"]),
        "answered_preferences": dict(st["answered_preferences"]),
        "deployment": selectors["deployment"],
        "scenarios": list(selectors["scenarios"]),
        "answered_deployment": selectors["answered_deployment"],
        "answered_scenarios": selectors["answered_scenarios"],
        "seed_profile": st["preferences"].get("seed_profile", "small"),
        "retain_runs": st["preferences"].get("retain_runs", False),
        "kubernetes_target": st["preferences"].get("kubernetes_target", "owned-local"),
    }
    view = _merge_draft(persisted, draft)
    deployment = view["deployment"]
    topology = _topology_for(deployment)
    is_kubernetes = topology == "kubernetes"

    # A scenario adapter may change services and settings, but deployment-level
    # requirements still apply.  Resolve the actual aggregate and expose only a
    # boolean presence check for the registration token: the value remains
    # secret and never enters the setup snapshot.
    from rc_repro import presets
    resolved = presets.resolve_selection(
        deployment=deployment, scenarios=view["scenarios"], saved={})
    license_required = bool(resolved.preset.requires_license)
    license_supplied = bool(str(cfg.get("reg_token") or "").strip())
    seed_requested = bool(view["seed_profile"] and view["seed_profile"] != "none")
    seed_deferred = bool(
        license_required and not license_supplied and seed_requested)
    license_info = {
        "required": license_required,
        "supplied": license_supplied,
        "status": (
            "supplied" if license_required and license_supplied
            else "required" if license_required
            else "not_required"
        ),
        "code": (
            "LICENSE_ABSENT_EE_PRESET"
            if license_required and not license_supplied else ""
        ),
        "seed_deferred": seed_deferred,
        "remediation": (
            "Supply an Enterprise registration token with --reg-token, "
            "RC_REPRO_REG_TOKEN, or the existing reg_token configuration before "
            "requesting seed data."
            if seed_deferred else ""
        ),
    }

    env: dict[str, Any]
    if environment is not None:
        env = dict(environment)
    elif probe_environment:
        env = detect_environment()
    else:
        env = {
            "os": "unknown", "os_version": "unknown", "architecture": "unknown",
            "cpus": 0, "memory_gib": 0.0, "disk_free_gib": 0.0,
            "tools": {}, "docker_ready": False, "engine_provider": "unavailable",
            "engine_memory_gib": 0.0, "engine_cpus": 0,
            "engine_kernel_version": "",
            "missing_kubernetes_tools": [], "microservices_ready": False,
            "engine_resize_supported": False, "engine_resize_relevant": False,
        }

    capacity = capacity_assessment(env, deployment=deployment, cfg=cfg)
    compatibility = compatibility_assessment(env)

    # Section filter for targeted reconfiguration.
    section_filter = (section or "").strip().lower() or None
    if section_filter and section_filter not in SETUP_SECTIONS:
        raise ValidationError(
            f"unknown setup section {section!r}; "
            f"available: {', '.join(SETUP_SECTIONS)}")

    def want(sec: str) -> bool:
        return section_filter is None or section_filter == sec

    questions: list[dict[str, Any]] = []
    if want("deployment"):
        questions.append(_question(
            qid="deployment", section="deployment",
            prompt="Which deployment type should new repros use?",
            kind="choice", choices=_deployment_choices(),
            value=deployment, answered=view["answered_deployment"],
            consequences=[
                "Compose paths never ask Kubernetes authority or capacity questions.",
                "Kubernetes microservices needs cluster tools and engine capacity.",
            ]))
    if want("scenarios"):
        choices = _scenario_choices(deployment)
        questions.append(_question(
            qid="scenarios", section="scenarios",
            prompt="Optional reproduction scenario for this deployment "
                   "(empty for deployment only).",
            kind="multi_choice" if choices else "none",
            choices=choices or [{"id": "", "label": "(none for this deployment)"}],
            value=list(view["scenarios"]),
            answered=view["answered_scenarios"],
            applicable=bool(choices) or deployment == "default",
            consequences=["Uses the existing preset catalog; no parallel catalog."]))
    if want("seed"):
        questions.append(_question(
            qid="seed_profile", section="seed",
            prompt="Seed dataset for the first run?",
            kind="choice",
            choices=[{"id": p, "label": p} for p in SEED_PROFILE_CHOICES],
            value=view["seed_profile"],
            answered=bool(view["answered_preferences"].get("seed_profile")),
            consequences=["none skips --seed on the first-run command."]))
    if want("cleanup"):
        questions.append(_question(
            qid="retain_runs", section="cleanup",
            prompt="Retain agent-driven runs after evidence capture?",
            kind="boolean",
            value=bool(view["retain_runs"]),
            answered=bool(view["answered_preferences"].get("retain_runs")),
            consequences=[
                "Default is teardown after evidence.",
                "Retention requires an explicit preference or task.",
            ]))

    # Kubernetes-only authority and capacity questions.
    if want("authority"):
        questions.append(_question(
            qid="owned_cluster", section="authority",
            prompt="May rc-repro create and later delete its owned Kind cluster "
                   "and repro namespaces?",
            kind="boolean",
            value=bool(view["grants"].get("owned_cluster")),
            answered=bool(view["answered_grants"].get("owned_cluster")),
            applicable=is_kubernetes,
            grant="owned-cluster",
            consequences=[
                "Only resources with rc-repro ownership labels are created or deleted.",
                "Ambient kubectl context is never selected implicitly.",
            ]))
    if want("capacity"):
        resize_needed = bool(
            is_kubernetes and capacity.get("supported_action") == "engine_resize")
        questions.append(_question(
            qid="engine_resize", section="capacity",
            prompt="May rc-repro stop, resize, and restart the Podman machine "
                   "when capacity is below the measured floor?",
            kind="boolean",
            value=bool(view["grants"].get("engine_resize")),
            answered=bool(view["answered_grants"].get("engine_resize")),
            applicable=resize_needed,
            grant="engine-resize",
            consequences=list(capacity.get("side_effects") or [])))

    # Drop non-applicable questions from the active list, but keep a separate
    # record so clients can still show "not asked for this selection".
    applicable_questions = [q for q in questions if q["applicable"]]
    hidden_questions = [q for q in questions if not q["applicable"]]

    gates: list[dict[str, Any]] = []
    if not compatibility.get("ready", True):
        gates.append({
            "kind": "compatibility",
            "code": compatibility.get("code"),
            "status": compatibility.get("status"),
            "message": compatibility.get("remediation"),
            "approve_with": "",
            "remediation": compatibility.get("remediation"),
            "verification": compatibility.get("verification"),
            "supported_action": compatibility.get("supported_action"),
            "side_effects": list(compatibility.get("side_effects") or []),
        })
    if is_kubernetes and not capacity.get("ready", True):
        gate = {
            "kind": "capacity",
            "code": capacity.get("code"),
            "status": capacity.get("status"),
            "message": capacity.get("remediation"),
            "approve_with": capacity.get("approve_with", ""),
            "remediation": capacity.get("remediation"),
            "verification": capacity.get("verification"),
            "supported_action": capacity.get("supported_action"),
            "side_effects": list(capacity.get("side_effects") or []),
        }
        gates.append(gate)
    if is_kubernetes and not view["grants"].get("owned_cluster"):
        answered = view["answered_grants"].get("owned_cluster")
        gates.append({
            "kind": "grant",
            "code": "GATE_OWNED_CLUSTER" if answered else "GATE_OWNED_CLUSTER",
            "status": "denied" if answered else "unanswered",
            "message": GRANTS["owned-cluster"],
            "approve_with": (RECONFIGURE_COMMAND if answered else ONBOARD_COMMAND),
            "remediation": (RECONFIGURE_COMMAND if answered else ONBOARD_COMMAND),
            "verification": "rc-repro onboard --json --accept-defaults "
                            "--grant owned-cluster",
            "supported_action": "grant_owned_cluster",
            "side_effects": [
                "Creates or deletes only rc-repro-owned cluster resources.",
            ],
        })

    actions: list[dict[str, Any]] = []
    for gate in gates:
        if gate.get("supported_action") or gate.get("approve_with"):
            actions.append({
                "id": gate.get("supported_action") or gate.get("code"),
                "code": gate.get("code"),
                "label": gate.get("supported_action") or "remediate",
                "approve_with": gate.get("approve_with") or "",
                "remediation": gate.get("remediation") or "",
                "verification": gate.get("verification") or "",
                "side_effects": list(gate.get("side_effects") or []),
            })

    first_run = build_first_run_command(
        deployment=deployment,
        scenarios=view["scenarios"],
        seed_profile=str(view["seed_profile"] or "small"),
        microservices_ready=bool(
            is_kubernetes and env.get("microservices_ready") and
            view["grants"].get("owned_cluster") and capacity.get("ready")),
        seed_allowed=not seed_deferred,
    )
    if not compatibility.get("ready", True):
        first_run = ""

    review = {
        "deployment": deployment,
        "topology": topology,
        "scenarios": list(view["scenarios"]),
        "seed_profile": view["seed_profile"],
        "seed_status": (
            "deferred_license_required" if seed_deferred else "ready"
        ),
        "retain_runs": bool(view["retain_runs"]),
        "license": dict(license_info),
        "grants": {
            "owned_cluster": bool(view["grants"].get("owned_cluster")),
            "engine_resize": bool(view["grants"].get("engine_resize")),
        },
        "first_run_command": first_run,
        "first_run_status": (
            "ready" if first_run else "blocked_compatibility"
        ),
        "capacity": {
            "code": capacity.get("code"),
            "status": capacity.get("status"),
            "provider": capacity.get("provider"),
            "ready": capacity.get("ready"),
        } if is_kubernetes else {"status": "not_applicable", "ready": True},
    }

    # Conflicts: draft grant denial vs required kubernetes action, etc.
    conflicts: list[dict[str, str]] = []
    if is_kubernetes and view["answered_grants"].get("owned_cluster") and not view["grants"].get("owned_cluster"):
        conflicts.append({
            "code": "CONFLICT_OWNED_CLUSTER_DENIED",
            "message": "owned-cluster was denied; Kubernetes create will gate until reconfigured.",
        })
    if (is_kubernetes and capacity.get("code") == CAPACITY_GRANT_REQUIRED and
            view["answered_grants"].get("engine_resize") and
            not view["grants"].get("engine_resize")):
        conflicts.append({
            "code": "CONFLICT_ENGINE_RESIZE_DENIED",
            "message": "engine-resize was denied while capacity is below floor.",
        })

    return {
        "schema": SETUP_SCHEMA,
        "completed": bool(st["completed"]),
        "persisted": {
            "grants": dict(st["grants"]),
            "answered_grants": dict(st["answered_grants"]),
            "preferences": dict(st["preferences"]),
            "answered_preferences": dict(st["answered_preferences"]),
            "clusters": list(st["clusters"]),
            "deployment": selectors["deployment"],
            "scenarios": list(selectors["scenarios"]),
            "answered_deployment": selectors["answered_deployment"],
            "answered_scenarios": selectors["answered_scenarios"],
            "completed_at": st["completed_at"],
            "rc_repro_version": st["rc_repro_version"],
            # Explicitly omit secret-bearing config keys.
            "omitted_secret_keys": sorted(secret_keys),
        },
        "selection": {
            "deployment": deployment,
            "scenarios": list(view["scenarios"]),
            "topology": topology,
            "seed_profile": view["seed_profile"],
            "retain_runs": bool(view["retain_runs"]),
        },
        "environment": env,
        "capacity": capacity,
        "compatibility": compatibility,
        "license": license_info,
        "questions": applicable_questions,
        "hidden_questions": hidden_questions,
        "gates": gates,
        "actions": actions,
        "conflicts": conflicts,
        "review": review,
        "first_run_command": first_run,
        "sections": list(SETUP_SECTIONS),
        "legacy_preset_alias": True,
    }


def apply_setup_patch(patch: Mapping[str, Any] | None = None, *,
                      mark_complete: bool = True) -> dict[str, Any]:
    """Apply a partial setup patch and return a fresh snapshot.

    Only supplied keys change. Untouched preferences, grants, and selector
    defaults survive. Secrets are never written. Uses the existing Preset
    selector keys (``default_deployment`` / ``default_scenarios``) rather than
    inventing a parallel catalog.
    """
    from rc_repro import presets

    patch = dict(patch or {})
    cfg_before = config.load_config(with_env=False)
    grants_in = patch.get("grants")
    granted: list[str] = []
    denied: list[str] = []
    if isinstance(grants_in, Mapping):
        for raw_name, raw_val in grants_in.items():
            name = _normalise_grant_name(str(raw_name))
            if raw_val:
                granted.append(name)
            else:
                denied.append(name)
    # Also accept grant/deny lists from CLI-shaped payloads.
    for name in patch.get("grant") or patch.get("grants_granted") or []:
        granted.append(_normalise_grant_name(str(name)))
    for name in patch.get("deny") or patch.get("denied_grants") or []:
        denied.append(_normalise_grant_name(str(name)))
    overlap = sorted(set(granted) & set(denied))
    if overlap:
        raise ValidationError(
            f"grant(s) cannot be both granted and denied: {', '.join(overlap)}")

    prefs: dict[str, object] = {}
    if "retain_runs" in patch and patch["retain_runs"] is not None:
        prefs["retain_runs"] = bool(patch["retain_runs"])
    if "kubernetes_target" in patch and patch["kubernetes_target"] is not None:
        prefs["kubernetes_target"] = str(patch["kubernetes_target"])
    if "seed_profile" in patch and patch["seed_profile"] is not None:
        profile = str(patch["seed_profile"]).strip().lower()
        if profile not in SEED_PROFILE_CHOICES:
            raise ValidationError(
                f"unknown seed_profile {profile!r}; "
                f"available: {', '.join(SEED_PROFILE_CHOICES)}")
        prefs["seed_profile"] = profile

    # Resolve and validate the entire selector pair before the first config write.
    # A deployment-only change may clear an old scenario that is no longer valid;
    # an explicitly supplied incompatible pair is rejected.
    saved_deployment, saved_scenarios = presets._saved_selectors(cfg_before)
    selected_deployment = saved_deployment or "default"
    selected_scenarios = tuple(saved_scenarios)
    deployment_supplied = "deployment" in patch and patch["deployment"] is not None
    scenarios_supplied = "scenarios" in patch and patch["scenarios"] is not None
    if deployment_supplied:
        selected_deployment, _ = _validated_selection(str(patch["deployment"]), ())
    if scenarios_supplied:
        raw_scenarios = patch["scenarios"]
        if isinstance(raw_scenarios, str):
            raw_scenarios = raw_scenarios.split(",")
        selected_deployment, selected_scenarios = _validated_selection(
            selected_deployment, raw_scenarios)
    elif deployment_supplied:
        try:
            _, selected_scenarios = _validated_selection(
                selected_deployment, selected_scenarios)
        except ValidationError:
            selected_scenarios = ()

    # Prefer complete() for grant/preference writes so both front doors share one
    # writer. When the patch only touches selectors, still mark complete if asked.
    mutation_keys = {
        "grants", "grant", "grants_granted", "deny", "denied_grants",
        "retain_runs", "kubernetes_target", "seed_profile", "clusters",
        "deployment", "scenarios",
    }
    has_mutation = bool(mutation_keys.intersection(patch))
    needs_complete = bool(granted or denied or prefs or
                          (mark_complete and has_mutation) or "clusters" in patch)
    if needs_complete:
        complete(
            grants=granted or None,
            denied_grants=denied or None,
            preferences=prefs or None,
            clusters=list(patch["clusters"]) if "clusters" in patch else None,
        )

    # Selector defaults: only overwrite keys present in the patch.
    if deployment_supplied or scenarios_supplied:
        cfg = config.load_config(with_env=False)
        if deployment_supplied:
            cfg["default_deployment"] = selected_deployment
        if scenarios_supplied or (deployment_supplied and
                                  tuple(saved_scenarios) != selected_scenarios):
            cfg["default_scenarios"] = list(selected_scenarios)
        config.save_config(cfg)
        # Ensure onboarding completed marker exists after selector-only patches.
        if mark_complete and not state()["completed"]:
            complete()

    # probe_environment=True so the returned snapshot rediscovers transient facts.
    return setup_snapshot(probe_environment=True)
