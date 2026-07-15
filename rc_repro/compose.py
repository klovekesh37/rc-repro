"""Build a docker-compose document for one repro.

Built as a Python dict (then dumped to YAML) rather than a fixed template, so
presets can deep-merge extra services / env / RC patches into it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import yaml

from rc_repro import config
from rc_repro.presets import Preset


@dataclass
class Spec:
    project_name: str
    rc_image: str
    rc_tag: str
    mongo_tag: str
    mongo_flavor: str        # "bitnami-legacy" | "official"
    mongo_shell: str         # used only by the official flavor's init container
    oplog: bool
    root_url: str
    host_port: int
    reg_token: str | None
    preset: Preset
    container_port: int = config.RC_CONTAINER_PORT
    # Host interface published ports bind to (the official rocketchat-compose
    # BIND_IP pattern). Loopback by default: repros run weak fixed credentials,
    # so they must not be reachable from the local network unless asked
    # (`up --bind 0.0.0.0` / RC_REPRO_BIND_HOST).
    bind_host: str = config.DEFAULT_BIND_HOST

    @classmethod
    def from_resolved(cls, resolved, *, project_name: str, root_url: str,
                      host_port: int, reg_token: str | None, preset: Preset,
                      bind_host: str = config.DEFAULT_BIND_HOST) -> "Spec":
        """Build a Spec from a versions.Resolved plus the launch-time choices."""
        return cls(
            project_name=project_name,
            rc_image=resolved.rc_image,
            rc_tag=resolved.rc_version,
            mongo_tag=resolved.mongo_tag,
            mongo_flavor=resolved.mongo_flavor,
            mongo_shell=resolved.mongo_shell,
            oplog=resolved.oplog,
            root_url=root_url,
            host_port=host_port,
            reg_token=reg_token,
            preset=preset,
            bind_host=bind_host,
        )


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base (patch wins), returning base."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _rc_environment(spec: Spec) -> dict:
    env: dict[str, str] = {
        "ROOT_URL": spec.root_url,
        "PORT": str(spec.container_port),
        "DEPLOY_METHOD": "docker",
        "DEPLOY_PLATFORM": "compose",
        "MONGO_URL": config.MONGO_URL,
        "ALLOW_UNSAFE_QUERY_AND_FIELDS_API_PARAMS": "true",
        # Every repro should be usable out of the box: skip the setup wizard
        # (rc-repro also finalizes it over the API on `ready`) and auto-provision
        # the first admin. Presets only add their scenario-specific settings.
        "OVERWRITE_SETTING_Show_Setup_Wizard": "completed",
        "INITIAL_USER": "yes",
        "ADMIN_USERNAME": config.ADMIN_USERNAME,
        "ADMIN_NAME": config.ADMIN_NAME,
        "ADMIN_EMAIL": config.ADMIN_EMAIL,
        "ADMIN_PASS": config.ADMIN_PASSWORD,
    }
    if spec.oplog:  # RC < 8 only; deprecated in 8.x
        env["MONGO_OPLOG_URL"] = config.MONGO_OPLOG_URL
    if spec.reg_token:
        env["REG_TOKEN"] = spec.reg_token
    # Preset env (OVERWRITE_SETTING_* etc.) wins over base defaults.
    env.update({k: str(v) for k, v in spec.preset.env.items()})
    return env


def _mongo_service_bitnami(spec: Spec) -> dict:
    # Bitnami auto-initiates the replica set via REPLICA_SET_MODE=primary,
    # so no separate init container is needed.
    return {
        "mongodb": {
            "image": f"docker.io/bitnamilegacy/mongodb:{spec.mongo_tag}",
            "restart": "always",
            # Bitnami MongoDB images are published for amd64 only; pin the
            # platform so they run (under emulation) on Apple Silicon too.
            "platform": "linux/amd64",
            "volumes": ["mongodb_data:/bitnami/mongodb"],
            "environment": {
                "MONGODB_REPLICA_SET_MODE": "primary",
                "MONGODB_REPLICA_SET_NAME": "rs0",
                "MONGODB_PORT_NUMBER": "27017",
                "MONGODB_INITIAL_PRIMARY_HOST": "mongodb",
                "MONGODB_INITIAL_PRIMARY_PORT_NUMBER": "27017",
                "MONGODB_ADVERTISED_HOSTNAME": "mongodb",
                "MONGODB_ENABLE_JOURNAL": "true",
                "ALLOW_EMPTY_PASSWORD": "yes",
            },
        }
    }


def _mongo_service_official(spec: Spec) -> dict:
    # Mirrors the official RocketChat/rocketchat-compose: MongoDB Inc's
    # community-server image (runs as uid 1001), so a fix-permission container
    # chowns the data dir first, then a one-shot container initiates the replica
    # set. `directConnection=true` lets mongosh reach the node before rs.initiate.
    image = f"docker.io/mongodb/mongodb-community-server:{spec.mongo_tag}-ubi8"
    shell = spec.mongo_shell  # mongosh for Mongo >= 5
    uri = "mongodb://mongodb:27017/?directConnection=true"
    initiate = (
        "try { rs.status() } catch (e) { rs.initiate({ _id: 'rs0', "
        "members: [{ _id: 0, host: 'mongodb:27017' }] }) }"
    )
    return {
        "mongodb-fix-permission": {
            "image": image,
            "user": "0",
            "restart": "on-failure",
            "volumes": ["mongodb_data:/data/db:rw"],
            "entrypoint": ["sh", "-c", "chown -R 1001 /data/db"],
        },
        "mongodb": {
            "image": image,
            "user": "1001",
            "restart": "always",
            "depends_on": {"mongodb-fix-permission": {"condition": "service_completed_successfully"}},
            "volumes": ["mongodb_data:/data/db:rw"],
            "entrypoint": ["mongod", "--replSet", "rs0", "--bind_ip_all"],
            "environment": {"ALLOW_EMPTY_PASSWORD": "yes"},
            "healthcheck": {
                "test": ["CMD", shell, uri, "--eval", "db.adminCommand('ping')"],
                "interval": "10s",
                "timeout": "10s",
                "retries": 30,
                "start_period": "20s",
            },
        },
        "mongo-init": {
            "image": image,
            "restart": "no",
            "depends_on": {"mongodb": {"condition": "service_healthy"}},
            "entrypoint": [shell, uri, "--eval", initiate],
        },
    }


# Healthcheck for a Rocket.Chat instance. Uses `node` (always in the RC image)
# to hit /api/info, so we don't depend on curl/wget being present.
_RC_HEALTHCHECK = {
    "test": [
        "CMD", "node", "-e",
        "require('http').get('http://localhost:3000/api/info',"
        "r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))",
    ],
    "interval": "10s",
    "timeout": "5s",
    "retries": 30,
    "start_period": "60s",
}


def _as_condition_map(deps) -> dict:
    """Normalise a depends_on (plain list or condition-map) to a condition-map."""
    if isinstance(deps, dict):
        return dict(deps)
    return {d: {"condition": "service_started"} for d in (deps or [])}


def _instance_services(base: dict, n: int, host_port: int) -> dict:
    """Clone the rocketchat service into N instances (rocketchat-1..N).

    Instances coordinate over NATS (the moleculer transporter), matching the
    official RocketChat/rocketchat-compose. INSTANCE_IP is intentionally left
    unset — the legacy DDP-mesh address isn't used once NATS is the transporter.

    The load balancer owns the workspace port (host_port); each instance is ALSO
    published directly on host_port+i so you can open a specific instance in the
    browser (e.g. to post on one and watch it arrive on another). Traefik reaches
    them over the compose network by service name regardless.

    Cold start is serialised: only rocketchat-1 boots against the empty database
    (running migrations and seeding settings); rocketchat-2..N wait for it to be
    healthy first, so they never race it to insert the same records — which on a
    fresh DB otherwise crashes an instance with E11000 duplicate-key errors.
    """
    out: dict = {}
    for i in range(1, n + 1):
        name = f"rocketchat-{i}"
        inst = copy.deepcopy(base)
        inst["ports"] = [f"{host_port + i}:{config.RC_CONTAINER_PORT}"]   # direct access
        inst["environment"]["TRANSPORTER"] = "monolith+nats://nats:4222"
        inst["healthcheck"] = copy.deepcopy(_RC_HEALTHCHECK)
        if i > 1:
            deps = _as_condition_map(inst.get("depends_on"))
            deps["rocketchat-1"] = {"condition": "service_healthy"}
            inst["depends_on"] = deps
        out[name] = inst
    return out


def _add_depends(svc: dict, extra: list[str]) -> None:
    """Add each name in `extra` to a service's depends_on (list or condition-dict)."""
    deps = svc.get("depends_on")
    for dep in extra:
        if isinstance(deps, list):
            if dep not in deps:
                deps.append(dep)
        elif isinstance(deps, dict):
            deps.setdefault(dep, {"condition": "service_started"})


