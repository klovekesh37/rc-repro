"""Load reproduction presets.

A preset bundles Rocket.Chat env vars and (later) backing services into a named
scenario. Built-ins are shipped in data/presets; a file of the same name in
~/.rc-repro/presets overrides the built-in so users can tweak or add their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources

import yaml

from rc_repro import config


@dataclass
class Preset:
    name: str
    description: str = ""
    env: dict[str, str] = field(default_factory=dict)
    services: dict[str, dict] = field(default_factory=dict)
    rocketchat: dict = field(default_factory=dict)  # raw patch for the RC service
    depends_on: list[str] = field(default_factory=list)
    requires_license: bool = False
    source: str = "built-in"
    # Extra files to drop into the repro workspace (relpath, contents) — e.g. a
    # generated LDIF that a service mounts. Written by runner.write().
    files: list[tuple[str, str]] = field(default_factory=list)
    # Params a dynamic preset accepts, for `rc-repro presets` display.
    params_help: dict[str, str] = field(default_factory=dict)
    # Actions rc-repro runs once RC is serving (see cli._do_ready). Used e.g. by
    # the Keycloak SAML variant to fetch the IdP cert and set it on RC.
    post_ready: list[dict] = field(default_factory=list)
    # Human-facing tips printed after `up` and by `info` (e.g. where the IdP
    # console lives and which realm the users are in).
    notes: list[str] = field(default_factory=list)
    # Number of Rocket.Chat app instances to run. >1 makes compose.build clone the
    # rocketchat service into rocketchat-1..N (meshed via NATS) — see the
    # multi-instance preset. Default 1 = the normal single-instance repro.
    instances: int = 1
    # Service the published host port maps to instead of `rocketchat` (e.g. a
    # load balancer that fronts the instances). Empty = rocketchat owns the port.
    entry_service: str = ""
    # Arbitrary metadata copied into the repro's repro.json (meta.extra) — e.g.
    # the email preset stores mailpit_url so rcapi.login can fetch OTP codes.
    extra: dict = field(default_factory=dict)


def _parse(text: str, source: str) -> Preset:
    raw = yaml.safe_load(text) or {}
    if not raw.get("name"):
        raise ValueError(f"preset {source} is missing a 'name' field")
    return Preset(
        name=raw["name"],
        description=raw.get("description", ""),
        env=raw.get("env") or {},
        services=raw.get("services") or {},
        rocketchat=raw.get("rocketchat") or {},
        depends_on=raw.get("depends_on") or [],
        requires_license=bool(raw.get("requires_license", False)),
        source=source,
        instances=int(raw.get("instances", 1) or 1),
        entry_service=raw.get("entry_service", "") or "",
        extra=raw.get("extra") or {},
    )


def _dynamic_builders() -> dict:
    """Registry of code-generated presets (imported lazily to avoid cycles)."""
    from rc_repro import (
        email_preset,
        ldap_preset,
        multi_instance_preset,
        oidc_preset,
        saml_preset,
    )

    return {
        "email": email_preset.build,
        "ldap": ldap_preset.build,
        "saml": saml_preset.build,
        "oidc": oidc_preset.build,
        "multi-instance": multi_instance_preset.build,
    }


def load(name: str, params: dict | None = None) -> Preset:
    """Return a preset by name.

    Precedence: a user file (~/.rc-repro/presets/<name>.yaml) wins over everything
    — so users can override even a dynamic preset like `ldap`/`saml`. Otherwise a
    dynamic preset is built from `params`, else the built-in YAML is used.
    """
    params = params or {}

    user_path = config.preset_dir() / f"{name}.yaml"
    if user_path.exists():
        return _parse(user_path.read_text(encoding="utf-8"), source=str(user_path))

    builders = _dynamic_builders()
    if name in builders:
        return builders[name](params)

    builtin = resources.files("rc_repro").joinpath("data", "presets", f"{name}.yaml")
    if not builtin.is_file():
        raise ValueError(f"unknown preset {name!r} (run `rc-repro presets` to list)")
    return _parse(builtin.read_text(encoding="utf-8"), source="built-in")


def list_presets() -> list[Preset]:
    """All available presets, user files shadowing built-ins, sorted by name."""
    seen: dict[str, Preset] = {}
    builtin_dir = resources.files("rc_repro").joinpath("data", "presets")
    for entry in builtin_dir.iterdir():
        if entry.name.endswith(".yaml"):
            p = _parse(entry.read_text(encoding="utf-8"), source="built-in")
            seen[p.name] = p

    for name, build in _dynamic_builders().items():
        seen[name] = build({})  # default params, just for description/params_help

    user_dir = config.preset_dir()
    if user_dir.exists():
        for path in sorted(user_dir.glob("*.yaml")):
            p = _parse(path.read_text(encoding="utf-8"), source=str(path))
            seen[p.name] = p

    return [seen[k] for k in sorted(seen)]
