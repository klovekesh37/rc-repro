"""Unit tests for GUI accounts (services/users.py).

These lock in the properties that make a shared deployment safe: passwords are
never recoverable from the file, an unknown name costs the same work as a known
one, a correct password is never refused by a counter, failed attempts are
recorded, and revoking a credential takes effect at once.
"""

from __future__ import annotations

import time

import pytest

from rc_repro import errors
from rc_repro.services import users


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    yield


GOOD = "correct-horse-battery"


def _legacy_account(name: str, password: str = GOOD) -> None:
    """Write an account line the way a version BEFORE roles existed did: no role
    column at all. `users.add()` cannot produce this any more, and the whole point
    of the blank-means-admin rule is what happens to these."""
    rows = users._read()
    rows[name] = (users.hash_password(password), "2026-01-01", "")
    users._write(rows)


# --- the file -------------------------------------------------------------------

def test_a_new_install_has_no_users():
    assert users.any_users() is False
    assert users.list_users() == []


def test_add_then_verify():
    users.add("alice", GOOD)
    assert users.any_users() is True
    assert users.verify("alice", GOOD) is True
    assert users.verify("alice", "wrong-password-x") is False


def test_the_file_is_owner_only_and_holds_no_password():
    users.add("alice", GOOD)
    path = users.users_file()
    assert oct(path.stat().st_mode)[-3:] == "600"
    text = path.read_text()
    assert GOOD not in text, "the password itself must never be written"
    assert "scrypt$" in text


def test_the_created_date_does_not_collide_with_the_field_delimiter():
    """`:` separates fields, and an ISO timestamp contains colons — which split
    straight into the role column and displayed as garbage."""
    users.add("alice", GOOD)
    (only,) = users.list_users()
    assert ":" not in only.created_at
    assert only.role == "admin", "the first account, not a date fragment"
    assert len(only.created_at) == 10   # YYYY-MM-DD


def test_a_comment_or_blank_line_is_ignored():
    users.add("alice", GOOD)
    path = users.users_file()
    path.write_text("# a comment\n\n   \n" + path.read_text(), encoding="utf-8")
    assert [u.name for u in users.list_users()] == ["alice"]


def test_a_malformed_line_is_skipped_not_fatal():
    users.add("alice", GOOD)
    path = users.users_file()
    path.write_text(path.read_text() + "garbage-with-no-fields\n", encoding="utf-8")
    assert [u.name for u in users.list_users()] == ["alice"]
    assert users.verify("alice", GOOD) is True


def test_a_role_column_round_trips():
    """Reserved for a later readonly/admin tier; must survive a rewrite."""
    users.add("alice", GOOD, role="readonly")
    users.add("bob", GOOD, role="admin")
    by_name = {u.name: u.role for u in users.list_users()}
    assert by_name == {"alice": "readonly", "bob": "admin"}


# --- validation -------------------------------------------------------------------

def test_a_short_password_is_refused():
    with pytest.raises(errors.ValidationError, match="at least"):
        users.add("alice", "short")


def test_names_are_normalised_to_a_dns_label_rather_than_refused():
    """The name becomes part of a workspace name and therefore a hostname — but
    that is a reason to TRANSFORM it, not to refuse it, exactly as `up --name
    TICKET-1234` has always created `ticket-1234`.

    Refusing was not the cosmetic problem it looked like: `serve` also refuses to
    start on a network-reachable bind with no accounts, so `users add lucy.felix`
    failing was the whole reason a box could not be brought up.
    """
    users.add("keeper", GOOD)           # so nothing below is the last admin
    for typed, stored in (("Alice", "alice"), ("al ice", "al-ice"),
                          ("alice.b", "alice-b"), ("-alice", "alice"),
                          ("al_ice", "al-ice"), ("Lucy.Felix", "lucy-felix"),
                          ("lovekesh.kumar", "lovekesh-kumar")):
        assert users.add(typed, GOOD).name == stored
        assert users.role_of(stored), f"{typed!r} was not stored as {stored!r}"
        users.remove(stored)
    users.add("alice-2", GOOD)          # already a label: untouched


