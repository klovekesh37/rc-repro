"""Progress event model shared by every long-running service function.

A single stream type unifies the two progress channels that exist today: Python
`log=` callbacks (seed, config-import) and streamed subprocess output (docker,
k6). A service function takes `emit: Emit` and calls it with `Event`s; the CLI
prints them, the web API buffers them per job and pushes them over SSE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    message: str
    phase: str = "info"        # pull|boot|wait|post_ready|seed|k6|restore|done|...
    level: str = "info"        # info|warn|error
    pct: float | None = None   # 0-100 when known
    data: dict[str, Any] = field(default_factory=dict)
    terminal: bool = False     # last event of a job; `data` carries the Result

    def as_dict(self) -> dict:
        return {"message": self.message, "phase": self.phase, "level": self.level,
                "pct": self.pct, "data": self.data, "terminal": self.terminal}


# An emitter is any callable taking one Event. Kept as a plain callable (not a
# class) so tests can pass `events.append` and the CLI can pass a small adapter.
Emit = Callable[[Event], None]


def null_emit(_: Event) -> None:
    """Discard events (default for callers that don't care about progress)."""


def info(emit: Emit, message: str, *, phase: str = "info", pct: float | None = None,
         **data: Any) -> None:
    emit(Event(message, phase=phase, pct=pct, data=data))


def warn(emit: Emit, message: str, *, phase: str = "info", **data: Any) -> None:
    emit(Event(message, phase=phase, level="warn", data=data))
