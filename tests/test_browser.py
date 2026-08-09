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
        page.wait_for_selector("#me-menu:not([hidden])")
        page.click("#me-out")
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
        # `#repros` is in the served HTML, so waiting for it proves nothing about
        # the async boot. `#whoami` is only unhidden once GET /api/session has
        # answered, which is what the role assertions below actually depend on --
        # this used to pass on timing alone.
        page.wait_for_selector("#whoami:not([hidden])")
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
        page.wait_for_selector("#me-people:not([hidden])")
        page.click("#me-people")
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
        page.wait_for_selector("#me-menu:not([hidden])")
        page.click("#me-out")
        page.wait_for_url("**/signin**")
        _sign_in(page, s.url, user="bob", password="bobs-good-password")
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#whoami")
        page.wait_for_selector("#me-menu:not([hidden])")
        assert page.is_hidden("#me-people"), "a member is not offered People at all"


def test_the_activity_page_opens_both_of_its_tabs(serve, page):
    """H5's read path. History is a different fetch from Running, so a broken one
    is invisible until somebody clicks it.

    A page rather than a dialog since v0.39.0: Activity is about the whole box and
    about things that have already happened, which is not what an overlay is for.
    """
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("#btn-jobs")
        page.wait_for_selector("#act-tabs")
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
                   # a fact the browser already shows permanently, and a PROBLEM
                   # about the same subject — only the first is the dialog's job
                   {"status": "ok", "message": "Edge running — holds :80/:443",
                    "elsewhere": "the edge chip in the header"},
                   {"status": "warn", "message": "1 edge route(s) with no workspace"},
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
        assert "Edge running" not in body, \
            "a row the header already carries was restated in the diagnostic"
        assert "no workspace" in body, \
            "a WARNING was dropped along with the fact it shares a subject with"
        # `rc-repro doctor` prints a repro tally because a terminal has nowhere
        # else to say it. The rail lists them by name, behind this dialog.
        assert "1 total, 1 running" not in body
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


def test_the_create_dialog_follows_the_box_policy_not_the_role(serve, page,
                                                               monkeypatch):
    """The dialog offered rc_image / reg_token / bind to everybody while the API
    refused them from a non-admin, so a member could fill one in and have the whole
    create fail -- app.py's comment claimed the GUI "never sends them for a member".

    It asks the SERVER now (GET /api/settings -> may_set_privileged), so the box
    policy reaches the interface too. Two places computing one permission is what
    produced the mismatch in the first place.

    Open is the DEFAULT: a member can already create workspaces and tear them down
    with their data, so withholding the interface their own listens on is an
    inconsistent ladder rather than a boundary.

    `port` is deliberately always offered: it is not privileged any more, because
    the privileged-port range it might have guarded is refused for everyone anyway.
    """
    from rc_repro import config as cfgmod
    from rc_repro.services import lifecycle as lcmod

    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    usersvc.add("mem", "members-good-password", role="member")

    def open_dialog():
        page.click("#btn-new")
        page.wait_for_selector("#create-dialog[open]")
        # These fields live inside <details class="adv">, which is COLLAPSED. Without
        # expanding it every one of them is invisible whatever the policy says, and
        # an is_hidden() assertion passes for the wrong reason -- the first version
        # of this test did exactly that and would have passed against the bug.
        page.eval_on_selector_all(
            "#create-dialog details.adv", "ds => ds.forEach(d => d.open = true)")
        page.wait_for_selector("#create-form [name=mongo]", state="visible")

    with serve() as s:
        _sign_in(page, s.url, user="mem", password="members-good-password")
        page.wait_for_selector("#btn-new")
        open_dialog()
        for name in lcmod.PRIVILEGED_CREATE_FIELDS:
            assert page.is_visible(f"#create-form [name={name}]"), \
                f"a member is not offered {name!r}, which the server accepts"
        assert page.is_visible("#create-form [name=port]"), \
            "a host port is a member's to choose"
        page.click("#create-cancel")
        page.wait_for_function("() => !document.querySelector('#create-dialog').open")

        # ...and a box that narrows the policy: the same member, same session.
        cfgmod.update_config(
            lambda c: c.__setitem__(lcmod.CREATE_POLICY_KEY, "admin"))
        open_dialog()
        for name in lcmod.PRIVILEGED_CREATE_FIELDS:
            assert page.is_hidden(f"#create-form [name={name}]"), \
                f"gui.create_policy=admin did not reach the dialog for {name!r}"
        assert page.errors == [], page.errors


