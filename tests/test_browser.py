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
        # Pre-existing race, same shape as the one CI caught next door: #repros
        # exists before app.js has filled #whoami from /api/session, so a slow
        # runner reads "" here. It has simply not lost the race yet.
        page.wait_for_selector("#whoami:not([hidden])")
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

def _fake_detail(name="t1234", state="running", **over):
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
        # THE SHAPES MATTER, and three of these were wrong for as long as this
        # helper existed: `env` was a dict where detail() returns a list of rows,
        # `notes` a string where it returns a list, `tls` a dict where it returns a
        # string. Nothing caught it because no test opened the tab that iterates
        # them -- `for (const e of {...})` throws "is not iterable", which is a blank
        # panel. test_fake_detail_matches_the_real_payload below now compares the two
        # so the next drift fails here instead of in production.
        "env": [{"key": "ROOT_URL", "value": "http://localhost:3001", "override": False}],
        "links": [], "notes": [], "restarts": 0, "monitoring": False,
        "tls": "", "is_default": False, "created_by": "alice", "owner": "alice",
        "made_by": "alice", "owner_history": [], "workspace": "/tmp/ws", "default": False,
        "grafana_url": "", "diag": {},
        # The three axes the panel now reads: which runtime it is, how it is
        # arranged, and what address it publishes on.
        "runtime": "docker", "deployment": "monolith", "bind_host": "127.0.0.1",
        "pinned": False,
        # Kubernetes-only, "" on Compose: the fixture is the UNION of what both
        # branches of detail() return, so one fixture covers either runtime.
        "namespace": "",
        **over,
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

        # The control is a drawn mark now, and it shows where the click GOES rather
        # than where you are: on a dark page it is a sun. Since it has no text, the
        # accessible name is the only name it has — so that is asserted, not the art.
        btn = page.locator("#theme-toggle")
        assert btn.inner_text().strip() == "", "the toggle went back to a word"
        assert btn.locator("svg").count() == 1
        assert btn.get_attribute("aria-label") == "Switch to the light theme"
        assert btn.locator("circle").count() == 1, "a dark page should offer the sun"
        page.click("#theme-toggle")
        assert btn.get_attribute("aria-label") == "Switch to the dark theme"
        assert btn.locator("circle").count() == 0, "a light page should offer the moon"
        # And the mark follows the theme's ink rather than carrying its own colour.
        # BOTH reads in one evaluation, deliberately: the chip has a 140ms colour
        # transition and the pointer is still over it after the click, so two
        # separate eval_on_selector calls sample the same transition at different
        # points and disagree -- `rgb(108,126,185)` vs `rgb(74,102,209)`, both of
        # them interpolated. Sampling once compares the two properties rather
        # than comparing one property against itself a few milliseconds later.
        assert page.evaluate("""() => {
            const b = document.querySelector('#theme-toggle');
            return getComputedStyle(b.querySelector('svg')).stroke
                 === getComputedStyle(b).color;
        }"""), "the mark stopped following the button's ink"
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


def test_people_rows_have_their_own_shape_not_activitys(serve, page, monkeypatch):
    """People borrowed `.jobrow`, which is four fixed cells for Activity — and a
    person needs six. Every row landed out of step with the one above it the
    moment Activity's grid was tightened.

    Also: no role PILL beside the role SELECT. The select already says which role
    this is, and saying it twice is not emphasis.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    usersvc.add("bob", PASSWORD, role="member")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        page.click("#whoami")
        page.wait_for_selector("#me-menu:not([hidden])")
        page.click("#me-people")
        page.wait_for_selector("#people-dialog[open]")
        page.wait_for_function("() => document.querySelectorAll('.prow').length >= 2")

        assert page.locator("#people-dialog .jobrow").count() == 0, \
            "People is wearing Activity's row again"
        assert page.locator("#people-dialog .jstatus").count() == 0, \
            "the role is shown twice — once as a pill and once as the select"
        row = page.locator(".prow", has_text="bob").first
        assert row.locator("select").input_value() == "member"
        assert row.locator("button", has_text="reset").count() == 1
        assert row.locator("button", has_text="remove").count() == 1
        # every row has the same number of cells, which is what lets them line up
        counts = page.eval_on_selector_all(
            ".prow", "rs => rs.map(r => r.children.length)")
        assert len(set(counts)) == 1, counts
        assert page.errors == [], page.errors


def test_https_is_part_of_advanced_options_not_a_section_beside_it(serve, page,
                                                                   monkeypatch):
    """Two sibling twisties made the create form look like it had two Advanced
    sections. HTTPS is one more thing about how the workspace gets built, so it is
    a titled group inside Advanced options — and its fields still work, which is
    the half a move like this quietly breaks.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#btn-new")
        page.click("#btn-new")
        page.wait_for_selector("#create-dialog[open]")

        assert page.locator("#create-dialog details.adv").count() == 1, \
            "the form has more than one Advanced disclosure again"
        assert page.eval_on_selector(
            "#create-https-block", "el => !!el.closest('details.adv')"), \
            "the HTTPS group is not inside Advanced options"
        # collapsed by default, so it is not in anybody's way
        assert page.eval_on_selector("#create-dialog details.adv", "d => d.open") is False

        page.eval_on_selector("#create-dialog details.adv", "d => { d.open = true; }")
        # and the mode selector still drives the Let's Encrypt fields
        assert page.locator("input[name=domain]").is_hidden()
        page.select_option("#https-mode", "acme")
        assert page.locator("input[name=domain]").is_visible()
        assert page.locator("input[name=acme_email]").is_visible()
        page.select_option("#https-mode", "local")
        assert page.locator("input[name=domain]").is_hidden()
        assert page.text_content("#https-mode-hint").strip() != ""
        assert page.errors == [], page.errors


