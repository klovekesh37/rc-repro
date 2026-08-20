"""Shared-server behaviour: who owns a workspace, and who is told about it.

The rules being locked in here come from docs/design/team-server.md §3.3 and §7:
everyone sees everything, every workspace records who made it, and destroying
somebody else's data says whose it is first.
"""

from __future__ import annotations

import dataclasses
import json
import re

import pytest
from typer.testing import CliRunner

from rc_repro import cli, runner
from rc_repro.services import lifecycle as lc
from rc_repro.services import users

def _ok_compose(*a, **k):
    """A successful `docker compose` run. `edge.up()` returns the CompletedProcess now,
    not its return code, so a failed start can say what docker said."""
    import subprocess
    return subprocess.CompletedProcess(["docker", "compose"], 0, "", "")



cli_runner = CliRunner()
GOOD = "correct-horse-battery"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def flat_help(output: str) -> str:
    """`--help` reduced to one line of plain text, for substring assertions.

    Two things have to come out first, and BOTH are environment-dependent, which is
    how a test that passes locally fails in CI:

    * ANSI colour. typer renders help through Rich, which highlights `--flags` --
      and it inserts the escape codes INSIDE the token, so the raw output does not
      contain the literal "--trust-proxy" at all when colour is on. CI enables it;
      a local run does not.
    * The option table's box rules, which land mid-sentence once help text wraps,
      so an assertion would really be about the terminal width.
    """
    return " ".join(_ANSI.sub("", output).replace("│", " ").split())


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.delenv("RC_REPRO_USER", raising=False)
    yield


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
    # 3 (`preflight`), the same as the no-accounts branch: these are the two halves
    # of one question — may this bind serve? — and they used to report it as 1 and
    # 3, so a script could not treat "fix your invocation" as a single case.
    assert r.exit_code == 3, f"want 3 (preflight), got {r.exit_code}"
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
    assert "token" not in served, "there is no session token any more, at all"


def test_loopback_needs_no_flag(served):
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, ["serve", "--no-open"])
    assert r.exit_code == 0, r.output
    assert served["accounts"] is True


def test_without_accounts_a_reachable_bind_is_refused(served):
    """This USED to be allowed, because token mode filled the gap. Token mode is
    gone, so a reachable interface with no accounts no longer starts -- the lab
    and Codespaces path is `users add` first, then --allow-host as before."""
    r = cli_runner.invoke(cli.app, ["serve", "--bind", "0.0.0.0", "--no-open"])
    assert r.exit_code != 0
    assert "needs an account" in r.output and served == {}


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
    monkeypatch.setattr(fdsvc, "up", lambda **kw: calls.update(started=True) or _ok_compose())
    served["fd"] = calls
    return served


def test_serve_says_when_the_domain_has_no_certificate(tmp_path, monkeypatch, capsys):
    """`serve --domain` printed `https://<domain>/` and checked nothing. Traefik obtains
    certificates in the BACKGROUND after it starts, so the edge comes up, the route
    loads, the URL is printed -- and the name serves Traefik's own default certificate
    and a 404 for as long as issuance keeps failing. Reported by someone whose GUI was
    fine on :7070 and dead on its domain, with the reason in a container log there was no
    command to read."""
    from rc_repro import cli, tls as tlsmod
    from rc_repro.services import edge as fdsvc

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    # Serving, but it is Traefik's fallback -- which is what a failed ACME looks like
    # from outside, and is indistinguishable from success unless you look at `fallback`.
    monkeypatch.setattr(tlsmod, "verify", lambda *a, **k: {
        "serving": True, "fallback": True, "issuer": "CN = TRAEFIK DEFAULT CERT"})
    monkeypatch.setattr(fdsvc, "acme_failure", lambda domain="", tail=400: (
        "DNS problem: NXDOMAIN looking up A for gui.example.test - check that a DNS "
        "record exists for this domain"))

    cli._report_gui_tls("gui.example.test", tries=1, pause=0)
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "NO certificate" in out, out
    assert "NXDOMAIN" in out, "quote the reason, or the reader has nowhere to go"
    assert "the name, not the server" in out, "say what is NOT broken"
    assert "edge logs" in out, "point at where the detail lives"
    assert "443" in out, "name the port the challenge actually uses"