def test_a_name_that_cannot_become_a_label_is_still_refused():
    """Normalising is not a licence to invent one. Nothing to work with, and too
    long to shorten safely, are the two that survive — and they say different
    things, because they have different fixes."""
    with pytest.raises(errors.ValidationError, match="at least one letter or digit"):
        users.add("...", GOOD)
    with pytest.raises(errors.ValidationError, match="at least one letter or digit"):
        users.add("", GOOD)
    # Truncating somebody's identity silently is worse than refusing it.
    with pytest.raises(errors.ValidationError, match="the limit is 31"):
        users.add("a" * 32, GOOD)


def test_the_normalisation_is_the_one_repro_names_already_use():
    """`normalize_name` is a deliberate copy of `lifecycle.sanitize` — users.py sits
    below lifecycle and importing it would drag compose, runner and the version
    index in. This is what stops the copy drifting; if it fails, the two rules have
    diverged and an account name no longer survives becoming a workspace name."""
    from rc_repro.services.lifecycle import sanitize
    for raw in ("lucy.felix", "Lucy.Felix", "lovekesh.kumar", "al_ice", "-alice-",
                "a..b", "a  b", "TICKET-1234", "alice", "ålice", "a.b_c d"):
        assert users.normalize_name(raw) == sanitize(raw), raw


def test_a_dotted_name_signs_in_and_is_managed_by_what_was_typed():
    """The name someone is given is the one they type. Having `add` fold the dot
    while `verify` did not would hand every dotted account a login it cannot use."""
    users.add("keeper", GOOD)           # the demotion below must not be the last admin
    created = users.add("lucy.felix", GOOD)
    assert created.name == "lucy-felix"
    assert users.verify("lucy.felix", GOOD) is True, "the name as typed must work"
    assert users.verify("lucy-felix", GOOD) is True, "and so must the stored one"
    assert users.verify("lucy.felix", "wrong-password-entirely") is False
    assert users.role_of("lucy.felix") == users.role_of("lucy-felix") != ""
    users.set_role("lucy.felix", "readonly")
    assert users.role_of("lucy-felix") == "readonly"
    users.set_password("lucy.felix", "another-good-password")
    assert users.verify("lucy.felix", "another-good-password") is True
    users.remove("lucy.felix")
    assert users.role_of("lucy-felix") == ""


def test_adding_twice_is_a_conflict_not_a_silent_overwrite():
    users.add("alice", GOOD)
    with pytest.raises(errors.ConflictError, match="already exists"):
        users.add("alice", "a-different-password")
    assert users.verify("alice", GOOD) is True, "the original must be untouched"


def test_passwd_and_remove_need_an_existing_user():
    with pytest.raises(errors.NotFoundError):
        users.set_password("nobody", GOOD)
    with pytest.raises(errors.NotFoundError):
        users.remove("nobody")


# --- verification behaviour ----------------------------------------------------------

def test_an_unknown_user_still_does_the_work():
    """Returning early would let response time enumerate who exists."""
    users.add("alice", GOOD)
    t0 = time.monotonic()
    assert users.verify("ghost", GOOD) is False
    unknown = time.monotonic() - t0
    assert unknown > 0.005, f"unknown-user check returned in {unknown*1000:.1f}ms"


def test_a_wrong_password_is_refused_after_a_correct_one(tmp_path, monkeypatch):
    """There is no verification cache any more (sessions replaced it), so this is
    now just "verify does what it says" -- but the property it protects is the one
    a cache would have broken: a success must never make a later wrong password
    succeed."""
    users.add("alice", GOOD)
    assert users.verify("alice", GOOD) is True
    assert users.verify("alice", "wrong-password-x") is False


def test_changing_a_password_invalidates_the_old_one_immediately():
    users.add("alice", GOOD)
    assert users.verify("alice", GOOD) is True
    users.set_password("alice", "a-brand-new-password")
    assert users.verify("alice", GOOD) is False, "the old password still worked"
    assert users.verify("alice", "a-brand-new-password") is True


def test_removing_a_user_takes_effect_at_once():
    users.add("alice", GOOD)
    users.add("bob", GOOD, role="admin")      # so alice is not the last admin
    assert users.verify("alice", GOOD) is True
    users.remove("alice")
    assert users.verify("alice", GOOD) is False


