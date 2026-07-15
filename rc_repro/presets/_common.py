"""Shared helpers for dynamic preset builders: `--set KEY=VALUE` params arrive
as raw strings, so every builder needs the same small coercions."""

from __future__ import annotations


def truthy_param(params: dict, key: str, default: bool = False) -> bool:
    """Read a boolean --set param ("1"/"true"/"yes"/"on", case-insensitive)."""
    val = params.get(key)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def int_param(params: dict, key: str, default: int) -> int:
    """Read an integer --set param; empty/absent -> default. A non-numeric
    value raises a ValueError the CLI shows verbatim (not a traceback)."""
    val = params.get(key)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ValueError(f"--set {key}={val!r} expects a whole number") from None


def str_param(params: dict, key: str, default: str) -> str:
    """Read a string --set param; empty/absent -> default."""
    val = params.get(key)
    if val is None or str(val) == "":
        return default
    return str(val)
