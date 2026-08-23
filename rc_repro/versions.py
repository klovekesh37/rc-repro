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


#: Set by `_resolve_online` when the release endpoint answered 404 -- i.e. it was
#: reachable and said this version does not exist. Distinct from "could not ask",
#: which is the only thing returning None used to mean.
_NOT_A_RELEASE = "not_a_release"


def _resolve_online(version: str, rc: Version, timeout: float = 5.0,
                    why: list | None = None) -> Resolved | None:
    """Query releases.rocket.chat. Returns None on any problem (caller falls back).

    `why` collects WHY, because the caller has been unable to tell the difference. A
    404 from a reachable endpoint means the version was never released; anything else
    means the lookup failed. Both returned None, so `rc-repro versions 99.99.99` fell
    through to the curated map, found a rule matching the series, and exited 0 with a
    confident pairing for a release that does not exist -- as did `8.5.1-rc.1`.
    """
    try:
        resp = requests.get(RELEASES_URL.format(version=version), timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code == 404 and why is not None:
        why.append(_NOT_A_RELEASE)
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
    # The API's answer is not necessarily a tag. Returning None here rather than
    # guessing means the curated map decides, which is the better answer when the
    # live value cannot be read at all.
    tag = _series_tag(best)
    if tag is None:
        return None

    # WHICH END WAS TAKEN, SAID OUT LOUD. `_highest` picks the newest compatible
    # MongoDB, and for a support reproduction the interesting one is usually the
    # customer's, which for an older Rocket.Chat is usually the OLDEST supported. RC
    # 4.8.7 offers 3.6,4.0,4.2,4.4,5.0: this resolves 5.0 while the shipped
    # versions.yaml rule gives 4.4 with the note "needs MONGO_OPLOG_URL" -- so `up -v
    # 4.8.7` and `up -v 4.8.7 --offline` pair a different MongoDB MAJOR, and one of
    # them changes whether the oplog path is exercised at all. `--mongo` overrides and
    # nothing prompted, which left the difference invisible.
    note = ("compatibleMongoVersions=" + ",".join(compatible)
            + (f" (took the NEWEST, {best}; the oldest supported is "
               f"{_lowest(compatible)} — `--mongo <tag>` to pin the customer's)"
               if len(compatible) > 1 else ""))
    if tag != best:
        # Said out loud in `rc-repro versions <x>`: the pairing on screen is then
        # explainable, instead of a number that appears nowhere in the API's answer.
        note += f" (bare major {best!r} -> {tag}, the series the registry publishes)"

    return Resolved(
        rc_version=version,
        rc_image="",  # filled in by resolve()
        mongo_tag=tag,
        mongo_flavor=_flavor(tag),
        mongo_shell=_shell(tag),
        oplog=_oplog(rc),
        source="releases.rocket.chat",
        note=note,
    )


def _series_tag(value: str) -> str | None:
    """A `compatibleMongoVersions` entry as a tag the registry actually publishes.

    releases.rocket.chat is not consistent about the shape of this field. RC 7.12
    and below answer ["5.0","6.0","7.0"] and 8.x answers ["8.0"] -- but RC 7.13.x
    answers ["5","6","7","8"], a bare major. The value was used verbatim as a
    docker tag, and `mongodb/mongodb-community-server` publishes only
    <major>.<minor>, so RC 7.13.x resolved to `8-ubi8` and every boot died on
    "failed to resolve reference ... not found". Checked against the registry:
    8-ubi8 and 7-ubi8 are 404; 8.0-ubi8 and 7.0-ubi8 are 200.

    A bare major means the GA series for that major, which is always `.0` -- every
    Mongo major from 4 to 8 publishes one. Anything that already carries a minor is
    left alone, including a three-part tag like 8.0.4, which the registry also
    publishes.

    None for anything that is not a plain numeric version, so the caller falls back
    to the curated map instead of inventing a tag out of something it cannot read.
    """
    parts = value.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return f"{parts[0]}.0" if len(parts) == 1 else value


def _lowest(tags: list[str]) -> str | None:
    """The oldest parseable tag. Reported beside the chosen one, never chosen: changing
    which end is taken would repair one class of reproduction and break another."""
    best_raw, best_ver = None, None
    for tag in tags:
        try:
            v = Version(tag)
        except InvalidVersion:
            continue
        if best_ver is None or v < best_ver:
            best_ver, best_raw = v, tag
    return best_raw


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

    why: list[str] = []
    if not offline:
        online = _resolve_online(version, rc, why=why)
        if online is not None:
            online.rc_image = rc_image
            return online

    for rule in data["rules"]:
        if Version(version) in SpecifierSet(rule["rc"]):
            note = rule.get("note", "")
            if _NOT_A_RELEASE in why:
                # SAID, rather than resolved silently. The pairing below is a rule for
                # the SERIES and is the best guess available -- but the endpoint was
                # reachable and answered 404, which is positive evidence that this
                # version was never published, and a caller that gets a clean answer
                # for a typo will spend the next ten minutes on a failing image pull.
                note = (f"releases.rocket.chat has no {version} — it may be a typo, "
                        f"unreleased, or a pre-release tag. The pairing below comes "
                        f"from the series rule, not from that release."
                        + (f" {note}" if note else ""))
            return Resolved(
                rc_version=version,
                rc_image=rc_image,
                mongo_tag=rule["mongo"],
                mongo_flavor=_flavor(rule["mongo"]),
                mongo_shell=_shell(rule["mongo"]),
                oplog=_oplog(rc),
                source="map (no such release)" if _NOT_A_RELEASE in why
                       else "map (fallback)",
                note=note,
            )

    raise ValueError(f"no rule matches RC {version} and the live lookup was unavailable")
