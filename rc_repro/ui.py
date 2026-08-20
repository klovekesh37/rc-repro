"""Terminal output helpers — one place for rc-repro's color conventions.

Plain informational lines stay `typer.echo` at the call site; these wrap the
*status* colors (and error exit) so they're consistent and greppable, and give
a single seam for a future --quiet/--json mode.
"""

from __future__ import annotations

from typing import NoReturn

import typer

#: JSON mode: every human-facing line in this module moves to stderr, for the life
#: of the process. This is the seam the module docstring above has always promised.
#:
#: Redirected, not suppressed. stdout has to be a valid JSON document -- `rc-repro
#: info --json > case.json` is the whole point -- and a person who runs the same
#: command at a terminal should still see what happened. Two streams answer both;
#: silence would answer only the first.
#:
#: It lives HERE rather than as a flag threaded through each command, because the
#: prose is written by helpers several frames below the flag: a panel printed by
#: `_render_create_result` cannot see the `--json` that a command signature took.
_JSON = False


def json_mode(on: bool = True) -> None:
    global _JSON
    _JSON = on


def _echo(msg: str, **kw) -> None:
    """typer.secho, on stderr whenever stdout belongs to a machine."""
    typer.secho(msg, err=kw.pop("err", False) or _JSON, **kw)


def ok(msg: str) -> None:
    _echo(msg, fg=typer.colors.GREEN)


def warn(msg: str, *, err: bool = False) -> None:
    """Amber: something wants the reader's attention but nothing has failed.

    `err` puts it on stderr, for the case where the warning precedes a non-zero
    exit -- `rc-repro serve > /dev/null` must still show why nothing started.
    Default stays stdout so no existing caller moves stream.
    """
    _echo(msg, fg=typer.colors.YELLOW, err=err)


def fail(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)


def note(msg: str, *, bold: bool = False) -> None:
    """Preset tips / supplementary info (cyan). `bold` for a section heading."""
    _echo(msg, fg=typer.colors.CYAN, bold=bold)


def line(msg: str = "") -> None:
    """A plain content line, routed like every other human-facing line in here.

    `typer.echo` at a call site puts prose on stdout unconditionally, which in --json
    mode is the stream a caller is parsing. Content that belongs to a panel or a
    section goes through this instead, so the json_mode() seam covers all of it.
    """
    _echo(msg)


def die(msg: str, exit_code: int = 1) -> NoReturn:
    """Red line on stderr, then exit.

    `exit_code` defaults to 1 so every existing caller is unchanged. The domain
    errors pass their own, so a script can tell "not ready yet" from "known dead"
    without parsing the sentence -- see errors.EXIT_CODES.
    """
    fail(f"error: {msg}")
    raise typer.Exit(exit_code)


def hint(msg: str) -> None:
    """A dim next-step line under a panel."""
    _echo(msg, fg=typer.colors.BRIGHT_BLACK)


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
    out = lambda s: typer.echo(s, err=_JSON)  # noqa: E731
    out(b("+- ") + typer.style(title, fg=title_color, bold=True) + b(" " + dashes + "+"))
    for ln in lines:
        out(f"{side} {ln.ljust(width)} {side}")
    out(b("+" + "-" * (width + 2) + "+"))


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
    out = lambda s: typer.echo(s, err=_JSON)  # noqa: E731
    out(bar("+- " + title + " " + "-" * (width - len(title) + 1) + "+"))
    for c in cells:
        out(f"{side}  {c.ljust(width)}  {side}")
    out(bar("+" + "-" * (width + 4) + "+"))
