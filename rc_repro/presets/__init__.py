"""Load reproduction presets.

A preset bundles Rocket.Chat env vars and (later) backing services into a named
scenario. Built-ins are shipped in data/presets; a file of the same name in
~/.rc-repro/presets overrides the built-in so users can tweak or add their own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources

import yaml

from rc_repro import config
from rc_repro.presets.scenario import Scenario


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
    # Named volumes merged into the compose top-level `volumes:` block, for
    # preset services that persist data (e.g. MinIO's object store). A service
    # mounting an undeclared volume fails compose validation, so any volume a
    # preset service references must be declared here.
    volumes: dict = field(default_factory=dict)
    # Host ports the preset's side services publish (from config.PRESET_PORTS).
    # Declared so port allocation/preflight can see them — two repros publishing
    # the same sidecar port would collide at `docker compose up`.
    ports: list[int] = field(default_factory=list)
    # Deployment topology this preset runs on. "compose" is Docker Compose, the
    # default and the only value every existing preset uses, so nothing changes
    # for them. A non-compose value routes create/ready/teardown to that
    # topology's service module instead of the Compose path.
    topology: str = "compose"
    # Internal scenario provenance. These fields are deliberately additive and
    # are not written to saved records; the resolved Preset remains the lifecycle
    # aggregate while the scenario definition stays built-in-only.
    scenario: str = ""
    scenario_params: dict = field(default_factory=dict)
    # Native Kubernetes resources emitted by a built-in scenario adapter. The
    # adapter reuses ``env`` for Rocket.Chat settings; this field carries only
    # concrete backing resources, not an arbitrary Helm-values overlay.
    kubernetes_manifests: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Selection:
    """A validated public deployment/scenario request.

    ``Preset`` remains the lifecycle aggregate.  This small wrapper carries the
    selector provenance needed by the public CLI and saved records without
    introducing a second rendering model.
    """

    preset: Preset
    deployment: str
    scenarios: tuple[str, ...] = ()
    legacy_preset: str = ""
    params: Mapping[str, str] = field(default_factory=dict)

    @property
    def topology(self) -> str:
        return self.preset.topology or "compose"

    @property
    def label(self) -> str:
        """Stable name fragment for an implicitly named repro."""
        if self.scenarios:
            suffix = "-".join(self.scenarios)
            return suffix if self.deployment == "default" else f"{self.deployment}-{suffix}"
        if self.legacy_preset and self.legacy_preset not in DEPLOYMENT_PRESETS:
            return self.legacy_preset
        return self.deployment


# These are the concrete deployment entries in the existing catalog.  They are
# intentionally separate from renderer names (``compose``/``kubernetes``): an
# adapter can be shared while applicability remains an explicit pair decision.
DEPLOYMENT_PRESETS = ("default", "multi-instance", "microservices")
_DEPLOYMENT_ALIASES = {
    "compose": "default",
    "docker": "default",
    "k8s": "microservices",
    "kubernetes": "microservices",
}


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
        notes=[str(line) for line in raw.get("notes") or []],
        params_help=raw.get("params_help") or {},
        instances=int(raw.get("instances", 1) or 1),
        entry_service=raw.get("entry_service", "") or "",
        extra=raw.get("extra") or {},
        volumes=raw.get("volumes") or {},
        ports=[int(p) for p in raw.get("ports") or []],
        topology=raw.get("topology", "compose") or "compose",
    )


def _dynamic_builders() -> dict:
    """Registry of code-generated presets (imported lazily to avoid cycles)."""
    from rc_repro.presets import (
        email,
        ldap,
        livechat,
        multi_instance,
        oidc,
        s3_minio,
        saml,
    )

    return {
        "email": email.build,
        "ldap": ldap.build,
        "livechat": livechat.build,
        "saml": saml.build,
        "oidc": oidc.build,
        "multi-instance": multi_instance.build,
        "s3_minio": s3_minio.build,
    }


def scenario_names() -> tuple[str, ...]:
    """Return the built-in scenario names, derived from the shipped catalog."""
    dynamic = set(_dynamic_builders()) - {"multi-instance"}
    builtin_dir = resources.files("rc_repro").joinpath("data", "presets")
    static = {
        entry.stem for entry in builtin_dir.iterdir()
        if entry.name.endswith(".yaml") and entry.stem not in DEPLOYMENT_PRESETS
    }
    return tuple(sorted(dynamic | static))


def deployment_names() -> tuple[str, ...]:
    """Return the public deployment entries in deterministic order."""
    return tuple(sorted(DEPLOYMENT_PRESETS))


def compatibility_matrix() -> dict[str, tuple[str, ...]]:
    """Initial proven Deployment Type -> Scenario applicability matrix.

    The matrix is deliberately narrow.  A scenario set with two or more entries
    is refused until a concrete pair has rendering, conflict, lifecycle, and
    acceptance proof; this function gives callers a stable way to explain the
    current boundary without guessing from renderer names.
    """
    return {
        "default": scenario_names(),
        "multi-instance": (),
        "microservices": ("ldap",),
    }


def _normalise_deployment(value: str | None) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    return _DEPLOYMENT_ALIASES.get(value, value)


def _normalise_scenarios(values: Iterable[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    elif not isinstance(values, Iterable):
        values = [str(values)]
    out: list[str] = []
    for value in values:
        # Comma-separated config values are convenient while repeated CLI flags
        # remain the canonical syntax. Empty pieces are ignored so a trailing
        # comma does not create a phantom scenario.
        for item in str(value).split(","):
            item = item.strip().lower()
            if item and item not in out:
                out.append(item)
    return tuple(out)


def _saved_selectors(saved: Mapping | None) -> tuple[str, tuple[str, ...]]:
    """Read additive selector defaults from config without exposing secrets."""
    if not isinstance(saved, Mapping):
        return "", ()
    nested = saved.get("defaults") if isinstance(saved.get("defaults"), Mapping) else {}
    deployment = (saved.get("default_deployment") or saved.get("deployment") or
                  nested.get("deployment") or "")
    scenarios = (saved.get("default_scenarios") if "default_scenarios" in saved
                 else saved.get("scenarios", nested.get("scenarios", ())))
    return _normalise_deployment(str(deployment)), _normalise_scenarios(scenarios)


def _deployment_topology(deployment: str) -> str:
    return "kubernetes" if deployment == "microservices" else "compose"


def _selection_error(message: str) -> ValueError:
    return ValueError(message)


def resolve_selection(*, preset: str | None = "", deployment: str | None = "",
                      scenarios: Iterable[str] | str | None = None,
                      params: Mapping[str, str] | None = None,
                      saved: Mapping | None = None) -> Selection:
    """Resolve public selectors into one validated :class:`Preset` aggregate.

    ``--preset`` is retained as a compatibility alias.  Built-in deployment
    names are hard deployment aliases; built-in scenario names are soft aliases
    that may be paired with an explicit deployment.  User YAML remains a legacy
    preset override and is never silently reinterpreted as a scenario.
    """
    params = dict(params or {})
    raw_preset = (preset or "").strip().lower()
    explicit_deployment = _normalise_deployment(deployment)
    explicit_scenarios = _normalise_scenarios(scenarios)
    scenarios_were_supplied = scenarios is not None

    if explicit_deployment and explicit_deployment not in DEPLOYMENT_PRESETS:
        valid = ", ".join(deployment_names())
        raise _selection_error(
            f"unknown deployment {deployment!r}; valid deployment presets: {valid}")

    # A user file keeps the exact old --preset meaning.  It cannot participate in
    # the built-in selector model because there is no public custom-scenario
    # adapter contract yet.
    user_override = bool(raw_preset and _user_path(raw_preset).exists())
    if user_override:
        if explicit_deployment or explicit_scenarios:
            raise _selection_error(
                f"custom preset {raw_preset!r} cannot be combined with --deployment "
                "or --scenario; use the legacy --preset form by itself")
        resolved = resolve(raw_preset, params=params)
        deployment_name = ("microservices" if resolved.topology == "kubernetes"
                           else "default")
        return Selection(resolved, deployment_name, (), raw_preset, params)

    alias_deployment = ""
    alias_scenarios: tuple[str, ...] = ()
    if raw_preset:
        if raw_preset in DEPLOYMENT_PRESETS:
            alias_deployment = raw_preset
        elif raw_preset in scenario_names():
            alias_scenarios = (raw_preset,)
        else:
            # Let the existing resolver produce its established unknown-preset
            # wording for a name that is neither a built-in alias nor a user file.
            resolve(raw_preset, params=params)

    saved_deployment, saved_scenarios = _saved_selectors(
        saved if saved is not None else config.load_config())

    if explicit_deployment and alias_deployment and explicit_deployment != alias_deployment:
        raise _selection_error(
            f"deployment conflict: --preset {raw_preset!r} selects "
            f"{alias_deployment!r}, but --deployment selects {explicit_deployment!r}")

    if raw_preset:
        chosen_deployment = explicit_deployment or alias_deployment or "default"
        chosen_scenarios = explicit_scenarios if scenarios_were_supplied else alias_scenarios
        # A scenario alias is intentionally soft: --preset ldap plus
        # --deployment microservices is the new composable form.
        if alias_scenarios and scenarios_were_supplied:
            chosen_scenarios = _normalise_scenarios((*alias_scenarios, *explicit_scenarios))
    else:
        # Saved values are defaults per selector: choosing one explicitly keeps
        # the other saved selector unless it is also overridden on the command
        # line. A legacy --preset is the explicit whole-request form above.
        chosen_deployment = explicit_deployment or saved_deployment or "default"
        chosen_scenarios = explicit_scenarios if scenarios_were_supplied else saved_scenarios

    if chosen_deployment not in DEPLOYMENT_PRESETS:
        valid = ", ".join(deployment_names())
        raise _selection_error(
            f"unknown deployment {chosen_deployment!r}; valid deployment presets: {valid}")

    shadowed = [scenario for scenario in chosen_scenarios
                if _user_path(scenario).exists()]
    if shadowed:
        raise _selection_error(
            f"custom preset {shadowed[0]!r} shadows the built-in scenario; use "
            f"--preset {shadowed[0]!r} by itself, or rename the custom preset before "
            "using --scenario")

    if len(chosen_scenarios) > 1:
        requested = ", ".join(chosen_scenarios)
        raise _selection_error(
            f"scenario set [{requested}] is not supported yet; the current public "
            "matrix proves zero or one scenario per deployment. Use one scenario, "
            "or a legacy preset alias")

    matrix = compatibility_matrix()
    unsupported = [s for s in chosen_scenarios
                   if s not in matrix.get(chosen_deployment, ())]
    if unsupported:
        supported = ", ".join(matrix.get(chosen_deployment, ())) or "none"
        raise _selection_error(
            f"deployment {chosen_deployment!r} does not support scenario "
            f"{unsupported[0]!r}; supported scenarios: {supported}")

    if chosen_scenarios:
        scenario_name = chosen_scenarios[0]
        resolved = resolve(scenario_name, _deployment_topology(chosen_deployment), params)
    else:
        resolved = resolve(chosen_deployment, _deployment_topology(chosen_deployment), params)
    return Selection(resolved, chosen_deployment, chosen_scenarios, raw_preset, params)


def _scenario_definitions() -> dict[str, Scenario]:
    """Built-in scenario registry; intentionally not a public plugin registry."""
    from rc_repro.presets import ldap

    return {"ldap": ldap.scenario()}


def _user_path(name: str):
    return config.preset_dir() / f"{name}.yaml"


def _load_non_scenario(name: str, params: dict) -> Preset:
    """Load a legacy dynamic or static preset without scenario dispatch."""
    builders = _dynamic_builders()
    if name in builders:
        return builders[name](params)

    builtin = resources.files("rc_repro").joinpath("data", "presets", f"{name}.yaml")
    if not builtin.is_file():
        raise ValueError(f"unknown preset {name!r} (run `rc-repro presets` to list)")
    return _parse(builtin.read_text(encoding="utf-8"), source="built-in")


def load(name: str, params: dict | None = None) -> Preset:
    """Return a preset by name.

    Precedence: a user file (~/.rc-repro/presets/<name>.yaml) wins over everything
    — so users can override even a dynamic preset like `ldap`/`saml`. Otherwise a
    dynamic preset is built from `params`, else the built-in YAML is used.
    """
    params = params or {}

    user_path = _user_path(name)
    if user_path.exists():
        return _parse(user_path.read_text(encoding="utf-8"), source=str(user_path))

    scenario = _scenario_definitions().get(name)
    if scenario:
        return scenario.resolve(params, "compose")
    return _load_non_scenario(name, params)


def resolve(name: str, deployment_type: str | None = None,
            params: dict | None = None) -> Preset:
    """Resolve one preset through its built-in scenario adapter when present.

    ``deployment_type`` is an internal seam. Leaving it unset preserves the
    current catalog behaviour and uses the preset's own declared topology. A
    user YAML override always wins and is never interpreted as a scenario.
    """
    params = params or {}
    user_path = _user_path(name)
    if user_path.exists():
        preset = _parse(user_path.read_text(encoding="utf-8"), source=str(user_path))
    else:
        scenario = _scenario_definitions().get(name)
        if scenario:
            target = deployment_type or "compose"
            return scenario.resolve(params, target)
        preset = _load_non_scenario(name, params)

    if deployment_type and preset.topology != deployment_type:
        raise ValueError(
            f"preset {name!r} declares deployment type {preset.topology!r}, "
            f"not {deployment_type!r}")
    return preset


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
