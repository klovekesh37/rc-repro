"""Unit tests for GUI accounts (services/users.py).

These lock in the properties that make a shared deployment safe: passwords are
never recoverable from the file, verification is constant-time-ish and cached,
repeated failures cost something, and revoking a credential takes effect at once.
"""

from __future__ import annotations

import time

import pytest

from rc_repro import errors
from rc_repro.services import users


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    users.forget()
    users._failures.clear()
    yield
    users.forget()
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


def test_a_successful_check_is_cached():
    """The browser sends Authorization on EVERY request — the 4s poll, each SSE
    reconnect. At ~100ms per derivation that is a self-inflicted denial of service."""
    users.add("alice", GOOD)
    t0 = time.monotonic(); users.verify("alice", GOOD); first = time.monotonic() - t0
    t0 = time.monotonic(); users.verify("alice", GOOD); cached = time.monotonic() - t0
    assert cached < first / 5, f"first {first*1000:.0f}ms, cached {cached*1000:.0f}ms"


def test_the_cache_is_keyed_on_the_password_too():
    """A cached success must not let a WRONG password through afterwards."""
    users.add("alice", GOOD)
    assert users.verify("alice", GOOD) is True
    assert users.verify("alice", "wrong-password-x") is False


def test_changing_a_password_invalidates_the_old_one_immediately():
    users.add("alice", GOOD)
    assert users.verify("alice", GOOD) is True      # now cached
    users.set_password("alice", "a-brand-new-password")
    users.forget("alice")
    assert users.verify("alice", GOOD) is False, "the old password still worked"
    assert users.verify("alice", "a-brand-new-password") is True


def test_removing_a_user_invalidates_the_cache():
    users.add("alice", GOOD)
    assert users.verify("alice", GOOD) is True
    users.remove("alice")
    users.forget("alice")
    assert users.verify("alice", GOOD) is False


def test_repeated_failures_lock_the_account_out():
    """A 128-bit token is not guessable; a human password is."""
    users.add("alice", GOOD)
    assert users.locked_out("alice") == 0
    for _ in range(users._LOCKOUT_AFTER):
        users.verify("alice", "wrong-password-x")
    assert users.locked_out("alice") > 0
    # Even the CORRECT password is refused while locked out.
    assert users.verify("alice", GOOD) is False


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


def test_the_cache_is_bounded():
    """It is keyed by (user, password-digest); an attacker trying many passwords
    must not be able to grow it without limit."""
    users.add("alice", GOOD)
    for i in range(users._CACHE_MAX + 10):
        users._cache[(f"u{i}", b"x")] = time.monotonic() + 60
    users.verify("alice", GOOD)
    assert len(users._cache) <= users._CACHE_MAX + 1


# --- hashing ---------------------------------------------------------------------------

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
# Verifications are cached for 5 minutes in a MODULE-LEVEL dict. `users remove`
# calls forget() -- in the CLI process, whose cache is empty. The long-lived
# `serve` process kept its own and only re-read the file on a MISS, so a removed
# account went on working against the running GUI for up to five minutes, with no
# signal that the documented remediation had not taken effect.

def _other_process_removes(name: str) -> None:
    """Edit the users file the way a SEPARATE process would: no forget() here."""
    from rc_repro.services import users as usersvc

    path = usersvc.users_file()
    kept = [ln for ln in path.read_text().splitlines()
            if not ln.startswith(f"{name}:")]
    path.write_text("\n".join(kept) + "\n")


def test_removing_a_user_takes_effect_immediately_in_a_running_server(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.services import users as usersvc

    usersvc.forget()
    usersvc._failures.clear()
    usersvc.add("alice", "correct-horse-battery")
    assert usersvc.verify("alice", "correct-horse-battery") is True   # now cached

    _other_process_removes("alice")
    assert usersvc.verify("alice", "correct-horse-battery") is False, \
        "a removed account kept working until the cache expired"


def test_a_changed_password_invalidates_the_old_one_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.services import users as usersvc

    usersvc.forget()
    usersvc._failures.clear()
    usersvc.add("alice", "correct-horse-battery")
    assert usersvc.verify("alice", "correct-horse-battery") is True

    # As another process would: rewrite the file, no forget() in THIS one.
    usersvc._write({"alice": (usersvc.hash_password("brand-new-password"),
                              "2026-01-01", "")})
    assert usersvc.verify("alice", "correct-horse-battery") is False
    assert usersvc.verify("alice", "brand-new-password") is True


def test_the_cache_still_works_when_the_file_is_untouched(tmp_path, monkeypatch):
    """The invalidation must not defeat the cache it guards -- that cache is what
    stops the browser's per-request Basic header saturating the threadpool."""
    import time

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.services import users as usersvc

    usersvc.forget()
    usersvc._failures.clear()
    usersvc.add("alice", "correct-horse-battery")
    usersvc.verify("alice", "correct-horse-battery")

    start = time.perf_counter()
    for _ in range(20):
        assert usersvc.verify("alice", "correct-horse-battery") is True
    per_call_ms = (time.perf_counter() - start) / 20 * 1000
    assert per_call_ms < 5, f"{per_call_ms:.1f}ms per call - the cache is not being used"
