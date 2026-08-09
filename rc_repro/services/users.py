"""Named accounts for the web GUI — shared by the CLI and the web API.

A shared deployment needs to answer "who tore down TICKET-1234?", and the session
token cannot: it is one secret handed to everybody, regenerated on every restart.
Named users give attribution, survive restarts, and let a person be removed without
disrupting anyone else.

Hashing is `hashlib.scrypt` from the standard library. rc-repro is deliberately
dependency-light and has no bcrypt/passlib; scrypt is memory-hard, and the ~100 ms
derivation also throttles guessing. The cost parameters live in each line, so they
can be raised later without invalidating existing entries.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rc_repro import config
from rc_repro.errors import ConflictError, NotFoundError, ValidationError

USERS_FILE = "users"

#: scrypt cost. n=16384 lands around 100 ms on a laptop, which is the point: it is
#: the difference between a guessable password being brute-forced in hours and in
#: centuries. Written into every line so raising it later is a per-user migration.
_N, _R, _P, _DKLEN, _SALT = 16384, 8, 1, 32, 16

#: A password below this is refused outright. Short of a policy engine, length is
#: the single control that matters most.
MIN_PASSWORD = 12

#: Usernames become part of a repro name and therefore a DNS label, so they are
#: restricted to what `sanitize()` would leave untouched.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def users_file() -> Path:
    return config.home() / USERS_FILE


@dataclass
class User:
    name: str
    created_at: str
    role: str = ""          # reserved: "" = full access. See _parse.


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """`scrypt$n$r$p$salt$hash`, self-describing so the cost can change later."""
    salt = salt or secrets.token_bytes(_SALT)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def _check(password: str, stored: str) -> bool:
    """Constant-time verify against a stored hash."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        want = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt),
                              n=int(n), r=int(r), p=int(p), dklen=len(_unb64(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(want, _unb64(digest))


def _parse(line: str) -> tuple[str, str, str, str] | None:
    """`name:hash:created_at[:role]` -> (name, hash, created_at, role).

    The trailing role is optional and unused today. It exists so a `readonly` or
    `admin` tier can be added without rewriting everyone's line.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) < 3:
        return None
    name, hashed, created = parts[0], parts[1], parts[2]
    role = parts[3] if len(parts) > 3 else ""
    if ":" in created:      # defensive: a legacy line with a full timestamp
        created = created.split(":")[0]
    return name, hashed, created, role


def _read() -> dict[str, tuple[str, str, str]]:
    """name -> (hash, created_at, role). Missing file is an empty dict, not an error."""
    path = users_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValidationError(f"cannot read {path}: {exc}") from exc
    out = {}
    for line in text.splitlines():
        parsed = _parse(line)
        if parsed:
            out[parsed[0]] = (parsed[1], parsed[2], parsed[3])
    return out


def _write(users: dict[str, tuple[str, str, str]]) -> None:
    """Rewrite the file atomically, owner-only.

    0600 is set BEFORE the content is in place: creating it world-readable and
    chmod-ing after leaves a window where the hashes are exposed.
    """
    path = users_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    lines = ["# rc-repro GUI accounts. Managed by `rc-repro users`.",
             "# name:hash:created_at[:role]"]
    for name in sorted(users):
        hashed, created, role = users[name]
        lines.append(f"{name}:{hashed}:{created}" + (f":{role}" if role else ""))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def require_valid_name(name: str) -> None:
    if not _NAME_RE.match(name or ""):
        raise ValidationError(
            f"invalid user name {name!r} — lowercase letters, digits and '-', "
            "starting with a letter or digit, at most 31 characters. It becomes "
            "part of a workspace name and therefore a DNS label.")


def require_valid_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ValidationError(
            f"password must be at least {MIN_PASSWORD} characters "
            f"(got {len(password or '')})")


# --- public API -----------------------------------------------------------------

def any_users() -> bool:
    """Whether Basic Auth can be enabled at all."""
    return bool(_read())


def list_users() -> list[User]:
    return [User(name=n, created_at=c, role=r)
            for n, (_h, c, r) in sorted(_read().items())]


def add(name: str, password: str, *, role: str = "") -> User:
    require_valid_name(name)
    require_valid_password(password)
    users = _read()
    if name in users:
        raise ConflictError(f"user {name!r} already exists "
                            f"(change the password with `rc-repro users passwd {name}`)")
    # Date, not a full timestamp: ":" is the field delimiter, and an ISO time
    # ("...T08:52:23+00:00") splits straight into the role column. Only ever
    # displayed, so day granularity is enough.
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    users[name] = (hash_password(password), created, role)
    _write(users)
    return User(name=name, created_at=created, role=role)


def set_password(name: str, password: str) -> None:
    require_valid_password(password)
    users = _read()
    if name not in users:
        raise NotFoundError(f"no user {name!r} (see `rc-repro users list`)")
    _hashed, created, role = users[name]
    users[name] = (hash_password(password), created, role)
    _write(users)


def remove(name: str) -> None:
    users = _read()
    if name not in users:
        raise NotFoundError(f"no user {name!r} (see `rc-repro users list`)")
    del users[name]
    _write(users)


# --- verification ------------------------------------------------------------------

#: There is deliberately NO verification cache here any more.
#:
#: One existed because HTTP Basic re-sent the password on EVERY request -- the
#: four-second dashboard poll, each SSE reconnect, every log fetch -- and ~100 ms
#: of scrypt per request would have saturated the threadpool. It cost a
#: password-derived value sitting in process memory for five minutes per session,
#: plus a stat() on every call to notice revocation, plus a whole invalidation
#: path that had to be right.
#:
#: With server-side sessions (services/sessions.py) verify() runs exactly once per
#: SIGN-IN, so the cache had nothing left to save. Removing it is the fix for that
#: memory exposure, and it is the kind of fix that cannot itself be buggy.

#: Failed attempts, for backoff. A 128-bit token is not guessable; a human password
#: is, so repeated failures have to cost something.
#:
#: Keyed on (name, source) -- the name ALONE was a denial of service against a
#: colleague. Names are not secret: `serve` prints "sign in as: alice, bob" at
#: startup and the dashboard renders an owner chip on every card, so five wrong
#: guesses from anyone who could reach the port locked a named person out. Keyed
#: on the pair, an attacker burns a counter belonging to their own address and
#: the real user is untouched. `source` is the resolved client address; "" for
#: the CLI, which has no client.
_LOCKOUT_AFTER = 5
_LOCKOUT_BASE = 2.0
_LOCKOUT_MAX = 300.0
_failures: dict[tuple[str, str], tuple[int, float]] = {}
#: Ceiling on the failure map. Far above any real workspace's user count, so a
#: legitimate lockout is never evicted; low enough that a name-varying flood
#: cannot grow it unboundedly.
_FAILURES_MAX = 4096
_fail_lock = threading.Lock()


def locked_out(name: str, source: str = "") -> float:
    """Seconds of backoff earned by failures for `name` from `source`, or 0.

    ADVISORY ONLY. This reports how long a client that keeps guessing has been
    asked to wait; it does not decide whether a credential is accepted. See
    verify(), where consulting this BEFORE checking the password meant a correct
    password was refused -- so the documented remedy for a compromised account
    (change the password) was itself what locked the owner out.
    """
    with _fail_lock:
        count, last = _failures.get((name, source), (0, 0.0))
    if count < _LOCKOUT_AFTER:
        return 0.0
    delay = min(_LOCKOUT_BASE ** (count - _LOCKOUT_AFTER + 1), _LOCKOUT_MAX)
    remaining = (last + delay) - time.monotonic()
    return max(0.0, remaining)


def _record_failure(name: str, source: str = "") -> None:
    with _fail_lock:
        now = time.monotonic()
        # An attacker who varies the name never trips the lockout AND grows this
        # dict without limit -- a slow memory leak driven by anyone who can reach
        # the port. Drop entries that can no longer produce a lockout, then cap:
        # the backoff is what this is for, and a bounded map still delivers it
        # for the pairs actually being attacked.
        if len(_failures) >= _FAILURES_MAX:
            stale = [k for k, (_c, t) in _failures.items() if now - t > _LOCKOUT_MAX]
            for k in stale:
                del _failures[k]
            if len(_failures) >= _FAILURES_MAX:
                _failures.clear()
        count, _last = _failures.get((name, source), (0, 0.0))
        _failures[(name, source)] = (count + 1, now)


def _clear_failures(name: str, source: str = "") -> None:
    with _fail_lock:
        _failures.pop((name, source), None)


def verify(name: str, password: str, *, source: str = "") -> bool:
    """Whether these credentials are valid. Constant-time; never cached.

    An unknown user still performs a full derivation: returning early would make
    the response time reveal which names exist.

    THE PASSWORD IS CHECKED FIRST, and a correct one is never refused because of a
    counter. The previous order -- lockout, then password -- had two reachable
    consequences. Anyone who could reach the port could lock out a named colleague
    with five wrong guesses. And after `rc-repro users passwd alice`, any browser
    tab still open re-sent the OLD credential on its four-second dashboard poll;
    each poll was a fresh failure that slid the window forward, so the account
    stayed locked indefinitely and the correct new password was refused with it.
    Measured before this change: after twenty minutes of one polling tab,
    locked_out() read 232s and the right password did not work.

    `source` is the resolved client address, so failures are counted per (name,
    address) -- see the note on _failures.
    """
    if not name or password is None:
        return False

    users = _read()
    stored = users.get(name)
    if stored is None:
        # Burn the same work as a real check so timing does not enumerate users.
        _check(password, hash_password("dummy-not-a-real-password"))
        _record_failure(name, source)
        return False
    if not _check(password, stored[0]):
        _record_failure(name, source)
        return False

    _clear_failures(name, source)
    return True
