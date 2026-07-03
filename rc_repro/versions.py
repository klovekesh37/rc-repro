"""Resolve a Rocket.Chat version to its MongoDB pairing and runtime knobs.

Two tiers:
  1. LIVE  — releases.rocket.chat/<version>/info, the authoritative per-release
             compatibility data (pick the highest supported MongoDB).
  2. FALLBACK — the shipped data/versions.yaml, used when offline or for old
             releases that lack the field.

flavor / shell / oplog are always derived from the RC and MongoDB majors, so
there is one rule (the MongoDB tag) to maintain, not four.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

import requests
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

RELEASES_URL = "https://releases.rocket.chat/{version}/info"


@dataclass
class Resolved:
    rc_version: str
    rc_image: str
    mongo_tag: str
    mongo_flavor: str  # keyed on the MONGO version: "official" (Mongo >= 8) | "bitnami-legacy" (< 8)
    mongo_shell: str   # "mongosh" (Mongo >= 5) | "mongo" (older) — official flavor's init container
    oplog: bool        # RC major < 8 -> emit MONGO_OPLOG_URL (deprecated in 8.x)
    source: str        # "releases.rocket.chat" | "map (fallback)"
    note: str


@lru_cache(maxsize=1)
def _load_map() -> dict:
    text = resources.files("rc_repro").joinpath("data", "versions.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not data or "rules" not in data:
        raise RuntimeError("data/versions.yaml is missing or has no rules")
    return data


def apply_mongo_override(resolved: "Resolved", mongo_tag: str) -> None:
    """Apply a manual `--mongo` override, re-deriving flavor + shell from the tag."""
    resolved.mongo_tag = mongo_tag
    resolved.mongo_flavor = _flavor(mongo_tag)
    resolved.mongo_shell = _shell(mongo_tag)


def _mongo_major(mongo_tag: str) -> int:
    try:
        return int(mongo_tag.split(".")[0])
    except ValueError:
        return 0


def _flavor(mongo_tag: str) -> str:
    """Image flavor keyed on the MONGODB version. Mongo 8+ uses the official
    multi-arch image; older tags use bitnami-legacy (Bitnami's public images
    were deprecated -> moved to the `bitnamilegacy` namespace)."""
    return "official" if _mongo_major(mongo_tag) >= 8 else "bitnami-legacy"


def _shell(mongo_tag: str) -> str:
    return "mongosh" if _mongo_major(mongo_tag) >= 5 else "mongo"


def _oplog(rc: Version) -> bool:
    return rc.major < 8


def _resolve_online(version: str, rc: Version, timeout: float = 5.0) -> Resolved | None:
    """Query releases.rocket.chat. Returns None on any problem (caller falls back)."""
    try:
        resp = requests.get(RELEASES_URL.format(version=version), timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None

    compatible = payload.get("compatibleMongoVersions") or []
    best = _highest(compatible)
    if best is None:
        return None  # old releases omit the field

    return Resolved(
        rc_version=version,
        rc_image="",  # filled in by resolve()
        mongo_tag=best,
        mongo_flavor=_flavor(best),
        mongo_shell=_shell(best),
        oplog=_oplog(rc),
        source="releases.rocket.chat",
        note="compatibleMongoVersions=" + ",".join(compatible),
    )


def _highest(tags: list[str]) -> str | None:
    best_raw, best_ver = None, None
    for tag in tags:
        try:
            v = Version(tag)
        except InvalidVersion:
            continue
        if best_ver is None or v > best_ver:
            best_ver, best_raw = v, tag
    return best_raw


def resolve(version: str, *, offline: bool = False) -> Resolved:
    """Resolve `version` to a full MongoDB pairing."""
    try:
        rc = Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"{version!r} is not a valid version (want e.g. 6.5.3)") from exc

    data = _load_map()
    rc_image = data.get("default_rc_image", "registry.rocket.chat/rocketchat/rocket.chat")

    if not offline:
        online = _resolve_online(version, rc)
        if online is not None:
            online.rc_image = rc_image
            return online

    for rule in data["rules"]:
        if Version(version) in SpecifierSet(rule["rc"]):
            return Resolved(
                rc_version=version,
                rc_image=rc_image,
                mongo_tag=rule["mongo"],
                mongo_flavor=_flavor(rule["mongo"]),
                mongo_shell=_shell(rule["mongo"]),
                oplog=_oplog(rc),
                source="map (fallback)",
                note=rule.get("note", ""),
            )

    raise ValueError(f"no rule matches RC {version} and the live lookup was unavailable")