# --- roles ------------------------------------------------------------------------

def test_a_blank_role_column_means_admin():
    """Not a default -- the migration. Every account made before roles existed has
    a blank column, and users.py has always documented it as full access. Reading
    blank as anything narrower would silently demote everyone, and on a box whose
    only account predates this it would leave ZERO admins and no way to make one
    from the GUI."""
    _legacy_account("alice")
    (only,) = users.list_users()
    assert only.role == "", "the column itself stays blank"
    assert users.role_of("alice") == "admin", "but it RESOLVES to admin"
    assert users.implicit_admins() == ["alice"]
    assert users.admins() == ["alice"]


def test_roles_round_trip_and_are_validated():
    _legacy_account("alice")
    users.add("bob", GOOD, role="readonly")
    assert users.role_of("bob") == "readonly"
    users.set_role("bob", "member")
    assert users.role_of("bob") == "member"
    assert users.implicit_admins() == ["alice"], "bob is explicit now, alice is not"
    for bad in ("root", "", "superuser", "admin;member"):
        with pytest.raises(errors.ValidationError):
            users.set_role("bob", bad)
    # Case and surrounding whitespace are tolerated and normalised -- these arrive
    # from a CLI argument and a JSON body, and rejecting " Admin" would be pedantry.
    users.set_role("bob", "  ReadOnly ")
    assert users.role_of("bob") == "readonly"


def test_the_last_admin_cannot_be_demoted_or_removed():
    """A box with no admins cannot make one from the GUI, and the repair is hand-
    editing a file most people would not know to look for."""
    users.add("alice", GOOD)                  # first account -> admin
    users.add("bob", GOOD, role="member")
    with pytest.raises(errors.ConflictError, match="only admin"):
        users.set_role("alice", "member")
    with pytest.raises(errors.ConflictError, match="only admin"):
        users.remove("alice")
    # With a second admin, both become allowed.
    users.set_role("bob", "admin")
    users.set_role("alice", "member")
    assert users.admins() == ["bob"]


def test_role_of_an_unknown_user_is_empty_not_admin():
    """The blank-means-admin rule must not apply to somebody who does not exist."""
    assert users.role_of("ghost") == ""


def test_a_correct_password_is_never_refused_by_a_counter():
    """No amount of guessing may lock the real owner out.

    An earlier version consulted a lockout counter BEFORE checking the password,
    which made the documented remedy for a compromised account the thing that
    locked its owner out: after `users passwd`, every still-open tab replayed the
    OLD credential on its four-second poll, each poll slid the window forward, and
    the new password was refused with it. Measured at the time: 232s of lockout
    after twenty minutes of one tab.

    The counter is gone now, so this is cheap to guarantee -- and worth keeping as
    a test, because it is the property that made it wrong.
    """
    users.add("alice", GOOD)
    for _ in range(40):
        users.verify("alice", "the-old-password-x", source="203.0.113.9")
    assert users.verify("alice", GOOD, source="10.0.0.4") is True
    assert users.verify("alice", GOOD, source="203.0.113.9") is True, \
        "even from the address that did the guessing"


def test_a_failed_sign_in_is_recorded():
    """Prevention stops at the per-address throttle in web/app.py; this is the
    detection that replaces the per-account counter nothing ever read.

    `attempt`, not `session`: the name on a failed sign-in is whatever the caller
    typed and belongs to nobody, so the log must not read as "alice did something".
    """
    from rc_repro.services import audit as auditsvc

    users.add("alice", GOOD)
    users.verify("alice", "wrong-password-x", source="203.0.113.9")
    users.verify("no-such-person", "wrong-password-x", source="203.0.113.9")

    lines = auditsvc.read(kind="signin")["lines"]
    assert len(lines) == 2, lines
    assert {ln["actor"] for ln in lines} == {"alice", "no-such-person"}
    for ln in lines:
        assert ln["outcome"] == "denied"
        assert ln["origin"] == "attempt", "a guessed-at name is not an actor"
        assert ln["label"] == "203.0.113.9", "the address is what you act on"


