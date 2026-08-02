"""Operator access handoff for loopback-bound repros.

Repros bind to loopback by default and never open a firewall or public ingress.
A local interactive session gets the direct URL. An SSH session cannot open the
remote host's localhost in a local browser, so the handoff supplies a copy-safe
tunnel command and the browser URL to open on the operator's own machine.
"""

from __future__ import annotations

import os
import socket
from typing import Callable


def is_ssh_session(env: dict[str, str] | None = None) -> bool:
    """True when the current process is running inside an SSH login."""
    e = env if env is not None else os.environ
    return bool(e.get("SSH_CONNECTION") or e.get("SSH_CLIENT") or e.get("SSH_TTY"))


def _hostname(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else os.environ
    return (e.get("RC_REPRO_SSH_HOST") or e.get("HOSTNAME")
            or socket.gethostname() or "remote-host")


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
        local_port = pick_local_port(
            preferred_local if preferred_local is not None else remote_port,
            local_port_in_use)
        user = _ssh_user(e)
        host = _hostname(e)
        tunnel = f"ssh -N -L {local_port}:127.0.0.1:{remote_port} {user}@{host}"
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
                     "browser URL there. Credentials stay private to that tunnel."),
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
