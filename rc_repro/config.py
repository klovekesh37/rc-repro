"""Paths, constants, and the small persisted config for rc-repro.

State lives under ~/.rc-repro (override with RC_REPRO_HOME):

    ~/.rc-repro/
      config.yaml            # default_repro, bind_host, acme.*, gui.* — NO secrets:
                             #   reg_token is env/flag only and is never persisted
                             #   (see update_config's with_env=False)
      presets/               # user/team presets (override built-ins)
      repros/<name>/         # one workspace per repro
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
from pathlib import Path

import yaml

# Serialises read-modify-write of config.yaml (see update_config). THREADS only --
# `_config_flock` is the other half, for processes.
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
    "ldap": (8082,),          # phpLDAPadmin — browse the directory the preset seeded
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

# Key under Metadata.extra naming the runtime a workspace runs on. ABSENT means
# docker -- every workspace created before this key existed is a compose one, and
# `services/topology.py` reads a missing value as such rather than migrating
# repro.json. See that module for why the default is silent.
EXTRA_RUNTIME = "runtime"

# Key under Metadata.extra naming HOW Rocket.Chat is arranged (monolith /
# multi-instance / microservices). Absent on a workspace older than the key, where
# the deployment can still be recovered: it WAS the preset name.
EXTRA_DEPLOYMENT = "deployment"

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


def home() -> Path:
    """Root state directory. RESOLVES a path; it does not create anything.

    The docstring said "created on demand", which reads as a promise this function
    makes and does not keep -- the mkdir lives in the two writers below (0700, because
    accounts and sessions live in here), so a reader who trusted this would file the
    resulting FileNotFoundError against the wrong function.
    """
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
    """Persist config.yaml atomically (temp file + rename).

    A plain write_text could be read half-written — the web GUI runs service calls
    on worker threads, so concurrent readers are real.
    """
    # 0700 ON CREATE, so a box that never runs `serve` is not left depending on the
    # umask. `serve` tightens an existing loose home (cli.py) and `doctor` reports
    # one, but neither runs on a pure-CLI box -- and this directory also holds the
    # audit log, the accounts file and the ACME material. config.yaml itself holds no
    # secret (see the module docstring), so this is depth rather than a fix.
    home().mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config_file()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # 0600 via os.open rather than write_text, which would take the umask.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(yaml.safe_dump(cfg, sort_keys=False))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)   # no-op after a successful replace


@contextlib.contextmanager
def _config_flock():
    """Hold an exclusive lock on config.yaml across PROCESSES, not just threads.

    `_CONFIG_LOCK` is a `threading.Lock`, which says nothing about a second process
    -- and two are the normal case here: `rc-repro serve` runs continuously while
    somebody uses the CLI on the same box. Both read config.yaml, each mutates a
    different key, both write; the later `os.replace` wins and the other change is
    gone with nothing logged. Atomic replacement prevents a torn FILE, never a lost
    UPDATE, and those are different problems.

    Same two-layer shape as `runner.repro_lock`, for the same reason and with the
    same degradation: no fcntl (Windows) means thread-only, because a hard
    dependency would be worse than the race it prevents.
    """
    try:
        import fcntl
    except ImportError:                     # pragma: no cover - not POSIX
        yield
        return
    root = home()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / ".config.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                        # releases the flock with it


def update_config(mutate) -> dict:
    """Read-modify-write config.yaml under a lock; `mutate(cfg)` edits in place.

    Serialised because the GUI's worker threads can otherwise interleave two
    read-modify-write cycles and lose one of the updates. Reads with
    with_env=False on purpose: this writes the file back, and an ephemeral
    RC_REPRO_REG_TOKEN must never be persisted into it.
    """
    with _CONFIG_LOCK, _config_flock():
        cfg = load_config(with_env=False)
        mutate(cfg)
        save_config(cfg)
        return cfg
