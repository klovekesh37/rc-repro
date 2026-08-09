"""Shared-server behaviour: who owns a workspace, and who is told about it.

The rules being locked in here come from docs/design/team-server.md §3.3 and §7:
everyone sees everything, every workspace records who made it, and destroying
somebody else's data says whose it is first.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from typer.testing import CliRunner

from rc_repro import cli, runner
from rc_repro.services import lifecycle as lc
from rc_repro.services import users

cli_runner = CliRunner()
GOOD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.delenv("RC_REPRO_USER", raising=False)
    users._failures.clear()
    yield
    users._failures.clear()


# --- who the CLI thinks you are -------------------------------------------------

def test_the_cli_has_no_actor_until_somebody_creates_an_account(monkeypatch):
    """Team mode is opt-in. Until `users add` is run, every workspace keeps the
    name it has always had -- no silent rename of anyone's existing repros."""
    monkeypatch.setenv("RC_REPRO_USER", "alice")
    assert cli._cli_actor() == ""


def test_the_login_name_is_used_when_it_matches_an_account(monkeypatch):
    users.add("alice", GOOD)
    monkeypatch.setattr(cli.os, "getlogin", lambda: "alice")
    assert cli._cli_actor() == "alice"


def test_an_unknown_login_name_is_not_guessed_at(monkeypatch):
    """Better to fall back to the shared namespace than to invent a namespace
    nobody can log into from the GUI."""
    users.add("alice", GOOD)
    monkeypatch.setattr(cli.os, "getlogin", lambda: "root")
    monkeypatch.delenv("USER", raising=False)
    assert cli._cli_actor() == ""


def test_the_env_override_wins_over_the_login_name(monkeypatch):
    """The OS account and the GUI account are often not the same name."""
    users.add("alice", GOOD)
    monkeypatch.setattr(cli.os, "getlogin", lambda: "ubuntu")
    monkeypatch.setenv("RC_REPRO_USER", "Alice")
    assert cli._cli_actor() == "alice", "and it is lowercased to match the name rules"


def test_no_controlling_terminal_falls_back_to_USER(monkeypatch):
    """os.getlogin() raises under cron and in containers."""
    users.add("alice", GOOD)

    def boom():
        raise OSError("no tty")

    monkeypatch.setattr(cli.os, "getlogin", boom)
    monkeypatch.setenv("USER", "alice")
    assert cli._cli_actor() == "alice"


def test_up_passes_the_actor_into_the_request(monkeypatch):
    """The whole point of the plumbing: without this the name is not namespaced."""
    users.add("alice", GOOD)
    monkeypatch.setattr(cli.os, "getlogin", lambda: "alice")
    seen = {}

    def fake_create(req, emit, stream_output=False):
        seen["actor"] = req.actor
        seen["name"] = lc._derive_for(req)
        raise SystemExit(0)                 # stop before any docker work

    monkeypatch.setattr(lc, "create_repro", fake_create)
    cli_runner.invoke(cli.app, ["up", "-v", "8.5.1"])
    assert seen == {"actor": "alice", "name": "alice-rc8-5-1"}


# --- the record on disk ---------------------------------------------------------

def _write_meta(name: str, owner: str = "") -> None:
    """Materialise just enough of a workspace for read_meta() to find it."""
    fields = {f.name: "" for f in dataclasses.fields(runner.Metadata)}
    fields.update(name=name, project=f"rcrepro-{name}", rc_version="8.5.1",
                  mongo_tag="6.0", host_port=3000, preset="default",
                  root_url="http://localhost:3000",
                  extra={"created_by": owner} if owner else {})
    ws = runner.workspace(name)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(
        json.dumps(dataclasses.asdict(runner.Metadata(**fields))))
    (ws / "docker-compose.yml").write_text("services: {}\n")   # runner.exists()