def test_a_background_poll_does_not_navigate_the_page_for_you(serve, page,
                                                              monkeypatch):
    """refreshHome() repainted the stage whenever no workspace was SELECTED — and
    Activity and Scenarios are stage views with nothing selected. So the timer
    dropped Home on top of whatever you were reading, seconds after you opened it:
    the page appearing to navigate itself.

    `!SELECTED` was never the same question as "is Home on screen". render() has
    always asked the right one; this is the same fix, in the place that missed it.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#whoami:not([hidden])")
        for opener, marker in (("#btn-jobs", "#act-tabs"), ("#btn-scenarios", ".hcard.scen")):
            page.click(opener)
            page.wait_for_selector(marker)
            page.evaluate("() => refreshHome(true)")
            page.wait_for_timeout(400)
            assert page.locator(marker).count() >= 1, \
                f"a background poll replaced {opener} with Home"
            assert "Good " not in page.text_content(".home-head h1"), \
                "the greeting is on screen — Home won"
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


def test_the_one_button_a_screen_is_asking_for_carries_the_accent(serve, page,
                                                                  monkeypatch):
    """This stylesheet's own rule is that colour is a signal — green = running,
    blue = you can click it — and the primary button was `--ink`: in dark, a white
    slab, the brightest thing on the page and the only thing on it saying nothing.

    Asserted as "the accent, and specifically not the text colour", because the
    failure mode is someone reaching for maximum contrast again.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#btn-new")
        got = page.eval_on_selector("#btn-new", """el => {
            const cs = getComputedStyle(el), root = getComputedStyle(document.documentElement);
            const swatch = (v) => { const d = document.createElement('div');
              d.style.color = v.trim(); document.body.append(d);
              const c = getComputedStyle(d).color; d.remove(); return c; };
            return { bg: cs.backgroundColor,
                     accent: swatch(root.getPropertyValue('--accent-fill')),
                     ink: swatch(root.getPropertyValue('--ink')),
                     shadow: cs.boxShadow };
        }""")
        assert got["bg"] == got["accent"], f"primary button is {got['bg']}, not the accent"
        assert got["bg"] != got["ink"], "the primary button is raw luminance again"
        assert got["shadow"] != "none", "and it does not sit above the page"
        assert page.errors == [], page.errors


def test_a_burst_of_log_lines_costs_one_layout_not_one_per_line(serve, page,
                                                                monkeypatch):
    """Every arriving line used to rescan the whole 3000-entry buffer to rebuild
    the service dropdown, append a row, and then READ scrollHeight to follow —
    which forces a synchronous layout. The stream opens with tail=300, so that was
    300 forced reflows back to back before a chatty container had even started.
    That is the one-to-two second freeze, and the scroll thrashing with it.

    Driven through the page's own handler with a burst of 400 lines, and asserted
    on the thing that actually caused it: how many times the DOM was touched.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click(".wrow")
        page.wait_for_selector("#d-body")
        page.locator(".tab", has_text="Logs").click()
        page.wait_for_selector("#logview")

        got = page.evaluate("""async () => {
            const box = document.querySelector('#logview');
            let appends = 0;
            const real = box.append.bind(box);
            box.append = (...a) => { appends++; return real(...a); };
            const t0 = performance.now();
            for (let i = 0; i < 400; i++) {
              logv.buf.push({ ts: '12:00:00', level: 'info',
                              service: 'rocketchat', msg: 'line ' + i });
              queueLog(logv.buf[logv.buf.length - 1]);
            }
            const queued = performance.now() - t0;
            await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
            return { appends, queued, rows: box.childElementCount };
        }""")
        assert got["rows"] >= 400, "the lines did not reach the view"
        # One insertion for the whole burst, not four hundred.
        assert got["appends"] <= 2, f"{got['appends']} DOM insertions for 400 lines"
        assert page.errors == [], page.errors


def test_scrolling_up_in_the_logs_offers_one_press_back_to_the_latest(serve, page,
                                                                      monkeypatch):
    """Scrolling up detaches you from the stream — otherwise it drags you back
    mid-sentence — so there has to be one press that returns. It counts what
    arrived while you were reading, which is the thing you want to know before
    deciding to go back at all.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click(".wrow")
        page.wait_for_selector("#d-body")
        page.locator(".tab", has_text="Logs").click()
        page.wait_for_selector("#logview")
        feed = """(n) => new Promise(r => {
            for (let i = 0; i < n; i++) {
              const e = { ts: '12:00:00', level: 'info', service: 'rocketchat',
                          msg: 'line ' + i };
              logv.buf.push(e); queueLog(e);
            }
            requestAnimationFrame(() => requestAnimationFrame(r));
        })"""
        page.evaluate(feed, 200)
        # at the bottom, following: nothing to offer
        assert page.locator("#log-jump").is_hidden()

        page.evaluate("() => { document.querySelector('#logview').scrollTop = 0; }")
        page.wait_for_selector("#log-jump:not([hidden])")
        assert page.evaluate("() => logv.follow") is False, \
            "the stream is still dragging the reader back"
        page.evaluate(feed, 5)
        assert page.locator("#log-jump").inner_text() == "↓ 5 new"

        page.click("#log-jump")
        page.wait_for_selector("#log-jump", state="hidden")
        assert page.evaluate("() => logv.follow") is True, "one press did not re-attach"
        assert page.evaluate(
            "() => { const b = document.querySelector('#logview');"
            "        return b.scrollHeight - b.scrollTop - b.clientHeight < 24; }")
        assert page.errors == [], page.errors


