"""Domain errors shared by the CLI and the web API.

The CLI historically failed by calling `ui.die()` (which raises `typer.Exit`) at
~75 sites. That is fine for a one-shot process but fatal for a long-lived server:
a single bad request would exit the whole thing. The service layer instead raises
these, and each front-end translates them:

    CLI  ->  except ReproError as e: _err(str(e))     # red line + exit 1
    API  ->  except ReproError as e: HTTP e.http_status

`http_status` lets the web layer map each cause to the right status code without
the service layer importing anything web-specific.
"""

from __future__ import annotations


class ReproError(Exception):
    """Base for all expected, user-facing failures. http_status -> HTTP mapping."""
    http_status = 400


class ValidationError(ReproError):
    """Bad input (port range, name, unknown --set param, bad version/preset)."""
    http_status = 400


class ConflictError(ReproError):
    """A resource is already taken (host port / repro name / monitoring slot)."""
    http_status = 409


class NotFoundError(ReproError):
    """No such repro / default repro missing."""
    http_status = 404


class NotReadyError(ReproError):
    """Docker is down, or Rocket.Chat isn't serving / can't be logged into."""
    http_status = 409


class DockerError(ReproError):
    """A `docker`/`docker compose` invocation failed."""
    http_status = 502