def test_serve_confirms_a_real_certificate_when_there_is_one(tmp_path, monkeypatch, capsys):
    """The other half: a healthy start must say so, or a warning nobody ever sees
    succeed is a warning people learn to ignore."""
    from rc_repro import cli, tls as tlsmod

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(tlsmod, "verify", lambda *a, **k: {
        "serving": True, "fallback": False, "issuer": "CN = R11, O = Let's Encrypt"})
    cli._report_gui_tls("gui.example.test", tries=1, pause=0)
    out = capsys.readouterr().out
    assert "serving a real certificate" in out, out
    assert "Let's Encrypt" in out, out


def test_serve_renders_an_edge_refusal_instead_of_a_traceback(tmp_path, monkeypatch):
    """`up()` refuses with a ConflictError when another home's edge already holds the
    port -- expected and actionable -- and nothing on the serve path caught it, so it
    reached the top as a Python TRACEBACK with cli.py source printed at somebody trying
    to start a server. Every other command in that file wraps its service call."""
    from typer.testing import CliRunner

    from rc_repro import cli
    from rc_repro.errors import ConflictError
    from rc_repro.services import edge as fdsvc
    from rc_repro.services import lifecycle as lc
    from rc_repro.services import users as usersvc

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    usersvc.add("someone", "correct-horse-battery", role="admin")
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr(cli.lcsvc, "require_docker", lambda: None)

    def refuse(**kw):
        raise ConflictError("another rc-repro edge is already running")

    monkeypatch.setattr(fdsvc, "ensure_running", refuse)
    monkeypatch.setattr(fdsvc, "write", lambda fd: None)
    monkeypatch.setattr(fdsvc, "bridge_address", lambda: "172.17.0.1")

    res = CliRunner().invoke(cli.app, [
        "serve", "--domain", "gui.example.test", "--email", "a@b.test", "--no-open"])
    assert res.exit_code == 8, res.output
    assert "Traceback" not in res.output, res.output
    assert "ConflictError" not in res.output, "a class name is not a message"
    assert "another rc-repro edge is already running" in res.output, res.output


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


def test_a_domain_with_no_accounts_is_refused_not_merely_warned(fronted):
    """It used to warn and start anyway. Publishing the control plane on the
    internet with only a session token is what accounts exist to end, and a
    warning is advice nobody reads until afterwards."""
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "support.example.com", "--email", "ops@example.com"])
    assert r.exit_code != 0
    assert "needs an account" in r.output and "users add" in r.output


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


# --- serve: posture is decided by the BIND (F5) -----------------------------------
# Security should not be a side effect of whether <home>/users happens to be
# empty. The session token that used to fill that gap is gone: it was a standing
# credential with no identity behind it, regenerated on every restart, and it made
# audit.log record `-` for every action in the mode that shipped by default.

def test_a_reachable_bind_with_no_accounts_refuses_to_start(served):
    """The replacement for the token is not a smaller token."""
    r = cli_runner.invoke(cli.app, ["serve", "--bind", "0.0.0.0", "--no-open"])
    assert r.exit_code != 0
    assert "needs an account" in r.output and "users add" in r.output
    assert served == {}, "nothing should be bound"


def test_insecure_does_not_buy_a_way_past_it(served):
    """--insecure says "this hop is protected", not "skip authentication"."""
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--insecure", "--no-open"])
    assert r.exit_code != 0 and "needs an account" in r.output


