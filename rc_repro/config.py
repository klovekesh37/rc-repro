"""Paths, constants, and the small persisted config for rc-repro.

State lives under ~/.rc-repro (override with RC_REPRO_HOME):

    ~/.rc-repro/
      config.yaml            # default_repro, selector defaults, reg_token/overrides
      clients/               # isolated Kubernetes and Helm client state
      presets/               # user/team presets (override built-ins)
      repros/<name>/         # one workspace per repro
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import yaml

# Serialises read-modify-write of config.yaml (see update_config).
_CONFIG_LOCK = threading.Lock()

# Container-internal Rocket.Chat port. The published host port is chosen per repro.
RC_CONTAINER_PORT = 3000

# Docker compose project (and container) name prefix, so rc-repro's stacks are
# easy to tell apart from unrelated compose projects.
PROJECT_PREFIX = "rcrepro-"

# Admin user auto-provisioned into every repro (see presets / compose).
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
ADMIN_EMAIL = "admin@example.com"
ADMIN_NAME = "Admin"


def first_admin_env() -> dict[str, str]:
    """Rocket.Chat's shared first-user and completed local-setup contract.

    Both Compose and Kubernetes inject this exact map. Do not set
    ``INITIAL_USER``: Rocket.Chat expects a JSON user object there, and the
    scalar ``yes`` produces a JSON parse error on boot. The ADMIN_* variables
    plus the setup-wizard overwrite are the supported first-admin path.
    """
    return {
        "OVERWRITE_SETTING_Show_Setup_Wizard": "completed",
        "ADMIN_USERNAME": ADMIN_USERNAME,
        "ADMIN_NAME": ADMIN_NAME,
        "ADMIN_EMAIL": ADMIN_EMAIL,
        "ADMIN_PASS": ADMIN_PASSWORD,
    }

# Host ports published by preset side services. One registry so presets can't
# silently collide with each other — a new preset picks a port by looking here.
# (RC's own port is allocated dynamically per repro; these are fixed for now —
# see runner.used_ports, which makes them visible to allocation/preflight.)
PRESET_PORTS: dict[str, tuple[int, ...]] = {
    "saml": (8081,),          # Keycloak (SAML)
    "oidc": (8085,),          # Keycloak (OIDC; same port inside+out)
    "email": (8025,),         # Mailpit web UI / API
    "s3_minio": (9000, 9001), # MinIO S3 API + console
    "livechat": (8090,),      # demo "customer website" embedding the widget
}

# Host ports for the --monitor add-on (Prometheus, Grafana). Not a preset, so
# kept separate from PRESET_PORTS but treated the same for collision checks.
MONITOR_PORTS: tuple[int, int] = (9090, 5050)

# Host interface published ports bind to. Loopback: repros use weak fixed
# credentials, so they should not be reachable from the local network unless
# the user opts in (`up --bind 0.0.0.0` or RC_REPRO_BIND_HOST). Matches the
# official rocketchat-compose .env.example posture for local/backing services.
DEFAULT_BIND_HOST = "127.0.0.1"

# In-network addresses of the Mongo replica set (compose service DNS).
MONGO_URL = "mongodb://mongodb:27017/rocketchat?replicaSet=rs0"
MONGO_OPLOG_URL = "mongodb://mongodb:27017/local?replicaSet=rs0"

# Key under Metadata.extra where the email preset records Mailpit's URL, so
# rcapi.login can fetch email-2FA codes for rc-repro's own admin calls.
EXTRA_MAILPIT_URL = "mailpit_url"

# RC's REST rate limiter — disabled for the duration of a load test (and the
# seed) so the offered load isn't throttled into a false result, then restored.
RC_RATE_LIMITER_SETTING = "API_Enable_Rate_Limiter"

# Environment overrides for config.yaml values (env wins over the file):
#   RC_REPRO_REG_TOKEN  -> reg_token     RC_REPRO_RC_IMAGE -> rc_image
#   RC_REPRO_BIND_HOST  -> bind_host     (RC_REPRO_HOME is handled in home())
_ENV_OVERRIDES = {
    "reg_token": "RC_REPRO_REG_TOKEN",
    "rc_image": "RC_REPRO_RC_IMAGE",
    "bind_host": "RC_REPRO_BIND_HOST",
}


# config.yaml can hold a Cloud registration token (see _ENV_OVERRIDES), so both
# the file and the state dir are owner-only. No-ops on Windows, where chmod
# cannot express POSIX bits.
FILE_MODE = 0o600
DIR_MODE = 0o700


def _chmod_quietly(path: Path, mode: int) -> None:
    """chmod, ignoring platforms/filesystems that don't support it.

    A failure here must not break saving config: the write is the user's intent,
    the mode is a hardening measure.
    """
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def home() -> Path:
    """Root state directory, created on demand."""
    root = os.environ.get("RC_REPRO_HOME")
    base = Path(root) if root else Path.home() / ".rc-repro"
    return base


def repros_dir() -> Path:
    return home() / "repros"


def preset_dir() -> Path:
    return home() / "presets"


def reports_dir() -> Path:
    """Where benchmark/perf reports are written by default."""
    return home() / "reports"


def config_file() -> Path:
    return home() / "config.yaml"


def load_config(with_env: bool = True) -> dict:
    """Load ~/.rc-repro/config.yaml (or {}), with env-var overrides applied.

    Pass with_env=False when the dict will be written back via save_config —
    otherwise ephemeral env values (e.g. RC_REPRO_REG_TOKEN) would be persisted
    into the file.
    """
    path = config_file()
    data: dict = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded
    if with_env:
        for key, env in _ENV_OVERRIDES.items():
            val = os.environ.get(env)
            if val:
                data[key] = val
    return data


def save_config(cfg: dict) -> None:
    """Persist config.yaml atomically with owner-only permissions.

    config.yaml can hold a Rocket.Chat Cloud registration token, so it must not
    be world-readable. A per-process temporary name avoids concurrent writers
    clobbering one another before the atomic rename.
    """
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    _chmod_quietly(root, DIR_MODE)

    path = config_file()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    body = yaml.safe_dump(cfg, sort_keys=False)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _chmod_quietly(tmp, FILE_MODE)
    os.replace(tmp, path)
    _chmod_quietly(path, FILE_MODE)


def update_config(mutate) -> dict:
    """Read-modify-write config.yaml under a lock; ``mutate(cfg)`` edits in place."""
    with _CONFIG_LOCK:
        cfg = load_config(with_env=False)
        mutate(cfg)
        save_config(cfg)
        return cfg
