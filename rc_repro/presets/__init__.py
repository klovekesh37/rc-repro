"""Load reproduction presets.

A preset bundles Rocket.Chat env vars and (later) backing services into a named
scenario. Built-ins are shipped in data/presets; a file of the same name in
~/.rc-repro/presets overrides the built-in so users can tweak or add their own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources

import yaml

from rc_repro import config
from rc_repro.presets.scenario import Scenario

#: `services.topology.DOCKER`. Spelled out rather than imported because presets is
#: the lower layer and must not depend on services; `test_preset_and_topology_agree`
#: asserts the two stay equal, which is the guarantee an import would have given.
_RUNTIME_DEFAULT = "docker"


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
    # Which RUNTIME this preset runs on, in `services/topology.py`'s words rather
    # than a second set for the same two things: every other layer -- repro.json,
    # the CLI, the GUI, the HTTP API -- already says "docker"/"kubernetes", and a
    # preset layer saying "compose" for the same thing is how `mongo_flavor` ended
    # up being reported for a runtime that does not honour it.
    # "docker" is Docker Compose, the
    # default and the only value every existing preset uses, so nothing changes
    # for them. A non-compose value routes create/ready/teardown to that
    # topology's service module instead of the Compose path.
    # EMPTY means "runs anywhere", not "runs on Docker". PR #3 defaulted this to
    # its Compose value, which turned an undeclared field into a requirement: every
    # ordinary preset then claimed to demand Docker and `resolve(name, KUBERNETES)`
    # rejected all of them. Only a preset that genuinely needs one runtime declares
    # it -- the LDAP scenario's Kubernetes rendering does, because its manifests are
    # native resources that mean nothing to Compose.
    topology: str = ""
    # Internal scenario provenance. These fields are deliberately additive and
    # are not written to saved records; the resolved Preset remains the lifecycle
    # aggregate while the scenario definition stays built-in-only.
    scenario: str = ""
    scenario_params: dict = field(default_factory=dict)
    # Native Kubernetes resources emitted by a built-in scenario adapter. The
    # adapter reuses ``env`` for Rocket.Chat settings; this field carries only
    # concrete backing resources, not an arbitrary Helm-values overlay.
    kubernetes_manifests: list[str] = field(default_factory=list)
    #: Why this preset could not be read, when `list_presets` met a bad file. Empty
    #: for every real preset; a catalog entry carrying it is a placeholder saying so
    #: rather than a silently missing row.
    error: str = ""


def _parse(text: str, source: str) -> Preset:
    raw = yaml.safe_load(text) or {}
    # A LIST OR A SCALAR IS NOT A PRESET. `raw.get` on either is an AttributeError,
    # which is not a ReproError -- so one malformed file in the presets directory took
    # out `rc-repro presets`, `/api/presets` and the create dialog, and made
    # `capabilities` report ZERO presets (it guards its own call with a bare
    # `except Exception`, so the failure became silence).
    if not isinstance(raw, dict):
        raise ValueError(f"preset {source} is a {type(raw).__name__}, not a mapping "
                         f"(a preset file is a YAML mapping with a 'name' key)")
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
        topology=raw.get("topology", "") or "",
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
    # No DEPLOYMENT_PRESETS exclusion: deployments are not presets here. PR #3
    # modelled "microservices" as a preset that implies Kubernetes; this branch
    # models it as a DEPLOYMENT under an explicit runtime, so there is nothing in
    # the preset catalogue to filter out.
    static = {
        entry.stem for entry in builtin_dir.iterdir()
        if entry.name.endswith(".yaml")
    }
    return tuple(sorted(dynamic | static))



def _scenario_definitions() -> dict[str, Scenario]:
    """Built-in scenario registry; intentionally not a public plugin registry."""
    from rc_repro.presets import ldap

    return {"ldap": ldap.scenario()}


#: A preset name, and nothing that can be read as a path.
#:
#: `config.preset_dir() / f"{name}.yaml"` looks like it confines the lookup to the
#: presets directory and does not: pathlib's `/` DISCARDS the left operand when the
#: right is absolute, so `--preset /tmp/x` resolved to `/tmp/x.yaml` and the presets
#: directory was never consulted. No `..` and no existing presets dir required.
#:
#: That mattered because a preset is a container spec. Its `services:` is deep-merged
#: into the compose document (compose.py), so any readable YAML on the box became an
#: arbitrary image with arbitrary bind mounts, running as whoever runs rc-repro --
#: reproduced with `volumes: ["/:/host:ro"]`. And `preset` is not in
#: `PRIVILEGED_CREATE_FIELDS`, so `POST /api/repros` is member+: on the shared server
#: this file's own README warns about, any member could hand the serve process a
#: container spec. Two lesser consequences travelled with it: naming another
#: workspace's `docker-compose.yml` (valid YAML with `name:` and `services:`) pulled
#: that workspace's Keycloak/MinIO/LDAP and their credentials into one of yours, and
#: the three distinguishable errors made the create dialog a filesystem oracle.
#:
#: The README already says "treat preset files as code — they can run arbitrary
#: containers and mount files". That is only safe because of the sentence before it,
#: "drop a YAML file in ~/.rc-repro/presets/<name>.yaml", and that sentence was not
#: enforced anywhere. It is now, at the one place every reader goes through.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _user_path(name: str):
    """The user-preset file for `name`, or refuse a name that is not a name.

    Validated here rather than at the call sites so `load`, `resolve` and
    `list_presets` all inherit it -- a check on two of the three is the shape this
    bug had in the first place.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"{name!r} is not a preset name (want lower-case letters, digits, '-' "
            f"and '_', starting with a letter or digit). A preset is a YAML file in "
            f"{config.preset_dir()}; it is named, never given as a path.")
    base = config.preset_dir()
    candidate = base / f"{name}.yaml"
    # Belt and braces: the regex already forbids '/', '\\' and '.', so this cannot
    # trigger today. It is here because the regex is one edit away from being
    # loosened, and the consequence of loosening it is arbitrary container specs.
    try:
        if candidate.resolve().parent != base.resolve():
            raise ValueError(f"preset {name!r} resolves outside {base}")
    except OSError:
        pass
    return candidate


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
        return scenario.resolve(params, _RUNTIME_DEFAULT)
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
            target = deployment_type or _RUNTIME_DEFAULT
            return scenario.resolve(params, target)
        preset = _load_non_scenario(name, params)

    # Enforced only when the preset actually declares a requirement.
    if deployment_type and preset.topology and preset.topology != deployment_type:
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
            # PER FILE, because one bad one used to take out the whole catalog: this
            # feeds `rc-repro presets`, `/api/presets` and the create dialog, and
            # `capabilities` swallows the exception and reports ZERO presets. A file
            # somebody is halfway through writing must not do that. Reported the way
            # `backup.list_backups` reports an unreadable bundle -- named, not hidden.
            try:
                p = _parse(path.read_text(encoding="utf-8"), source=str(path))
            except (ValueError, OSError, yaml.YAMLError) as exc:
                seen[path.stem] = Preset(
                    name=path.stem,
                    description=f"UNREADABLE — {exc}",
                    error=str(exc))
                continue
            seen[p.name] = p

    return [seen[k] for k in sorted(seen)]
