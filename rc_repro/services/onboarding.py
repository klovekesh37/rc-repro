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
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from rc_repro import __version__, config
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
}

#: A gate always sends a human through the interactive authority flow. Automation
#: may use ``--accept-defaults`` with explicit grants, but an agent must never be
#: instructed to approve its own baseline.
ONBOARD_COMMAND = "rc-repro onboard"

#: A settled denial must be changed deliberately, while a newly introduced,
#: unanswered grant belongs in the normal interactive flow.
RECONFIGURE_COMMAND = "rc-repro onboard --reconfigure"

#: The human path ends with one exact, proven lifecycle command rather than a
#: placeholder the operator has to assemble.
FIRST_RUN_COMMAND = (
    "rc-repro up --preset microservices --version 8.6.1 "
    "--name first-repro --wait"
)


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
    engine_memory, engine_cpus = k8s.engine_capacity() if docker_ready else (0.0, 0)
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
    resize_relevant = (
        docker_ready and engine_memory < k8s.FLOOR_MEMORY_GIB and
        engine_cpus >= k8s.FLOOR_CPUS and k8s.engine_resize_supported()
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
        "engine_memory_gib": engine_memory,
        "engine_cpus": engine_cpus,
        "missing_kubernetes_tools": missing,
        "microservices_ready": ready,
        "engine_resize_relevant": resize_relevant,
    }
