"""The first-run key: how the very first account gets created.

Somebody has to be able to create the first admin, and at that moment there is
nobody to authorise them. The old answer was a session token in the URL, printed
on every `serve` start -- which is a standing credential with no identity behind
it, regenerated on every restart, landing in shell history and screenshots.

This is the narrow replacement:

* It exists ONLY on a loopback bind with no accounts. On any reachable interface
  `serve` refuses to start instead, because a bootstrap credential reachable from
  the network is the thing being removed, not a smaller version of it.
* It rides in the URL FRAGMENT (`/setup#k=...`). A fragment is never sent to the
  server, so it cannot appear in an access log, a proxy log or a `Referer`.
* Single use and 15 minutes. Consuming it CREATES the first admin in the same
  request, so there is never an anonymous privileged session -- not even briefly.
* Only the sha256 reaches disk, like every other credential here.

A second `serve` reports the outstanding key rather than minting another, so the
URL printed in a scrollback stays the one that works.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path

from rc_repro import config

FIRST_RUN_FILE = "first-run"
TTL_SECONDS = 15 * 60


def path() -> Path:
    return config.home() / FIRST_RUN_FILE


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _read() -> tuple[str, int] | None:
    """(sha256, expires) for the outstanding key, or None."""
    try:
        for line in path().read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            digest, _minted, expires = line.split("\t")[:3]
            return digest, int(expires)
    except (OSError, ValueError):
        return None
    return None


def outstanding() -> bool:
    """Whether an unexpired key is already waiting to be used."""
    found = _read()
    return bool(found and found[1] > time.time())


def mint() -> str:
    """Create a key and return it. The caller prints it; only its hash is kept."""
    key = secrets.token_urlsafe(24)
    now = int(time.time())
    body = ("# rc-repro first-run key. Single use, 15 minutes.\n"
            "# sha256(key)\tminted\texpires\n"
            f"{_digest(key)}\t{now}\t{now + TTL_SECONDS}\n")
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, p)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return key


def valid(key: str) -> bool:
    """Whether `key` is the outstanding, unexpired one. Constant-time compare."""
    found = _read()
    if not found or not key:
        return False
    digest, expires = found
    return secrets.compare_digest(digest, _digest(key)) and expires > time.time()


def consume() -> None:
    """Burn the key. Called once the first account exists, so it cannot be replayed."""
    path().unlink(missing_ok=True)