def test_the_no_accounts_refusal_names_the_command_to_run_again(served):
    """Both steps, and the second one is the line that was just typed.

    Naming only `users add` left the reader to reconstruct a `--domain --email`
    invocation from scrollback, and on a rebuilt box -- where ~/.rc-repro is empty
    and EVERY serve says this -- it read as rc-repro having stopped working rather
    than as two commands in a row. That is how a support box stayed down.
    """
    r = cli_runner.invoke(cli.app, [
        # --bind, so the Docker bridge is never consulted: with --domain and no
        # --bind, `serve` reads edgesvc.bridge_address() before it reaches the
        # accounts check, and on a machine without Docker these three failed with
        # "could not read the Docker bridge address" instead of the message under
        # test. Nothing in this suite should need the engine.
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com",
        "--bind", "172.17.0.1", "--no-open"])
    assert r.exit_code != 0
    assert "users add" in r.output
    # The re-run line, rebuilt from argv, so it carries the flags that were given.
    assert "--domain rc.example.com" in r.output, r.output
    assert "--email ops@example.com" in r.output, r.output
    # "the command was fine" is carried by the amber styling and the numbered
    # steps now, rather than by a sentence saying so — see
    # test_the_no_accounts_stop_is_not_dressed_as_a_failure.
    assert "users add <name>" in r.output


def test_there_is_a_way_to_ask_which_version_is_installed():
    """`rc-repro --version` did not exist — it answered "No such option", exit 2 —
    while the project's own notes told people to run it to check what a box has,
    and the number appeared nowhere but the sign-in page footer, which needs the
    GUI up and reachable to read.

    So the one question worth asking a remote box after deploying to it had no
    answer from a shell, and every version bump made to render that answerable was
    answerable only in a browser.
    """
    from rc_repro import __version__
    for flag in ("--version", "-V"):
        r = cli_runner.invoke(cli.app, [flag])
        assert r.exit_code == 0, f"{flag}: {r.output}"
        assert __version__ in r.output, f"{flag} did not name the version: {r.output}"
        assert "rc-repro" in r.output
    # Eager: it must not need a subcommand, and must not be swallowed by one.
    assert cli_runner.invoke(cli.app, ["--version", "list"]).exit_code == 0


def test_the_refusal_is_where_insecure_is_taught(served):
    """Hiding it from --help only works if the one moment it IS needed says so.

    That moment is this refusal, and it is precisely targeted: the reader has a
    reachable bind, no domain and no proxy, which is the entire set of people the
    flag applies to.
    """
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, ["serve", "--bind", "0.0.0.0", "--no-open"])
    assert r.exit_code != 0
    assert "--insecure" in r.output, "hidden from --help, but named where it is needed"
    assert served == {}, "nothing should be bound"


def test_a_bare_trust_proxy_address_is_called_out_not_silently_useless(served):
    """`--trust-proxy 0.0.0.0` parses, is not dropped, and becomes 0.0.0.0/32 — an
    address no peer has. The flag is then indistinguishable from --insecure and
    the cookie it was passed to mark Secure is not Secure.

    Found on a real box, where it had been running that way for weeks: the server
    starts, it serves, and the one thing the flag was for silently did not happen.
    """
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--allow-host", "*",
        "--trust-proxy", "0.0.0.0", "--no-open"])
    assert r.exit_code == 0, r.output
    assert "0.0.0.0/32" in r.output and "did you mean 0.0.0.0/0" in r.output
    # And the posture line agrees with reality. It used to read "https, trusted
    # from 0.0.0.0" — the one line an operator reads to learn the posture, saying
    # the opposite of it — because it tested the LIST for emptiness rather than
    # whether anything in it could match.
    assert "trusted from" not in r.output, r.output
    assert "PLAIN HTTP on a reachable interface" in r.output
    assert served.get("trust_proxy") == [], "nothing usable was passed to the app"

    # A CIDR that CAN match says nothing, and is reported as trusted.
    r2 = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--allow-host", "*",
        "--trust-proxy", "0.0.0.0/0", "--no-open"])
    assert "did you mean" not in r2.output, r2.output
    # Conditional wording on purpose: --trust-proxy grants permission to believe
    # X-Forwarded-Proto, so until a peer inside the CIDR actually sends it this is
    # plain http with a non-Secure cookie. "https, trusted from X" claimed otherwise.
    assert "https when the proxy says so, trusted from 0.0.0.0/0" in r2.output


