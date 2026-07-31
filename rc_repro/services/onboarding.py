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

from datetime import datetime, timezone

from rc_repro import __version__, config
from rc_repro.errors import AuthorityGateError, ValidationError

#: The grants a human can hand over. Each exists because some action needs
#: authority rc-repro does not otherwise have.
GRANTS: dict[str, str] = {
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

#: The non-interactive command a gate points at. Kept in one place so the message
#: an agent relays cannot drift from the flag that actually works.
ONBOARD_COMMAND = "rc-repro onboard --accept-defaults"


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
        "grants": {g.replace("-", "_"): bool(grants.get(g.replace("-", "_")))
                   for g in GRANTS},
        "clusters": list(grants.get("clusters") or []),
        "preferences": prefs,
    }


def complete(*, grants: list[str] | None = None,
             preferences: dict | None = None,
             clusters: list[str] | None = None) -> dict:
    """Record onboarding. Idempotent: re-running updates rather than duplicating.

    The single writer both front doors call, so the interactive flow and the flags
    cannot drift apart.
    """
    unknown = sorted(set(grants or []) - set(GRANTS))
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
    for g in GRANTS:
        stored_grants[g.replace("-", "_")] = g in (grants or [])
    if clusters is not None:
        stored_grants["clusters"] = list(clusters)
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
    key = name.replace("-", "_")
    if state(cfg)["grants"].get(key):
        return
    raise AuthorityGateError(
        f"rc-repro is not authorised to {GRANTS.get(name, name)}",
        kind="grant", subject=name,
        approve_with=f"rc-repro onboard --grant {name}",
        code=f"GATE_{key.upper()}")
