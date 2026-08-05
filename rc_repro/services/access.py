"""Operator access handoff for loopback-bound repros.

Repros bind to loopback by default and never open a firewall or public ingress.
A local interactive session gets the direct URL. An SSH session cannot open the
remote host's localhost in a local browser, so the handoff supplies a copy-safe
tunnel command and the browser URL to open on the operator's own machine.
"""

from __future__ import annotations

import os
import shlex
import socket
from typing import Callable
from urllib.parse import urlsplit

from rc_repro.errors import ValidationError


def is_ssh_session(env: dict[str, str] | None = None) -> bool:
    """True when the current process is running inside an SSH login."""
    e = env if env is not None else os.environ
    return bool(e.get("SSH_CONNECTION") or e.get("SSH_CLIENT") or e.get("SSH_TTY"))


def _hostname(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else os.environ
    if e.get("RC_REPRO_SSH_HOST"):
        return e["RC_REPRO_SSH_HOST"]
    # SSH_CONNECTION is "client-ip client-port server-ip server-port". The
    # server address is normally a more useful return destination than the
    # remote machine's internal HOSTNAME.
    connection = (e.get("SSH_CONNECTION") or "").split()
    if len(connection) == 4:
        return connection[2]
    return e.get("HOSTNAME") or socket.gethostname() or "remote-host"


def _ssh_user(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else os.environ
    return e.get("RC_REPRO_SSH_USER") or e.get("USER") or e.get("LOGNAME") or "user"


def pick_local_port(preferred: int,
                    in_use: Callable[[int], bool] | None = None) -> int:
    """Choose a local tunnel bind port.

    Prefer the repro's host port so the browser URL matches the remote URL.
    When that port is known occupied on the operator side (or the test injects
    that knowledge), walk upward deterministically.
    """
    if preferred < 1:
        preferred = 3000
    check = in_use or (lambda _p: False)
    port = preferred
    for _ in range(1000):
        if not check(port):
            return port
        port += 1
    return preferred


def _preferred_local_port(remote_port: int, preferred: int | None,
                          env: dict[str, str]) -> int:
    """Resolve the operator-selected client port.

    A remote process cannot inspect listeners on the operator's machine. The
    public ``RC_REPRO_SSH_LOCAL_PORT`` override is therefore the honest way to
    select a deterministic alternative when the suggested port is occupied.
    """
    raw = preferred if preferred is not None else env.get("RC_REPRO_SSH_LOCAL_PORT")
    if raw in (None, ""):
        return remote_port
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "RC_REPRO_SSH_LOCAL_PORT must be an integer between 1024 and 65535") from exc
    if not 1024 <= port <= 65535:
        raise ValidationError(
            "RC_REPRO_SSH_LOCAL_PORT must be between 1024 and 65535")
    return port


def _forward_target(root_url: str) -> str:
    """Return the loopback address matching the recorded root URL."""
    try:
        host = urlsplit(root_url).hostname
    except ValueError:
        host = None
    return "[::1]" if host == "::1" else "127.0.0.1"


def handoff(host_port: int, root_url: str = "", *,
            remote: bool | None = None,
            preferred_local: int | None = None,
            local_port_in_use: Callable[[int], bool] | None = None,
            env: dict[str, str] | None = None) -> dict:
    """Structured access information for humans and agents.

    Never changes the repro's bind address. Loopback remains the secure default;
    remote operators tunnel to it.
    """
    e = env if env is not None else os.environ
    is_remote = is_ssh_session(e) if remote is None else remote
    remote_port = int(host_port)
    # Prefer IPv4 loopback in the browser URL for copy-safety; IPv6 uses brackets.
    browser_host = "127.0.0.1"
    if is_remote:
        selected = _preferred_local_port(remote_port, preferred_local, e)
        local_port = pick_local_port(
            selected, local_port_in_use)
        user = _ssh_user(e)
        host = _hostname(e)
        destination = shlex.quote(f"{user}@{host}")
        target = _forward_target(root_url)
        tunnel = f"ssh -N -L {local_port}:{target}:{remote_port} {destination}"
        browser = f"http://{browser_host}:{local_port}"
        return {
            "mode": "remote_ssh",
            "bind": "loopback",
            "host_port": remote_port,
            "local_port": local_port,
            "browser_url": browser,
            "tunnel_command": tunnel,
            "note": ("The repro listens on loopback on the remote host only. "
                     "Run the tunnel command on your machine, then open the "
                     "browser URL there. Credentials stay private to that tunnel. "
                     "If the local port is occupied, set RC_REPRO_SSH_LOCAL_PORT "
                     "on the remote command and request the handoff again."),
        }

    # Local interactive: direct loopback, no tunnel instructions.
    url = root_url or f"http://localhost:{remote_port}"
    return {
        "mode": "local",
        "bind": "loopback",
        "host_port": remote_port,
        "local_port": remote_port,
        "browser_url": url,
        "tunnel_command": None,
        "note": None,
    }
