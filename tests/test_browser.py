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
    usersvc._failures.clear()

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