def test_owner_of_reads_the_record(tmp_path):
    _write_meta("alice-rc8-5-1", "alice")
    _write_meta("rc8-5-1")
    assert lc.owner_of("alice-rc8-5-1") == "alice"
    assert lc.owner_of("rc8-5-1") == "", "a pre-team workspace has no owner"
    assert lc.owner_of("does-not-exist") == "", "and a missing one must not raise"


def test_list_and_describe_expose_the_owner(monkeypatch):
    _write_meta("alice-rc8-5-1", "alice")
    monkeypatch.setattr(runner, "docker_available", lambda **_k: False)
    (row,) = lc.list_repros()
    assert row["created_by"] == "alice"


# --- the guardrail --------------------------------------------------------------

def test_deleting_a_colleagues_data_names_them_first(monkeypatch):
    """§3.3: anyone may act on anything, but not without being told whose it is."""
    users.add("alice", GOOD)
    users.add("bob", GOOD)
    _write_meta("alice-rc8-5-1", "alice")
    monkeypatch.setattr(cli.os, "getlogin", lambda: "bob")
    monkeypatch.setattr(lc, "teardown", lambda *a, **k: None)

    r = cli_runner.invoke(cli.app, ["down", "-n", "alice-rc8-5-1", "--volumes"], input="n\n")
    assert r.exit_code != 0, "answering no must abort"
    assert "belongs to alice" in r.output


def test_yes_still_says_whose_data_it_was(monkeypatch):
    """--yes exists for scripts, where there is no prompt to read -- but there is
    a log, and it should record that this was somebody else's workspace."""
    users.add("alice", GOOD)
    users.add("bob", GOOD)
    _write_meta("alice-rc8-5-1", "alice")
    monkeypatch.setattr(cli.os, "getlogin", lambda: "bob")
    monkeypatch.setattr(lc, "teardown", lambda *a, **k: None)

    r = cli_runner.invoke(cli.app, ["down", "-n", "alice-rc8-5-1", "--volumes", "--yes"])
    assert r.exit_code == 0, r.output
    assert "owned by alice" in r.output


def test_deleting_your_own_workspace_is_not_nagged_about(monkeypatch):
    users.add("alice", GOOD)
    _write_meta("alice-rc8-5-1", "alice")
    monkeypatch.setattr(cli.os, "getlogin", lambda: "alice")
    monkeypatch.setattr(lc, "teardown", lambda *a, **k: None)

    r = cli_runner.invoke(cli.app, ["down", "-n", "alice-rc8-5-1", "--volumes", "--yes"])
    assert r.exit_code == 0, r.output
    assert "owned by" not in r.output


# --- serving the login ----------------------------------------------------------
# Basic Auth over plain http is refused on a reachable interface unless asked for.
# The refusal shipped before the front door that was meant to satisfy it, so
# --insecure is the only way to run accounts on a shared box today; it went
# untested and hidden, and a remote proxy CANNOT use the loopback route the error
# used to name. These pin both directions.

@pytest.fixture
def served(monkeypatch):
    """Run `serve` without serving: capture what it would have bound."""
    import sys
    import types

    seen: dict = {}
    fake = types.ModuleType("uvicorn")
    fake.run = lambda app, host="", port=0, **kw: seen.update(host=host, port=port, **kw)
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    from rc_repro.web import app as webapp
    monkeypatch.setattr(webapp, "create_app", lambda **kw: seen.update(kw) or object())
    return seen


def test_accounts_on_a_reachable_interface_are_refused_over_plain_http(served):
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, ["serve", "--bind", "0.0.0.0", "--no-open"])
    assert r.exit_code != 0
    assert "0.0.0.0" in r.output, "the refusal should name the interface it refused"
    assert "--insecure" in r.output, "and the way out that works behind a remote proxy"
    assert served == {}, "nothing should be bound"