_LAB = "6a6ee0279ef1d3d348968822-0e9f82.node-ap-b1d4.iximiuz.com"


@pytest.mark.parametrize("given, note", [
    (f"https://{_LAB}/", "dropped 'https://'"),
    (f"{_LAB}/", "dropped the trailing slash"),
    (f"{_LAB}:9944", "dropped the port"),
    (f"{_LAB}.", "dropped the trailing dot"),
    (_LAB.upper(), "lower-cased it"),
])
def test_an_allow_host_that_is_not_a_bare_hostname_is_corrected(served, given, note):
    """Every one of these was silently unmatchable, and `serve` reported itself
    healthy each time before 403ing every request.

    The Host guard strips the port off the INCOMING header but compared against the
    raw string it was given, so `--allow-host <lab>:9944` — the obvious thing to
    type when you are serving on 9944 — matched nothing. Three of five forms tried
    in a row on a real lab box failed this way.

    The same raw value also built the startup URL, which is where
    `http://https://<lab>/:9944/` came from.
    """
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--insecure", "--port", "9944",
        "--allow-host", given, "--no-open"])
    assert r.exit_code == 0, r.output
    assert note in r.output, f"the correction was not reported: {r.output}"
    assert served["allow_hosts"] == [_LAB], served.get("allow_hosts")
    # And the URL is built from the corrected value, not the raw one.
    assert f"rc-repro GUI: http://{_LAB}:9944/" in r.output, r.output


def test_the_allow_host_wildcard_survives_correction(served):
    """`*` is the documented any-Host value and must not be mangled into a name."""
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--insecure", "--allow-host", "*", "--no-open"])
    assert r.exit_code == 0, r.output
    assert served["allow_hosts"] == ["*"]


@pytest.mark.parametrize("given", ["https://", "/", "host/path"])
def test_an_allow_host_with_no_hostname_in_it_is_refused(served, given):
    """Correcting is not inventing. Nothing left to match, or a path the Host header
    cannot carry, are refused rather than quietly turned into something else."""
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--insecure", "--allow-host", given, "--no-open"])
    assert r.exit_code != 0
    assert "--allow-host" in r.output
    assert served == {}, "nothing should be bound"


def test_a_reachable_bind_with_an_empty_allow_list_says_only_localhost_works(served):
    """The mirror of the --allow-host-without-a-bind note, and the one that cost the
    time: bound where others can reach it, but the allow-list holds nothing, so only
    localhost passes the Host guard and every request by a real name is a 403.
    `serve` printed a healthy startup screen and said nothing.
    """
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--trust-proxy", "10.0.0.1", "--no-open"])
    assert r.exit_code == 0, r.output
    assert "nothing is in --allow-host" in r.output
    assert "will be a 403" in r.output


def test_trusting_every_address_is_called_out(served):
    """`--trust-proxy 0.0.0.0/0` is usable, so nothing refuses it — but it says
    "believe X-Forwarded-* from anyone", which is a different statement from
    "believe my proxy". resolve_peer()'s docstring names both consequences: a chosen
    scheme turns Secure on, a chosen address dodges the sign-in throttle."""
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--allow-host", "*",
        "--trust-proxy", "0.0.0.0/0", "--no-open"])
    assert r.exit_code == 0, r.output
    assert "ANY client" in r.output and "dodge the sign-in throttle" in r.output
    # A specific proxy address says nothing.
    r2 = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--allow-host", "*",
        "--trust-proxy", "10.0.0.1", "--no-open"])
    assert "ANY client" not in r2.output, r2.output