def test_a_running_workspace_on_home_offers_both_places_it_could_mean(serve, page,
                                                                      monkeypatch):
    """Two destinations live in this row and they are genuinely different: the row
    opens the WORKSPACE (rc-repro's page about it) and Open goes to Rocket.Chat
    itself. The old row only did the first, which made reaching the actual chat
    server — the thing everybody is here for — a two-step.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector(".wsrow")
        row = page.locator(".wsrow").first
        assert "t1234" in row.locator(".wsrow-t b").inner_text()
        assert row.locator(".wsrow-m").inner_text() == "7.4.1 · base · :3001"
        assert "2h" in row.locator(".wsrow-up").inner_text()

        # Open leaves for Rocket.Chat, in a new tab
        opener = row.locator("a", has_text="Open")
        assert opener.get_attribute("target") == "_blank"
        # The workspace's ROOT_URL, verbatim -- no trailing slash, because nothing
        # normalises it through URL.toString() any more.
        assert opener.get_attribute("href") == "http://localhost:3001", \
            opener.get_attribute("href")

        # and the row itself stays inside rc-repro
        row.locator(".wsrow-main").click()
        page.wait_for_selector("#d-body")
        assert "t1234" in page.text_content(".d-head")
        assert page.errors == [], page.errors


def test_a_card_is_not_the_same_colour_as_the_thing_it_stands_on(serve, page,
                                                                 monkeypatch):
    """The stage carried `--panel` and so does every card on it, which made a card
    an outline drawn on a field of its own colour — and left 242px below the last
    one reading as a hole in the panel rather than as page.

    The stage is ground now. The property is simply that the two differ, which is
    what "in a box" has to mean.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_function(
            "() => document.querySelector('#detail .hcard') !== null")
        got = page.evaluate("""() => {
            const seen = (el) => {           // the first ancestor that paints
              for (let n = el; n; n = n.parentElement) {
                const c = getComputedStyle(n).backgroundColor;
                if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c;
              }
              return null;
            };
            const card = document.querySelector('#detail .hcard');
            return { card: getComputedStyle(card).backgroundColor,
                     ground: seen(document.querySelector('#detail')),
                     rail: getComputedStyle(document.querySelector('.rail')).backgroundColor };
        }""")
        assert got["card"] != got["ground"], \
            f"card and its ground are both {got['card']} — the box is only a border"
        # the rail stays a surface; it is the well between the panes that is ground
        assert got["rail"] == got["card"], "the rail stopped agreeing with the cards"
        assert page.errors == [], page.errors


def test_reduced_motion_keeps_every_state_and_removes_the_travel(serve, browser,
                                                                 monkeypatch):
    """The polish pass put a transition on everything you can touch. A person who
    has asked their OS for less motion must still get every hover and press state —
    just instantly. Durations rather than `none`, so `transitionend` still fires.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    ctx = browser.new_context(reduced_motion="reduce")
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    try:
        with serve() as s:
            _sign_in(pg, s.url)
            pg.wait_for_selector("#btn-new")
            dur = pg.eval_on_selector("#btn-new",
                                      "el => getComputedStyle(el).transitionDuration")
            # every declared duration collapses; Chromium prints 1e-05s for .01ms
            assert all(float(d.strip().rstrip("s")) <= 0.001 for d in dur.split(",")), dur
            # the state itself is untouched — still the accent, still raised
            assert pg.eval_on_selector(
                "#btn-new", "el => getComputedStyle(el).boxShadow") != "none"
            assert pg.errors == [], pg.errors
    finally:
        ctx.close()


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
        assert row.first.get_attribute("href") == "http://keycloak:8085"
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


# --- plain http on a real hostname (the --insecure / --bind 0.0.0.0 box) -----------
#
# Every other test here drives http://127.0.0.1, which browsers class as
# "potentially trustworthy" -- and that one property is why the whole suite, and
# every local run, was blind to the defect below. A shared box reached by NAME over
# plain http is not trustworthy, so the browser omits `Sec-Fetch-*` entirely, and
# the cross-site guard falls through to `Origin`.

PUBLIC_NAME = "rcrepro.support.example.com"


@pytest.fixture
def public_browser():
    """A browser that resolves a real-looking hostname to the loopback server.

    `--host-resolver-rules` is what makes the origin `http://<name>:<port>` rather
    than `http://127.0.0.1:<port>`, without touching /etc/hosts or needing DNS.
    """
    with sync_playwright() as p:
        b = p.chromium.launch(
            args=[f"--host-resolver-rules=MAP {PUBLIC_NAME} 127.0.0.1"])
        yield b
        b.close()


@pytest.fixture
def public_page(public_browser):
    ctx = public_browser.new_context()
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    ctx.close()


@pytest.fixture
def serve_public(tmp_path, monkeypatch):
    """As `serve`, but answering to the public name as well as the loopback one."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def _start(**kw):
        app = create_app(allow_hosts=["127.0.0.1", PUBLIC_NAME], **kw)
        port = _free_port()
        srv = _Server(app, port)
        srv.public_url = f"http://{PUBLIC_NAME}:{port}"
        return srv
    return _start