def test_insecure_serves_the_login_on_a_reachable_interface(served):
    """The shared-box case: TLS terminates at a proxy this process cannot see."""
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--port", "9944",
        "--allow-host", "*", "--insecure", "--no-open"])
    assert r.exit_code == 0, r.output
    assert served["host"] == "0.0.0.0" and served["port"] == 9944
    assert served["accounts"] is True
    assert served["token"] == "", "accounts replace the session token"


def test_loopback_needs_no_flag(served):
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, ["serve", "--no-open"])
    assert r.exit_code == 0, r.output
    assert served["accounts"] is True


def test_without_accounts_a_reachable_bind_is_still_allowed(served):
    """The refusal is about the password, so it must not fire in token mode --
    that would break every existing lab/Codespaces setup."""
    r = cli_runner.invoke(cli.app, ["serve", "--bind", "0.0.0.0", "--no-open"])
    assert r.exit_code == 0, r.output
    assert served["accounts"] is False
    assert served["token"], "token mode still mints one"


# --- serve --domain: rc-repro arranges the TLS itself ---------------------------

@pytest.fixture
def fronted(served, monkeypatch):
    """`serve --domain` without Docker: capture the front door it would start."""
    from rc_repro.services import edge as fdsvc

    calls: dict = {}
    monkeypatch.setattr(fdsvc, "bridge_address", lambda: "172.17.0.1")
    def fake_write(door):
        # Write the same files the real one does. A fake that skips the side
        # effects its callers key off is not a fake of that function: without the
        # compose file, ensure_running() thinks nothing is installed; without the
        # `domain` file, serve() cannot reuse the name the box was set up with.
        calls.setdefault("door", door)
        fdsvc.compose_path().parent.mkdir(parents=True, exist_ok=True)
        fdsvc.compose_path().write_text("services: {edge: {}}\n")
        (fdsvc.edge_dir() / "domain").write_text(door.domain + "\n")

    monkeypatch.setattr(fdsvc, "write", fake_write)
    monkeypatch.setattr(fdsvc, "has_acme", lambda: True)
    monkeypatch.setattr(fdsvc, "running", lambda: False)
    monkeypatch.setattr(fdsvc, "holders_of_443", lambda: [])
    monkeypatch.setattr(fdsvc, "up", lambda **kw: calls.update(started=True) or 0)
    served["fd"] = calls
    return served


def test_domain_binds_the_bridge_not_the_whole_network(fronted):
    """The front door is the only thing that needs to reach the GUI, so the port
    never has to be exposed. 0.0.0.0 would publish docker control to the LAN."""
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "support.example.com", "--email", "ops@example.com"])
    assert r.exit_code == 0, r.output
    assert fronted["host"] == "172.17.0.1"
    assert fronted["fd"]["started"] is True


def test_domain_stands_the_plain_http_refusal_down(fronted):
    """Decision (a): with --domain, rc-repro IS the TLS terminator, so refusing
    its own most secure configuration would be absurd -- and no --insecure has to
    appear in a printed unit file, where it would read as a warning about itself.
    """
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "support.example.com", "--email", "ops@example.com"])
    assert r.exit_code == 0, r.output
    assert "refused" not in r.output


def test_the_domain_is_accepted_as_a_host_header(fronted):
    """Traefik forwards the original Host; without this every proxied request is
    a 403 from the DNS-rebind guard."""
    users.add("alice", GOOD)
    cli_runner.invoke(cli.app, [
        "serve", "--domain", "support.example.com", "--email", "ops@example.com"])
    assert "support.example.com" in fronted["allow_hosts"]


def test_a_domain_without_an_email_says_how_to_set_one(fronted):
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, ["serve", "--domain", "support.example.com"])
    assert r.exit_code != 0
    assert "--email" in r.output and "config set acme.email" in r.output


def test_a_domain_with_no_accounts_is_warned_about_loudly(fronted):
    """Publishing the control plane on the internet with only a session token is
    exactly what accounts exist to end."""
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "support.example.com", "--email", "ops@example.com"])
    assert r.exit_code == 0, r.output
    assert "no login" in r.output and "users add" in r.output


