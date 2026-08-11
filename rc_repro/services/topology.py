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

from rc_repro import config, errors, runner

#: Canonical runtime names. These reach `repro.json` and are stable API.
DOCKER = "docker"
KUBERNETES = "kubernetes"

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