def test_the_theme_is_light_by_default_and_the_choice_survives_a_reload(serve, page):
    """Light is the default; the choice is remembered per BROWSER, not per account —
    it is a property of the screen you are sitting at, and the same person on a
    projector and a laptop wants different answers."""
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        assert page.get_attribute("html", "data-theme") == "light"

        page.click("#theme-toggle")
        assert page.get_attribute("html", "data-theme") == "dark"

        page.reload()
        page.wait_for_selector("#repros")
        assert page.get_attribute("html", "data-theme") == "dark", "the choice was forgotten"
        assert page.errors == [], page.errors


def test_nothing_in_the_page_is_painted_a_hardcoded_dark_colour(serve, page):
    """Every colour comes from a token, so one palette swap re-themes the whole GUI.
    A literal that assumed a dark ground survives the swap and shows up as a black
    bar on a white page — the top bar and the dot grid both did exactly that."""
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        for sel in ("body", ".topbar", ".statusbar"):
            bg = page.eval_on_selector(sel, "e => getComputedStyle(e).backgroundColor")
            nums = [int(n) for n in __import__("re").findall(r"\d+", bg)[:3]]
            assert sum(nums) / 3 > 150, f"{sel} is dark ({bg}) on the light theme"


def test_no_control_is_hidden_behind_the_status_bar(serve, page, monkeypatch):
    """The status bar used to be position:fixed, and the old layout reserved 3.5rem
    of bottom padding for it. The three-pane shell has no such gutter, so "Prune
    down repros" rendered at y=860 underneath a bar occupying 870-900 — present in
    the DOM, clickable by a test, invisible to a person.

    The bar is a row of the shell now rather than an overlay. This asserts the
    property rather than the implementation: nothing sits under it.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        bar = page.eval_on_selector(".statusbar", "e => e.getBoundingClientRect().top")
        for sel in ("#btn-prune", "#btn-new", "#filter"):
            box = page.eval_on_selector(sel, "e => e.getBoundingClientRect().bottom")
            assert box <= bar + 1, f"{sel} is underneath the status bar ({box} > {bar})"
        assert page.errors == [], page.errors


def test_the_home_page_leads_with_capacity(serve, page, monkeypatch):
    """The stage used to say "Select a workspace", which answers a question nobody
    asked. It leads with the constraint that actually bites: `up` refuses without
    headroom, and seven concurrent stacks once OOM-killed a 10 GB host with every
    individual create having succeeded.

    It must agree with the refusal — one formula, three consumers (check_capacity,
    doctor, here). A home page saying "room for 2 more" while the create is refused
    is worse than one saying nothing.
    """
    from rc_repro.services import lifecycle as lcmod

    _stub_lifecycle(monkeypatch)
    monkeypatch.setattr(lcmod, "capacity", lambda: {
        "known": True, "total_mb": 10240, "available_mb": 4096, "reserve_mb": 2048,
        "swap_mb": 0, "workspace_mb": 1100, "room": 1})
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.wait_for_function(
            "() => document.querySelector('#detail').textContent.includes('room for')")
        body = page.text_content("#detail")
        assert "room for about 1 more" in body
        assert "Memory on this machine" in body
        assert page.errors == [], page.errors


def test_a_down_workspace_is_something_home_asks_you_about(serve, page, monkeypatch):
    """It still holds a port and a data volume, and nothing anywhere said so."""
    d = _fake_detail(name="upgrade-test", state="down")
    _stub_lifecycle(monkeypatch, d)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.wait_for_function(
            "() => document.querySelector('#detail').textContent.includes('Needs you')")
        assert "upgrade-test is down" in page.text_content("#detail")
        assert "still holds port" in page.text_content("#detail")

        # ...and picking it replaces home with the workspace, actions and all
        page.click(".wrow")
        page.wait_for_selector("#d-body")
        assert "upgrade-test" in page.text_content(".d-head")
        assert page.errors == [], page.errors


def test_activity_and_a_workspace_are_different_views_of_the_stage(serve, page,
                                                                   monkeypatch):
    """Opening Activity must not silently deselect the workspace you were on, and
    picking a workspace must leave Activity. The rail selection and the stage view
    are separate pieces of state for exactly that reason."""
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")

        page.click(".wrow")
        page.wait_for_selector("#d-body")
        assert page.is_visible("#actpane .apdanger"), "the workspace pane is up"

        page.click("#btn-jobs")
        page.wait_for_selector("#act-tabs")
        assert page.locator("#actpane .apdanger").count() == 0, \
            "the per-workspace pane has no business on a box-wide page"
        assert page.locator(".wrow[aria-current=true]").count() == 1, \
            "the workspace stayed selected in the rail"

        page.click(".wrow")
        page.wait_for_selector("#d-body")
        assert page.locator("#act-tabs").count() == 0, "picking a workspace leaves Activity"
        assert page.errors == [], page.errors


def test_the_action_pane_names_the_thing_not_one_of_its_parts(serve, page, monkeypatch):
    """`monitor` attaches Prometheus, Grafana, Loki and the exporters; "Stream to
    Grafana" named the last hop of it, and the job it starts has always said
    "Attaching monitoring". The button now agrees with its own progress line.

    Asserted because a label is exactly what a later edit changes back by accident.
    """
    d = _fake_detail()
    d["monitoring"] = False
    _stub_lifecycle(monkeypatch, d)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click(".wrow")
        page.wait_for_selector("#d-body")
        labels = page.eval_on_selector_all("#actpane button", "e => e.map(x => x.textContent)")
        assert "Attach monitoring" in labels
        assert "PAT and Token" in labels
        assert not [x for x in labels if "Grafana" in x or "Mint" in x], labels
        assert page.errors == [], page.errors


def test_a_note_that_names_a_place_joins_the_row_that_already_lists_it(serve, page,
                                                                       monkeypatch):
    """Monitoring's notes name Grafana and Prometheus — the two links the server
    ALREADY reports — and add the one thing the links cannot: the password. Shown
    as prose they became a second, duplicate list of the same urls directly under
    the first, which is what made the block read as a dump of leftovers.

    So: one row per place, carrying everything known about it.
    """
    d = _fake_detail()
    d["links"] = [
        {"label": "Rocket.Chat", "url": "http://localhost:3001", "kind": "rc"},
        {"label": "Grafana", "url": "http://localhost:5050", "kind": "monitor"},
        {"label": "Prometheus", "url": "http://localhost:9090", "kind": "monitor"},
    ]
    d["notes"] = [
        "Grafana:    http://localhost:5050  (admin/admin; anonymous view enabled)",
        "Prometheus: http://localhost:9090  (Status -> Targets: rocketchat, mongodb)",
        "Dashboards auto-provisioned: Rocket.Chat Metrics, MongoDB, Node Exporter Full.",
    ]
    _stub_lifecycle(monkeypatch, d)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click(".wrow")
        page.wait_for_selector("#d-body")

        rows = page.locator("#detail .linkrow")
        assert rows.count() == 3, "a url is listed twice, or a row went missing"
        graf = page.locator("#detail .linkrow:has(.l-u:text-is('http://localhost:5050'))")
        assert graf.locator(".l-c").inner_text() == "admin/admin", \
            "the password the links cannot carry did not make it onto the row"
        assert "anonymous view" in graf.locator(".l-sub").inner_text()
        # Prometheus's parenthetical is a remark, not a login — inventing a
        # credential chip out of "Status -> Targets: …" would be worse than none.
        prom = page.locator("#detail .linkrow:has(.l-u:text-is('http://localhost:9090'))")
        assert prom.locator(".l-c").count() == 0
        assert "Targets" in prom.locator(".l-sub").inner_text()
        # and the line that is genuinely prose stays prose
        assert page.locator("#detail .note-p", has_text="auto-provisioned").count() == 1
        assert page.errors == [], page.errors


def test_the_scenarios_page_shows_the_setup_step_a_preset_needs(serve, page):
    """A preset's `notes` were printed by `up` and `info` and shown NOWHERE in the
    browser. For `oidc` that is not cosmetic: the scenario does not work at all
    until `127.0.0.1  keycloak` is in /etc/hosts, and a GUI-only user had no way to
    learn that — the login simply fails and nothing says why.

    Real preset data, not a fixture: if a note stops carrying its setup step, this
    fails.
    """
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#btn-scenarios")
        page.wait_for_function(
            "() => document.querySelector('#detail').textContent.includes('oidc')")
        body = page.text_content("#detail")
        assert "127.0.0.1  keycloak" in body, "the hosts entry oidc cannot work without"
        assert "/etc/hosts" in body
        assert "8085" in body, "and where the Keycloak console is"
        # The line you have to TYPE keeps its spacing exactly and is copyable; the
        # prose around it is free to wrap, because it was wrapped for an 80-column
        # terminal and re-flowing it is the right thing to do. Asserting `pre` on
        # the whole block, as this once did, protected the wrong half.
        cmd = page.locator(".note-cmd code", has_text="127.0.0.1")
        assert cmd.count() == 1, "the hosts entry is not a copyable line"
        assert cmd.first.inner_text() == "127.0.0.1  keycloak", "its spacing changed"
        assert page.eval_on_selector(
            ".note-cmd code", "e => getComputedStyle(e).whiteSpace") == "pre"
        assert page.locator(".note-cmd .cp").first.is_visible(), "and it can be copied"
        # The line that names WHERE the console is is not prose — it is a place,
        # and the two facts on it are the url and the password. As a paragraph
        # ("Keycloak admin console: http://keycloak:8085  (admin/admin, realm
        # 'rcrepro').") both were buried mid-sentence in grey text.
        # by url, because `saml` names its own Keycloak console on the same page
        row = page.locator(".linkrow:has(.l-u:text-is('http://keycloak:8085'))")
        assert row.count() == 1, "the console line did not become a row"
        assert "Keycloak admin console" in row.locator(".l-n").inner_text()
        assert row.locator(".l-c").inner_text() == "admin/admin", "credentials not lifted out"
        assert "rcrepro" in row.locator(".l-sub").inner_text(), "the realm was dropped"
        assert row.first.get_attribute("href").endswith("8085/")
        assert page.errors == [], page.errors


def test_you_can_change_your_own_password_from_the_browser(serve, page):
    """POST /api/me/password has existed since v0.23.0 — role-gated, throttled like
    a login, requires the current password, ends every other session and keeps this
    one. NOTHING in the browser called it. It was built and unreachable.

    The end-to-end property: change it, stay signed in, and the new password is the
    one that works.
    """
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#whoami")
        page.wait_for_selector("#me-menu:not([hidden])")
        page.click("#me-passwd")
        page.wait_for_selector("#passwd-dialog[open]")

        # the current password is genuinely required
        page.fill("#passwd-form [name=old]", "not-my-password")
        page.fill("#passwd-form [name=new]", "a-brand-new-password")
        page.click("#passwd-go")
        page.wait_for_selector("#passwd-err:not([hidden])")
        assert "not your current password" in page.text_content("#passwd-err")

        page.fill("#passwd-form [name=old]", PASSWORD)
        page.click("#passwd-go")
        page.wait_for_function("() => !document.querySelector('#passwd-dialog').open")

        # still signed in — the endpoint re-issues this session on purpose
        page.reload()
        page.wait_for_selector("#whoami:not([hidden])")
        assert page.text_content("#whoami").strip() == "alice"
        assert usersvc.verify("alice", "a-brand-new-password") is True
        assert usersvc.verify("alice", PASSWORD) is False
        assert page.errors == [], page.errors


def test_every_page_is_reachable_and_says_which_one_you_are_on(serve, page,
                                                               monkeypatch):
    """Activity and Scenarios shipped one-way: you could reach them and nothing led
    back, because the brand was a <div> and no link was ever marked current. You
    could not tell where you were, or that there was anywhere else to be."""
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        assert page.get_attribute("#btn-home", "aria-current") is not None

        page.click("#btn-jobs")
        page.wait_for_selector("#act-tabs")
        assert page.get_attribute("#btn-jobs", "aria-current") is not None
        assert page.get_attribute("#btn-home", "aria-current") is None

        page.click("#btn-home")
        page.wait_for_function(
            "() => document.querySelector('#detail').textContent.includes('workspace(s)')")

        page.click("#btn-scenarios")
        page.wait_for_function(
            "() => document.querySelector('#detail').textContent.includes('Scenarios')")
        page.click(".brand")                       # the wordmark goes home too
        page.wait_for_function(
            "() => document.querySelector('#detail').textContent.includes('workspace(s)')")
        assert page.errors == [], page.errors


def test_the_dashboard_poll_does_not_close_the_page_you_are_reading(serve, page,
                                                                    monkeypatch):
    """render() runs every four seconds to redraw the rail. It used to redraw the
    STAGE too, so opening Scenarios and reading it for four seconds put you back on
    the home page with no interaction at all."""
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#btn-scenarios")
        page.wait_for_function(
            "() => document.querySelector('#detail').textContent.includes('Scenarios')")
        page.wait_for_timeout(5000)                # longer than POLL_MS
        assert "Scenarios" in page.text_content("#detail"), \
            "the poll replaced the page while it was being read"
        assert page.errors == [], page.errors


def test_preset_notes_are_read_as_points_not_as_lines(serve, page):
    """The notes are prose wrapped for an 80-column terminal. Rendering one line per
    <div> makes the /etc/hosts entry look like the sentence beside it; joining every
    consecutive line turns the email preset's five points into one wall.

    The rule reads what the previous line ENDS with, because that is the only
    reliable signal in the data: a line trailing off in a plain lowercase word was
    wrapped, one ending in a url, a path, a number or punctuation was finished.
    `livechat` is the case that proves it — two consecutive points that each end in
    a url, followed by one starting with a lowercase username.
    """
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#btn-scenarios")
        page.wait_for_function(
            "() => document.querySelector('#detail').textContent.includes('livechat')")

        def parts(scenario, sel):
            return page.eval_on_selector_all(
                f"xpath=//div[contains(@class,'hcard')][.//b[text()='{scenario}']]"
                f"//*[contains(@class,'{sel}')]",
                "els => els.map(e => e.textContent.trim())")

        # livechat's first two points each NAME A PLACE, so they are rows; the two
        # that follow are prose, and they are two rather than one.
        assert parts("livechat", "l-u") == ["http://localhost:8090",
                                            "<repro-url>/livechat"]
        assert parts("livechat", "l-w") == ["widget embedded, cross-origin",
                                            "same-origin"], "the aside naming what it is for"
        live = parts("livechat", "note-p")
        assert len(live) == 2, f"livechat's two prose points became {len(live)}: {live}"
        assert live[0].startswith("admin is an available"), \
            "a point starting with a lowercase username was folded into the one above"

        oidc = parts("oidc", "note-p")
        assert oidc[0].startswith("OIDC needs one host entry") and "same URL" in oidc[0], \
            "a sentence wrapped across two lines was split into two paragraphs"
        assert page.errors == [], page.errors
