"""Unit tests for GUI accounts (services/users.py).

These lock in the properties that make a shared deployment safe: passwords are
never recoverable from the file, verification is constant-time-ish, repeated
failures cost something without ever refusing a correct password, and revoking a
credential takes effect at once.
"""

from __future__ import annotations

import time

import pytest

from rc_repro import errors
from rc_repro.services import users


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    users._failures.clear()
    yield
    users._failures.clear()


GOOD = "correct-horse-battery"


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
    assert only.role == "", "the role column must not have been filled by the date"
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
    users.add("bob", GOOD)
    by_name = {u.name: u.role for u in users.list_users()}
    assert by_name == {"alice": "readonly", "bob": ""}


# --- validation -------------------------------------------------------------------

def test_a_short_password_is_refused():
    with pytest.raises(errors.ValidationError, match="at least"):
        users.add("alice", "short")


def test_names_are_restricted_to_a_dns_label():
    """The name becomes part of a workspace name and therefore a hostname."""
    for bad in ("Alice", "al ice", "alice.b", "-alice", "", "a" * 32, "al_ice"):
        with pytest.raises(errors.ValidationError):
            users.add(bad, GOOD)
    users.add("alice-2", GOOD)          # allowed shape


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
    assert users.verify("alice", GOOD) is True
    users.remove("alice")
    assert users.verify("alice", GOOD) is False


def test_repeated_failures_earn_a_backoff():
    """A 128-bit token is not guessable; a human password is, so failures count."""
    users.add("alice", GOOD)
    assert users.locked_out("alice") == 0
    for _ in range(users._LOCKOUT_AFTER):
        users.verify("alice", "wrong-password-x")
    assert users.locked_out("alice") > 0


def test_a_correct_password_is_never_refused_because_of_a_lockout():
    """The counter is advisory. It reports; it does not decide.

    Consulting it BEFORE checking the password made the documented remedy for a
    compromised account the thing that locked its owner out: after `users passwd`,
    every still-open tab replayed the OLD credential on its four-second dashboard
    poll, each poll slid the window forward, and the new password was refused with
    it. Measured at the time: 232s of lockout after twenty minutes of one tab, and
    the right password did not work.
    """
    users.add("alice", GOOD)
    for _ in range(users._LOCKOUT_AFTER * 4):
        users.verify("alice", "the-old-password-x")
    assert users.locked_out("alice") > 0, "the backoff should still be reported"
    assert users.verify("alice", GOOD) is True
    assert users.locked_out("alice") == 0, "success clears the count"


def test_one_client_cannot_lock_out_another():
    """Names are not secret -- `serve` prints them at startup and the dashboard
    renders an owner chip on every card. Keyed on the name alone, five wrong
    guesses from anyone who could reach the port denied service to a colleague."""
    users.add("alice", GOOD)
    for _ in range(50):
        users.verify("alice", "guess-guess-guess", source="203.0.113.9")
    assert users.locked_out("alice", "203.0.113.9") > 0, "the attacker is throttled"
    assert users.locked_out("alice", "10.0.0.4") == 0, "alice's own address is clean"
    assert users.verify("alice", GOOD, source="10.0.0.4") is True


def test_a_success_clears_the_failure_count():
    users.add("alice", GOOD)
    for _ in range(users._LOCKOUT_AFTER - 1):
        users.verify("alice", "wrong-password-x")
    assert users.verify("alice", GOOD) is True
    assert users.locked_out("alice") == 0


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

    usersvc._failures.clear()
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

    usersvc._failures.clear()
    usersvc.add("alice", "correct-horse-battery")
    assert usersvc.verify("alice", "correct-horse-battery") is True

    _other_process_edits(lambda rows: rows.__setitem__(
        "alice", (usersvc.hash_password("brand-new-password"), "2026-01-01", "")))
    assert usersvc.verify("alice", "correct-horse-battery") is False
    assert usersvc.verify("alice", "brand-new-password") is True