def build(spec: Spec) -> dict:
    """Return the compose document (a plain dict) for `spec`."""
    official = spec.mongo_flavor == "official"
    n = max(1, spec.preset.instances or 1)

    rocketchat: dict = {
        "image": f"{spec.rc_image}:{spec.rc_tag}",
        "restart": "always",
        "environment": _rc_environment(spec),
        "ports": [f"{spec.host_port}:{spec.container_port}"],
        "depends_on": (
            {"mongo-init": {"condition": "service_completed_successfully"}}
            if official
            else ["mongodb"]
        ),
    }

    # Single instance (the default): one `rocketchat` service, unchanged. Many
    # instances: clone it into rocketchat-1..N behind a load balancer.
    rc_services: dict = (
        {"rocketchat": rocketchat} if n == 1
        else _instance_services(rocketchat, n, spec.host_port)
    )

    services: dict = dict(rc_services)
    services.update(
        _mongo_service_official(spec) if official else _mongo_service_bitnami(spec)
    )

    doc: dict = {
        "name": spec.project_name,
        "services": services,
        "volumes": {"mongodb_data": {"driver": "local"}},
    }

    # --- apply the preset's backing services / volumes / RC patch / depends_on ---
    if spec.preset.services:
        _deep_merge(doc["services"], copy.deepcopy(spec.preset.services))
    if spec.preset.volumes:
        doc["volumes"].update(copy.deepcopy(spec.preset.volumes))
    # The RC patch and extra depends_on apply to EVERY rocketchat instance.
    for svc in rc_services.values():
        if spec.preset.rocketchat:
            _deep_merge(svc, copy.deepcopy(spec.preset.rocketchat))
        _add_depends(svc, spec.preset.depends_on)

    # A preset can hand the published host port to a front-end service (a load
    # balancer) instead of rocketchat — the port isn't known until `up`, so it's
    # injected here rather than in the preset.
    entry = spec.preset.entry_service
    if entry and entry in doc["services"]:
        doc["services"][entry]["ports"] = [f"{spec.host_port}:80"]

    _bind_ports(doc, spec.bind_host)

    return doc


def _bind_ports(doc: dict, bind: str) -> None:
    """Prefix every published port with the bind host, unless one is already
    given. Applied to ALL services in one pass — RC, multi-instance direct
    ports, the load balancer, and preset sidecars — mirroring the official
    rocketchat-compose `${BIND_IP}:${HOST_PORT}:${PORT}` pattern."""
    if not bind:
        return
    for svc in doc["services"].values():
        ports = svc.get("ports")
        if not ports:
            continue
        svc["ports"] = [
            f"{bind}:{p}" if str(p).split(":", 1)[0].isdigit() else str(p)
            for p in ports
        ]


def to_yaml(doc: dict) -> str:
    header = "# Generated by rc-repro. Do not edit by hand -- re-run `rc-repro up`.\n"
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
