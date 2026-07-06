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
        "MONGO_URL": "mongodb://mongodb:27017/rocketchat?replicaSet=rs0",
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
        env["MONGO_OPLOG_URL"] = "mongodb://mongodb:27017/local?replicaSet=rs0"
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
    shell = spec.mongo_shell
    ping = f"{shell} --quiet --eval 'db.adminCommand({{ping:1}}).ok' | grep -q 1 || exit 1"
    initiate = (
        "try { rs.status() } catch (e) { rs.initiate({ _id: 'rs0', "
        "members: [{ _id: 0, host: 'mongodb:27017' }] }) }"
    )
    return {
        "mongodb": {
            "image": f"docker.io/mongo:{spec.mongo_tag}",
            "restart": "always",
            "command": ["mongod", "--replSet", "rs0", "--bind_ip_all"],
            "volumes": ["mongodb_data:/data/db"],
            "healthcheck": {
                "test": ["CMD-SHELL", ping],
                "interval": "10s",
                "timeout": "10s",
                "retries": 30,
                "start_period": "20s",
            },
        },
        "mongo-init": {
            "image": f"docker.io/mongo:{spec.mongo_tag}",
            "restart": "no",
            "depends_on": {"mongodb": {"condition": "service_healthy"}},
            "entrypoint": [shell, "--host", "mongodb:27017", "--quiet", "--eval", initiate],
        },
    }


def build(spec: Spec) -> dict:
    """Return the compose document (a plain dict) for `spec`."""
    official = spec.mongo_flavor == "official"

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

    services: dict = {"rocketchat": rocketchat}
    services.update(
        _mongo_service_official(spec) if official else _mongo_service_bitnami(spec)
    )

    doc: dict = {
        "name": spec.project_name,
        "services": services,
        "volumes": {"mongodb_data": {"driver": "local"}},
    }

    # --- apply the preset's backing services / RC patch / depends_on ---
    if spec.preset.services:
        _deep_merge(doc["services"], copy.deepcopy(spec.preset.services))
    if spec.preset.rocketchat:
        _deep_merge(rocketchat, copy.deepcopy(spec.preset.rocketchat))
    for dep in spec.preset.depends_on:
        deps = rocketchat["depends_on"]
        if isinstance(deps, list):
            if dep not in deps:
                deps.append(dep)
        elif isinstance(deps, dict):
            deps.setdefault(dep, {"condition": "service_started"})

    return doc


def to_yaml(doc: dict) -> str:
    header = "# Generated by rc-repro. Do not edit by hand -- re-run `rc-repro up`.\n"
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