def test_a_successful_sign_in_records_nothing():
    """Only failures. A line per successful verify would double every sign-in and
    bury the ones worth seeing."""
    from rc_repro.services import audit as auditsvc

    users.add("alice", GOOD)
    users.verify("alice", GOOD, source="10.0.0.4")
    assert auditsvc.read(kind="signin")["lines"] == []


def test_a_hostile_user_name_cannot_forge_an_audit_line():
    """The name reaches the log before any validation -- deliberately, because an
    invalid name is exactly the traffic worth seeing. So the FIELD has to be safe:
    a tab forges a column and a newline forges a whole line, in the one file whose
    entire job is to say who did what."""
    from rc_repro.services import audit as auditsvc

    users.verify("evil\tadmin\tuser-add\tbackdoor\tsession\tok\nnext-line",
                 "wrong-password-x", source="203.0.113.9")
    text = auditsvc.audit_path().read_text(encoding="utf-8")
    assert text.count("\n") == 1, "the injected newline created a second line"
    lines = auditsvc.read(kind="signin")["lines"]
    assert len(lines) == 1
    assert "\t" not in lines[0]["actor"]
    assert lines[0]["outcome"] == "denied" and lines[0]["origin"] == "attempt"


def test_empty_credentials_are_refused_without_touching_the_file():
    users.add("alice", GOOD)
    assert users.verify("", GOOD) is False
    assert users.verify("alice", "") is False


def test_the_same_password_hashes_differently_each_time():
    """Per-user salt: identical passwords must not produce identical lines."""
    a, b = users.hash_password(GOOD), users.hash_password(GOOD)
    assert a != b
    assert users._check(GOOD, a) and users._check(GOOD, b)


def test_the_hash_records_its_own_cost_parameters():
    """So the cost can be raised later without invalidating existing entries."""
    hashed = users.hash_password(GOOD)
    scheme, n, r, p, _salt, _digest = hashed.split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (users._N, users._R, users._P)


def test_a_corrupt_hash_verifies_false_rather_than_raising():
    for bad in ("", "notascheme$1$2$3$4$5", "scrypt$x$y$z$q$w", "scrypt$16384$8$1$@@$@@"):
        assert users._check(GOOD, bad) is False


# --- revocation must not lag (F12) ----------------------------------------------
# Verifications USED to be cached for five minutes in a module-level dict. `users
# remove` called forget() -- in the CLI process, whose cache was empty. The
# long-lived `serve` process kept its own and only re-read the file on a MISS, so
# a removed account went on working against the running GUI for up to five
# minutes, with no signal that the documented remediation had not taken effect.
#
# The cache is gone (sessions replaced its reason for existing), so the lag cannot
# recur -- but the PROPERTY is what mattered, so it is still asserted, and now
# also for the credential the browser actually carries: the session.

def _other_process_edits(mutate) -> None:
    """Edit the users file the way a SEPARATE process would."""
    from rc_repro.services import users as usersvc
    rows = usersvc._read()
    mutate(rows)
    usersvc._write(rows)


