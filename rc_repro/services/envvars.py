"""Change a running repro's Rocket.Chat environment — shared by CLI and web API.

An env var cannot be changed inside a running container: the environment is fixed
when the container is created. What CAN be done is rewrite the compose file and let
`docker compose up -d` recreate just the Rocket.Chat service — verified: Mongo is
left running and its volume untouched, so no data is lost and it takes seconds
(no image pull).

Overrides are stored in the repro's metadata (`extra["env"]`), not only in the
compose file, because `up --force` rebuilds compose from the spec — anything living
only in the generated file is silently dropped on the next rebuild.

Same shape as services/monitor.py, which mutates a running repro the same way.
"""

from __future__ import annotations

import re

from rc_repro import compose, config, rcapi, runner
from rc_repro.errors import DockerError, ValidationError
from rc_repro.services import lifecycle
from rc_repro.services.events import Emit, info, null_emit, warn

# A POSIX-ish environment variable name. Anything else produces a compose file
# docker will not accept, so it is refused rather than written out.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Overriding these breaks how rc-repro itself talks to the repro. Allowed anyway —
# reproducing a broken configuration is the point of the tool — but never silently.
_LOAD_BEARING = {
    "MONGO_URL": "rc-repro's Mongo connection; Rocket.Chat will not start if it is wrong",
    "PORT": "the container port rc-repro publishes; the repro becomes unreachable",
    "ROOT_URL": "set from --root-url/--https; overriding it here will fight those",
    "TRANSPORTER": "the multi-instance NATS mesh",
}


SETTING_PREFIX = "OVERWRITE_SETTING_"


def prefix_settings(settings: dict) -> dict:
    """{"Some_Setting": v} -> {"OVERWRITE_SETTING_Some_Setting": v}.

    A Rocket.Chat SETTING only takes effect from the environment with this prefix; a
    bare setting id changes nothing at all. Both front-ends hand their "this is a
    setting" input through here rather than each prepending the prefix itself, so
    the rule lives in one place.

    Already-prefixed keys pass through, so a caller that pastes the full name is not
    double-prefixed.
    """
    out = {}
    for key, value in (settings or {}).items():
        key = str(key).strip()
        bare = key[len(SETTING_PREFIX):] if key.startswith(SETTING_PREFIX) else key
        check_names([bare])
        out[SETTING_PREFIX + bare] = value
    return out


def as_setting(pairs: list[str]) -> dict:
    """`--setting Id=value` -> {"OVERWRITE_SETTING_Id": "value"}."""
    return prefix_settings(parse_set(pairs))


def warn_bare_settings(meta, keys: list[str], emit: Emit = null_emit) -> None:
    """Warn when a key is a Rocket.Chat setting id used WITHOUT the prefix.

    This is the one mistake here with no feedback of its own: docker accepts the
    variable, Rocket.Chat ignores it, and the setting silently keeps its old value.
    Verified against a live workspace -- `Accounts_ShowFormLogin=false` left the
    setting `true`, while `OVERWRITE_SETTING_Accounts_ShowFormLogin=false` applied.

    Best-effort: the workspace is asked which names are settings, so this is right
    for whatever version is running. If it cannot be reached, say nothing.
    """
    candidates = [k for k in keys if not k.startswith(SETTING_PREFIX)]
    if not candidates:
        return
    try:
        auth = lifecycle.login(meta)
        known = rcapi.setting_ids(meta.root_url, auth, config.ADMIN_PASSWORD)
    except Exception:  # noqa: BLE001 - not serving yet, or not logged in; skip quietly
        return
    if not known:
        return
    for key in candidates:
        if key in known:
            warn(emit, f"{key} is a Rocket.Chat SETTING, not an environment variable. "
                       f"On its own it has no effect - use {SETTING_PREFIX}{key} "
                       f"(or `--setting {key}=...`).", phase="config")


def parse_set(pairs: list[str]) -> dict:
    """Turn ["K=V", ...] into {K: V}. Raises on a malformed pair or bad name."""
    out: dict = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise ValidationError(
                f"--set {raw!r} is not KEY=VALUE (to remove a key, use --unset {raw})")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not _NAME_RE.match(key):
            raise ValidationError(
                f"{key!r} is not a valid environment variable name (letters, digits "
                "and underscore; not starting with a digit)")
        out[key] = value
    return out


def check_names(keys: list[str]) -> None:
    for key in keys:
        if not _NAME_RE.match(key.strip()):
            raise ValidationError(f"{key!r} is not a valid environment variable name")


