"""Which runtime a workspace runs on.

Until now the answer was structural rather than recorded: a repro WAS a
`docker-compose.yml` on disk, and `runner.exists()` said so literally. That is
true of every workspace this tool has ever made, and it is the assumption a
second runtime has to displace -- so this module makes the answer a value that
can be read, rather than a shape that can only be inferred.

**A missing value means docker.** Every workspace created before this key existed
is a compose one, and there is no migration: `repro.json` files are not rewritten,
not on read and not on the next `up`. A migration would have to run against
workspaces belonging to other users on a shared box, could half-finish, and would
buy nothing that a default does not -- the absent key is not ambiguous, it is
older than the question.

**Unknown values also mean docker, and that is deliberate.** `of_meta` never
raises. A workspace written by a NEWER rc-repro naming a runtime this build has
never heard of must still list, still show its ports and still tear down; the
alternative is a repro that cannot be removed by the version installed. Refusing
belongs at the point of USE -- `require_compose` -- where the operation is known
and the message can say what to do instead. Reading is not use.

`normalize` is the opposite and does raise, because it is only ever called on
input a human just typed, where a silent fallback would boot the wrong thing.

Only `docker` is registered. A second runtime is an entry in `REGISTERED` plus
the module implementing it; nothing else in the codebase compares this string to
a literal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rc_repro import config, errors, runner

#: Canonical runtime names. These reach `repro.json` and are stable API.
DOCKER = "docker"
KUBERNETES = "kubernetes"

#: Canonical deployment names -- HOW Rocket.Chat is arranged, as opposed to WHERE
#: it runs. Which are legal depends on the runtime; see DEPLOYMENTS.
MONOLITH = "monolith"
MULTI_INSTANCE = "multi-instance"
MICROSERVICES = "microservices"

#: The deployments each runtime can serve, first entry being its default.
#: Kubernetes defaults to microservices because the chart's own default is
#: `microservices.enabled: true` and it is the shape customers actually run;
#: monolith-on-Kubernetes is the control experiment, not the common case.
DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    DOCKER: (MONOLITH, MULTI_INSTANCE),
    KUBERNETES: (MICROSERVICES, MONOLITH),
}

#: Deployment spellings, same reasoning as the runtime aliases.
DEPLOYMENT_ALIASES: dict[str, str] = {
    "monolith": MONOLITH,
    "monolithic": MONOLITH,
    "single": MONOLITH,
    "default": MONOLITH,
    "multi-instance": MULTI_INSTANCE,
    "multi_instance": MULTI_INSTANCE,
    "multiinstance": MULTI_INSTANCE,
    "microservices": MICROSERVICES,
    "microservice": MICROSERVICES,
    "micro": MICROSERVICES,
}

#: The preset that IS a deployment rather than a scenario, and the `--set` key it
#: used to take. Both spellings keep working; see `resolve_axes`.
LEGACY_MULTI_PRESET = "multi-instance"
LEGACY_REPLICA_PARAM = "instances"

#: What a human may type. Values are canonical names; keys are lowercased before
#: lookup. `compose` and `k8s` are here because they are what people actually
#: write, and a tool that rejects `--runtime k8s` is being pedantic at the user's
#: expense.
ALIASES: dict[str, str] = {
    "docker": DOCKER,
    "compose": DOCKER,
    "docker-compose": DOCKER,
    "dockercompose": DOCKER,
    "kubernetes": KUBERNETES,
    "k8s": KUBERNETES,
    "kube": KUBERNETES,
}

#: Runtimes this build can actually create. `KUBERNETES` is a canonical name with
#: no implementation yet -- it is spellable so that the refusal names it, rather
#: than the parser pretending never to have heard of it.
REGISTERED: frozenset[str] = frozenset({DOCKER})

#: Human labels, for a message or a GUI row.
LABELS: dict[str, str] = {
    DOCKER: "Docker Compose",
    KUBERNETES: "Kubernetes",
}


def normalize(value: str | None) -> str:
    """Canonical runtime name for something a human typed.

    Empty/None means the default, which is docker -- omitting `--runtime` is not
    an error, it is the common case. An unrecognised spelling raises, because it
    is a typo the caller can fix and guessing would boot the wrong topology.
    """
    text = str(value or "").strip().lower()
    if not text:
        return DOCKER
    found = ALIASES.get(text)
    if not found:
        known = ", ".join(sorted(set(ALIASES.values())))
        raise errors.ValidationError(
            f"unknown runtime {text!r}. Known runtimes: {known}."
        )
    return found


def is_registered(name: str) -> bool:
    """Whether this build can create workspaces on that runtime."""
    return name in REGISTERED


def label(name: str) -> str:
    """A human-readable name, falling back to the raw value for an unknown one."""
    return LABELS.get(name, name or DOCKER)


def of_meta(meta) -> str:
    """The runtime a workspace's metadata records. Never raises -- see the module
    docstring for why an unreadable or unknown value reads as docker."""
    extra = getattr(meta, "extra", None)
    if not isinstance(extra, dict):
        return DOCKER
    return ALIASES.get(str(extra.get(config.EXTRA_RUNTIME) or "").strip().lower(), DOCKER)


def of_repro(name: str) -> str:
    """The runtime of a named workspace.

    Tolerant of a workspace that is missing or mid-write: this must never be the
    thing that breaks an operation. If the metadata cannot be read, the answer
    that preserves today's behaviour is docker, and the caller's own existence
    check reports the real problem a moment later with a better message.
    """
    try:
        return of_meta(runner.read_meta(name))
    except (OSError, ValueError, TypeError):
        return DOCKER


def stamp(extra: dict | None, runtime: str = DOCKER) -> dict:
    """Record the runtime in an `extra` bag, returning it for chaining.

    Written on every create, including compose ones. Stamping only the new
    runtime would leave "absent" meaning two different things -- an old workspace
    and a new compose one -- and the first question asked of any workspace field
    is eventually "is this missing because it is old, or because it is false?".
    """
    bag = extra if isinstance(extra, dict) else {}
    bag[config.EXTRA_RUNTIME] = runtime
    return bag


def require_compose(name: str, operation: str, *, instead: str = "") -> None:
    """Refuse an operation that only Docker Compose can serve.

    The guard lives at the operation, not at the front door, because most of the
    tool is runtime-agnostic -- `seed`, `api`, `token` and `config-import` all
    speak REST to a URL and do not care what is behind it. Only the handful of
    commands that reach for a compose project need this.

    `instead` is the Kubernetes equivalent, when there is one. Saying only "not
    supported" leaves the user to search for the alternative that the person
    writing the refusal already knew.
    """
    runtime = of_repro(name)
    if runtime == DOCKER:
        return
    tail = f" {instead}" if instead else ""
    raise errors.ValidationError(
        f"{operation} reads the Docker Compose project, and {name!r} runs on "
        f"{label(runtime)}.{tail}"
    )


def normalize_deployment(value: str | None, runtime: str) -> str:
    """Canonical deployment name, checked against what the runtime can serve.

    Empty means that runtime's default. An unknown spelling raises; so does a
    legal-elsewhere one, and that refusal names the runtime it IS legal on --
    "unknown deployment 'microservices'" would be a lie, since it is a real
    deployment that this runtime simply cannot host.
    """
    text = str(value or "").strip().lower()
    legal = DEPLOYMENTS[runtime]
    if not text:
        return legal[0]
    found = DEPLOYMENT_ALIASES.get(text)
    if not found:
        raise errors.ValidationError(
            f"unknown deployment {text!r}. Known deployments: "
            f"{', '.join(sorted(set(DEPLOYMENT_ALIASES.values())))}."
        )
    if found not in legal:
        other = next((r for r, d in DEPLOYMENTS.items() if found in d), "")
        where = f" It is available on --runtime {other}." if other else ""
        raise errors.ValidationError(
            f"{label(runtime)} cannot run a {found!r} deployment.{where}"
        )
    return found


@dataclass
class Axes:
    """The three axes, resolved, plus how they map onto today's machinery.

    `preset` and `params` are what the existing preset loader is handed, so the
    deployment axis reaches compose.build through exactly the path it always did.
    That is what makes `--deployment multi-instance --replicas 3` produce a
    byte-identical compose file to `--preset multi-instance --set instances=3`:
    it IS that call, reached by a different spelling.
    """
    runtime: str = DOCKER
    deployment: str = MONOLITH
    replicas: int = 1
    preset: str = "default"
    params: dict = field(default_factory=dict)
    #: Deprecation notes for the caller to show. Not warnings -- nothing is wrong,
    #: the old spelling simply has a newer name.
    hints: list[str] = field(default_factory=list)


def resolve_axes(*, runtime: str = "", deployment: str = "", replicas: int = 0,
                 preset: str = "", params: dict | None = None) -> Axes:
    """Decide runtime x deployment x scenario, in one place.

    Every refusal in the matrix lives here rather than at a call site, because the
    combinations are what a user gets wrong and a rule enforced in two places
    eventually disagrees with itself.

    The old spellings are permanent aliases, not a deprecation with a deadline:
    `--preset multi-instance` and `--set instances=N` cost one lookup each, and
    breaking a command line that is pasted into support tickets buys nothing.
    """
    out = Axes(params=dict(params or {}))
    raw_preset = str(preset or "").strip() or "default"
    raw_deployment = str(deployment or "").strip().lower()

    # `--deployment microservices` with no --runtime implies Kubernetes. #3
    # established that spelling and there is no other runtime it could mean.
    implied = (DEPLOYMENT_ALIASES.get(raw_deployment) == MICROSERVICES and not runtime)
    out.runtime = KUBERNETES if implied else normalize(runtime)
    if implied:
        out.hints.append("--deployment microservices implies --runtime kubernetes")

    # `--preset multi-instance` is the deployment axis wearing last year's name.
    if raw_preset == LEGACY_MULTI_PRESET:
        if raw_deployment and DEPLOYMENT_ALIASES.get(raw_deployment) != MULTI_INSTANCE:
            raise errors.ValidationError(
                f"--preset {LEGACY_MULTI_PRESET} and --deployment {raw_deployment} "
                "ask for two different deployments. Drop the --preset; it is now "
                "spelled --deployment multi-instance."
            )
        raw_deployment, raw_preset = MULTI_INSTANCE, "default"
        out.hints.append(
            "--preset multi-instance is now --deployment multi-instance "
            "(the old spelling still works)")

    # `--set instances=N` is `--replicas N` wearing the same.
    legacy_n = out.params.get(LEGACY_REPLICA_PARAM)
    if legacy_n not in (None, "") and not replicas:
        out.hints.append(
            f"--set {LEGACY_REPLICA_PARAM}={legacy_n} is now --replicas {legacy_n} "
            "(the old spelling still works)")

    out.deployment = normalize_deployment(raw_deployment, out.runtime)
    out.preset = raw_preset

    # A scenario on top of multi-instance would need the two Presets merged, and
    # a Preset is all-or-nothing today -- `services`, `env` and `files` are
    # additive but nothing adds them. Refusing is honest; silently dropping one
    # would hand back a workspace missing the thing that was asked for.
    if out.deployment == MULTI_INSTANCE and out.preset not in ("", "default"):
        raise errors.ValidationError(
            f"the {out.preset!r} scenario cannot yet be combined with a "
            "multi-instance deployment on Docker Compose. Run them separately, "
            "or use --runtime kubernetes --replicas N once that lands."
        )

    out.replicas = _resolve_replicas(replicas, legacy_n, out)
    if out.deployment == MULTI_INSTANCE:
        # Reached through the preset loader exactly as before -- see Axes.
        out.preset = LEGACY_MULTI_PRESET
        out.params.setdefault(LEGACY_REPLICA_PARAM, out.replicas)
    return out


def _resolve_replicas(replicas: int, legacy_n, axes: Axes) -> int:
    """How many app instances, and whether asking for more than one makes sense.

    A lone Compose monolith cannot be replicated -- there is no load balancer in
    front of it and no NATS mesh between them, so `--replicas 3` there would
    silently produce one. Refusing names the flag that would make it work.
    """
    want = int(replicas or 0)
    if not want and legacy_n not in (None, ""):
        try:
            want = int(legacy_n)
        except (TypeError, ValueError):
            raise errors.ValidationError(
                f"--set {LEGACY_REPLICA_PARAM}={legacy_n!r} expects a whole number"
            ) from None
    if want and want < 1:
        raise errors.ValidationError("--replicas must be at least 1.")
    if axes.deployment == MONOLITH and want > 1:
        alt = ("--deployment multi-instance" if axes.runtime == DOCKER
               else "--deployment microservices")
        raise errors.ValidationError(
            f"a {label(axes.runtime)} monolith runs one Rocket.Chat, so --replicas "
            f"{want} would silently give you one. Add {alt}."
        )
    if axes.deployment == MULTI_INSTANCE:
        return want or 2          # the multi-instance preset's own default
    return want or 1


def require_registered(runtime: str) -> None:
    """Refuse a runtime this build cannot yet create.

    Kept apart from `resolve_axes` on purpose. Resolving answers "what did you
    ask for", which a GUI needs in order to show Kubernetes as an option that is
    visible but not yet available; this answers "can I do it", which only the
    create path asks. Folding them together would mean the GUI could not name a
    runtime without also failing on it.
    """
    if is_registered(runtime):
        return
    raise errors.ValidationError(
        f"{label(runtime)} workspaces are not available in this build yet. "
        f"Use --runtime docker."
    )


def axes_of_meta(meta) -> dict:
    """The axes a workspace was created with, as CreateReq kwargs.

    Two paths rebuild a request from an existing workspace -- `restore` and the
    GUI's recreate-a-downed-workspace button -- and both used to pass
    `preset=meta.preset`. For a multi-instance workspace that is the LEGACY
    spelling, so rebuilding one would print "--preset multi-instance is now
    --deployment multi-instance" at a user who typed neither and merely clicked
    Start. Reconstructing the axes properly means the deprecation hint only ever
    appears for a spelling someone actually used.

    Workspaces older than the deployment key are not lost: for those the
    deployment WAS the preset name, which is exactly the ambiguity the split
    removed going forward.
    """
    extra = getattr(meta, "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    runtime = of_meta(meta)
    preset = str(getattr(meta, "preset", "") or "default")
    recorded = str(extra.get(config.EXTRA_DEPLOYMENT) or "").strip().lower()
    deployment = DEPLOYMENT_ALIASES.get(recorded, "")
    if not deployment:
        deployment = (MULTI_INSTANCE if preset == LEGACY_MULTI_PRESET
                      else DEPLOYMENTS[runtime][0])
    if deployment == MULTI_INSTANCE:
        preset = "default"          # it is the deployment axis, not a scenario
    try:
        replicas = int(extra.get(LEGACY_REPLICA_PARAM) or 0)
    except (TypeError, ValueError):
        replicas = 0
    return {"runtime": runtime, "deployment": deployment,
            "preset": preset, "replicas": replicas}