def test_removing_a_user_takes_effect_immediately_in_a_running_server(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.services import sessions as sessionsvc
    from rc_repro.services import users as usersvc

    usersvc.add("alice", "correct-horse-battery")
    assert usersvc.verify("alice", "correct-horse-battery") is True
    token = sessionsvc.create("alice")
    assert sessionsvc.verify(token) is not None

    _other_process_edits(lambda rows: rows.pop("alice"))
    assert usersvc.verify("alice", "correct-horse-battery") is False, \
        "a removed account kept working"
    # And the session it already had must die with it -- an account that can no
    # longer sign in but is still signed in is not removed.
    sessionsvc.revoke_user("alice")
    assert sessionsvc.verify(token) is None


def test_a_changed_password_invalidates_the_old_one_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.services import users as usersvc

    usersvc.add("alice", "correct-horse-battery")
    assert usersvc.verify("alice", "correct-horse-battery") is True

    _other_process_edits(lambda rows: rows.__setitem__(
        "alice", (usersvc.hash_password("brand-new-password"), "2026-01-01", "")))
    assert usersvc.verify("alice", "correct-horse-battery") is False
    assert usersvc.verify("alice", "brand-new-password") is True


def test_verifying_an_unknown_user_derives_scrypt_exactly_once():
    """An unknown name must cost the SAME work as a known one, not double it.

    The dummy check used to be `_check(password, hash_password("dummy..."))`, which
    derives twice: once to build the throwaway hash, once to check against it. So a
    miss took twice as long as a hit and the timing channel the line exists to close
    was created by it instead -- measured at 46.6 ms unknown vs 23.7 ms known, a
    ratio of 1.97.

    Counting derivations rather than timing them: the invariant is "the same work",
    and a wall-clock assertion on a shared CI box is a flake waiting to happen.
    """
    import hashlib

    users.add("alice", GOOD)
    calls = []
    real = hashlib.scrypt

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    original, hashlib.scrypt = hashlib.scrypt, counting
    try:
        users.verify("alice", "not-the-right-password")
        known = len(calls)
        calls.clear()
        users.verify("no-such-person", "not-the-right-password")
        unknown = len(calls)
    finally:
        hashlib.scrypt = original

    assert known == 1, "a known user with a wrong password derives once"
    assert unknown == known, (
        f"an unknown user derived {unknown} times against {known} for a known one; "
        "that difference is a user-enumeration oracle")


def test_concurrent_writes_in_one_process_do_not_lose_accounts():
    """Eight threads adding at once must leave eight accounts.

    Every mutation is read-whole-file -> change -> rewrite, so without exclusion
    the later write is based on a snapshot taken before the earlier one landed and
    the earlier account vanishes -- while its caller was told it was created, and
    handed the password to prove it.
    """
    import threading

    errors_seen: list[str] = []

    def add(i):
        try:
            users.add(f"racer{i}", GOOD, role="member")
        except Exception as exc:                      # noqa: BLE001 - reported below
            errors_seen.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors_seen == []
    names = sorted(u.name for u in users.list_users())
    assert names == [f"racer{i}" for i in range(8)], \
        f"accounts were lost to a concurrent write: {names}"


def test_a_second_process_cannot_clobber_an_account(tmp_path):
    """The same race across PROCESSES, which is the one that actually happens.

    `serve` writes this file from its worker threads while somebody runs
    `rc-repro users add` in a terminal, and a thread lock says nothing about that.
    Measured before the flock existed: eight concurrent `users add` left TWO
    accounts, and the other six were reported created.

    Slower than the thread test and kept anyway -- it is the only one of the two
    that would notice the file lock being dropped.
    """
    import subprocess
    import sys

    prog = (
        "import os, sys;"
        "os.environ['RC_REPRO_HOME'] = sys.argv[1];"
        "from rc_repro.services import users;"
        "users.add(sys.argv[2], 'correct-horse-battery', role='member')")
    procs = [subprocess.Popen([sys.executable, "-c", prog, str(tmp_path), f"proc{i}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
             for i in range(8)]
    failed = [p.communicate()[1].decode()[-300:] for p in procs if p.wait() != 0]
    assert failed == [], f"a concurrent `users add` failed outright: {failed}"

    names = sorted(u.name for u in users.list_users())
    assert names == [f"proc{i}" for i in range(8)], \
        f"accounts were lost across processes: {names}"


def test_a_new_account_never_becomes_an_implicit_admin():
    """Blank-means-admin is the MIGRATION for accounts that predate roles. If it
    were also the behaviour for new ones, `rc-repro users add bob` would silently
    hand bob the ability to delete everybody's data."""
    first = users.add("alice", GOOD)
    assert first.role == "admin", "the first account must be able to promote others"
    second = users.add("bob", GOOD)
    assert second.role == "member"
    assert users.implicit_admins() == [], "neither line has a blank role column"
    assert users.role_of("bob") == "member"


# --- session bookkeeping (services/sessions.py) ------------------------------------

def test_the_flush_map_shrinks_with_the_sessions_file():
    """`_flushed` gates the once-a-minute last_seen write-back, and entries were
    only ever popped on an EXPLICIT revoke. A session normally ends by expiring, or
    by being dropped by the _MAX_SESSIONS cap -- neither goes through revoke_sid()
    -- so the file was bounded two ways while the map shadowing it grew for the
    life of the process. Slow for a human team; fast for anything scripted against
    POST /api/session, which mints a session per call.
    """
    import time as _t

    from rc_repro.services import sessions as sessionsvc

    for i in range(30):
        token = sessionsvc.create("alice", label=f"tab-{i}")
        sessionsvc._flushed[sessionsvc._digest(token)] = 0.0   # force the flush
        sessionsvc.verify(token)
    assert len(sessionsvc._flushed) == 30

    # Time passes: every session ages past the absolute bound, the way real ones go.
    old = int(_t.time()) - sessionsvc.ABSOLUTE_SECONDS - 10
    rows = {sid: sessionsvc.Session(sid=s.sid, user=s.user, created=old,
                                    last_seen=old, label=s.label, origin=s.origin)
            for sid, s in sessionsvc._load(force=True).items()}
    sessionsvc._write(rows)

    assert sessionsvc._load(force=True) == {}, "the file drops what expired"
    assert sessionsvc._flushed == {}, "and the map that mirrors it has to follow"


def test_listing_sessions_survives_the_cache_being_rebuilt_underneath(monkeypatch):
    """_load() hands back the module-level _cache itself, not a copy, and both
    _load(force=True) and _write() clear it. list_for() used to build a generator
    and let sorted() consume it AFTER releasing the lock, so a concurrent request
    rebuilding the cache mid-iteration raised RuntimeError -- a 500 in the browser.
    GET /api/users calls this once per account, so an admin opening People rolled
    that dice once per person on the server.

    Forced deterministically here: the first alive() check does what a REVOCATION
    landing at that moment does. It has to be a revocation rather than a plain
    reload -- _load() clears and repopulates to the same size, and CPython only
    raises when the size differs from when the iteration began, so a same-size
    rebuild slips past the very guard this is about.
    """
    from rc_repro.services import sessions as sessionsvc

    for i in range(20):
        sessionsvc.create("alice", label=f"tab-{i}")

    real_alive, state = sessionsvc.Session.alive, {"n": 0}

    def alive(self, now=None):
        state["n"] += 1
        if state["n"] == 1:
            # somebody else signs out half their devices, mid-listing
            keep = dict(list(sessionsvc._load().items())[:10])
            sessionsvc._write(keep)
        return real_alive(self, now)

    monkeypatch.setattr(sessionsvc.Session, "alive", alive)
    out = sessionsvc.list_for("alice")        # must not raise
    assert len(out) == 20
    assert state["n"] >= 1, "the interleaving actually happened"


def test_the_service_ends_sessions_when_a_CREDENTIAL_is_invalidated():
    """The invariant lives in the service, not in each front end.

    It used to be enforced four times in web/app.py and three times in cli.py --
    an invariant that every caller has to remember is one the next caller silently
    breaks, and the thing it protects is "a replaced or deleted credential must not
    keep working through a session that outlived it".
    """
    from rc_repro.services import sessions as sessionsvc

    users.add("alice", GOOD, role="admin")
    users.add("bob", GOOD, role="member")

    bob = sessionsvc.create("bob")
    assert sessionsvc.verify(bob) is not None
    assert users.set_password("bob", "a-brand-new-password") == 1
    assert sessionsvc.verify(bob) is None, "the old session outlived the password"

    again = sessionsvc.create("bob")
    assert users.remove("bob") == 1
    assert sessionsvc.verify(again) is None, "a removed account was still signed in"

    # ...and alice, who was not touched, keeps hers.
    alice = sessionsvc.create("alice")
    users.add("carol", GOOD, role="member")
    assert sessionsvc.verify(alice) is not None


def test_a_role_change_is_NOT_a_credential_change():
    """Deliberately not folded in with the two above. The web guard reads role_of()
    live on every request, so a demotion already takes effect at once -- signing the
    person out on top of that is a courtesy the front ends choose, not something the
    service must guarantee. Bundling it here would make `users role` end sessions
    from the CLI too, which is a different decision than the one being made.
    """
    from rc_repro.services import sessions as sessionsvc

    users.add("alice", GOOD, role="admin")
    users.add("bob", GOOD, role="member")
    token = sessionsvc.create("bob")
    users.set_role("bob", "readonly")
    assert sessionsvc.verify(token) is not None, "set_role must not revoke by itself"
    assert users.role_of("bob") == "readonly", "but the demotion is already in force"