def test_a_scheme_in_the_domain_is_corrected_not_carried(fronted):
    """It becomes the Host() rule, an SNI name and the printed URL at once."""
    users.add("alice", GOOD)
    cli_runner.invoke(cli.app, [
        "serve", "--domain", "https://support.example.com/", "--email", "o@e.com"])
    assert fronted["fd"]["door"].domain == "support.example.com"


# --- --print-service ------------------------------------------------------------

def test_print_service_writes_nothing_and_starts_nothing(fronted):
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "support.example.com", "--email", "ops@example.com",
        "--print-service"])
    assert r.exit_code == 0, r.output
    assert "started" not in fronted["fd"], "must not start the front door"
    assert "host" not in fronted, "must not serve"


def test_the_unit_uses_an_absolute_execstart(fronted):
    """systemd refuses a relative ExecStart outright, and `which` misses a venv
    console script that is not on PATH."""
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "d.example.com", "--email", "o@e.com", "--print-service"])
    line = next(ln for ln in r.output.splitlines() if ln.startswith("ExecStart="))
    assert line.removeprefix("ExecStart=").startswith("/"), line


def test_the_unit_reproduces_the_flags_it_was_given(fronted):
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "d.example.com", "--email", "o@e.com",
        "--port", "9000", "--print-service"])
    exec_line = next(ln for ln in r.output.splitlines() if ln.startswith("ExecStart="))
    assert "--domain d.example.com" in exec_line
    assert "--port 9000" in exec_line
    assert "--no-open" in exec_line, "a service has no browser to open"


def test_both_ways_to_run_it_are_offered_with_the_tradeoff_stated(fronted):
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "d.example.com", "--email", "o@e.com", "--print-service"])
    assert "systemctl enable --now rc-repro" in r.output
    assert "nohup " in r.output
    assert "NOT restart on crash" in r.output, "nohup's limits must be stated"


def test_print_service_without_accounts_says_why_that_defeats_the_point(fronted):
    """A token that changes on every restart makes a restarting service useless."""
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "d.example.com", "--email", "o@e.com", "--print-service"])
    assert "users add" in r.output


# --- audit: destructive actions leave a trace (F1) ------------------------------
# audit() had exactly one call site, JobManager.submit(), so every SYNCHRONOUS
# endpoint wrote nothing -- and that set was teardown and prune, the two most
# destructive operations there are. The log filled with creates and stayed silent
# about deletions, which is worse than no log: it looks complete.

def test_tearing_down_a_workspace_is_recorded(monkeypatch, tmp_path):
    from rc_repro import runner
    from rc_repro.services import audit as auditsvc

    _write_meta("alice-rc8-5-1", "alice")
    monkeypatch.setattr(runner, "docker_available", lambda **k: True)
    monkeypatch.setattr(runner, "down", lambda *a, **k: 0)
    monkeypatch.setattr(runner, "remove", lambda *a, **k: None)
    auditsvc.set_actor("alice")

    lc.teardown("alice-rc8-5-1", volumes=True, confirm=True)
    line = auditsvc.audit_path().read_text().strip()
    assert "alice" in line and "down-volumes" in line and "alice-rc8-5-1" in line


def test_the_cli_writes_to_the_same_log_as_the_gui(monkeypatch):
    """The CLI could not reach audit() at all: it lived in web/jobs.py, and the
    core CLI deliberately does not import the web layer."""
    from rc_repro.services import audit as auditsvc

    users.add("alice", GOOD)
    monkeypatch.setattr(cli.os, "getlogin", lambda: "alice")
    _write_meta("alice-rc8-5-1", "alice")
    monkeypatch.setattr(lc, "teardown", lambda *a, **k: {"name": "x", "removed": True})

    r = cli_runner.invoke(cli.app, ["down", "-n", "alice-rc8-5-1", "--volumes", "--yes"])
    assert r.exit_code == 0, r.output
    assert auditsvc.actor() == "alice", "the CLI must publish who is running it"