def test_a_wildcard_bind_does_not_print_a_url_nobody_can_open(served):
    """`--bind 0.0.0.0 --allow-host '*'` — the shape a lab box is run in — has no
    host to name, and `http://0.0.0.0:7070/` is not something anyone can open. A
    placeholder is honest; a copy-pasteable-looking URL that fails is not."""
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--allow-host", "*", "--insecure",
        "--port", "7070", "--no-open"])
    assert r.exit_code == 0, r.output
    assert "http://0.0.0.0:7070/" not in r.output
    assert "rc-repro GUI: http://<this-box>:7070/" in r.output, r.output


def test_a_reachable_bind_prints_a_url_somebody_can_actually_open(served):
    """It printed `http://localhost:<port>/` while bound to 0.0.0.0.

    That is the one run where the URL is not obvious — the whole reason for the
    bind is that the useful address is somewhere else — and the reader was left to
    assemble it from the --allow-host they had just typed. Observed on a real box:
    the operator worked out `http://18.232.139.213:7070/` by hand.
    """
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--bind", "0.0.0.0", "--allow-host", "18.232.139.213",
        "--insecure", "--port", "7070", "--no-open"])
    assert r.exit_code == 0, r.output
    assert "rc-repro GUI: http://18.232.139.213:7070/" in r.output, r.output


def test_allow_host_without_a_bind_says_it_is_doing_nothing(served):
    """--allow-host names WHO may reach it; --bind decides whether anyone can.

    Given one without the other the flag is inert, and the server then reports
    itself as working — so the reader retries the same command or concludes the
    address is wrong. It cost three attempts on a real box. A note, not a refusal:
    naming a Host reached through a proxy on loopback is a legitimate setup.
    """
    users.add("alice", GOOD)
    r = cli_runner.invoke(cli.app, [
        "serve", "--allow-host", "18.232.139.213", "--insecure", "--no-open"])
    assert r.exit_code == 0, r.output
    assert "the bind is still" in r.output and "--bind 0.0.0.0" in r.output
    # Still starts, and still on loopback.
    assert served["host"] == "127.0.0.1"


def test_the_no_accounts_stop_is_not_dressed_as_a_failure(served):
    """Nothing has gone wrong when a fresh box has no accounts — that is the
    documented first-run sequence, and on a rebuilt box EVERY serve says it. A red
    `error:` there reads as rc-repro being broken rather than as two commands in a
    row, which is exactly how a working box got reported as dead.

    It still exits non-zero, and with 3 (`preflight` in errors.EXIT_CODES): serve
    did not serve, and a systemd unit reporting success while nothing listens is
    the worse lie.
    """
    r = cli_runner.invoke(cli.app, [
        # --bind, so the Docker bridge is never consulted: with --domain and no
        # --bind, `serve` reads edgesvc.bridge_address() before it reaches the
        # accounts check, and on a machine without Docker these three failed with
        # "could not read the Docker bridge address" instead of the message under
        # test. Nothing in this suite should need the engine.
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com",
        "--bind", "172.17.0.1", "--no-open"])
    assert r.exit_code == 3, f"want 3 (preflight), got {r.exit_code}"
    assert "error:" not in r.output, "this is a next step, not a failure"
    assert "needs an account" in r.output
    # The two paragraphs that pushed the commands off the top of the screen. The
    # DNS-label rule matters when a name is REFUSED, and names are normalised now;
    # the loopback aside answers a question nobody standing here is asking.
    assert "folded to a DNS label" not in r.output
    assert "one-time setup link" not in r.output
    assert served == {}, "nothing should be bound"