def test_signing_in_works_over_plain_http_on_a_real_hostname(serve_public, public_page):
    """The defect a support engineer hit on a shared AWS box, and the reason the
    GUI could not be used there at all.

    `rc-repro serve --bind 0.0.0.0 --allow-host <name> --insecure` serves the login
    over plain http on a public name. A browser attaches `Sec-Fetch-*` only to a
    potentially trustworthy URL, which that is not -- so the guard fell through to
    `Origin`, and the page's own `Referrer-Policy: no-referrer` had already nulled
    it (per Fetch, a non-CORS POST's Origin follows the referrer policy). The guard
    cannot match "null" against the allow-list, so every sign-in was answered
    `{"error": "cross-site request refused"}` and there was no way into the GUI.

    Nothing localhost-shaped reproduces it: 127.0.0.1 IS potentially trustworthy,
    so there the `Sec-Fetch-Site: same-origin` branch answers first and the null
    Origin is never consulted.
    """
    usersvc.add("alice", PASSWORD, role="admin")
    with serve_public() as s:
        _sign_in(public_page, s.public_url)
        public_page.wait_for_load_state()
        # Checked before waiting on any selector, so the refusal reports itself
        # instead of arriving 30s later as "#repros never appeared".
        assert "cross-site request refused" not in public_page.content(), \
            "the sign-in form POST was refused as cross-site"
        public_page.wait_for_selector("#repros")
        # `#whoami` is filled by app.js after /api/session answers, so reading it
        # the moment #repros exists is a race -- it passed on a fast laptop and
        # failed in CI with `assert '' == 'alice'`. Every other test in this file
        # waits for the :not([hidden]) form; this one has to as well.
        public_page.wait_for_selector("#whoami:not([hidden])")
        assert public_page.text_content("#whoami").strip() == "alice"
        assert public_page.errors == [], public_page.errors


def test_a_write_from_the_spa_survives_plain_http_on_a_real_hostname(serve_public,
                                                                    public_page):
    """The same missing `Sec-Fetch-*` reaches every `fetch()` the SPA makes.

    A same-origin POST/DELETE also carries its Origin per the referrer policy, so
    with `no-referrer` every write from the dashboard was a 403 too -- signing in
    was merely the first thing that could not be done.

    The pass is "the guard did not eat it", not a particular status. Asserting 404
    tied this to the engine: resolve_name() shells out to docker, so without it the
    handler answers 502 and the test failed for a reason it is not about.
    """
    usersvc.add("alice", PASSWORD, role="admin")
    with serve_public() as s:
        _sign_in(public_page, s.public_url)
        public_page.wait_for_load_state()
        # Pre-fix this is where it stops: the login itself was refused, so the
        # write below could not even be reached to be measured.
        assert "cross-site request refused" not in public_page.content(), \
            "the sign-in form POST was refused as cross-site"
        public_page.wait_for_selector("#repros")
        res = public_page.evaluate(
            """async () => {
                const r = await fetch('/api/repros/does-not-exist', {
                    method: 'DELETE', credentials: 'same-origin' });
                return {status: r.status, text: (await r.text()).slice(0, 200)};
            }""")
        assert res["status"] != 403, \
            f"the guard refused a same-origin write ({res['status']}: {res['text']})"
        assert "cross-site" not in res["text"], res["text"]


def test_a_link_row_shows_the_workspaces_root_url_verbatim(serve_public, public_page,
                                                          monkeypatch):
    """One workspace, one address, everywhere: the ROOT_URL the server reported.

    The panel used to show two. `localUrl()` swapped `localhost` for whatever host
    the GUI was loaded from -- applied to the href but not the label, so "What is in
    this workspace" read http://localhost:3000 while the URL row below it read
    http://<gui-host>:3000/. The rewrite is gone entirely: ROOT_URL is what Rocket
    .Chat itself is configured with, so it is the address that identifies the
    workspace, and a number that appears in no config file is not an improvement on
    it.

    Never caught because `_fake_detail()` carries `"links": []`, so no browser test
    had ever rendered one of these rows.
    """
    d = _fake_detail()
    d["links"] = [{"label": "Rocket.Chat", "url": "http://localhost:3001", "kind": ""}]
    _stub_lifecycle(monkeypatch, d)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve_public() as s:
        _sign_in(public_page, s.public_url)
        public_page.wait_for_selector("#repros")
        public_page.wait_for_selector("#whoami:not([hidden])")
        public_page.click("text=t1234")
        public_page.wait_for_selector(".linkrow")
        row = public_page.locator(".linkrow").first
        shown = row.locator(".l-u").inner_text().strip()
        href = row.get_attribute("href")
        assert shown == "http://localhost:3001", shown
        assert href == "http://localhost:3001", href
        assert shown == href, "the row must open exactly what it displays"
        # The GUI is being viewed at a public name; that must not leak into the url.
        assert PUBLIC_NAME not in shown and PUBLIC_NAME not in href
        assert public_page.errors == [], public_page.errors