def test_the_audit_log_is_not_world_readable(monkeypatch):
    import stat

    from rc_repro.services import audit as auditsvc

    auditsvc.audit("alice", "down", "x")
    mode = stat.S_IMODE(auditsvc.audit_path().stat().st_mode)
    assert mode == 0o600, oct(mode)


# --- serve: no authentication at all (F5) ---------------------------------------

def test_no_token_on_a_reachable_bind_is_refused(served):
    """No accounts and no token on a public interface is unauthenticated docker
    control -- containers, volumes, admin tokens, arbitrary REST."""
    r = cli_runner.invoke(cli.app, ["serve", "--bind", "0.0.0.0", "--no-token", "--no-open"])
    assert r.exit_code != 0
    assert "NO authentication" in r.output and "users add" in r.output
    assert served == {}, "nothing should be bound"


def test_no_token_stays_available_for_an_ephemeral_lab(served):
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--no-token", "--insecure", "--no-open"])
    assert r.exit_code == 0, r.output
    assert served["token"] == "" and served["accounts"] is False


def test_no_token_on_loopback_is_untouched(served):
    r = cli_runner.invoke(cli.app, ["serve", "--no-token", "--no-open"])
    assert r.exit_code == 0, r.output


def test_a_domain_served_gui_still_carries_its_token(fronted):
    """Reported from EC2: the page loaded and every API call answered "bad or
    missing token". A token IS minted when there are no accounts, but the printed
    https URL left the ?t= off -- and the guard only covers /api/, so the SPA
    rendered an empty shell with an error and no hint a token even existed."""
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com"])
    assert r.exit_code == 0, r.output
    line = next(ln for ln in r.output.splitlines() if "rc-repro GUI:" in ln)
    assert "?t=" in line, line


def test_with_accounts_the_url_carries_no_token(fronted):
    """Accounts replace the token entirely; a ?t= there would be meaningless."""
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com"])
    line = next(ln for ln in r.output.splitlines() if "rc-repro GUI:" in ln)
    assert "?t=" not in line and line.strip().endswith("rc.example.com/")


def test_serve_reuses_the_domain_the_box_was_set_up_with(fronted):
    """Asked directly: "do I need to run this every time with the same
    parameters?" It was yes -- and a plain `serve` did not merely forget, it went
    back to loopback while the edge's GUI route still pointed at a process it
    could no longer reach, so the name 502'd."""
    # Set it up the way somebody actually would, once.
    first = cli_runner.invoke(cli.app, [
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com"])
    assert first.exit_code == 0, first.output

    r = cli_runner.invoke(cli.app, ["serve"])          # no flags at all
    assert r.exit_code == 0, r.output
    assert "rc.example.com" in fronted["allow_hosts"]
    assert fronted["host"] == "172.17.0.1", "and it binds the bridge, as before"


def test_no_domain_gives_a_deliberately_local_session(fronted):
    from rc_repro.services import edge as edgesvc

    edgesvc.write(edgesvc.Edge(domain="rc.example.com", acme_email="o@e.com"))
    r = cli_runner.invoke(cli.app, ["serve", "--no-domain", "--no-open"])
    assert r.exit_code == 0, r.output
    assert fronted["host"] == "127.0.0.1"


def test_the_email_is_remembered_after_the_first_use(fronted, tmp_path):
    """`up` documents it as remembered; for serve it was not, so every restart
    needed it retyped."""
    from rc_repro import config

    cli_runner.invoke(cli.app, [
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com"])
    assert config.load_config().get("acme_email") == "ops@example.com"

    r = cli_runner.invoke(cli.app, ["serve", "--domain", "rc.example.com"])
    assert r.exit_code == 0, r.output      # no --email needed the second time
