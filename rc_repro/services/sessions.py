"""Server-side sessions for the web GUI — the credential a browser actually carries.

Replaces HTTP Basic. The difference that matters is not cryptographic: a session is
a thing the SERVER holds, so it can be ended. Basic cannot be — there is no logout,
the password rides on every request for the life of the tab, and it is what forced
the password-derived cache in services/users.py. None of that is true here.

A session id is 256 bits from `secrets.token_urlsafe(32)`. Only its sha256 reaches
disk, for the reason the users file holds no passwords: a stolen copy of
`<home>/sessions` must not be a set of live credentials. Revoking one is deleting
a line, which is also what makes the file a working repair tool at 2am.

Two clocks, both wall-clock because they outlive the process:

    idle       12h   a support engineer should not sign in mid-investigation
    absolute    7d   a forgotten tab should not be a standing credential

`last_seen` is written back at most once a minute per session (_FLUSH_EVERY): the
dashboard polls every four seconds, and a disk write per poll per open tab is a
self-inflicted load for a value nobody reads at that resolution.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from rc_repro import config

SESSIONS_FILE = "sessions"

#: Bumped if the on-disk shape ever changes. Written as a comment line so
#: `rc-repro doctor` can refuse to run against a file a newer version wrote,
#: rather than silently misreading it.
STATE_VERSION = 1

IDLE_SECONDS = 12 * 3600
ABSOLUTE_SECONDS = 7 * 86400
_FLUSH_EVERY = 60.0

#: A session id is unguessable, so there is no lockout and no backoff here — the
#: only bound needed is on the number of lines, so a long-lived server cannot grow
#: this file without limit. Far above any real team's open-tab count.
_MAX_SESSIONS = 4096


@dataclass(frozen=True)
class Session:
    sid: str            # sha256 hex of the token — what is stored, never the token
    user: str
    created: int
    last_seen: int
    label: str          # "Firefox on Linux", for the "your sessions" list
    origin: str         # session | setup

    @property
    def expires_at(self) -> int:
        """Whichever bound bites first. Derived, never stored — storing it would
        mean two sources of truth for one fact."""
        return min(self.created + ABSOLUTE_SECONDS, self.last_seen + IDLE_SECONDS)

    def alive(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.expires_at

    def public(self) -> dict:
        """What may be shown to the owner. `sid` is truncated: the full value is a
        verifier, and a page that lists it hands over every session it lists."""
        return {"sid": self.sid[:8], "user": self.user, "created": self.created,
                "last_seen": self.last_seen, "label": self.label,
                "origin": self.origin, "expires_at": self.expires_at}


def sessions_file() -> Path:
    return config.home() / SESSIONS_FILE


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- persistence ----------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, Session] = {}
_stamp: tuple[int, int] = (-1, -1)
_flushed: dict[str, float] = {}

_HEADER = (
    "# rc-repro GUI sessions. Managed by the server; delete a line to revoke it.\n"
    f"# rc-repro-state: {STATE_VERSION}\n"
    "# sha256(token)\tuser\tcreated\tlast_seen\tlabel\torigin\n"
)


@contextmanager
def _flocked():
    """Cross-process exclusion, layered under the thread lock.

    The same two layers `runner.repro_lock` uses, and for the same reason: `serve`
    mutates this from worker threads while `rc-repro users remove` mutates it from
    another process entirely. flock is per open file description, so the thread
    lock is not redundant.
    """
    with _lock:
        try:
            import fcntl
        except ImportError:                       # pragma: no cover - Windows
            yield
            return
        lock_dir = config.home() / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_dir / "sessions.lock", "w", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _file_stamp() -> tuple[int, int]:
    try:
        st = sessions_file().stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (-1, -1)


def _parse(line: str) -> Session | None:
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        return None
    parts = line.split("\t")
    if len(parts) < 6:
        return None
    try:
        return Session(sid=parts[0], user=parts[1], created=int(parts[2]),
                       last_seen=int(parts[3]), label=parts[4], origin=parts[5])
    except (ValueError, IndexError):
        return None


def _load(force: bool = False) -> dict[str, Session]:
    """The session map, re-read only when the file has actually changed.

    mtime AND size, exactly as services/users.py does it: a same-size rewrite
    inside the mtime granularity is what a revocation looks like.
    """
    global _stamp
    stamp = _file_stamp()
    if not force and stamp == _stamp and _cache:
        return _cache
    out: dict[str, Session] = {}
    try:
        text = sessions_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError:
        return _cache
    for line in text.splitlines():
        s = _parse(line)
        if s:
            out[s.sid] = s
    _cache.clear()
    _cache.update(out)
    _stamp = stamp
    return _cache


def _write(rows: dict[str, Session]) -> None:
    """Rewrite atomically, owner-only, dropping anything already expired.

    0600 is applied at CREATION through os.open rather than by a later chmod: this
    file is a set of live credentials and a world-readable window is the whole
    problem.
    """
    global _stamp
    now = time.time()
    keep = [s for s in rows.values() if s.alive(now)]
    keep.sort(key=lambda s: s.last_seen, reverse=True)
    del keep[_MAX_SESSIONS:]
    path = sessions_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    body = _HEADER + "".join(
        f"{s.sid}\t{s.user}\t{s.created}\t{s.last_seen}\t{s.label}\t{s.origin}\n"
        for s in keep)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    _cache.clear()
    _cache.update({s.sid: s for s in keep})
    # `_flushed` mirrors this file, so it has to shrink with it. Entries were only
    # ever popped on an explicit revoke -- and a session normally ends by EXPIRING,
    # or by being dropped by the _MAX_SESSIONS cap, neither of which went through
    # revoke_sid(). So the file was bounded two ways while the map that shadows it
    # grew for the life of the process. Slow for a human team; fast for anything
    # scripted against POST /api/session, which mints a session per call.
    live = {s.sid for s in keep}
    for sid in [k for k in _flushed if k not in live]:
        del _flushed[sid]
    _stamp = _file_stamp()


def _clean(value: str, limit: int = 60) -> str:
    """A field that cannot break the record separator. The label comes from a
    User-Agent header, which is attacker-controlled."""
    return "".join(c for c in (value or "") if c.isprintable() and c != "\t")[:limit]


# --- public API ------------------------------------------------------------------

def create(user: str, *, label: str = "", origin: str = "session") -> str:
    """Mint a session for `user` and return the RAW token — the only time it exists.

    The caller puts it in a cookie; the server keeps only its digest, so a session
    cannot be recovered from disk, only revoked.
    """
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    s = Session(sid=_digest(token), user=user, created=now, last_seen=now,
                label=_clean(label) or "unknown", origin=origin)
    with _flocked():
        rows = dict(_load(force=True))
        rows[s.sid] = s
        _write(rows)
    return token


def verify(token: str) -> Session | None:
    """The session this token names, or None if it is unknown or expired.

    Touches `last_seen` so an active tab keeps its session alive, but writes that
    back at most once a minute per session (see _FLUSH_EVERY).
    """
    if not token:
        return None
    sid = _digest(token)
    with _lock:
        rows = _load()
        s = rows.get(sid)
    if s is None:
        return None
    now = time.time()
    if not s.alive(now):
        return None
    if now - _flushed.get(sid, 0.0) >= _FLUSH_EVERY:
        _flushed[sid] = now
        touched = Session(sid=s.sid, user=s.user, created=s.created,
                          last_seen=int(now), label=s.label, origin=s.origin)
        try:
            with _flocked():
                rows = dict(_load(force=True))
                if sid in rows:                  # not revoked while we waited
                    rows[sid] = touched
                    _write(rows)
        except OSError:
            pass                                 # a read-only home must not 500
        return touched
    return s


def revoke(token: str) -> bool:
    """End one session by its token (what sign-out does)."""
    return revoke_sid(_digest(token))


def revoke_sid(sid: str) -> bool:
    with _flocked():
        rows = dict(_load(force=True))
        if sid not in rows:
            return False
        del rows[sid]
        _write(rows)
    _flushed.pop(sid, None)
    return True


def revoke_user(user: str) -> int:
    """End every session belonging to `user`. Returns how many.

    Called on 'sign out everywhere', and on a password change or account removal —
    a credential that has been revoked must not keep working through a session that
    outlived it.
    """
    with _flocked():
        rows = dict(_load(force=True))
        doomed = [sid for sid, s in rows.items() if s.user == user]
        for sid in doomed:
            del rows[sid]
            _flushed.pop(sid, None)
        if doomed:
            _write(rows)
    return len(doomed)


def list_for(user: str) -> list[Session]:
    """A user's own live sessions, newest activity first."""
    now = time.time()
    with _lock:
        # SNAPSHOT inside the lock. _load() returns the module-level _cache itself,
        # not a copy, and both _load(force=True) and _write() clear it -- so a lazy
        # generator consumed by sorted() AFTER the lock released could be iterating
        # the dict while another request rebuilt it. Reproduced: RuntimeError,
        # "dictionary changed size during iteration", which reaches the browser as
        # a 500. GET /api/users calls this once per account, so an admin opening
        # People rolled that dice once for every person on the server.
        rows = list(_load().values())
    return sorted((s for s in rows if s.user == user and s.alive(now)),
                  key=lambda s: s.last_seen, reverse=True)


def describe_agent(user_agent: str) -> str:
    """"Firefox on Linux" from a User-Agent, for the sessions list.

    Deliberately crude. This exists so somebody can recognise their own devices in
    a list, not to profile anything, and a wrong guess costs a slightly odd label.
    """
    ua = user_agent or ""
    browser = next((n for n, k in (
        ("Edge", "Edg/"), ("Opera", "OPR/"), ("Firefox", "Firefox/"),
        ("Chrome", "Chrome/"), ("Safari", "Safari/"), ("curl", "curl/")
    ) if k in ua), "")
    system = next((n for n, k in (
        ("Windows", "Windows"), ("macOS", "Mac OS"), ("Android", "Android"),
        ("iOS", "iPhone"), ("iOS", "iPad"), ("Linux", "Linux")
    ) if k in ua), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "unknown"
