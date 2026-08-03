"""Emulate customer-sized hardware for a load test.

`--constrain "rc=2cpu/2g,mongo=1cpu/1g"` caps the repro's services with
`docker update` (live, no restart) so results answer "what does *their*
2-CPU/2GB box handle?" instead of "what does my laptop handle" — then restores
the caps afterwards.

Restore semantics: `docker update` cannot CLEAR a limit (setting 0 is silently
ignored by the engine), so a service that was unlimited before is restored to
the docker VM's full capacity — functionally identical, since a container can
never exceed the VM anyway.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from rc_repro import runner

# svc aliases: `rc` covers the single- and multi-instance service names.
_MEM_RE = re.compile(r"^\d+(\.\d+)?(g|gb|m|mb)$")


def parse(spec: str) -> dict[str, dict]:
    """'rc=2cpu/2g,mongo=1cpu' -> {'rc': {'cpus': 2.0, 'mem': '2g'}, …}.
    Each entry is svc=PARTS with '/'-separated parts: a CPU count ('2cpu',
    '0.5cpu') and/or a memory cap ('2g', '512m'). Raises ValueError on nonsense."""
    out: dict[str, dict] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(f"bad constraint {entry!r} (want e.g. rc=2cpu/2g)")
        svc, val = entry.split("=", 1)
        svc = svc.strip().lower()
        cpus, mem = None, None
        for part in val.split("/"):
            p = part.strip().lower()
            if p.endswith("cpu"):
                try:
                    cpus = float(p[:-3])
                except ValueError:
                    raise ValueError(f"bad CPU count in {entry!r} (want e.g. 2cpu, 0.5cpu)")
                if cpus <= 0:
                    raise ValueError(f"CPU count must be > 0 in {entry!r}")
            elif _MEM_RE.match(p):
                mem = p.rstrip("b")   # docker accepts 2g / 512m
            else:
                raise ValueError(f"bad constraint part {part!r} in {entry!r} "
                                 "(want e.g. 2cpu, 2g, 512m)")
        if cpus is None and mem is None:
            raise ValueError(f"constraint {entry!r} sets neither CPU nor memory")
        out[svc] = {"cpus": cpus, "mem": mem}
    return out


def resolve_services(constraints: dict[str, dict], services: list[str]) -> dict[str, dict]:
    """Expand aliases against the repro's actual compose services:
    `rc` -> rocketchat / rocketchat-1..N, `mongo` -> mongodb; anything else must
    be an exact service name. Raises ValueError for unknown services."""
    resolved: dict[str, dict] = {}
    for alias, lim in constraints.items():
        if alias == "rc":
            targets = [s for s in services if s == "rocketchat" or s.startswith("rocketchat-")]
        elif alias == "mongo":
            targets = [s for s in services if s == "mongodb"]
        else:
            targets = [s for s in services if s == alias]
        if not targets:
            raise ValueError(f"no service {alias!r} in this repro "
                             f"(services: {', '.join(sorted(services))})")
        for t in targets:
            resolved[t] = lim
    return resolved


def human(constraints: dict[str, dict]) -> str:
    """'rc=2cpu/2g, mongo=1cpu' — for banners, reports and baselines."""
    parts = []
    for svc, lim in constraints.items():
        bits = []
        if lim.get("cpus") is not None:
            bits.append(f"{lim['cpus']:g}cpu")
        if lim.get("mem"):
            bits.append(lim["mem"])
        parts.append(f"{svc}={'/'.join(bits)}")
    return ", ".join(parts)


@dataclass
class Applied:
    container: str          # container id
    service: str
    prior_nano: int         # NanoCpus before we touched it (0 = unlimited)
    prior_mem: int          # Memory bytes before (0 = unlimited)
    prior_swap: int         # MemorySwap before (0/-1 = default/unlimited)
    # WHICH dimensions apply() actually capped. Without this, restore() writes
    # both and imposes a memory limit (and disables swap) on a service that was
    # only ever CPU-capped — a change the test never asked for.
    set_cpus: bool = False
    set_mem: bool = False


def _inspect_limits(cid: str) -> tuple[int, int, int]:
    r = subprocess.run(
        ["docker", "inspect", "--format",
         "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}", cid],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"docker inspect failed: {r.stderr.strip()}")
    try:
        nano, mem, swap = (int(x) for x in r.stdout.split())
    except ValueError as exc:
        # Anything but three integers (a template mismatch, a truncated read)
        # must surface as the RuntimeError every caller catches, not a ValueError.
        raise RuntimeError(
            f"could not read current limits for {cid[:12]}: {r.stdout.strip()!r}") from exc
    return nano, mem, swap


def _update(cid: str, cpus: float | None, mem: str | None) -> None:
    cmd = ["docker", "update"]
    if cpus is not None:
        cmd += ["--cpus", f"{cpus:g}"]
    if mem:
        cmd += ["--memory", mem, "--memory-swap", mem]   # same value: no extra swap
    cmd.append(cid)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"docker update failed: {r.stderr.strip()}")


def apply(name: str, per_service: dict[str, dict]) -> list[Applied]:
    """Cap each service's running container(s); returns what was applied (with
    prior limits) for `restore`. A mid-way failure rolls back what was already
    applied, then re-raises — the caller never has to clean up a partial apply."""
    applied: list[Applied] = []
    try:
        for svc, lim in per_service.items():
            ids = runner.service_container_ids(name, svc)
            if not ids:
                raise RuntimeError(f"no running container for service {svc!r} — is the repro up?")
            for cid in ids:
                prior = _inspect_limits(cid)
                _update(cid, lim.get("cpus"), lim.get("mem"))
                applied.append(Applied(cid, svc, *prior,
                                       set_cpus=lim.get("cpus") is not None,
                                       set_mem=bool(lim.get("mem"))))
    except Exception:
        restore(applied)
        raise
    return applied


def _restore_swap(a: Applied, mem: float) -> int:
    """The MemorySwap value to restore alongside `mem`.

    Docker's MemorySwap: -1 = unlimited, > 0 = an explicit limit, and 0 = "unset",
    which the runtime reads as TWICE the memory limit. Folding that 0 into
    "swap == memory" (i.e. no swap at all) restores a stricter configuration than
    the container started with, so reproduce the 2x default instead.
    """
    if not a.prior_mem:            # previously unlimited -> match apply()'s no-extra-swap
        return int(mem)
    if a.prior_swap:               # -1 (unlimited) or an explicit byte limit
        return int(a.prior_swap)
    return int(a.prior_mem) * 2    # docker's implicit default


def restore(applied: list[Applied]) -> list[str]:
    """Undo `apply`, touching ONLY the dimensions apply() actually capped.

    Prior real limits are restored exactly; a dimension that was unlimited goes
    back to the docker VM's full capacity (see module docstring). Returns
    human-readable problems instead of raising: restore runs inside the callers'
    `finally`, so an exception here would skip their remaining cleanup and mask
    whatever error triggered it.
    """
    if not applied:
        return []
    cap = runner.docker_capacity() or (0.0, 0)
    problems: list[str] = []
    for a in applied:
        cmd = ["docker", "update"]
        unresolved: list[str] = []
        if a.set_cpus:
            cpus = (a.prior_nano / 1e9) if a.prior_nano else cap[0]
            if cpus:
                cmd += ["--cpus", f"{cpus:g}"]
            else:
                unresolved.append("CPU")
        if a.set_mem:
            mem = a.prior_mem if a.prior_mem else cap[1]
            if mem:
                cmd += ["--memory", str(int(mem)),
                        "--memory-swap", str(_restore_swap(a, mem))]
            else:
                unresolved.append("memory")
        # Report EVERY dimension that could not be put back. Testing only "is the
        # command still empty" hid the mixed case (one dimension known, the other
        # not), leaving half the test's cap applied with no warning at all.
        if unresolved:
            problems.append(
                f"{a.service}: could not determine {'/'.join(unresolved)} capacity "
                "to restore - the test's cap is still applied")
        if len(cmd) == 2:
            continue
        cmd.append(a.container)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip())
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            problems.append(f"{a.service}: {exc}")
    return problems