def test_the_domain_path_does_not_warn_about_publishing_before_refusing(served):
    """It used to warn "this publishes the GUI on the internet with no login" and
    then refuse to publish anything, one line apart.

    With --domain the bind is never loopback, so the accounts check ALWAYS refuses
    -- the warning described something that cannot happen, and two contradictory
    answers on one screen is what made the whole message untrustworthy.
    """
    r = cli_runner.invoke(cli.app, [
        # --bind, so the Docker bridge is never consulted: with --domain and no
        # --bind, `serve` reads edgesvc.bridge_address() before it reaches the
        # accounts check, and on a machine without Docker these three failed with
        # "could not read the Docker bridge address" instead of the message under
        # test. Nothing in this suite should need the engine.
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com",
        "--bind", "172.17.0.1", "--no-open"])
    assert "publishes the GUI" not in r.output, r.output
    assert "needs an account" in r.output


def test_insecure_is_not_advertised_as_replaceable_by_trust_proxy():
    """They answer different questions, and --help used to say otherwise while its
    own examples, and the running server's own hint, both told you to use
    --insecure. `--trust-proxy` cannot replace it: with no proxy in front there is
    no address to name."""
    r = cli_runner.invoke(cli.app, ["serve", "--help"])
    assert r.exit_code == 0
    flat = flat_help(r.output)
    assert "deprecated: prefer --trust-proxy" not in flat
    # --insecure is HIDDEN now: it is the only way to serve plain http on a
    # reachable bind, so it keeps working, but a first-time reader on localhost or
    # behind a lab's TLS should not have to weigh a security decision that does
    # not apply to them. It is taught where it is needed instead — see
    # test_the_refusal_is_where_insecure_is_taught.
    assert "--insecure" not in flat, "--help should not offer it unprompted"
    assert "--trust-proxy" in flat, "the one that DOES apply stays"
    # The footgun that ran for weeks looking like it worked.
    assert "a bare 0.0.0.0 is one address and matches nothing" in flat
    # And the case a first-timer is really in — localhost — comes first. The TLS
    # options used to be introduced by network examples, so the commonest reader
    # met them before learning they had no use for them.
    assert "TRYING IT OUT? There is nothing to learn: rc-repro serve" in flat
    # ...but not by claiming something that is false on a box already set up with
    # a domain, where plain `serve` keeps serving it and never touches loopback.
    # Reported from exactly such a box: `rc-repro serve` came up on https with the
    # public name, against a --help that had just promised localhost.
    assert "--no-domain for a local session" in flat


def test_loopback_with_no_accounts_prints_a_one_time_setup_link(served):
    r = cli_runner.invoke(cli.app, ["serve", "--no-open"])
    assert r.exit_code == 0, r.output
    line = next(ln for ln in r.output.splitlines() if "rc-repro GUI:" in ln)
    assert "/setup#k=" in line, line
    assert "?t=" not in line, "no credential in the query string any more"
    assert served["first_run"] is True


def test_the_setup_key_is_in_the_fragment_so_it_reaches_no_log(served):
    """A fragment is never sent to the server: not to an access log, not to a
    proxy log, not in a Referer."""
    r = cli_runner.invoke(cli.app, ["serve", "--no-open"])
    url = next(ln for ln in r.output.splitlines() if "rc-repro GUI:" in ln).split()[-1]
    path, _, frag = url.partition("#")
    assert path.endswith("/setup") and frag.startswith("k=")


