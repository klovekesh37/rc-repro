"""FastAPI application for the local rc-repro GUI.

Imported lazily by `rc-repro serve`. Blocking service calls run in path
operations declared with `def` (Starlette runs those in a threadpool), so the
event loop is never blocked. Long operations become background jobs (see
jobs.py) streamed to the browser over SSE.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from contextlib import asynccontextmanager
import json
import re
import time
import uuid
from importlib import resources
from pathlib import Path
from urllib.parse import quote

import subprocess
import threading

from fastapi import (Body, FastAPI, File, Form, Request, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match

from rc_repro import config
from rc_repro import presets as presets_mod
from rc_repro import runner
from rc_repro.errors import NotReadyError, ReproError, ValidationError
from rc_repro.services import data as datasvc
from rc_repro.services import audit as auditsvc
from rc_repro.services import lifecycle as lc
from rc_repro.services import sessions
from rc_repro.web import jobs as jobs_mod
from rc_repro.web import signin as signin_page
from rc_repro.web.jobs import JobManager

# Failed sign-ins per client address, for the one endpoint that derives scrypt.
# This is where a guessing bound BELONGS: services/users.py cannot refuse on a
# counter without refusing correct passwords too (that was B2), but a login
# endpoint can refuse to spend the CPU at all. Keyed on the address, so nobody can
# throttle a colleague by guessing at their name.
SIGNIN_MAX_FAILURES = 10
SIGNIN_WINDOW = 60.0
_signin_fails: dict[str, list[float]] = {}
_signin_lock = threading.Lock()


def _signin_retry_after(source: str) -> int:
    """Seconds this address must wait, or 0. Sliding window, so it recovers."""
    now = time.monotonic()
    with _signin_lock:
        hits = [t for t in _signin_fails.get(source, []) if now - t < SIGNIN_WINDOW]
        _signin_fails[source] = hits
        if len(hits) < SIGNIN_MAX_FAILURES:
            return 0
        return max(1, int(SIGNIN_WINDOW - (now - hits[0])) + 1)


def _signin_failed(source: str) -> None:
    now = time.monotonic()
    with _signin_lock:
        # Bounded: an address-varying flood must not grow this without limit.
        if len(_signin_fails) > 4096:
            _signin_fails.clear()
        _signin_fails.setdefault(source, []).append(now)


def _signin_ok(source: str) -> None:
    with _signin_lock:
        _signin_fails.pop(source, None)


# The minimum role for every route, by (method, route template). Three rules make
# this the whole of the authorization story rather than a checklist that drifts:
#
#   * DEFAULT DENY. A route missing from this table is admin-only at runtime, and
#     a test walks app.routes and fails the build if any /api/ route is unlisted.
#     An endpoint shipping unguarded is structurally the same mistake as the audit
#     gap -- something added in one place and not registered in the other.
#   * It lives in the WEB layer, deliberately, not in the service layer. The CLI
#     reaches the same service functions and `_cli_actor` honours RC_REPRO_USER as
#     given (cli.py:132), so a service-layer check would make `RC_REPRO_USER=<any
#     admin>` a one-word escalation -- a boundary that is not one.
#   * Reads are readonly+, writes are member+, and people-management is admin.
#     The exceptions are the reads that are not really reads: logs, the effective
#     environment, a minted PAT and an arbitrary REST call all hand over
#     credentials, so they are member+.
#
#   "" (empty) marks a route reachable with no session at all -- see `open_path`.
_OPEN, _READ, _WRITE, _ADMIN = "", "readonly", "member", "admin"

ROUTE_ROLES: dict[tuple[str, str], str] = {
    # the login itself, and the uptime check
    ("GET", "/signin"): _OPEN, ("POST", "/signin"): _OPEN,
    ("POST", "/signout"): _OPEN,
    ("GET", "/api/health"): _OPEN,
    ("GET", "/api/session"): _OPEN, ("POST", "/api/session"): _OPEN,
    ("DELETE", "/api/session"): _OPEN,
    # your own sessions
    ("GET", "/api/sessions"): _READ, ("DELETE", "/api/sessions"): _READ,
    # looking
    ("GET", "/api/repros"): _READ,
    ("GET", "/api/repros/{name}"): _READ,
    ("GET", "/api/repros/{name}/detail"): _READ,
    ("GET", "/api/repros/{name}/stats"): _READ,
    ("GET", "/api/repros/{name}/tls"): _READ,
    ("GET", "/api/repros/{name}/upgrade"): _READ,
    ("GET", "/api/jobs"): _READ,
    ("GET", "/api/jobs/{job_id}"): _READ,
    ("GET", "/api/jobs/{job_id}/stream"): _READ,
    ("GET", "/api/backups"): _READ,
    ("POST", "/api/backups/compatibility"): _READ,   # a question, not a change
    ("GET", "/api/doctor"): _READ,
    ("GET", "/api/edge"): _READ,
    ("GET", "/api/presets"): _READ,
    ("GET", "/api/settings"): _READ,
    ("GET", "/api/versions/{version}"): _READ,
    # reads that hand over credentials -- see the note above
    ("GET", "/api/repros/{name}/logs"): _WRITE,
    ("WS", "/api/repros/{name}/logs/stream"): _WRITE,
    ("GET", "/api/repros/{name}/env"): _WRITE,
    # changing a workspace
    ("POST", "/api/repros"): _WRITE,
    ("POST", "/api/repros/{name}/up"): _WRITE,
    ("POST", "/api/repros/{name}/state"): _WRITE,
    ("POST", "/api/repros/{name}/ready"): _WRITE,
    ("POST", "/api/repros/{name}/seed"): _WRITE,
    ("POST", "/api/repros/{name}/scale"): _WRITE,
    ("DELETE", "/api/repros/{name}/scale"): _WRITE,
    ("POST", "/api/repros/{name}/env"): _WRITE,
    ("POST", "/api/repros/{name}/monitor"): _WRITE,
    ("POST", "/api/repros/{name}/default"): _WRITE,
    ("POST", "/api/repros/{name}/config-import"): _WRITE,
    ("POST", "/api/repros/{name}/config-import/plan"): _WRITE,
    ("POST", "/api/repros/{name}/backup"): _WRITE,
    ("POST", "/api/repros/{name}/upgrade"): _WRITE,
    ("POST", "/api/repros/{name}/upgrade/rollback"): _WRITE,
    ("POST", "/api/repros/{name}/loadtest"): _WRITE,
    ("POST", "/api/repros/{name}/capacity"): _WRITE,
    ("POST", "/api/benchmark"): _WRITE,
    ("POST", "/api/restore"): _WRITE,
    ("POST", "/api/repros/{name}/pat"): _WRITE,
    ("POST", "/api/repros/{name}/call"): _WRITE,
    ("DELETE", "/api/repros/{name}"): _WRITE,   # ownership is checked in the handler
    ("POST", "/api/prune"): _WRITE,
    ("DELETE", "/api/backups"): _WRITE,
    # people
    ("GET", "/api/users"): _ADMIN,
    ("POST", "/api/users"): _ADMIN,
    ("DELETE", "/api/users/{name}"): _ADMIN,
    ("POST", "/api/users/{name}/role"): _ADMIN,
    ("POST", "/api/users/{name}/password"): _ADMIN,
    ("POST", "/api/me/password"): _READ,        # your own, whoever you are
    # readonly+ : the handler forces non-admins to their OWN lines, so the
    # role gate here is "signed in", not "may see everyone".
    ("GET", "/api/audit"): _READ,
}


def route_requirement(method: str, template: str) -> str | None:
    """The minimum role for a route, or None when it is not in the table.

    None means DENY -- returned rather than defaulted so the caller can tell
    "admin only" apart from "nobody registered this", and log the difference.
    """
    return ROUTE_ROLES.get((method.upper(), template))

# `docker compose logs --tail N` is buffered in memory server-side, so a
# caller-supplied N needs a ceiling.
TAIL_MAX = 5000
# An uploaded support dump is read into memory; cap it rather than trusting it.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
# Bounded so a chatty container plus a slow reader can't grow the queue forever.
WS_QUEUE_MAX = 10_000
# What the API-call console may send. A whitelist because `method` reaches
# requests.request() verbatim; the CLI's own examples only use these.
_API_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

_UPLOAD_ID_RE = re.compile(r"^u[0-9a-f]{12}$")


def _clamp_tail(tail: int) -> int:
    try:
        return max(1, min(int(tail), TAIL_MAX))
    except (TypeError, ValueError):
        return 200


def _only_set(only: str) -> set[str] | None:
    """Parse the comma-separated id-prefix filter into a set, or None for 'all'."""
    return {p.strip() for p in (only or "").split(",") if p.strip()} or None


def _prune_uploads(dest: Path, keep: int = 5) -> None:
    """Keep only the newest `keep` uploads.

    A previewed-but-never-applied dump has nothing to delete it, and these are
    customers' configuration files — they should not accumulate indefinitely.
    """
    uploads = sorted(dest.glob("u*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in uploads[keep:]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".only").unlink(missing_ok=True)


def _route_host(edgesvc, name: str) -> str:
    """The hostname a route file serves, for display."""
    try:
        for line in edgesvc.route_path(name).read_text().splitlines():
            if "rule:" in line and "Host(" in line:
                return line.split("Host(`", 1)[1].split("`", 1)[0]
    except (OSError, IndexError):
        pass
    return ""


def _confined_backup_out(out: str, backupsvc) -> str:
    """A caller-supplied backup destination, restricted to the managed directory.

    resolve() before comparing, so `..` and a symlink pointing out are both caught
    rather than only literal prefixes.
    """
    if not out:
        return ""
    root = backupsvc.backups_dir().resolve()
    dest = Path(out).expanduser()
    if not dest.is_absolute():
        dest = root / dest
    dest = dest.resolve()
    if dest != root and root not in dest.parents:
        raise ValidationError(
            f"`out` must be inside {root} — the HTTP API cannot choose arbitrary "
            "paths on the server (the CLI's --out still can)")
    return str(dest)


def _read_upload(file: UploadFile) -> bytes:
    """Read an upload with a hard cap (it used to be an unbounded .read())."""
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"settings file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    return data


def create_app(token: str = "", allow_hosts: list[str] | None = None, *,
               basic_auth: bool = False, accounts: bool = False,
               public_https: bool = False) -> FastAPI:
    # `accounts` replaces `basic_auth`: same meaning (named accounts exist, so the
    # login is enforced), different mechanism behind it. The old name is still
    # accepted so nothing outside has to change in the same commit.
    accounts = accounts or basic_auth
    # openapi_url=None as well as the doc UIs: the schema path does not start
    # with /api/, so `guard` below would hand it out without a token.
    jobs = JobManager()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # Shutdown. Job threads are daemons, so without this a `systemctl restart`
        # kills them mid-operation and skips every `finally` -- leaving Rocket.Chat
        # stopped after an interrupted backup, or the rate limiter off and CPU caps
        # applied after an interrupted load test. Run in a thread because drain()
        # blocks on join() and this is the event loop.
        left = await asyncio.to_thread(jobs.drain)
        if left:
            # Named, so the operator knows which repro to look at rather than
            # finding it in a strange state days later.
            print(f"rc-repro: shut down with {len(left)} job(s) still running: "
                  f"{', '.join(left)}", file=sys.stderr)

    app = FastAPI(title="rc-repro", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.token = token
    app.state.jobs = jobs
    # Whether the login is enforced.
    app.state.basic_auth = accounts      # legacy name, still read by tests
    app.state.accounts = accounts
    # Whether the BROWSER's hop is https. Not derived from the request: behind the
    # edge every request arrives as plain http on the docker bridge, and
    # X-Forwarded-Proto is a client claim until `--trust-proxy` exists to say whose
    # claim to believe. `serve --domain` knows the answer as a server-side fact --
    # rc-repro arranged the TLS itself -- so it passes it in. Decides the cookie's
    # `Secure` and `__Host-` prefix, so guessing here would be guessing about a
    # security attribute.
    app.state.public_https = public_https

    # Host allow-list (DNS-rebind/CSRF guard). Loopback always allowed; extra
    # hosts (e.g. a reverse-proxy domain like *.iximiuz.com) opt in via
    # --allow-host, and "*" trusts any Host.
    # Case-folded on both sides: hostnames are case-insensitive, and comparing
    # raw meant `curl http://LOCALHOST:7070/` and a proxy that forwards
    # `Host: Lab.Example.Com` were both rejected as "host not allowed".
    allowed = {h.lower() for h in ({"localhost", "127.0.0.1", "::1"} | set(allow_hosts or []))}
    any_host = "*" in (allow_hosts or [])

    def _hostname(hdr: str | None) -> str:
        """A Host header's bare hostname: port stripped, lowercased.

        Handles bracketed IPv6 ("[::1]:7070" -> "::1"), which a plain
        split(":") would mangle to "[" — making the "::1" entry unmatchable.
        """
        h = (hdr or "").strip()
        if h.startswith("["):
            return h[1:].split("]", 1)[0].lower()
        return h.split(":", 1)[0].lower()

    def host_ok(hdr: str | None) -> bool:
        # A missing/empty Host is rejected rather than allowed: "" must not be a
        # member of the allow-list, or any Host-less request slips past it.
        return any_host or _hostname(hdr) in allowed
    app.state.host_ok = host_ok

    def _cross_site(headers, *, require_origin: bool = False) -> bool:
        """Whether this request was made from another site.

        ONE implementation, because the duplication was the defect. `guard` had
        this check and the WebSocket handler re-implemented the Host and
        credential checks around it while omitting this one -- and a WebSocket
        handshake is exempt from CORS, so the browser makes it cross-origin and
        attaches the cached credential itself. Confirmed before this existed: a
        forged `Origin: https://evil.example` was refused 403 on a POST and
        ACCEPTED on `/api/repros/{name}/logs/stream`, which then streamed the
        workspace's container log -- where LDAP bind passwords and OAuth client
        secrets live.

        `require_origin` is for the WebSocket handshake alone. There an absent
        `Origin` cannot be read as "same-origin", because the browser always
        sends one on a WS upgrade; anything without it is a non-browser client.
        It must NOT be set for SSE: a same-origin `EventSource` sends no `Origin`
        at all, so requiring one would refuse the SPA's own job stream. SSE is
        covered instead by being subject to CORS, plus `Sec-Fetch-Site` below.
        """
        site = headers.get("sec-fetch-site", "")
        origin = headers.get("origin", "")
        if site not in ("", "same-origin", "none"):
            return True
        if origin:
            return not host_ok(origin.split("://", 1)[-1])
        return require_origin
    app.state.cross_site = _cross_site

    # Defence in depth for the SPA. script-src 'self' blocks inline handlers, so an
    # injected `<img onerror=...>` cannot run even if a renderer forgets to escape.
    # Styles need 'unsafe-inline' because the UI sets style ATTRIBUTES; frame-src
    # allows the monitoring stack's Grafana, which the k6 result embeds.
    csp = ("default-src 'self'; "
           "script-src 'self'; "
           "style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data:; "
           "connect-src 'self' ws: wss:; "
           f"frame-src 'self' http://localhost:{config.MONITOR_PORTS[1]}; "
           # 'self', not 'none': /signin is a real <form method="post">, and the
           # whole point of it is being a page rather than the browser's own
           # credential dialog. 'self' still blocks posting this page's fields
           # anywhere off-origin.
           "base-uri 'none'; form-action 'self'; object-src 'none'")

    # `__Host-` is not decoration. team-server.md §3.6 puts workspaces on sibling
    # names (t1234.support.example.com) beside the GUI (support.example.com), and
    # §8 says every workspace runs admin/admin123 -- so without the prefix a
    # workspace can set a Domain-scoped cookie of the same name that the GUI cannot
    # tell from its own. The prefix requires Secure, which http://localhost cannot
    # have, so there are two names and exactly ONE is live per scheme: honouring
    # the plain name over https would hand back everything the prefix bought.
    COOKIE_SECURE = "__Host-rc_repro_session"
    COOKIE_PLAIN = "rc_repro_session"

    def cookie_name() -> str:
        return COOKIE_SECURE if app.state.public_https else COOKIE_PLAIN

    def _matched_template(scope) -> str:
        """The route TEMPLATE this request will dispatch to, e.g.
        "/api/repros/{name}".

        Resolved by asking Starlette's own matcher rather than by keeping a second
        regex table beside ROUTE_ROLES: two independent ways of deciding which
        route a path belongs to is precisely the drift the table exists to stop.
        Needed because middleware runs BEFORE the router, so scope["route"] does
        not exist yet.
        """
        for route in app.router.routes:
            if route.matches(scope)[0] is Match.FULL:
                return getattr(route, "path", "")
        return ""

    def _session_token(headers_cookies) -> str:
        return headers_cookies.get(cookie_name(), "")

    def _set_session_cookie(response, token: str) -> None:
        secure = bool(app.state.public_https)
        response.set_cookie(
            cookie_name(), token,
            max_age=sessions.ABSOLUTE_SECONDS, path="/", httponly=True,
            secure=secure,
            # Lax, not Strict: pasting a workspace link into chat and following it
            # is the support workflow, and Strict blanks the first load. Lax still
            # withholds the cookie from cross-site POSTs and from subresource
            # requests, which includes the WebSocket and EventSource handshakes.
            samesite="lax")

    def _clear_session_cookie(response) -> None:
        # Both names, so switching a box between http and https cannot strand a
        # cookie the new scheme will not overwrite.
        for name in (COOKIE_SECURE, COOKIE_PLAIN):
            response.delete_cookie(name, path="/")

    # --- security: Host allow-list, then Basic Auth or the session token
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not host_ok(request.headers.get("host")):
            return JSONResponse({"error": "host not allowed (use serve --allow-host)"}, status_code=403)
        path = request.url.path
        # Cross-site request. In TOKEN mode the unguessable ?t= doubles as a CSRF
        # token, but a Basic credential is attached by the BROWSER on every
        # request and `Host:` is whatever this server answers to -- so the host
        # allow-list waves a forged request straight through, and Basic is not a
        # cookie so SameSite never applies. Confirmed reachable: a body-less POST
        # to /upgrade/rollback (which drops the database) from any origin.
        #
        # Every /api/ path, not just the state-changing methods it used to cover.
        # A cross-site READ of /api/repros/{name}/logs hands over LDAP bind
        # passwords and OAuth secrets, so it was never the milder case. The SPA
        # itself is same-origin; non-browser clients (curl, CI) send neither
        # header and are unaffected, so nothing scripted changes.
        if request.method not in ("GET", "HEAD", "OPTIONS") or path.startswith("/api/"):
            if _cross_site(request.headers):
                return JSONResponse(
                    {"error": "cross-site request refused", "kind": "Forbidden"},
                    status_code=403)
        actor = ""
        if app.state.accounts:
            # Everything is behind the login, not just /api/ -- the SPA itself
            # should not render for someone who cannot use it. The open set is
            # exactly what a signed-OUT browser needs to render the sign-in page,
            # plus the uptime check:
            #   /signin, /signout  the login itself
            #   /api/session       its JSON twin -- signing in cannot require
            #                      being signed in, and GET answers {"user": ""}
            #                      for a caller who is not
            #   /app.css           or the sign-in page is unstyled
            #   /api/health        so a monitor needs no credential
            template = _matched_template(request.scope)
            need = route_requirement(request.method, template)
            # `""` in the table means "reachable with no session". /app.css is
            # open too, or the sign-in page is an unstyled wall of text.
            open_path = need == _OPEN or path == "/app.css"
            sess = sessions.verify(request.cookies.get(cookie_name(), ""))
            actor = sess.user if sess else ""
            if actor and path.startswith("/api/") and not open_path:
                # DEFAULT DENY. `need is None` means the route is not in the
                # table at all -- a new endpoint that nobody registered. Denying
                # is the only safe reading, and the message says which case it is
                # so it is a five-second fix rather than a mystery.
                from rc_repro.services import users as usersvc
                role = usersvc.role_of(actor)      # live, never from the session
                if need is None:
                    auditsvc.audit(actor, "denied", f"{request.method} {template} "
                                   "(no role declared)", origin_="session",
                                   outcome="denied")
                    return JSONResponse(
                        {"error": f"{request.method} {template} declares no minimum "
                                  "role, so it is refused (see ROUTE_ROLES)",
                         "kind": "Forbidden"}, status_code=403)
                if not usersvc.at_least(role, need):
                    # Recorded, because "is readonly drawn in the right place?" is
                    # an open question in the design and `grep denied` is the
                    # evidence to settle it with -- opinion is not.
                    auditsvc.audit(actor, "denied",
                                   f"{request.method} {template} needs {need}",
                                   origin_="session", outcome="denied")
                    return JSONResponse(
                        {"error": f"this needs the {need!r} role; you are {role!r}",
                         "kind": "Forbidden"}, status_code=403)
            if not open_path and not actor:
                # An API caller gets a machine-readable 401 it can act on; a
                # BROWSER gets sent to the page that fixes the problem, carrying
                # where it was going. Answering an HTML navigation with JSON is
                # how the old design ended up with no login screen at all.
                if path.startswith("/api/"):
                    return JSONResponse(
                        {"error": "sign in to use rc-repro", "kind": "Unauthorized"},
                        status_code=401)
                nxt = request.url.path
                if request.url.query:
                    nxt += "?" + request.url.query
                return RedirectResponse(
                    f"/signin?e=required&next={quote(nxt, safe='/?=&')}",
                    status_code=303)
        elif token and path.startswith("/api/") and path != "/api/health":
            given = request.headers.get("x-rc-repro-token") or request.query_params.get("t")
            if given != token:
                return JSONResponse({"error": "bad or missing token"}, status_code=401)
        # Who did this, for job attribution. "" when the token is in use, because a
        # shared secret genuinely cannot say. A contextvar rather than fifteen
        # extra handler parameters; it propagates into the threadpool where every
        # `def` handler runs.
        request.state.actor = actor
        jobs_mod.CURRENT_ACTOR.set(actor)
        # How that identity was established, so the log can say which of its lines
        # are evidence. "session" only when a real credential was checked.
        auditsvc.set_origin("session" if actor else "")
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # StaticFiles sends ETag + Last-Modified but no Cache-Control, and with no
        # freshness directive a browser is free to reuse a cached copy WITHOUT
        # revalidating (heuristic freshness, RFC 9111 4.2.2). After upgrading
        # rc-repro that means the old app.js/app.css keep being used and the new UI
        # simply is not there -- indistinguishable from a missing feature, and only
        # fixable with a hard refresh nobody should have to know about.
        # `no-cache` means "revalidate first", not "don't store": the ETag still
        # answers 304, so this costs a conditional request, not a re-download.
        if not path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        # The session token rides in ?t= (EventSource/WebSocket cannot set
        # headers), so suppress the Referer that would carry it off-origin.
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.exception_handler(ReproError)
    async def _repro_error(_: Request, exc: ReproError):
        return JSONResponse({"error": str(exc), "kind": type(exc).__name__},
                            status_code=exc.http_status)

    # --- the session: sign in, sign out, who am I -----------------------------
    def _client(request: Request) -> str:
        return request.client.host if request.client else ""

    def _do_signin(user: str, password: str, source: str, agent: str):
        """Shared by the form post and the JSON twin. Returns (token, error).

        scrypt runs HERE and nowhere else, which is what makes a guessing bound
        possible at all: one endpoint to throttle, instead of every door in the
        app re-deriving whatever Authorization header it was handed.
        """
        from rc_repro.services import users as usersvc

        if not usersvc.any_users():
            return "", "nouser"
        if _signin_retry_after(source):
            return "", "rate"
        if not usersvc.verify(user, password, source=source):
            _signin_failed(source)
            return "", "bad"
        _signin_ok(source)
        return sessions.create(user, label=sessions.describe_agent(agent)), ""

    @app.get("/signin")
    def signin_form(request: Request, e: str = "", next: str = "/"):
        # Already signed in? Nobody needs to see this page. Straight through to
        # where they were going, so a stale /signin bookmark is not a dead end.
        if sessions.verify(request.cookies.get(cookie_name(), "")):
            return RedirectResponse(signin_page.safe_next(next), status_code=303)
        # The "not encrypted" warning is about the password crossing a NETWORK.
        # On a loopback connection it does not, so showing it there is a false
        # alarm on the most common install -- and a red banner that is wrong on a
        # laptop is how people learn to ignore red banners.
        local = _client(request) in ("127.0.0.1", "::1", "localhost")
        html = signin_page.page(
            error=e, next_url=next,
            server=(request.headers.get("host") or "").split(":")[0],
            secure=bool(app.state.public_https) or local,
            retry_after=_signin_retry_after(_client(request)))
        return HTMLResponse(html, status_code=401 if e in ("bad", "rate") else 200)

    @app.post("/signin")
    def signin_submit(request: Request, user: str = Form(""),
                      password: str = Form(""), next: str = Form("/")):
        source = _client(request)
        token, err = _do_signin(user, password, source,
                                request.headers.get("user-agent", ""))
        target = signin_page.safe_next(next)
        if err:
            # The name is deliberately NOT echoed back into the form. It would be
            # reflected input on an unauthenticated page, and the browser's own
            # autofill restores it anyway.
            return RedirectResponse(f"/signin?e={err}&next={quote(target, safe='/?=&')}",
                                    status_code=303)
        # 303 so the POST is not in history and a refresh cannot resubmit it.
        resp = RedirectResponse(target, status_code=303)
        _set_session_cookie(resp, token)
        return resp

    @app.post("/api/session")
    def session_create(request: Request, body: dict = Body(default={})):
        """The JSON twin of POST /signin, for scripts and for the SPA."""
        source = _client(request)
        token, err = _do_signin(str(body.get("user") or ""),
                                str(body.get("password") or ""), source,
                                request.headers.get("user-agent", ""))
        if err:
            detail = {"bad": "that name or password is not right",
                      "rate": "too many attempts from this address",
                      "nouser": "there are no accounts on this server yet"}[err]
            return JSONResponse({"error": detail, "kind": "Unauthorized"},
                                status_code=429 if err == "rate" else 401)
        sess = sessions.verify(token)
        resp = JSONResponse({"user": sess.user, "expires_at": sess.expires_at})
        _set_session_cookie(resp, token)
        return resp

    @app.get("/api/session")
    def session_whoami(request: Request):
        """Who am I. Replaces the `actor` field bolted onto /api/health, which
        only existed because there was nowhere else to ask."""
        from rc_repro.services import users as usersvc
        sess = sessions.verify(request.cookies.get(cookie_name(), ""))
        if not sess:
            return {"user": "", "role": "", "accounts": bool(app.state.accounts)}
        return {"user": sess.user, "role": usersvc.role_of(sess.user),
                "accounts": True, "origin": sess.origin,
                "expires_at": sess.expires_at, "label": sess.label}

    @app.delete("/api/session")
    def session_end(request: Request):
        """Sign out — a logout that actually ends something on the server."""
        raw = request.cookies.get(cookie_name(), "")
        ended = sessions.revoke(raw) if raw else False
        resp = JSONResponse({"ok": True, "ended": ended})
        _clear_session_cookie(resp)
        return resp

    @app.post("/signout")
    def signout_form(request: Request):
        raw = request.cookies.get(cookie_name(), "")
        if raw:
            sessions.revoke(raw)
        # No goodbye page: what you want next is to walk away or to sign back in,
        # and /signin?e=signedout is both, with the name field focused.
        resp = RedirectResponse("/signin?e=signedout", status_code=303)
        _clear_session_cookie(resp)
        return resp

    @app.get("/api/sessions")
    def sessions_list(request: Request):
        """Your own live sessions. The answer to "I signed in on the customer's
        laptop", which no cookie attribute gives you and which only exists
        because sessions are server-side."""
        sess = sessions.verify(request.cookies.get(cookie_name(), ""))
        if not sess:
            return {"sessions": []}
        here = sess.sid[:8]
        return {"sessions": [{**s.public(), "current": s.sid[:8] == here}
                             for s in sessions.list_for(sess.user)]}

    @app.delete("/api/sessions")
    def sessions_revoke(request: Request, all: bool = False, sid: str = ""):
        sess = sessions.verify(request.cookies.get(cookie_name(), ""))
        if not sess:
            return JSONResponse({"error": "not signed in", "kind": "Unauthorized"},
                                status_code=401)
        if all:
            n = sessions.revoke_user(sess.user)
            resp = JSONResponse({"ok": True, "ended": n})
            _clear_session_cookie(resp)
            return resp
        # By sid PREFIX, because that is all the listing hands out -- the full
        # value is a verifier and a page that shows it gives away every session
        # it lists. Scoped to your own sessions, so a prefix collision with
        # somebody else's cannot revoke theirs.
        target = next((s for s in sessions.list_for(sess.user)
                       if s.sid[:8] == sid), None)
        if not target:
            return JSONResponse({"error": "no such session", "kind": "NotFoundError"},
                                status_code=404)
        sessions.revoke_sid(target.sid)
        return {"ok": True, "ended": 1}

    # --- people (admin) -------------------------------------------------------
    #: Long enough that guessing is hopeless, short enough to read aloud once.
    _NEW_PASSWORD_BYTES = 12

    @app.get("/api/users")
    def users_list():
        from rc_repro.services import users as usersvc
        rows = []
        for u in usersvc.list_users():
            live = sessions.list_for(u.name)
            rows.append({"name": u.name, "role": usersvc.role_of(u.name),
                         "implicit": not u.role, "created_at": u.created_at,
                         "sessions": len(live),
                         "last_seen": max((s.last_seen for s in live), default=0)})
        return {"users": rows, "roles": list(usersvc.ROLES),
                "implicit_admins": usersvc.implicit_admins()}

    @app.post("/api/users")
    def users_add(body: dict = Body(...)):
        """Create an account. The SERVER mints the password and returns it once.

        An admin who types a colleague's password also knows it, which makes every
        audit line signed with that name deniable. Generating it means the admin
        can reset the credential but never hold it.
        """
        from rc_repro.services import users as usersvc
        name = str(body.get("name") or "").strip().lower()
        role = str(body.get("role") or "member")
        usersvc.require_valid_name(name)
        usersvc.require_valid_role(role)
        password = secrets.token_urlsafe(_NEW_PASSWORD_BYTES)
        usersvc.add(name, password, role=usersvc.normalise_role(role))
        return {"name": name, "role": usersvc.normalise_role(role),
                "password": password,
                "note": "shown once; rc-repro does not store it in readable form"}

    @app.post("/api/users/{name}/role")
    def users_role(name: str, body: dict = Body(...)):
        from rc_repro.services import users as usersvc
        role = str(body.get("role") or "")
        u = usersvc.set_role(name, role)
        # A demotion must reach the sessions the user already holds, or it takes
        # effect only when they next sign in -- and the person being demoted is
        # the least likely to sign out. (role_of is read live per request, so this
        # is belt and braces; the sign-out is what makes it visible to them.)
        ended = sessions.revoke_user(name)
        return {"name": u.name, "role": u.role, "sessions_ended": ended}

    @app.post("/api/users/{name}/password")
    def users_reset_password(name: str):
        """Reset somebody's password to a freshly minted one, shown once."""
        from rc_repro.services import users as usersvc
        password = secrets.token_urlsafe(_NEW_PASSWORD_BYTES)
        usersvc.set_password(name, password)
        ended = sessions.revoke_user(name)
        return {"name": name, "password": password, "sessions_ended": ended}

    @app.delete("/api/users/{name}")
    def users_remove(name: str):
        from rc_repro.services import users as usersvc
        usersvc.remove(name)                 # refuses the last admin
        ended = sessions.revoke_user(name)
        return {"name": name, "removed": True, "sessions_ended": ended}

    @app.post("/api/me/password")
    def me_password(request: Request, body: dict = Body(...)):
        """Change your OWN password. Requires the current one.

        Every other session you hold ends; this one survives, so changing your
        password does not sign you out of the tab you changed it in.
        """
        from rc_repro.services import users as usersvc
        me = getattr(request.state, "actor", "") or ""
        old, new = str(body.get("old") or ""), str(body.get("new") or "")
        if not usersvc.verify(me, old, source=_client(request)):
            raise ValidationError("that is not your current password")
        usersvc.require_valid_password(new)
        usersvc.set_password(me, new)
        keep = request.cookies.get(cookie_name(), "")
        current = sessions.verify(keep)
        ended = sessions.revoke_user(me)
        token = sessions.create(me, label=current.label if current else "",
                                origin="session") if current else ""
        auditsvc.record("me-passwd", me)
        resp = JSONResponse({"ok": True, "sessions_ended": max(0, ended - 1)})
        if token:
            _set_session_cookie(resp, token)
        return resp

    @app.get("/api/audit")
    def audit_read(request: Request, limit: int = 200, actor: str = "",
                   kind: str = "", q: str = "", since: str = ""):
        """The activity trail. Until now it was written and never read.

        An admin sees every line. A member or readonly sees only their OWN,
        forced server-side -- self-audit rather than admin-only, because "wait,
        did I do that?" is a legitimate question and answering it with a 403
        teaches people the log is not for them.
        """
        from rc_repro.services import users as usersvc
        me = getattr(request.state, "actor", "") or ""
        if me and not usersvc.at_least(usersvc.role_of(me), "admin"):
            actor = me                       # not a filter they can widen
        out = auditsvc.read(limit=max(1, min(int(limit), 1000)),
                            actor_name=actor, kind=kind, q=q, since=since)
        out["scope"] = "all" if actor == "" else actor
        return out

    # --- read (blocking -> def -> threadpool) ---------------------------------
    @app.get("/api/health")
    def health(request: Request):
        # Deliberately says nothing about identity any more. `actor` lived here
        # only because there was nowhere else to ask who was signed in; that is
        # `GET /api/session` now, and an endpoint left open for uptime checks
        # should not be reading credentials at all.
        return {"ok": True, "docker": runner.docker_available()}

    @app.get("/api/repros")
    def list_repros():
        return {"repros": lc.list_repros()}

    @app.get("/api/edge")
    def edge_status():
        """The shared Traefik, and whether each name it serves is actually
        reachable.

        Its own endpoint rather than a field on /api/health: this shells out to
        docker twice and health is the cheap, unauthenticated one that every tab
        polls every four seconds.
        """
        from rc_repro.services import edge as edgesvc

        st = edgesvc.status()
        attached = set(st.pop("attached", []))
        st["domain"] = edgesvc.served_domain()
        st["routes"] = [
            {"name": n,
             "host": _route_host(edgesvc, n),
             # A route the edge cannot reach answers 502 rather than erroring,
             # which is the one failure nothing else in the UI would surface.
             "reachable": edgesvc.workspace_network(n) in attached}
            for n in st.get("routes", [])
        ]
        return st

    @app.get("/api/doctor")
    def doctor():
        """The same preflight checks as `rc-repro doctor`.

        The dashboard's docker badge could only say up/down; when it said down,
        every card reported "docker unavailable — actions disabled" and offered no
        diagnosis. This is the click-through.
        """
        from rc_repro.services import doctor as doctorsvc
        return doctorsvc.run_checks()

    @app.get("/api/repros/{name}")
    def describe(name: str):
        return lc.describe(name)

    @app.get("/api/settings")
    def settings():
        """Remembered settings the create dialog needs to behave correctly.

        Only whether a Let's Encrypt contact email exists, never the address: the
        form's email field said "leave blank, it is remembered", which is true for
        someone who ran `rc-repro config set acme.email` and false for everyone
        else -- and the GUI has no way to set it. A blank field then produced a job
        that failed on a required value the form had called optional.
        """
        return {"acme_email_remembered": bool(config.load_config().get("acme_email"))}

    @app.get("/api/presets")
    def list_presets():
        return {"presets": [
            {"name": p.name, "description": p.description, "params_help": p.params_help,
             "requires_license": p.requires_license} for p in presets_mod.list_presets()]}

    @app.get("/api/versions/{version}")
    def resolve_version(version: str, offline: bool = False):
        """Resolve an RC version to its MongoDB pairing WITHOUT launching anything.

        Lets the create dialog show the pairing before the user commits to a
        multi-gigabyte image pull, and pre-empts the trap that otherwise only
        surfaces minutes in as a mongod crash.
        """
        from rc_repro import versions as versions_mod
        try:
            r = versions_mod.resolve(version, offline=offline)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        out = {"rc_version": r.rc_version, "rc_image": r.rc_image, "mongo_tag": r.mongo_tag,
               "mongo_flavor": r.mongo_flavor, "mongo_shell": r.mongo_shell,
               "oplog": r.oplog, "source": r.source, "note": r.note, "kernel": ""}
        # SERVER-121912: mongod 8.0 hard-exits on kernel >= 6.19, and the failure
        # reads like a volume/permission problem. Say so before the pull, not after.
        kv = runner.docker_kernel_version()
        out["kernel"] = kv or ""
        mm = re.match(r"(\d+)\.(\d+)", kv or "")
        try:
            mongo_major = int(r.mongo_tag.split(".")[0])
        except ValueError:
            mongo_major = 0
        if mm and mongo_major >= 8 and (int(mm.group(1)), int(mm.group(2))) >= (6, 19):
            out["warning"] = (f"this engine's kernel ({kv}) cannot run MongoDB 8.0 "
                              "(SERVER-121912) — mongod will exit on boot")
        return out

    @app.get("/api/repros/{name}/detail")
    def detail(name: str):
        return lc.detail(name)

    @app.get("/api/repros/{name}/stats")
    def stats(name: str):
        from rc_repro.perf import resources as R
        target = lc.resolve_name(name)
        ids = runner.container_ids(target)
        prefix = f"{config.PROJECT_PREFIX}{target}-"
        cpu = mem = 0.0
        for line in runner.docker_stats(ids).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            # Strip the `rcrepro-<name>-` prefix before matching. A substring test
            # against the full container name meant a repro NAMED e.g.
            # "rocketchat-slow" matched every container, silently summing Mongo and
            # every sidecar into the RC chart.
            svc = parts[0][len(prefix):] if parts[0].startswith(prefix) else parts[0]
            if svc == "rocketchat" or svc.startswith("rocketchat-"):
                cpu += R._parse_cpu(parts[1])
                used, _ = R._parse_mem(parts[2])
                mem += used
        return {"cpu": round(cpu, 1), "mem_mb": round(mem / 1e6, 1)}

    @app.websocket("/api/repros/{name}/logs/stream")
    async def logs_stream(ws: WebSocket, name: str, tail: int = 300):
        # WS bypasses the http middleware, so the same checks run here -- and the
        # cross-site one now runs from the SAME helper `guard` uses, because
        # having a second, shorter copy of this list is exactly how the Origin
        # check went missing here while being present three lines away.
        if not app.state.host_ok(ws.headers.get("host")):
            await ws.close(code=1008); return
        # Before accept(), and before any credential is read. `require_origin` is
        # on only when the credential is ambient: a Basic header is attached by
        # the browser itself, so an absent Origin cannot be trusted. In token mode
        # the ?t= value is the proof and no browser can supply it cross-site, so a
        # header-less client (curl, CI, the tests) still connects.
        if _cross_site(ws.headers, require_origin=app.state.accounts):
            await ws.close(code=1008); return
        if app.state.accounts:
            # The cookie rides along on the upgrade automatically, which is what
            # finally takes the credential out of the query string: `?t=` was only
            # ever there because a browser cannot set headers on a WebSocket.
            sess = sessions.verify(ws.cookies.get(cookie_name(), ""))
            if not sess:
                await ws.close(code=1008); return
            # The role table is consulted here too: the middleware never sees a
            # WebSocket, so a check that lives only there does not cover the one
            # endpoint that streams credentials.
            from rc_repro.services import users as usersvc
            need = route_requirement("WS", "/api/repros/{name}/logs/stream")
            if need is None or not usersvc.at_least(usersvc.role_of(sess.user), need):
                auditsvc.audit(sess.user, "logs-open", name,
                               origin_="session", outcome="denied")
                await ws.close(code=1008); return
            # This handler never passes through `guard`, so it publishes its own
            # identity or the logs-open line below is written with no actor.
            auditsvc.set_actor(sess.user)
            auditsvc.set_origin("session")
        elif token and ws.query_params.get("t") != token:
            await ws.close(code=1008); return
        await ws.accept()
        try:
            target = lc.resolve_name(name)
            # Once per stream, never per line: these logs carry LDAP bind
            # passwords and OAuth client secrets, so who opened one is worth
            # recording -- and a line per log line would be a denial of service
            # against the log itself.
            auditsvc.record("logs-open", target)
        except ReproError as exc:
            await ws.send_json({"error": str(exc)}); await ws.close(); return

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=WS_QUEUE_MAX)
        proc = subprocess.Popen(
            ["docker", "compose", "logs", "-f", "--no-color",
             "--tail", str(_clamp_tail(tail))],
            cwd=runner.workspace(target), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)

        def offer(line: str | None) -> None:
            """Enqueue on the loop thread, dropping lines when the reader falls
            behind — but never dropping the end-of-stream sentinel."""
            if line is not None:
                try:
                    q.put_nowait(line)
                except asyncio.QueueFull:
                    pass
                return
            while True:
                try:
                    q.put_nowait(None)
                    return
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        return

        def pump():
            for line in proc.stdout or []:
                loop.call_soon_threadsafe(offer, line.rstrip("\n"))
            loop.call_soon_threadsafe(offer, None)

        threading.Thread(target=pump, daemon=True).start()

        async def watch_client() -> None:
            """Completes when the client goes away.

            Without it the handler blocks on q.get() forever for a QUIET
            container: the send that would raise never happens, so the
            `docker compose logs -f` child and the pump thread outlive the
            browser tab indefinitely.
            """
            try:
                while True:
                    await ws.receive()
            except Exception:  # noqa: BLE001 - a disconnect of any flavour
                return

        watcher = asyncio.create_task(watch_client())
        try:
            while True:
                getter = asyncio.ensure_future(q.get())
                done, _pending = await asyncio.wait(
                    {getter, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    getter.cancel()
                    break
                line = getter.result()
                if line is None:
                    break
                await ws.send_text(line)
        except WebSocketDisconnect:
            pass
        finally:
            watcher.cancel()
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    @app.get("/api/repros/{name}/logs")
    def logs(name: str, tail: int = 200):
        target = lc.resolve_name(name)
        lines: list[str] = []
        runner.compose_stream(target, "logs", "--no-color",
                              "--tail", str(_clamp_tail(tail)),
                              on_line=lines.append)
        return {"name": target, "logs": "\n".join(lines)}

    # --- mutating ------------------------------------------------------------
    @app.post("/api/repros")
    def create(request: Request, req: dict = Body(...)):
        allowed = set(lc.CreateReq.__dataclass_fields__) - {"actor"}
        fields = {k: v for k, v in req.items() if k in allowed}
        # From the session, never the body: a caller must not be able to create a
        # workspace in somebody else's namespace by asking.
        fields["actor"] = getattr(request.state, "actor", "") or ""
        # `version` is CreateReq's only required field, so omitting it raised a
        # TypeError from the constructor -- an opaque 500 for any caller using the
        # documented HTTP API rather than the GUI (which always sends the key).
        if not str(fields.get("version") or "").strip():
            raise ValidationError("`version` is required, e.g. {\"version\": \"8.5.1\"}")
        # Four fields decide what CODE runs and where it listens, which is a
        # different question from "make me a workspace". `rc_image` runs an
        # arbitrary image as the serve user; `bind` can publish a workspace with
        # fixed admin/admin123 credentials to the whole network. The GUI never
        # sends them for a member -- it sends the resolved image for the version.
        privileged = [k for k in ("rc_image", "reg_token", "bind", "port")
                      if fields.get(k)]
        if privileged:
            from rc_repro.services import users as usersvc
            who = getattr(request.state, "actor", "") or ""
            if who and not usersvc.at_least(usersvc.role_of(who), "admin"):
                raise ValidationError(
                    f"{', '.join(privileged)} may only be set by an admin — they "
                    "choose the image and the interface, not just the workspace")
        creq = lc.CreateReq(**fields)
        job = jobs.submit("create", lc.create_repro, creq, stream_output=True,
                          label=creq.name or creq.version)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/state")
    def state(name: str, body: dict = Body(...)):
        lc.set_state(name, body.get("action", ""))
        return {"ok": True}

    @app.post("/api/repros/{name}/up")
    def bring_up(name: str):
        """Recreate a `down`ed repro's containers from its stored metadata.

        `docker compose start` cannot revive a repro that was `down`ed — there are
        no containers left to start — so /state is useless for it. This is the
        GUI's equivalent of the CLI's `up --version <same> --name <same>`: the
        workspace and repro.json survive a `down`, so nothing needs re-entering.
        offline=True because the stored version needs no fresh lookup.
        """
        target = lc.resolve_name(name)
        meta = runner.read_meta(target)
        req = lc.CreateReq(version=meta.rc_version, preset=meta.preset,
                           name=target, wait=True, offline=True)
        job = jobs.submit("up", lc.create_repro, req, stream_output=True, label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/ready")
    def ready(name: str):
        meta = runner.read_meta(lc.resolve_name(name))
        job = jobs.submit("ready", lc.wait_and_finalize, meta, label=meta.name)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/seed")
    def seed(name: str, body: dict = Body(default={})):
        meta = runner.read_meta(lc.resolve_name(name))
        job = jobs.submit("seed", lc.run_seed_inline, meta,
                          body.get("profile", "small"), bool(body.get("stats", False)),
                          label=meta.name)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/scale")
    def scale(name: str, body: dict = Body(...)):
        target = lc.resolve_name(name)
        job = jobs.submit("scale", datasvc.run_scale, target, body.get("scale", ""), label=target)
        return {"job_id": job.id}

    @app.delete("/api/repros/{name}/scale")
    def clear_scale(name: str):
        target = lc.resolve_name(name)
        job = jobs.submit("clear-scale", datasvc.clear_scale, target, label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/config-import/plan")
    def config_import_plan(name: str, file: UploadFile = File(...), only: str = Form("")):
        target = lc.resolve_name(name)
        dest = runner.workspace(target) / "import"
        dest.mkdir(parents=True, exist_ok=True)
        # One file per upload, not a single shared settings.json: two tabs
        # previewing different dumps raced, and the second silently won the
        # first's apply.
        upload_id = "u" + uuid.uuid4().hex[:12]
        (dest / f"{upload_id}.json").write_bytes(_read_upload(file))
        # Pin the filter TO the upload. `apply` used to re-read `only` from its own
        # request, so editing the field after previewing silently applied a different
        # plan than the one that was reviewed — which defeats having a preview.
        (dest / f"{upload_id}.only").write_text(only, encoding="utf-8")
        _prune_uploads(dest)
        onlyset = _only_set(only)
        plan = datasvc.import_plan(target, str(dest / f"{upload_id}.json"), only=onlyset)
        plan["upload_id"] = upload_id
        return plan

    def _import_then_delete(target: str, path: str, onlyset, emit) -> dict:
        """Apply the plan, then remove the uploaded dump — it is a customer's
        configuration and was previously left in the workspace forever."""
        try:
            return datasvc.import_apply(target, path, onlyset, emit=emit)
        finally:
            Path(path).unlink(missing_ok=True)
            Path(path).with_suffix(".only").unlink(missing_ok=True)

    @app.post("/api/repros/{name}/config-import")
    def config_import_apply(name: str, body: dict = Body(default={})):
        target = lc.resolve_name(name)
        upload_id = str(body.get("upload_id") or "")
        # Pattern-checked before it becomes a filename.
        if not _UPLOAD_ID_RE.match(upload_id):
            return JSONResponse({"error": "missing or malformed upload_id - preview the plan first",
                                 "kind": "ValidationError"}, status_code=400)
        path = runner.workspace(target) / "import" / f"{upload_id}.json"
        if not path.exists():
            return JSONResponse({"error": "no uploaded settings.json - preview the plan first",
                                 "kind": "ValidationError"}, status_code=400)
        # The filter is whatever the PREVIEW used, not whatever this request says:
        # what was reviewed is what gets applied. `body["only"]` is ignored.
        onlyfile = path.with_suffix(".only")
        onlyset = _only_set(onlyfile.read_text(encoding="utf-8") if onlyfile.exists() else "")
        job = jobs.submit("config-import", _import_then_delete, target, str(path), onlyset,
                          label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/loadtest")
    def loadtest(name: str, body: dict = Body(default={})):
        from rc_repro.services import perf as perfsvc
        target = lc.resolve_name(name)
        fields = set(perfsvc.LoadtestReq.__dataclass_fields__) - {"name"}
        req = perfsvc.LoadtestReq(name=target, **{k: v for k, v in body.items() if k in fields})
        job = jobs.submit("loadtest", perfsvc.run_loadtest, req, label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/capacity")
    def capacity(name: str, body: dict = Body(default={})):
        from rc_repro.services import perf as perfsvc
        target = lc.resolve_name(name)
        fields = set(perfsvc.CapacityReq.__dataclass_fields__) - {"name"}
        req = perfsvc.CapacityReq(name=target, **{k: v for k, v in body.items() if k in fields})
        job = jobs.submit("capacity", perfsvc.run_capacity, req, label=target)
        return {"job_id": job.id}

    @app.post("/api/benchmark")
    def benchmark(body: dict = Body(...)):
        from rc_repro.services import perf as perfsvc
        vers = body.get("versions") or []
        if isinstance(vers, str):
            vers = [v.strip() for v in vers.split(",") if v.strip()]
        # A non-list, or a list of non-strings, used to reach `", ".join(vers)` in
        # the label below and TypeError there -- a 500 before the job even started.
        if not isinstance(vers, list) or not all(isinstance(v, str) for v in vers):
            raise ValidationError(
                "`versions` must be a list of version strings, or a comma-separated string")
        vers = [v.strip() for v in vers if v.strip()]
        if not vers:
            raise ValidationError("no versions given, e.g. {\"versions\": \"8.4.2,8.5.1\"}")
        job = jobs.submit("benchmark", perfsvc.run_benchmark, vers,
                          body.get("seed_profile", "standard"),
                          bool(body.get("offline", False)), bool(body.get("no_pull", False)),
                          label=", ".join(vers))
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/monitor")
    def monitor(name: str, off: bool = False):
        from rc_repro.services import monitor as monitorsvc
        target = lc.resolve_name(name)
        job = jobs.submit("monitor-off" if off else "monitor",
                          monitorsvc.detach if off else monitorsvc.attach, target,
                          label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/default")
    def set_default(name: str):
        """Make this repro the default (the CLI's `rc-repro use`).

        The dashboard displayed the `default` pill but had no way to move it --
        the create dialog's Pin checkbox was the only path, so changing it meant
        recreating a repro or dropping to the CLI.
        """
        target = lc.resolve_name(name)
        config.update_config(lambda cfg: cfg.__setitem__("default_repro", target))
        return {"ok": True, "default": target}

    @app.post("/api/repros/{name}/pat")
    def create_pat(name: str, body: dict = Body(default={})):
        """Mint a Personal Access Token and return ready-to-use API headers.

        Synchronous rather than a job: it is two HTTP calls against a repro that
        is already serving, and the caller wants the value back.
        """
        from rc_repro import rcapi
        lc.require_docker()
        meta = runner.read_meta(lc.resolve_name(name))
        label = str(body.get("label") or "rc-repro")
        bypass_2fa = bool(body.get("bypass_2fa", True))
        try:
            auth = lc.login(meta)
            # Not `token` -- that name is create_app's server auth token, and
            # rebinding it here reads like the handler is overwriting it.
            pat = rcapi.generate_pat(meta.root_url, auth, config.ADMIN_PASSWORD,
                                     token_name=label, bypass_2fa=bypass_2fa,
                                     workspace=meta.name)
        except Exception as exc:  # noqa: BLE001 - surface as a 409, not a 500
            raise NotReadyError(
                f"could not create a token (is it ready? `rc-repro ready -n {meta.name}`): {exc}"
            ) from exc
        if not pat:
            raise NotReadyError("Rocket.Chat did not return a token (is it ready?)")
        return {"token": pat, "user_id": auth.user_id, "label": label,
                "bypass_2fa": bypass_2fa, "root_url": meta.root_url}

    @app.get("/api/repros/{name}/env")
    def env_get(name: str):
        """The RC service's effective environment, with user overrides marked."""
        from rc_repro.services import envvars as envsvc
        return envsvc.current(name)

    @app.post("/api/repros/{name}/env")
    def env_set(name: str, body: dict = Body(...)):
        """Change env vars and recreate Rocket.Chat so they take effect.

        A job, not synchronous: recreating the container takes seconds to tens of
        seconds, and the GUI already knows how to stream a job's progress.
        """
        from rc_repro.services import envvars as envsvc
        target = lc.resolve_name(name)
        sets = body.get("set") or {}
        if not isinstance(sets, dict):
            raise ValidationError("`set` must be an object of KEY: VALUE")
        # Rocket.Chat SETTINGS come in separately and are prefixed here, not by the
        # caller: a bare setting id silently does nothing, so the rule belongs on
        # the server where every front-end gets it.
        settings = body.get("setting") or {}
        if not isinstance(settings, dict):
            raise ValidationError("`setting` must be an object of SettingId: VALUE")
        sets = {**sets, **envsvc.prefix_settings(settings)}
        unset = body.get("unset") or []
        if not isinstance(unset, list):
            raise ValidationError("`unset` must be a list of key names")
        job = jobs.submit("env", envsvc.set_env, target, sets, [str(u) for u in unset],
                          restart=bool(body.get("restart", True)), label=target)
        return {"job_id": job.id}

    # --- backup / restore / upgrade -------------------------------------------
    @app.post("/api/repros/{name}/backup")
    def backup_create(name: str, body: dict = Body(default={})):
        """Dump the repro's database into a bundle. A job: minutes on a seeded repro."""
        from rc_repro.services import backup as backupsvc
        target = lc.resolve_name(name)
        # `out` chooses a path on the SERVER. Over HTTP the caller is remote, so it
        # is confined to the managed backup directory: unconfined, this wrote a
        # tar.gz anywhere the server user could -- a systemd drop-in directory, a
        # webroot -- and combined with a forged cross-site request the attacker
        # chose the path too. The CLI keeps an unconfined --out on purpose: there
        # the caller IS the user, at their own shell.
        out = _confined_backup_out(str(body.get("out") or ""), backupsvc)
        job = jobs.submit("backup", backupsvc.create, target, label=target,
                          out=out,
                          note=str(body.get("label") or ""),
                          live=bool(body.get("live", False)))
        return {"job_id": job.id}

    @app.get("/api/backups")
    def backups_list(name: str = ""):
        from rc_repro.services import backup as backupsvc
        return {"backups": backupsvc.list_backups(name)}

    @app.delete("/api/backups")
    def backup_delete(path: str):
        from rc_repro.services import backup as backupsvc
        return backupsvc.delete(path)

    @app.post("/api/backups/compatibility")
    def backup_compatibility(body: dict = Body(...)):
        """Whether a bundle may be restored into a repro — asked BEFORE committing.

        The restore dialog enables its button from this, so a user learns that a
        downgrade is refused while choosing, not after starting a job.
        """
        from rc_repro.services import backup as backupsvc
        bundle = str(body.get("bundle") or "")
        if not bundle:
            raise ValidationError("no bundle given")
        manifest = backupsvc.read_manifest(bundle)
        target = str(body.get("name") or "")
        if not target:
            return {"manifest": manifest, "compatibility": None}
        meta = runner.read_meta(lc.resolve_name(target))
        return {"manifest": manifest,
                "compatibility": backupsvc.compatibility(manifest, meta)}

    @app.post("/api/restore")
    def restore(body: dict = Body(...)):
        from rc_repro.services import backup as backupsvc
        bundle = str(body.get("bundle") or "")
        if not bundle:
            raise ValidationError("no bundle given")
        job = jobs.submit("restore", backupsvc.restore, bundle,
                          name=str(body.get("name") or ""),
                          new=bool(body.get("new", False)),
                          allow_upgrade=bool(body.get("allow_upgrade", False)),
                          force=bool(body.get("force", False)),
                          label=str(body.get("name") or Path(bundle).name))
        return {"job_id": job.id}

    @app.get("/api/repros/{name}/upgrade")
    def upgrade_state(name: str, to: str = "", offline: bool = False):
        """Whether Upgrade may be offered, and what a given target would do.

        Without `to` this is just the gate: the GUI renders the action only for a
        RUNNING workspace, because the pre-upgrade backup needs MongoDB up and the
        migrations only run when Rocket.Chat boots.
        """
        from rc_repro.services import upgrade as upgradesvc
        state = upgradesvc.can_upgrade(name)
        if to and state["can_upgrade"]:
            state["plan"] = upgradesvc.plan(name, to, offline=offline)
        return state

    @app.post("/api/repros/{name}/upgrade")
    def upgrade_run(name: str, body: dict = Body(...)):
        from rc_repro.services import upgrade as upgradesvc
        target = lc.resolve_name(name)
        to = str(body.get("to") or "")
        if not to:
            raise ValidationError("no target version given, e.g. {\"to\": \"8.6.1\"}")
        job = jobs.submit("upgrade", upgradesvc.run, target, to,
                          offline=bool(body.get("offline", False)),
                          force=bool(body.get("force", False)),
                          no_backup=bool(body.get("no_backup", False)),
                          rollback_on_failure=bool(body.get("rollback_on_failure", True)),
                          label=target)
        return {"job_id": job.id}

    @app.post("/api/repros/{name}/upgrade/rollback")
    def upgrade_rollback(name: str, body: dict = Body(default={})):
        from rc_repro.services import upgrade as upgradesvc
        target = lc.resolve_name(name)
        job = jobs.submit("rollback", upgradesvc.rollback, target,
                          bundle=str(body.get("bundle") or ""), label=target)
        return {"job_id": job.id}

    @app.get("/api/repros/{name}/tls")
    def tls_state(name: str):
        """What the repro is ACTUALLY serving over TLS — the GUI's `tls-status`.

        `up --wait` only proves Rocket.Chat booted: it polls the internal http port.
        Traefik obtains its certificate in the background afterwards and falls back
        to a self-signed placeholder when ACME fails, so HTTPS needs its own check.
        """
        from rc_repro import tls as tlsmod
        meta = runner.read_meta(lc.resolve_name(name))
        if not meta.public_url:
            raise ValidationError(f"{meta.name!r} was not created with --https")
        mode = str(meta.extra.get("tls") or "")
        host = meta.public_url.split("://", 1)[1].split(":")[0]
        port = int((meta.extra.get("tls_ports") or [443])[0])
        cafile = (str(tlsmod.ca_dir() / tlsmod.CA_CRT)
                  if mode == tlsmod.MODE_LOCAL else None)
        # Dial THIS host with the domain as SNI: probing the public name lets a
        # proxy in front answer instead, and its certificate is not ours.
        out = tlsmod.verify("127.0.0.1", port, cafile=cafile, sni=host)
        out.update(mode=mode, public_url=meta.public_url, name=meta.name)
        if mode == tlsmod.MODE_ACME:
            pub = tlsmod.verify(host, 443)
            out["public_issuer"] = pub["issuer"]
            out["public_error"] = pub["error"]
        return out

    @app.post("/api/repros/{name}/call")
    def api_call(name: str, body: dict = Body(...)):
        """One authenticated REST call against a repro -- the GUI's `rc-repro api`.

        A non-2xx *from Rocket.Chat* is a result, not a failure of this endpoint:
        it comes back 200 with the status in the payload, so the panel can show
        "HTTP 403" and the response body the way the CLI prints them. Only being
        unable to make the call at all (bad input, no auth, no connection) is an
        error status here.

        Not an open proxy: rcapi.call() builds `root_url + "/" + path.lstrip("/")`,
        so the host is always this repro's own. A `path` of "http://elsewhere/x"
        or "//elsewhere/x" lands as a path segment on the workspace, not a new
        destination.
        """
        import requests

        from rc_repro import rcapi
        lc.require_docker()
        meta = runner.read_meta(lc.resolve_name(name))

        method = str(body.get("method") or "GET").upper()
        if method not in _API_METHODS:
            raise ValidationError(
                f"unsupported method {method!r} (use {', '.join(_API_METHODS)})")
        path = str(body.get("path") or "").strip()
        if not path:
            raise ValidationError("no API path given, e.g. /api/v1/me")
        raw = str(body.get("data") or "").strip()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise ValidationError(f"request body is not valid JSON: {exc}") from exc

        use_pat = bool(body.get("pat"))
        two_fa = bool(body.get("two_fa"))
        try:
            auth = lc.login(meta)
            if use_pat:
                # Same swap the CLI's --pat does: exercise the workspace the way a
                # customer script does, through a token rather than a login session.
                auth = rcapi.Auth(
                    token=rcapi.generate_pat(meta.root_url, auth, config.ADMIN_PASSWORD,
                                             bypass_2fa=True, workspace=meta.name),
                    user_id=auth.user_id)
        except Exception as exc:  # noqa: BLE001 - surface as a 409, not a 500
            raise NotReadyError(
                f"could not authenticate (is it ready? `rc-repro ready -n {meta.name}`): {exc}"
            ) from exc

        # Audited at the USER-INITIATED call site, never inside rcapi.call() --
        # that is the internal transport for login, seeding and every load test,
        # so auditing there would drown the log in rc-repro talking to itself.
        # The body is never recorded: it can carry a password being set.
        auditsvc.record("api-call", f"{meta.name} {method} {path}")
        extra = rcapi.password_2fa_headers(config.ADMIN_PASSWORD) if two_fa else None
        started = time.monotonic()
        try:
            status, text = rcapi.call(meta.root_url, method, path,
                                      auth=auth, data=payload, extra_headers=extra)
        except requests.RequestException as exc:
            raise NotReadyError(f"request failed: {exc}") from exc
        tag = ("PAT" if use_pat else "admin") + ("+2fa" if two_fa else "")
        return {"status": status, "text": text, "tag": tag,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "url": f"{meta.root_url.rstrip('/')}/{path.lstrip('/')}"}

    @app.delete("/api/repros/{name}")
    def teardown(name: str, volumes: bool = False, confirm: bool = False):
        return lc.teardown(name, volumes=volumes, confirm=confirm)

    @app.post("/api/prune")
    def prune(body: dict = Body(default={})):
        return lc.prune(confirm=bool(body.get("confirm", False)))

    # --- jobs ----------------------------------------------------------------
    @app.get("/api/jobs")
    def jobs_list():
        """Retained jobs, newest first. A job outlives the dialog that started it
        (and any page refresh), so without this the output of a long benchmark or
        capacity search was computed and then unreachable."""
        return {"jobs": jobs.list_jobs()}

    @app.get("/api/jobs/{job_id}")
    def job_state(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "no such job"}, status_code=404)
        return {"id": job.id, "kind": job.kind, "status": job.status,
                "result": job.result, "error": job.error, "error_kind": job.error_kind,
                "n_events": job.n_events}

    @app.get("/api/jobs/{job_id}/stream")
    async def job_stream(job_id: str, since: int = 0):
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "no such job"}, status_code=404)

        async def gen():
            idx = since
            while True:
                # The next index comes from the job, not from counting: a flood
                # trims the oldest events, so the absolute index can jump.
                evs, done, nxt = job.snapshot(idx)
                base = nxt - len(evs)
                for n, e in enumerate(evs):
                    yield f"id: {base + n}\ndata: {json.dumps(e)}\n\n"
                idx = nxt
                if done and not evs:
                    break
                await asyncio.sleep(0.2)
        # no-cache / no-transform keep reverse proxies (the case --allow-host
        # exists for) from buffering the stream into uselessness.
        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        })

    # --- static SPA ----------------------------------------------------------
    webui = resources.files("rc_repro").joinpath("data", "webui")
    app.mount("/", StaticFiles(directory=str(webui), html=True), name="ui")
    return app
