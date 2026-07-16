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


def hint(msg: str) -> None:
    """A dim next-step line under a panel."""
    typer.secho(msg, fg=typer.colors.BRIGHT_BLACK)


def rule(n: int, color: str = typer.colors.BRIGHT_BLACK) -> str:
    """A horizontal rule made of ASCII '-' (width 1 in every terminal)."""
    return typer.style("-" * n, fg=color)


def box(title: str, lines: list[str], width: int,
        border: str = typer.colors.BRIGHT_BLACK, title_color: str = typer.colors.GREEN) -> None:
    """Box `lines` (already wrapped to <= width) under a titled top border.

    ASCII box chars only (width-1 everywhere). `width` is the inner content
    width; all boxes sharing a width line up. Title is colored, border dimmed.
    """
    b = lambda s: typer.style(s, fg=border)  # noqa: E731
    side = b("|")
    dashes = "-" * max(1, width - len(title) - 1)
    typer.echo(b("+- ") + typer.style(title, fg=title_color, bold=True) + b(" " + dashes + "+"))
    for ln in lines:
        typer.echo(f"{side} {ln.ljust(width)} {side}")
    typer.echo(b("+" + "-" * (width + 2) + "+"))


def panel(title: str, rows: list[tuple[str, str]], color: str = typer.colors.GREEN) -> None:
    """Render a boxed key/value summary panel with a titled top border.

    Uses ASCII box characters (+ - |) on purpose: Unicode box-drawing glyphs are
    East-Asian "ambiguous" width and render double-wide in some terminals, which
    breaks alignment. ASCII is width-1 everywhere. rows: (label, value) pairs;
    an empty label prints the value full-width.
    """
    label_w = max((len(lbl) for lbl, _ in rows if lbl), default=0)
    cells = [(f"{lbl:<{label_w}}  {val}" if lbl else val) for lbl, val in rows]
    width = max([len(title) + 2] + [len(c) for c in cells])
    bar = lambda s: typer.style(s, fg=color)  # noqa: E731
    side = bar("|")
    typer.echo(bar("+- " + title + " " + "-" * (width - len(title) + 1) + "+"))
    for c in cells:
        typer.echo(f"{side}  {c.ljust(width)}  {side}")
    typer.echo(bar("+" + "-" * (width + 4) + "+"))