def test_a_domain_with_no_accounts_refuses_rather_than_minting_anything(fronted):
    """Reported from EC2 against the old design: the page loaded and every API
    call answered "bad or missing token". There is no token to leave off now --
    a public name with no accounts does not start at all."""
    r = cli_runner.invoke(cli.app, [
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com"])
    assert r.exit_code != 0 and "needs an account" in r.output


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
    users.add("alice", GOOD)   # serve refuses to start without one
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
    users.add("alice", GOOD)   # serve refuses to start without one
    from rc_repro import config

    cli_runner.invoke(cli.app, [
        "serve", "--domain", "rc.example.com", "--email", "ops@example.com"])
    assert config.load_config().get("acme_email") == "ops@example.com"

    r = cli_runner.invoke(cli.app, ["serve", "--domain", "rc.example.com"])
    assert r.exit_code == 0, r.output      # no --email needed the second time


# --- `users add` mints a password, like the GUI always has -------------------------

def test_users_add_generates_the_password_and_shows_it_once():
    """The GUI has always minted these. The CLI took whatever a human typed above a
    twelve-character minimum, which is the only thing between an account and a
    distributed guessing attack -- the sign-in throttle bounds one address, and
    nothing bounds a thousand.

    Two properties: the password works, and it appears in the output exactly once
    because that is the only moment it exists in readable form anywhere.
    """
    r = cli_runner.invoke(cli.app, ["users", "add", "bob"])
    assert r.exit_code == 0, r.output

    # pull the minted value back out of the panel and prove it is the real one
    words = [w for w in r.output.replace("|", " ").split()
             if len(w) >= 16 and w not in ("rc-repro",)]
    minted = next(w for w in words if users.verify("bob", w))
    assert users.verify("bob", minted) is True
    assert r.output.count(minted) == 1, "shown once, or it is not 'shown once'"
    assert "Shown once" in r.output


def test_users_add_still_lets_you_type_one():
    """An escape hatch, not a default. Somebody restoring a documented account, or
    working where copying a generated string is awkward, still needs this."""
    r = cli_runner.invoke(cli.app, ["users", "add", "carol", "--ask-password"],
                          input="a-typed-out-password\na-typed-out-password\n")
    assert r.exit_code == 0, r.output
    assert users.verify("carol", "a-typed-out-password") is True
    assert "Shown once" not in r.output, "nothing was minted, so nothing to show"


def test_a_typed_password_that_does_not_match_is_refused():
    r = cli_runner.invoke(cli.app, ["users", "add", "dave", "--ask-password"],
                          input="one-good-password\na-different-password\n")
    assert r.exit_code != 0
    assert "do not match" in r.output
    assert users.list_users() == []


def test_users_passwd_mints_too_and_ends_the_old_sessions():
    from rc_repro.services import sessions as sessionsvc

    users.add("erin", GOOD, role="admin")
    token = sessionsvc.create("erin")
    assert sessionsvc.verify(token) is not None

    r = cli_runner.invoke(cli.app, ["users", "passwd", "erin"])
    assert r.exit_code == 0, r.output
    assert users.verify("erin", GOOD) is False, "the old password still worked"
    minted = next(w for w in r.output.replace("|", " ").split()
                  if len(w) >= 16 and users.verify("erin", w))
    assert users.verify("erin", minted) is True
    assert sessionsvc.verify(token) is None, "a reset must reach the live sessions"


def test_both_front_ends_mint_the_same_shape_of_password():
    """One definition of "a password rc-repro generated", or the two drift apart on
    the single value that decides whether an account is guessable."""
    import re

    from rc_repro.services import users as usersvc
    minted = usersvc.mint_password()
    assert len(minted) >= 16
    assert re.fullmatch(r"[A-Za-z0-9_-]+", minted), "must survive a URL and a shell"
    assert minted != usersvc.mint_password()
    # and it clears the policy the service enforces on typed ones
    usersvc.require_valid_password(minted)


def test_a_policy_typo_is_refused_rather_than_silently_meaning_strict():
    """A misspelt policy falls back to the strict reading, which is safe but silent
    — somebody who typed `anyonr` would believe they had opened the box."""
    bad = cli_runner.invoke(cli.app, ["config", "set", "gui.create_policy", "anyonr"])
    assert bad.exit_code != 0
    assert "takes 'anyone'" in bad.output

    from rc_repro.services import lifecycle as lc
    users.add("bob", GOOD, role="member")
    assert lc.may_set_privileged_fields("bob") is True, "open by default"

    ok = cli_runner.invoke(cli.app, ["config", "set", "gui.create_policy", "admin"])
    assert ok.exit_code == 0, ok.output
    assert lc.may_set_privileged_fields("bob") is False, "and narrowing works"
