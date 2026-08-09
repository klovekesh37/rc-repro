"""Browser smoke tests for the GUI (skipped unless Playwright is installed).

Everything else in this suite talks HTTP. That left a real gap: the sign-in page,
the first-run flow, the account menu, the People dialog and the role-aware
rendering are all things only a browser executes, and `app.js` is ~2000 lines with
no test of any kind. A typo in it fails nothing.

These are SMOKE tests on purpose -- one path through each screen that would catch
"the page is blank", "the button does nothing", "the SPA never signs you in". They
are not a substitute for the HTTP tests, which own the actual contracts.

Deliberately cheap to skip. `pytest -q` on a machine with no browser reports them
skipped and stays green, exactly as tests/test_web.py does for the [gui] extra:

    pip install -e '.[dev,gui,browser]'
    playwright install chromium --only-shell
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

from rc_repro.services import firstrun as frsvc  # noqa: E402
from rc_repro.services import users as usersvc  # noqa: E402
from rc_repro.web.app import create_app  # noqa: E402

PASSWORD = "alice-good-password"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """A real uvicorn on a real port. TestClient cannot be driven by a browser."""

    def __init__(self, app, port: int):
        import uvicorn
        self.port = port
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                return self
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    # A JS error that leaves the page half-rendered is exactly the failure these
    # exist to catch, so collect them and let each test assert none happened.
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    ctx.close()


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """A server factory bound to an isolated home."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def _start(**kw):
        return _Server(create_app(allow_hosts=["127.0.0.1"], **kw), _free_port())
    return _start


def _sign_in(page, url, user="alice", password=PASSWORD):
    page.goto(f"{url}/signin")
    page.fill("#u", user)
    page.fill("#p", password)
    page.click("button[type=submit]")


# --- the flows ---------------------------------------------------------------------

def test_a_signed_out_browser_lands_on_the_sign_in_page(serve, page):
    """M7: there was no login page at all -- just the browser's grey Basic dialog,
    which cannot show an error, cannot be styled and has no way back."""
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        page.goto(s.url)
        page.wait_for_url("**/signin**")
        assert page.is_visible("#u") and page.is_visible("#p")
        assert "rc-repro" in page.title()
        assert page.errors == []


def test_a_wrong_password_shows_an_error_in_the_page(serve, page):
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url, password="not-the-password")
        page.wait_for_selector(".signin-banner.bad")
        assert "not right" in page.text_content(".signin-banner.bad")
        assert page.errors == []


def test_signing_in_renders_the_dashboard(serve, page):
    """The whole point: a session cookie, and app.js actually running."""
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.wait_for_selector("#whoami:not([hidden])")
        assert page.text_content("#whoami").strip() == "alice"
        assert page.is_visible("#btn-new"), "an admin may create"
        assert page.errors == [], page.errors


def test_sign_out_ends_the_session_and_returns_to_the_login(serve, page):
    """HTTP Basic had no logout at all; the browser cached until the tab closed."""
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#whoami")
        page.wait_for_selector("#session-dialog[open]")
        page.click("#session-out")
        page.wait_for_url("**/signin**")
        # and the session is really gone: going back does not restore it
        page.goto(s.url)
        page.wait_for_url("**/signin**")