def test_a_placeholder_url_in_a_link_row_is_left_alone(serve_public, public_page,
                                                       monkeypatch):
    """A preset note can name a place as `<repro-url>/livechat` rather than a real
    url. That is not a URL and must not go through the host rewrite, which would
    turn it into `http://<host>/<repro-url>/livechat`."""
    d = _fake_detail()
    d["links"] = [{"label": "Livechat", "url": "<repro-url>/livechat", "kind": ""}]
    _stub_lifecycle(monkeypatch, d)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve_public() as s:
        _sign_in(public_page, s.public_url)
        public_page.wait_for_selector("#repros")
        public_page.wait_for_selector("#whoami:not([hidden])")
        public_page.click("text=t1234")
        public_page.wait_for_selector(".linkrow")
        shown = public_page.locator(".linkrow").first.locator(".l-u").inner_text().strip()
        assert shown == "<repro-url>/livechat", shown
        assert public_page.errors == [], public_page.errors


def test_the_workspace_row_says_kubernetes_and_stays_quiet_about_docker(
        serve, page, monkeypatch):
    """A Kubernetes workspace must never look like a Compose one in the list.

    Which commands refuse, where the data lives and how to reach it all differ, and
    the row was the one place the two were indistinguishable. Shown only when it is
    NOT the default: Compose is the overwhelming majority, so labelling every row
    "docker" would be noise on the common case and make the rare one HARDER to spot.
    """
    from rc_repro.services import lifecycle as lc

    rows = [dict(_fake_detail(name="kube-one"), runtime="kubernetes"),
            dict(_fake_detail(name="compose-one"), runtime="docker")]
    monkeypatch.setattr(lc, "list_repros", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(lc, "detail", lambda name: dict(rows[0]))
    monkeypatch.setattr(lc, "resolve_name", lambda name: name)
    usersvc.add("alice", PASSWORD, role="admin")

    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.wait_for_function(
            "() => document.querySelector('#repros').textContent.includes('kube-one')")
        kube = page.locator(".wrow", has_text="kube-one").locator(".meta").inner_text()
        compose = page.locator(".wrow", has_text="compose-one").locator(".meta").inner_text()

    assert "k8s" in kube, kube
    assert "docker" not in compose and "k8s" not in compose, compose


def test_the_create_dialog_offers_a_runtime_and_follows_it(serve, page, monkeypatch):
    """You could not create a Kubernetes workspace from the GUI at all -- the axes
    existed on CreateReq and in the CLI, and the dialog had no control for either.

    The options come from /api/settings, never a list written in JS: the same
    reasoning as the privileged fields, where two places deciding the same thing
    made the dialog offer a field the API rejected.

    And picking Kubernetes has to CHANGE the form, not just the value sent:
    multi-instance does not exist there, microservices does not exist on Compose,
    the MongoDB operator is Kubernetes-only, and HTTPS is refused by the service
    layer -- so offering it would produce a create that fails on submit.
    """
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")

    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#btn-new")
        page.click("#btn-new")
        page.wait_for_selector("#create-dialog[open]")

        # Compose is the default and its arrangement list is Compose's.
        assert page.locator("#runtime-select").input_value() == "docker"
        deployments = page.locator("#deployment-select option").all_inner_texts()
        assert any("monolith" in d for d in deployments), deployments
        assert any("multi-instance" in d for d in deployments), deployments
        assert not any("microservices" in d for d in deployments), deployments
        assert page.locator("#mongo-operator-row").is_hidden()

        page.select_option("#runtime-select", "kubernetes")
        deployments = page.locator("#deployment-select option").all_inner_texts()
        assert any("microservices" in d for d in deployments), deployments
        # The RUNTIME'S default, not the one Compose had selected: `--runtime k8s`
        # on the CLI defaults to microservices, and the two must not disagree.
        assert page.locator("#deployment-select").input_value() == "microservices"
        assert not any("multi-instance" in d for d in deployments), deployments
        # Kubernetes-only control appears; HTTPS, which the service layer refuses
        # on this runtime, goes away.
        assert page.locator("#mongo-operator-row").is_visible()
        # `hidden` rather than is_visible: the HTTPS block lives inside the
        # Advanced disclosure, so is_visible() would be answering about the
        # twisty, not about the runtime.
        assert page.eval_on_selector("#create-https-block", "e => e.hidden") is True
        assert "Kubernetes" in page.locator("#runtime-hint").inner_text() or \
            page.locator("#runtime-hint").inner_text() != ""

        # An arrangement the user PICKS must stick. Wiring the runtime handler to
        # this select too made every choice snap back to the runtime default:
        # picking monolith on Kubernetes created a microservices workspace, and the
        # dialog looked correct throughout. Found by clicking, not by a test.
        page.select_option("#deployment-select", "monolith")
        assert page.locator("#deployment-select").input_value() == "monolith"
        assert page.locator("#replicas-row").is_hidden(), \
            "replicas mean nothing on a monolith"
        page.select_option("#deployment-select", "microservices")
        assert page.locator("#deployment-select").input_value() == "microservices"

        # Back to Compose and HTTPS returns -- the hiding is a consequence of the
        # choice, not a one-way door.
        page.select_option("#runtime-select", "docker")
        assert page.eval_on_selector("#create-https-block", "e => e.hidden") is False
        assert page.locator("#mongo-operator-row").is_hidden()


def test_creating_a_kubernetes_workspace_sends_the_axes(serve, page, monkeypatch):
    """The payload is what matters: a dialog that renders the control and posts
    nothing extra would look right and build a Compose workspace."""
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")

    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#btn-new")
        sent = []
        page.route("**/api/repros", lambda route: (
            sent.append(route.request.post_data),
            route.fulfill(status=200, content_type="application/json",
                          body='{"job_id": "j1"}')))
        page.click("#btn-new")
        page.wait_for_selector("#create-dialog[open]")
        page.fill("input[name=version]", "8.5.1")
        page.select_option("#runtime-select", "kubernetes")
        page.select_option("#deployment-select", "microservices")
        page.check("input[name=mongo_operator]")
        page.click("#create-submit")
        for _ in range(40):
            if sent:
                break
            page.wait_for_timeout(100)

    assert sent, "the create was never POSTed"
    import json as _json
    body = _json.loads(sent[0])
    assert body["runtime"] == "kubernetes", body
    assert body["deployment"] == "microservices", body
    assert body["mongo_operator"] is True, body


# --- reached from another machine ----------------------------------------------------
#
# The EC2 shape: one `serve` on a box everybody reaches over its address, and nobody
# has a shell on it. Every link this GUI renders is built from `localhost`, which for
# a browser somewhere else names the READER'S machine -- so the panel has to say what
# the address means from where the reader is standing.
#
# Nothing is detected to work that out. `remoteHost()` reads location.hostname, which
# is the address in the browser's own URL bar, and `serve_public`'s host-resolver rule
# makes that a real name rather than 127.0.0.1 -- the same fixture the plain-http
# sign-in defect needed, for the same reason.

def test_the_panel_says_that_localhost_means_the_readers_own_machine(
        serve_public, public_page, monkeypatch):
    """And says how to fix it -- without touching the URL it is talking about.

    ROOT_URL is Rocket.Chat's own advertised address and rc-repro's 71 internal API
    calls go through it, so the notice is ADDITIVE: the URL row must still read
    exactly what the workspace was built with.
    """
    _stub_lifecycle(monkeypatch, _fake_detail(bind_host="127.0.0.1"))
    usersvc.add("alice", PASSWORD, role="admin")
    with serve_public() as s:
        _sign_in(public_page, s.public_url)
        public_page.wait_for_selector("#repros")
        public_page.click("text=t1234")
        public_page.wait_for_selector("#d-body")
        public_page.wait_for_selector("#d-body .banner.warn")
        said = public_page.text_content("#d-body .banner.warn")
        assert PUBLIC_NAME in said, said
        assert "0.0.0.0" in said, said
        assert "admin/admin123" in said, said
        # THE URL IS UNTOUCHED. This is the whole constraint on the feature.
        assert public_page.text_content("#d-body .urlbox").find(
            "http://localhost:3001") >= 0
        assert public_page.errors == [], public_page.errors


def test_a_wide_bound_workspace_is_given_the_address_that_works_from_here(
        serve_public, public_page, monkeypatch):
    """Bound to 0.0.0.0 there is nothing to warn about -- only an address to state.

    Stated as text with a copy button rather than as a link: the links are built from
    the workspace's own root_url and stay that way.
    """
    _stub_lifecycle(monkeypatch, _fake_detail(bind_host="0.0.0.0"))
    usersvc.add("alice", PASSWORD, role="admin")
    with serve_public() as s:
        _sign_in(public_page, s.public_url)
        public_page.wait_for_selector("#repros")
        public_page.click("text=t1234")
        public_page.wait_for_selector("#d-body")
        public_page.wait_for_function(
            "() => document.querySelector('#d-body').textContent"
            ".includes('From this machine')")
        body = public_page.text_content("#d-body")
        assert f"http://{PUBLIC_NAME}:3001" in body, body
        # Not the amber one: nothing is being asked of the reader.
        assert public_page.query_selector("#d-body .banner.warn") is None
        assert public_page.errors == [], public_page.errors


def test_a_local_browser_is_told_none_of_this(serve, page, monkeypatch):
    """On a laptop every localhost link is correct, so the notice would be noise.

    The same assertion proves the loopback branch of `remoteHost()`: this server is
    reached at 127.0.0.1, which is exactly what must NOT count as remote.
    """
    _stub_lifecycle(monkeypatch, _fake_detail(bind_host="127.0.0.1"))
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body .urlbox")
        assert page.query_selector("#d-body .banner.warn") is None
        assert "From this machine" not in page.text_content("#d-body")
        assert page.evaluate("remoteHost()") == ""
        assert page.errors == [], page.errors


def test_the_create_dialog_warns_a_remote_reader_about_the_default_bind(
        serve_public, public_page, monkeypatch):
    """The one fact a browser-only user cannot find out any other way: the workspace
    they are about to create will answer on the server and nowhere else."""
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve_public() as s:
        _sign_in(public_page, s.public_url)
        public_page.wait_for_selector("#repros")
        public_page.click("#btn-new")
        public_page.wait_for_selector("#create-dialog[open]")
        public_page.wait_for_selector("#create-bind-hint:not([hidden])")
        said = public_page.text_content("#create-bind-hint")
        assert "0.0.0.0" in said, said
        assert PUBLIC_NAME in said, said
        assert public_page.errors == [], public_page.errors


def test_the_create_dialog_stays_quiet_on_a_laptop(serve, page, monkeypatch):
    _stub_lifecycle(monkeypatch)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("#btn-new")
        page.wait_for_selector("#create-dialog[open]")
        assert page.is_hidden("#create-bind-hint")
        assert page.errors == [], page.errors


# --- the panel against a Kubernetes workspace ---------------------------------------
#
# `serve` exists so a support engineer needs no shell, and on Kubernetes that is the
# only way in for anybody without kubectl. Three of these tabs rendered a confident
# wrong answer on that runtime and one rendered nothing at all.

def _kube_detail(name="t1234", **over):
    over.setdefault("containers", [])
    return _fake_detail(name, runtime="kubernetes", deployment="microservices",
                        namespace=f"rc-repro-{name}", **over)


def test_the_env_tab_renders_on_kubernetes_where_it_used_to_be_blank(
        serve, page, monkeypatch):
    """It read `detail().env`, which the Kubernetes branch of detail() never set --
    so the tab rendered an empty table and a hint, on a workspace whose environment
    the CLI could print. It fetches /env now, which is the one seam both front-ends
    share."""
    from rc_repro.services import envvars as envsvc

    _stub_lifecycle(monkeypatch, _kube_detail())
    monkeypatch.setattr(envsvc, "current", lambda name: {
        "name": name, "overrides": ["MY_FLAG"],
        "env": [{"key": "ROOT_URL", "value": "http://localhost:3001", "override": False},
                {"key": "MY_FLAG", "value": "1", "override": True},
                {"key": "MONGO_URL", "value": "********", "override": False}]})
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        page.click("button.tab:has-text('Env vars')")
        page.wait_for_function(
            "() => document.querySelector('#d-body').textContent.includes('MY_FLAG')")
        body = page.text_content("#d-body")
        assert "ROOT_URL" in body and "MONGO_URL" in body
        # Every write here is refused by the server with a helm hint, so the controls
        # that could only produce a red toast are not rendered -- and the helm route
        # is given instead.
        assert page.query_selector("#d-body button.danger") is None
        assert "Set + restart" not in body
        assert "helm -n rc-repro-t1234 upgrade" in body, body
        assert page.errors == [], page.errors


def test_the_env_tab_still_offers_the_controls_on_compose(serve, page, monkeypatch):
    from rc_repro.services import envvars as envsvc

    _stub_lifecycle(monkeypatch)
    monkeypatch.setattr(envsvc, "current", lambda name: {
        "name": name, "overrides": [],
        "env": [{"key": "ROOT_URL", "value": "http://localhost:3001", "override": False}]})
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        page.click("button.tab:has-text('Env vars')")
        page.wait_for_function(
            "() => document.querySelector('#d-body').textContent.includes('ROOT_URL')")
        assert "Set + restart" in page.text_content("#d-body")
        assert page.query_selector("#d-body button.danger") is not None
        assert page.errors == [], page.errors


def test_the_env_tab_shows_the_refusal_rather_than_an_empty_table(
        serve, page, monkeypatch):
    """A stopped Kubernetes workspace has no container to read the environment out
    of, and the server says so. That message is the answer -- rendering an empty
    table instead would look like a workspace with no environment."""
    from rc_repro import errors as errs
    from rc_repro.services import envvars as envsvc

    def refuse(name):
        raise errs.NotReadyError("no Rocket.Chat pod in rc-repro-t1234 to read the "
                                 "environment from — is 't1234' running?")

    _stub_lifecycle(monkeypatch, _kube_detail(state="stopped"))
    monkeypatch.setattr(envsvc, "current", refuse)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        page.click("button.tab:has-text('Env vars')")
        page.wait_for_function(
            "() => document.querySelector('#d-body').textContent"
            ".includes('no Rocket.Chat pod')")
        assert page.errors == [], page.errors


def test_the_containers_tab_shows_pods_and_says_so_on_kubernetes(
        serve, page, monkeypatch):
    """It printed "No containers — this repro is down." under a RUNNING Kubernetes
    workspace, because the payload's list was always empty on that runtime."""
    _stub_lifecycle(monkeypatch, _kube_detail(containers=[
        {"service": "rocketchat-rocketchat-6d9-abc", "state": "running",
         "status": "1/1 ready", "health": "healthy"},
        {"service": "rocketchat-mongodb-0", "state": "pending",
         "status": "ImagePullBackOff", "health": ""}]))
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        page.click("button.tab:has-text('Containers')")
        page.wait_for_selector("#d-body table.dtable")
        body = page.text_content("#d-body")
        assert "rocketchat-mongodb-0" in body
        # The reason a pod cannot start, which the GUI had no way to show at all.
        assert "ImagePullBackOff" in body
        assert "No containers" not in body
        assert "Pods, not containers" in body
        # The column is named for what is in it.
        assert page.text_content("#d-body table.dtable th") == "pod"
        assert page.errors == [], page.errors


def test_the_live_chart_stops_asking_when_the_answer_is_a_refusal(
        serve, page, monkeypatch, tmp_path):
    """kind ships no metrics-server, so /stats answers 409 forever. The chart used to
    swallow it and repaint an empty box every three seconds under a title that says
    "live", throwing away the server's own instructions for fixing it."""
    import json as _json

    from rc_repro import errors as errs
    from rc_repro.services import k8s as k8ssvc

    (tmp_path / "repros" / "t1234").mkdir(parents=True)
    (tmp_path / "repros" / "t1234" / "repro.json").write_text(_json.dumps({
        "name": "t1234", "project": "p", "rc_version": "7.4.1", "rc_image": "i",
        "mongo_tag": "7.0", "mongo_flavor": "official", "preset": "default",
        "root_url": "http://localhost:3001", "host_port": 3001,
        "version_source": "x", "extra": {"runtime": "kubernetes"}}))
    _stub_lifecycle(monkeypatch, _kube_detail())

    def no_metrics(name, **kw):
        raise errs.NotReadyError("this cluster has no metrics-server, so there is "
                                 "nothing to read CPU and memory from. Install it "
                                 "with:\n  kubectl apply -f https://example/x.yaml")

    monkeypatch.setattr(k8ssvc, "pod_metrics", no_metrics)
    usersvc.add("alice", PASSWORD, role="admin")
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_function(
            "() => document.querySelector('#chart')"
            " && document.querySelector('#chart').textContent.includes('metrics-server')")
        # And it stopped asking, rather than repainting the same refusal forever.
        assert page.evaluate("dstate.statsTimer") is None
        assert page.errors == [], page.errors


def test_fake_detail_matches_the_real_payload(monkeypatch, tmp_path):
    """The payload-shape trap, closed. Every browser test above renders from
    `_fake_detail`, so a key that differs from what `lifecycle.detail()` really
    returns breaks the panel in production with this whole file green. It had drifted
    on three keys before this existed.

    Both branches of detail() are compared, because they do not return the same set.
    """
    import json as _json

    from rc_repro import runner
    from rc_repro.services import k8s as k8ssvc
    from rc_repro.services import lifecycle as lcsvc

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    (tmp_path / "repros" / "cmp").mkdir(parents=True)
    (tmp_path / "repros" / "cmp" / "repro.json").write_text(_json.dumps({
        "name": "cmp", "project": "p", "rc_version": "7.4.1", "rc_image": "i",
        "mongo_tag": "7.0", "mongo_flavor": "official", "preset": "default",
        "root_url": "http://localhost:3001", "host_port": 3001,
        "version_source": "x", "extra": {}}))
    (tmp_path / "repros" / "cmp" / "docker-compose.yml").write_text(
        "services:\n  rocketchat:\n    image: i\n    ports:\n"
        "    - 127.0.0.1:3001:3000\n    environment:\n      ROOT_URL: x\n")
    (tmp_path / "repros" / "kub").mkdir(parents=True)
    (tmp_path / "repros" / "kub" / "repro.json").write_text(_json.dumps({
        "name": "kub", "project": "p", "rc_version": "7.4.1", "rc_image": "i",
        "mongo_tag": "7.0", "mongo_flavor": "official", "preset": "default",
        "root_url": "http://localhost:3002", "host_port": 3002,
        "version_source": "x",
        "extra": {"runtime": "kubernetes", "namespace": "rc-repro-kub"}}))
    # Neither branch may reach a daemon or a cluster from a unit test.
    monkeypatch.setattr(runner, "docker_available", lambda **_k: False)
    monkeypatch.setattr(lcsvc, "kubernetes_state", lambda name, meta: "running")
    monkeypatch.setattr(k8ssvc, "pod_rows", lambda name, **kw: [])

    fake = _fake_detail()
    for name in ("cmp", "kub"):
        real = lcsvc.detail(name)
        missing = sorted(set(real) - set(fake))
        assert not missing, (
            f"detail({name!r}) returns keys the browser fixture does not have: "
            f"{missing} — every test in this file renders from the fixture, so the "
            f"panel can break in production with this file green")
        for key in sorted(set(real) & set(fake)):
            if real[key] is None or fake[key] is None:
                continue
            assert type(real[key]) is type(fake[key]), (
                f"detail({name!r})[{key!r}] is {type(real[key]).__name__}, the "
                f"fixture has {type(fake[key]).__name__}")


def test_a_credential_note_is_not_labelled_a_sudo_setup_step(serve, page, monkeypatch):
    """The setup block said "Do this once, on your machine … Needs sudo." over
    whatever the first indented note happened to be.

    For `ldap` that is a phpLDAPadmin CREDENTIAL, so the panel told an ldap reader to
    sudo their way through "log in with DN cn=admin,dc=example,dc=com / admin" -- a
    sentence that is not true of anything. `stopped`, so the live chart's /stats poll
    stays out of a test about text.
    """
    from rc_repro import presets as presets_mod

    notes = list(presets_mod.load("ldap").notes or [])
    assert any(n.startswith("    ") for n in notes), \
        "the indented credential is the condition under test"
    usersvc.add("alice", PASSWORD, role="admin")
    _stub_lifecycle(monkeypatch, _fake_detail(state="stopped", preset="ldap",
                                              notes=notes))
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body")
        body = page.text_content("#d-body")
        assert "cn=admin,dc=example,dc=com" in body, "the credential is still shown"
        assert "Needs sudo" not in body, \
            "a credential was labelled a setup step that needs sudo"
        assert page.errors == [], page.errors


def test_the_one_preset_with_a_real_setup_step_keeps_it(serve, page, monkeypatch):
    """`oidc` cannot log anyone in until `127.0.0.1  keycloak` is in /etc/hosts, and
    the introducing sentence says exactly that -- which is what now qualifies a note
    as a setup step."""
    from rc_repro import presets as presets_mod

    usersvc.add("alice", PASSWORD, role="admin")
    _stub_lifecycle(monkeypatch, _fake_detail(
        state="stopped", preset="oidc",
        notes=list(presets_mod.load("oidc").notes or [])))
    with serve() as s:
        _sign_in(page, s.url)
        page.wait_for_selector("#repros")
        page.click("text=t1234")
        page.wait_for_selector("#d-body .setup", timeout=15000)
        setup = page.text_content("#d-body .setup")
        assert "127.0.0.1  keycloak" in setup, setup
        assert "Needs sudo" in setup
        assert page.errors == [], page.errors
