"""Domain errors shared by the CLI and the web API.

The CLI historically failed by calling `ui.die()` (which raises `typer.Exit`) at
~75 sites. That is fine for a one-shot process but fatal for a long-lived server:
a single bad request would exit the whole thing. The service layer instead raises
these, and each front-end translates them:

    CLI  ->  except ReproError as e: _fail(e)        # red line + e.exit_code
    API  ->  except ReproError as e: HTTP e.http_status

One taxonomy, three consumers. `http_status` lets the web layer map each cause to
the right status code without the service layer importing anything web-specific;
`exit_code` does the same for the CLI so a script or agent can tell "not ready
yet" from "known dead" without parsing text; `code` is the stable identifier a
machine-readable payload reports, so callers branch on it rather than on prose.

`code` values are stable API. Renaming one is a breaking change; the message
attached to an exception is not stable and may be reworded freely.
"""

from __future__ import annotations


class ReproError(Exception):
    """Base for all expected, user-facing failures.

    Subclasses set `code` (stable identifier), `exit_code` (CLI process exit),
    and `http_status` (web API). The base itself is the catch-all for an expected
    failure with no more specific cause.
    """
    http_status = 400
    code = "REPRO_ERROR"
    exit_code = 1


class ValidationError(ReproError):
    """Bad input (port range, name, unknown --set param, bad version/preset)."""
    http_status = 400
    code = "VALIDATION_FAILED"
    exit_code = 2      # usage error: fix the call, retrying unchanged won't help


class ConflictError(ReproError):
    """A resource is already taken (host port / repro name / monitoring slot)."""
    http_status = 409
    code = "CONFLICT"
    exit_code = 8      # pick another name/port and retry


class NotFoundError(ReproError):
    """No such repro / default repro missing."""
    http_status = 404
    code = "NOT_FOUND"
    exit_code = 4


class NotReadyError(ReproError):
    """Docker is down, or Rocket.Chat isn't serving / can't be logged into."""
    http_status = 409
    code = "NOT_READY"
    exit_code = 5      # still unknown: the caller may poll again


class DockerError(ReproError):
    """A `docker`/`docker compose` invocation failed."""
    http_status = 502
    code = "ENGINE_UNAVAILABLE"
    exit_code = 3      # preflight/engine problem: run `rc-repro doctor`


class CreateFailedError(ReproError):
    """A create hit a condition that cannot self-heal, so waiting is pointless.

    Distinct from NotReadyError on purpose: 5 means "still unknown, the clock ran
    out", 7 means "known dead, stop now". Collapsing them is what makes callers
    wait out a run that had already failed.
    """
    http_status = 500
    code = "CREATE_FAILED"
    exit_code = 7


class AuthorityGateError(ReproError):
    """An action needs a human decision rc-repro is not authorised to make.

    Public exposure, an unapproved cluster, new credentials, deleting something
    rc-repro does not own, retaining a run. Never retryable and never
    auto-approvable: `approve_with` is what to ask a human to run, not something
    the caller may run itself.
    """
    http_status = 403
    code = "GATE"
    exit_code = 6

    def __init__(self, message: str, *, kind: str = "", subject: str = "",
                 approve_with: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind                  # e.g. "cluster", "retention"
        self.subject = subject            # what the gate is about
        self.approve_with = approve_with  # exact command for a human to run
        if code:
            # Gates are reported with a specific code (GATE_UNAPPROVED_CLUSTER,
            # GATE_DELETE_UNOWNED, ...) while sharing one exception type.
            self.code = code

    def as_gate(self) -> dict:
        """The gate as a plain dict, for a machine-readable error payload."""
        return {"kind": self.kind, "subject": self.subject,
                "approve_with": self.approve_with}


#: Every authority gate rc-repro can raise, declared rather than invented at the
#: call site. AuthorityGateError takes its code as an argument, so without this
#: registry the published error-code set could only advertise the base "GATE" and a
#: caller could not know what to expect. Adding a gate means adding it here, which
#: is the point: a gate a caller cannot anticipate is one it cannot handle.
GATE_CODES: dict[str, str] = {
    "GATE_NOT_ONBOARDED": "rc-repro has not been onboarded on this machine",
    "GATE_OWNED_CLUSTER": "the rc-repro-owned local cluster was not authorised",
    "GATE_ENGINE_RESIZE": "resizing the container engine was not authorised",
    "GATE_UNAPPROVED_CLUSTER": "the target Kubernetes cluster is not an approved one",
    "GATE_DELETE_UNOWNED": "the resource was not created by rc-repro",
    "GATE_PUBLIC_EXPOSURE": "the request would expose a repro beyond loopback",
    "GATE_RETENTION": "retaining this run was not authorised",
}


#: Every exit code this taxonomy can produce, with a short machine-readable
#: label. Front-ends publish this rather than hardcoding the numbers, so the map
#: has exactly one definition.
EXIT_CODES: dict[int, str] = {
    0: "ok",
    1: "internal",
    2: "usage",
    3: "preflight",
    4: "not_found",
    5: "not_ready",
    6: "gate",
    7: "create_failed",
    8: "conflict",
}
