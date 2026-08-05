"""Internal contract for built-in reproduction scenarios.

The public aggregate remains :class:`rc_repro.presets.Preset`.  A scenario only
owns the deployment-neutral intent and its parameter contract; each adapter
turns that intent into the native fields needed by one supported deployment
type.  This module deliberately contains no plugin loading or public CLI
surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class Scenario:
    """One built-in scenario and its small deployment-specific adapters."""

    name: str
    params_help: Mapping[str, str]
    resolve_intent: Callable[[Mapping[str, str]], Any]
    adapters: Mapping[str, Callable[[Any], Any]]

    def resolve(self, params: Mapping[str, str] | None, deployment_type: str):
        params = dict(params or {})
        unknown = sorted(set(params) - set(self.params_help))
        if unknown:
            valid = ", ".join(sorted(self.params_help)) or "(this preset takes no --set params)"
            raise ValueError(
                f"unknown --set param(s) for preset {self.name!r}: "
                f"{', '.join(unknown)} - valid: {valid}")
        try:
            adapter = self.adapters[deployment_type]
        except KeyError as exc:
            supported = ", ".join(sorted(self.adapters)) or "none"
            raise ValueError(
                f"scenario {self.name!r} does not support deployment type "
                f"{deployment_type!r}; supported: {supported}") from exc
        return adapter(self.resolve_intent(params))
