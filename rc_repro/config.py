"""Paths, constants, and the small persisted config for rc-repro.

State lives under ~/.rc-repro (override with RC_REPRO_HOME):

    ~/.rc-repro/
      config.yaml            # default_repro, reg_token, rc_image overrides
      presets/               # user/team presets (override built-ins)
      repros/<name>/         # one workspace per repro
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

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


def home() -> Path:
    """Root state directory, created on demand."""
    root = os.environ.get("RC_REPRO_HOME")
    base = Path(root) if root else Path.home() / ".rc-repro"
    return base


def repros_dir() -> Path:
    return home() / "repros"


def preset_dir() -> Path:
    return home() / "presets"


def config_file() -> Path:
    return home() / "config.yaml"


def load_config() -> dict:
    """Load ~/.rc-repro/config.yaml, or {} if absent."""
    path = config_file()
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        return {}
    return data


def save_config(cfg: dict) -> None:
    home().mkdir(parents=True, exist_ok=True)
    config_file().write_text(yaml.safe_dump(cfg, sort_keys=False))