def test_first_run_creates_the_first_admin_in_the_browser(serve, page):
    """The only path that can create an account with nobody signed in. The key
    lives in the URL FRAGMENT, so only a browser can complete this -- which is
    precisely why it needed a browser test."""
    key = frsvc.mint()
    with serve(first_run=True) as s:
        page.goto(f"{s.url}/setup#k={key}")
        page.fill("#u", "alice")
        page.fill("#p", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_selector("#repros")          # straight into the dashboard
        assert page.text_content("#whoami").strip() == "alice"
        # the key must not survive in the address bar
        assert "#k=" not in page.url
        assert usersvc.role_of("alice") == "admin"
        assert page.errors == [], page.errors


def test_the_setup_page_says_so_when_the_key_is_missing(serve, page):
    frsvc.mint()
    with serve(first_run=True) as s:
        page.goto(f"{s.url}/setup")               # no fragment
        page.wait_for_selector("#err:not([hidden])")
        assert "setup link" in page.text_content("#err")


def test_a_readonly_user_is_not_offered_what_it_cannot_do(serve, page):
    """The bug reported from a real session: the server refused correctly, but the
    interface still rendered Stop / Restart / Seed / Logs, so a readonly user
    clicked them and reasonably concluded roles were not working."""
    usersvc.add("alice", PASSWORD, role="admin")
    usersvc.add("ronly", "read-only-password", role="readonly")
    with serve() as s:
        _sign_in(page, s.url, user="ronly", password="read-only-password")
        page.wait_for_selector("#repros")
        assert page.text_content("#whoami").strip() == "ronly · readonly"
        assert page.is_hidden("#btn-new"), "creating is member+"
        assert page.is_hidden("#btn-prune"), "pruning is member+"
        assert page.errors == [], page.errors


def test_an_admin_can_open_people_and_a_member_cannot(serve, page):
    usersvc.add("alice", PASSWORD, role="admin")
    usersvc.add("bob", "bobs-good-password", role="member")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#whoami")
        page.wait_for_selector("#session-people:not([hidden])")
        page.click("#session-people")
        page.wait_for_selector("#people-dialog[open]")
        # The list is fetched after the dialog opens, so wait for the content
        # rather than for the container -- asserting on "loading…" would be a
        # test that passes whether or not /api/users works.
        page.wait_for_function(
            "() => document.querySelector('#people-body').textContent.includes('alice')")
        assert "admin" in page.text_content("#people-body")
        assert page.errors == [], page.errors

        # ...and a member is not offered the entry point at all.
        # Signed out through the UI, not by GETting /signout: that route is POST
        # only on purpose, because a logout reachable by GET is CSRF-able and gets
        # fired by link prefetchers.
        page.click("#people-close")
        page.click("#whoami")
        page.wait_for_selector("#session-dialog[open]")
        page.click("#session-out")
        page.wait_for_url("**/signin**")
        _sign_in(page, s.url, user="bob", password="bobs-good-password")
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#whoami")
        page.wait_for_selector("#session-dialog[open]")
        assert page.is_hidden("#session-people")


def test_the_activity_dialog_opens_both_of_its_tabs(serve, page):
    """H5's read path. History is a different fetch from Running, so a broken one
    is invisible until somebody clicks it."""
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("#btn-jobs")
        page.wait_for_selector("#jobs-dialog[open]")
        page.click("#act-tab-history")
        page.wait_for_selector("#jobs-filter:not([hidden])")
        # the account that was just created is in the trail
        page.wait_for_function(
            "() => document.querySelector('#jobs-list').textContent.includes('alice')")
        assert page.errors == [], page.errors


# --- surfaces the auth tests never render -------------------------------------------
#
# Everything above this line is about signing in. The create dialog, the log viewer
# and the backups tab had never been rendered by any test at all -- and app.js is
# ~2200 lines where a typo fails nothing. These are the same kind of smoke test:
# one path through each screen, enough to catch "it is blank" or "the button does
# nothing".

def _fake_detail(name="t1234", state="running"):
    """A detail payload shaped like lc.detail()'s, so the panel renders offline.

    Every key the panel INDEXES INTO has to be here -- `d.containers.map` on an
    undefined throws, which surfaces as a blank panel rather than an error, and
    that is precisely the failure these tests exist to catch.
    """
    return {
        "name": name, "state": state, "rc_version": "7.4.1", "mongo_tag": "7.0",
        "mongo_flavor": "mongodb", "host_port": 3001, "uptime": "2h", "preset": "base",
        "health": "healthy", "url": "http://localhost:3001", "root_url": "http://localhost:3001",
        "public_url": "", "login": {"user": "admin", "password": "admin123"},
        # `service`, not `name` -- that is the key the containers tab reads, and
        # getting it wrong renders an empty table rather than an error.
        "containers": [{"service": "rocketchat", "state": "running",
                        "status": "Up 2 hours", "health": "healthy"}],
        "env": {"ROOT_URL": "http://localhost:3001"},
        "links": [], "notes": "", "restarts": 0, "monitoring": False,
        "tls": {}, "is_default": False, "created_by": "alice", "owner": "alice",
        "made_by": "alice", "owner_history": [], "workspace": "/tmp/ws", "default": False,
        "grafana_url": "", "diag": {},
    }


def _stub_lifecycle(monkeypatch, detail=None):
    from rc_repro.services import lifecycle as lc
    d = detail or _fake_detail()
    monkeypatch.setattr(lc, "list_repros", lambda: [dict(d)])
    monkeypatch.setattr(lc, "detail", lambda name: dict(d))
    monkeypatch.setattr(lc, "resolve_name", lambda name: d["name"])
    return d


def test_the_dashboard_renders_a_workspace_card(serve, page, monkeypatch):
    d = _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.wait_for_function(
            "() => document.querySelector('#repros').textContent.includes('t1234')")
        assert d["name"] in page.text_content("#repros")
        assert page.errors == [], page.errors


def test_the_detail_panel_opens_and_switches_tabs(serve, page, monkeypatch):
    """The panel is built by hand from ~200 lines of el() calls. A single undefined
    key throws mid-render and leaves half a panel on screen."""
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        assert "7.4.1" in page.text_content("#detail"), "the overview did not render"
        page.click("button.tab:has-text('Containers')")
        page.wait_for_function(
            "() => document.querySelector('#d-body').textContent.includes('rocketchat')")
        assert page.errors == [], page.errors


def test_the_create_dialog_opens_and_lists_presets(serve, page, monkeypatch):
    """The most-used dialog in the product, and nothing had ever rendered it."""
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#btn-new")
        page.click("#btn-new")
        page.wait_for_selector("#create-dialog[open]")
        page.wait_for_function(
            "() => document.querySelector('#preset-select').options.length > 0")
        opts = page.eval_on_selector(
            "#preset-select", "s => [...s.options].map(o => o.value)")
        assert "default" in opts and "ldap" in opts, \
            f"the built-in presets are missing: {opts}"
        page.click("#create-cancel")
        # A closed <dialog> is not "hidden" in the way wait_for_selector means, so
        # ask the element itself.
        page.wait_for_function(
            "() => !document.querySelector('#create-dialog').open")
        assert page.errors == [], page.errors


def test_the_log_viewer_streams_container_output(serve, page, monkeypatch):
    """The log tab and its WebSocket, which no test had ever rendered. `docker` is
    replaced by a fake process, so this needs no engine.

    The OVERFLOW policy is covered by unit tests in test_web.py rather than here:
    tripping it through a browser needs 3000+ lines and a reader slow enough to
    fall behind, which is a flake generator, not a test.
    """
    from rc_repro import runner
    from rc_repro.web import app as webapp

    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")

    # Stubbed BEFORE Popen is replaced: the stats poll runs every three seconds
    # against this same repro and would otherwise reach the fake through
    # runner.container_ids, which is not what it expects at all.
    monkeypatch.setattr(runner, "container_ids", lambda name: [])
    monkeypatch.setattr(runner, "docker_stats", lambda ids: "")

    class _FakeProc:
        """Stands in for `docker compose logs -f`."""
        def __enter__(self): return self
        def __exit__(self, *exc): return False

        def __init__(self, *a, **kw):
            lines = [f'rcrepro-t1234-rocketchat-1  | {{"level":30,"msg":"line {i}"}}'
                     for i in range(5)]
            self.stdout = iter(line + "\n" for line in lines)
            self.returncode = 0

        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    monkeypatch.setattr(webapp, "open_log_process", lambda ws, tail: _FakeProc())
    monkeypatch.setattr(runner, "workspace", lambda name: "/tmp")

    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        page.click("button.tab:has-text('Logs')")
        page.wait_for_selector("button.tab.active:has-text('Logs')")
        page.wait_for_selector("#logview")
        page.wait_for_function(
            "() => document.querySelector('#logview').textContent.includes('line 4')",
            timeout=10000)
        body = page.text_content("#logview")
        assert "line 0" in body and "line 4" in body
        assert page.errors == [], page.errors


def test_the_backups_tab_lists_what_is_there(serve, page, monkeypatch):
    """The tab fetches AFTER it renders, so it opens showing "loading…". Asserting
    on the container rather than on its content would pass whether or not
    /api/backups works — that mistake was made once already in this file."""
    from rc_repro.services import backup as backupsvc

    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    monkeypatch.setattr(backupsvc, "list_backups", lambda name="": [
        {"path": "/tmp/t1234-2026-08-09.tar.zst", "bytes": 1234567,
         "rc_version": "7.4.1", "mongo_tag": "7.0", "preset": "base",
         "label": "", "created_at": "2026-08-09T10:00:00+00:00"}])

    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        page.click("button.tab:has-text('Backups')")
        page.wait_for_function(
            "() => document.querySelector('#d-body').textContent"
            ".includes('t1234-2026-08-09')")
        assert "7.4.1" in page.text_content("#d-body")
        assert page.errors == [], page.errors


def test_the_load_test_dialog_opens_from_the_panel(serve, page, monkeypatch):
    """Reached only from the detail panel's action row, so it needs a workspace to
    exist before it can be rendered at all — which is why nothing had rendered it."""
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        page.click("button:has-text('Load test')")
        page.wait_for_selector("#perf-dialog[open]")
        assert "t1234" in page.text_content("#perf-title")
        # `live` needs the monitoring stack, and the fixture says it is off
        assert page.is_disabled("#perf-form >> [name=live]"), \
            "the live option must be disabled without monitoring attached"
        page.click("#perf-cancel")
        page.wait_for_function("() => !document.querySelector('#perf-dialog').open")
        assert page.errors == [], page.errors


def test_the_doctor_dialog_renders_its_checks(serve, page, monkeypatch):
    """The badge could only ever say up/down. This is the panel that says WHY."""
    from rc_repro.services import doctor as doctorsvc

    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    monkeypatch.setattr(doctorsvc, "run_checks", lambda: {
        "checks": [{"status": "ok", "message": "Docker daemon running (27.0.3)"},
                   {"status": "fail",
                    "message": "2 GB free - a workspace needs about 6"}],
        "counts": {}, "verdict": "fail",
        "repros": {"total": 1, "running": 1}})

    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#docker-badge")
        page.click("#docker-badge")
        page.wait_for_selector("#doctor-dialog[open]")
        page.wait_for_function(
            "() => !document.querySelector('#doctor-body').textContent.includes('Checking')")
        body = page.text_content("#doctor-body")
        assert "Docker daemon running" in body
        assert "a workspace needs about 6" in body, "the reason has to reach the screen"
        assert "1 total, 1 running" in body
        page.click("#doctor-close")
        assert page.errors == [], page.errors


def test_the_edge_badge_surfaces_an_unreachable_route(serve, page, monkeypatch):
    """A route the edge cannot reach answers 502 rather than erroring, so it looks
    like a broken workspace instead of a broken route. Surfacing that is the whole
    reason the badge exists.

    Deliberately NOT the empty state: refreshEdgeBadge() hides the badge when no
    edge is installed, so "No edge yet" is unreachable from it and a test clicking
    there would only ever have proved the badge was hidden.
    """
    from rc_repro.services import edge as edgesvc

    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    monkeypatch.setattr(edgesvc, "status", lambda: {
        "installed": True, "running": True, "routes": ["t1234", "t9999"],
        "attached": ["rcrepro-t1234_default"]})
    monkeypatch.setattr(edgesvc, "served_domain", lambda: "support.example.com")
    monkeypatch.setattr(edgesvc, "workspace_network",
                        lambda n: f"rcrepro-{n}_default")

    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#edge-badge:not([hidden])")
        assert "1 unreachable" in page.text_content("#edge-badge"), \
            "t9999's network is not attached, so the edge cannot reach it"
        page.click("#edge-badge")
        page.wait_for_selector("#edge-dialog[open]")
        assert "t9999" in page.text_content("#edge-body")
        page.click("#edge-close")
        page.wait_for_function("() => !document.querySelector('#edge-dialog').open")
        assert page.errors == [], page.errors


def test_a_readonly_user_is_offered_nothing_in_the_detail_panel_either(serve, page,
                                                                       monkeypatch):
    """The panel's action row branches on canWrite(), and two buttons were appended
    PAST the branch: "Make default" and the red, destructive "Down". So a readonly
    user saw "you can look, but not change anything here" with two change buttons
    directly beneath it.

    The existing readonly test covers the dashboard toolbar and the cards, which
    were right -- the card renderer returns early. Nothing covered the panel.

    Found in the audit trail, not by reading:
        dheeraj  denied  POST /api/repros/{name}/default needs member
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    usersvc.add("ronly", "read-only-password", role="readonly")
    with serve() as s:
        _sign_in(page, s.url, user="ronly", password="read-only-password")
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        assert "readonly" in page.text_content(".d-actions")
        for label in ("Make default", "Down", "Stop", "Restart", "Load test"):
            assert page.locator(f".d-actions button:has-text('{label}')").count() == 0, \
                f"a readonly user is offered {label!r}, which the server refuses"
        # ...and the thing that IS allowed is still there.
        assert page.locator(".d-actions a:has-text('Open RC')").count() == 1, \
            "opening the workspace is a link, not an action on it"
        assert page.errors == [], page.errors


def test_the_create_dialog_hides_what_a_member_may_not_set(serve, page, monkeypatch):
    """rc_image / reg_token / bind are refused from a non-admin by POST /api/repros,
    and the dialog offered all three to everybody -- so a member could fill one in
    and have the whole create fail. app.py's comment claimed the GUI "never sends
    them for a member"; it did.

    `port` is deliberately still offered: it is not admin-only any more, because the
    privileged-port range it might have guarded is refused for everyone anyway.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    usersvc.add("mem", "members-good-password", role="member")

    with serve() as s:
        _sign_in(page, s.url, user="mem", password="members-good-password")
        page.wait_for_selector("#btn-new")
        page.click("#btn-new")
        page.wait_for_selector("#create-dialog[open]")
        for name in ("rc_image", "reg_token", "bind"):
            assert page.is_hidden(f"#create-form [name={name}]"), \
                f"a member is offered {name!r}, which the server refuses"
        assert page.is_visible("#create-form [name=port]"), \
            "a host port is a member's to choose"
        assert page.errors == [], page.errors
