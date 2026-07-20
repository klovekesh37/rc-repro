"""Apply a customer's exported settings to a repro.

Rocket.Chat support dumps contain a `*-settings.json`: a flat list of
`{_id, value, packageValue, ...}` for every setting (~1000). To reproduce a
customer's config we import only what they actually CHANGED (value != default),
minus secrets the dump redacts and a deny-list of settings that are identity- or
environment-specific and would break/pollute a local repro.

Bypasses nothing dangerous: settings are applied through the same 2FA-guarded
REST endpoint the admin UI uses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rc_repro import config, rcapi

# Values a support dump uses to mask a secret it won't export (this dump uses
# "XXXXXXXX"; others use bullets/asterisks/hashes). We can't apply these (they
# aren't the real value), so skip rather than write the mask as a garbage value.
# 4+ chars of a single mask alphabet; case-insensitive so "xxxx" matches too.
_REDACTED_RE = re.compile(r"^[X*•·●▪#.]{4,}$", re.IGNORECASE)

# Never import these — they identify the customer's specific install or would
# break local access. Exact ids and id-prefixes (trailing '*').
_DENY = {
    "Site_Url",                          # breaks localhost access to the repro
    "Enterprise_License",                # their license; invalid here, legally theirs
    "Enterprise_License_Status",
    "Deployment_FingerPrint_Hash",       # identity of their deployment
    "Deployment_FingerPrint_Verified",
    "uniqueID",
    "Update_LatestAvailableVersion",     # cloud-reported, not a real preference
    "Register_Server",                   # would try to register the repro to cloud
    "Show_Setup_Wizard",                 # controls the repro's own onboarding state
    "Country", "Industry", "Size",       # org-profile telemetry, not behaviour
}
_DENY_PREFIX = ("Assets_", "Cloud_", "Statistics_", "Deployment_")

# The provider name is the segment after "Custom-"; RC uses "-" as the setting-id
# separator, so custom-provider names never contain a hyphen (they use "_", e.g.
# Ms_entra_id) — [^-]+ captures the whole name.
_OAUTH_RE = re.compile(r"^Accounts_OAuth_Custom-([^-]+)")

_UNSET = object()   # "the dump doesn't set this" — distinct from a value of None/False


@dataclass
class Plan:
    apply: list[tuple[str, object]] = field(default_factory=list)   # (id, value)
    redacted: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    oauth_services: list[str] = field(default_factory=list)          # provider names to pre-create


def _denied(sid: str) -> bool:
    return sid in _DENY or sid.startswith(_DENY_PREFIX)


def _is_redacted(v) -> bool:
    return isinstance(v, str) and bool(_REDACTED_RE.match(v.strip()))


def build_plan(settings_json: Path, *, only: set[str] | None = None) -> Plan:
    """Parse a dump's settings list into an import plan. `only` filters to ids
    whose prefix (before the first '_') matches, e.g. {'Livechat', 'LDAP'}."""
    data = json.loads(settings_json.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("expected a list of settings (a Rocket.Chat *-settings.json)")

    plan = Plan()
    services: set[str] = set()
    for it in data:
        # Be tolerant of a hand-edited / truncated dump: skip anything that
        # isn't a {_id: str, value: ...} entry rather than crashing the import.
        if not isinstance(it, dict):
            continue
        sid = it.get("_id")
        if not isinstance(sid, str) or not sid or "value" not in it:
            continue
        if it["value"] == it.get("packageValue"):     # unchanged from default
            continue
        if only and sid.split("_", 1)[0] not in only:
            continue
        if _is_redacted(it["value"]):
            plan.redacted.append(sid)
            continue
        if _denied(sid):
            plan.denied.append(sid)
            continue
        m = _OAUTH_RE.match(sid)
        if m:
            services.add(m.group(1))
        plan.apply.append((sid, it["value"]))
    plan.oauth_services = sorted(services)
    return plan


def apply(root_url: str, admin: rcapi.Auth, plan: Plan, *,
          dry_run: bool = False, log=lambda m: None) -> dict:
    """Apply the plan. Disables the API rate limiter during the bulk PATCH and
    restores it in a finally. Returns {applied, failed, skipped, failures}."""
    if dry_run:
        return {"applied": 0, "failed": 0,
                "skipped": len(plan.redacted) + len(plan.denied),
                "failures": []}

    pw = config.ADMIN_PASSWORD
    limiter = "API_Enable_Rate_Limiter"
    # Disable the rate limiter for the bulk PATCH. But the dump itself may set
    # this setting — if so its value wins; otherwise restore the prior value.
    plan_limiter = next((v for sid, v in plan.apply if sid == limiter), _UNSET)
    limiter_was_off = rcapi.get_setting(root_url, admin, pw, limiter) is False
    if not limiter_was_off:
        rcapi.set_setting(root_url, admin, pw, limiter, False)

    applied, failures = 0, []
    try:
        # Custom OAuth providers' settings don't exist until the provider is
        # created — do that first so its Accounts_OAuth_Custom-*-* PATCHes land.
        for name in plan.oauth_services:
            if rcapi.add_oauth_service(root_url, admin, pw, name):
                log(f"  created custom OAuth provider: {name}")
        # Apply everything except the limiter (handled last, in finally) so a
        # dump that re-enables it mid-run can't throttle the rest of the import.
        for sid, value in plan.apply:
            if sid == limiter:
                continue
            if rcapi.set_setting(root_url, admin, pw, sid, value):
                applied += 1
            else:
                failures.append(sid)
    finally:
        if plan_limiter is not _UNSET:
            if rcapi.set_setting(root_url, admin, pw, limiter, plan_limiter):
                applied += 1
            else:
                failures.append(limiter)
        elif not limiter_was_off:
            rcapi.set_setting(root_url, admin, pw, limiter, True)

    return {"applied": applied, "failed": len(failures),
            "skipped": len(plan.redacted) + len(plan.denied),
            "failures": failures}
