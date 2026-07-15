"""Terminal output helpers — one place for rc-repro's color conventions.

Plain informational lines stay `typer.echo` at the call site; these wrap the
*status* colors (and error exit) so they're consistent and greppable, and give
a single seam for a future --quiet/--json mode.
"""

from __future__ import annotations

from typing import NoReturn

import typer


def ok(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.GREEN)


def warn(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.YELLOW)


def fail(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)


def note(msg: str) -> None:
    """Preset tips / supplementary info (cyan)."""
    typer.secho(msg, fg=typer.colors.CYAN)


def die(msg: str) -> NoReturn:
    fail(f"error: {msg}")
    raise typer.Exit(1)