def current(name: str) -> dict:
    """The RC service's effective environment, credentials masked, plus which keys
    are user overrides — so a caller can show what was changed versus inherited.

    Answered per runtime. On Compose that is the generated compose document; on
    Kubernetes there is no such document, so the running container is asked -- which
    is strictly more accurate, since the chart contributes variables rc-repro never
    set and the helm values would not show them.

    This path first raised a bare FileNotFoundError naming
    `repros/<n>/docker-compose.yml` — a path, with no statement of what was wrong —
    which escaped the ReproError contract so `serve` answered 500 to a request that
    was merely unsupported. It then refused cleanly, and now answers.
    """
    from rc_repro.services import topology
    target = lifecycle.resolve_name(name)
    meta = runner.read_meta(target)
    if topology.of_repro(target) == topology.KUBERNETES:
        # Read from the RUNNING CONTAINER, not from a document. Compose can answer
        # this from the file it generated; there is no such file here, and the helm
        # values are only what rc-repro asked for -- the chart adds its own on top,
        # so they are not the answer to "what is Rocket.Chat running with". This
        # path used to raise a bare FileNotFoundError naming a compose file that
        # does not exist, and then refused outright; asking the container is both
        # honest and strictly more accurate than the Compose answer.
        from rc_repro.services import k8s
        context = str((meta.extra or {}).get("context") or k8s.CONTEXT)
        env = k8s.container_env(target, context=context)
        overrides = meta.extra.get("env") if isinstance(meta.extra, dict) else {}
        overrides = overrides if isinstance(overrides, dict) else {}
        return {
            "name": target,
            "env": [{"key": k, "value": lifecycle.redact_env(k, str(v)),
                     "override": k in overrides}
                    for k, v in sorted(env.items())],
            "overrides": sorted(overrides),
        }
    doc = runner.read_compose(target)
    svcs = doc.get("services", {})
    rc = svcs.get("rocketchat") or svcs.get("rocketchat-1") or {}
    env = rc.get("environment", {})
    if isinstance(env, list):                       # compose list form "K=V"
        env = dict((e.split("=", 1) + [""])[:2] for e in env)
    overrides = meta.extra.get("env") if isinstance(meta.extra, dict) else {}
    overrides = overrides if isinstance(overrides, dict) else {}
    return {
        "name": target,
        "env": [{"key": k, "value": lifecycle.redact_env(k, str(v)),
                 "override": k in overrides}
                for k, v in sorted(env.items())],
        "overrides": sorted(overrides),
    }


def set_env(name: str, sets: dict | None = None, unset: list[str] | None = None,
            *, restart: bool = True, emit: Emit = null_emit) -> dict:
    """Apply env changes to a repro and recreate Rocket.Chat so they take effect.

    `sets` adds or replaces; `unset` removes a key entirely (including a base or
    preset default, which merely blanking it would not do).
    """
    from rc_repro.services import topology
    # No Kubernetes path yet. Reaching for the compose project answers
    # "no configuration file provided", which names nothing a user can act
    # on -- so this refuses and hands over the command that does the job.
    topology.require_compose(name, "env",
                             instead="Use `helm -n rc-repro-{t} upgrade rocketchat --reuse-values --set extraEnv[0].name=...`, or `rc-repro api` for a runtime setting.".replace("{t}", name))
    lifecycle.require_docker()
    target = lifecycle.resolve_name(name)
    # Serialised against every other mutating operation on this repro: this does
    # read-compose -> write-compose -> `docker compose up`, and interleaving that
    # with a backup, an upgrade or another env change races compose against itself.
    with runner.repro_lock(target):
        return _set_env_locked(target, sets, unset, restart=restart, emit=emit)


def _set_env_locked(target: str, sets: dict | None, unset: list[str] | None,
                    *, restart: bool = True, emit: Emit = null_emit) -> dict:
    sets = dict(sets or {})
    unset = list(unset or [])
    check_names(list(sets) + unset)
    if not sets and not unset:
        raise ValidationError("nothing to change (use --set KEY=VALUE or --unset KEY)")

    meta = runner.read_meta(target)
    overrides = dict(meta.extra.get("env") or {}) if isinstance(meta.extra, dict) else {}
    for key in unset:
        # None (not "") records "remove this key", so a preset default really goes
        # away instead of being set to an empty string.
        overrides[key] = None
    overrides.update(sets)

    for key in sets:
        if key in _LOAD_BEARING:
            warn(emit, f"{key} is load-bearing: {_LOAD_BEARING[key]}. Setting it "
                       "anyway.", phase="config")
    warn_bare_settings(meta, list(sets), emit)

    doc = runner.read_compose(target)
    rc_services = [s for s in doc.get("services", {})
                   if s == "rocketchat" or s.startswith("rocketchat-")]
    if not rc_services:
        raise DockerError(f"{target!r} has no rocketchat service to change")
    for svc in rc_services:
        env = doc["services"][svc].setdefault("environment", {})
        if isinstance(env, list):
            env = dict((e.split("=", 1) + [""])[:2] for e in env)
            doc["services"][svc]["environment"] = env
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)

    meta.extra["env"] = overrides
    runner.write(target, compose.to_yaml(doc), meta)
    changed = ", ".join(sorted(list(sets) + unset))
    info(emit, f"env updated on {target!r}: {changed}", phase="config")

    if not restart:
        info(emit, "not restarting; the change applies next time the container is "
                   "recreated (`rc-repro up --name " + target + "`)", phase="done")
        return {"name": target, "restarted": False, "overrides": _shown(overrides)}

    # An env change only reaches Rocket.Chat by recreating the container. compose
    # recreates just the services whose config changed, so Mongo keeps running and
    # its volume is untouched -- no data loss, and no image pull.
    info(emit, "recreating the Rocket.Chat container (data is kept)...", phase="boot")
    if runner.up(target, pull=False) != 0:
        raise DockerError("`docker compose up` failed applying the env change")
    info(emit, f"{target!r} restarted with the new environment", phase="done")
    return {"name": target, "restarted": True, "overrides": _shown(overrides)}


def _shown(overrides: dict) -> list[dict]:
    """Overrides for display: values masked by key, removals marked."""
    out = []
    for key, value in sorted(overrides.items()):
        out.append({"key": key, "removed": value is None,
                    "value": "" if value is None
                             else lifecycle.redact_env(key, str(value))})
    return out
