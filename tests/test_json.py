"""The machine-readable contract: `--json` on every command that has it.

Its own file rather than a corner of test_core.py, because it is its own layer.
`--json` is the surface a script, a CI step or an agent reads, and the promises it
makes are not the ones the human output makes: exactly one envelope, always last,
stdout parseable on its own, a stable error code, an exit code that says which kind
of failure it was. Those are contract tests, and a contract deserves a file where
breaking it is obvious.

Style follows test_services.py -- `pytest.raises` and direct assertions -- because
this exercises a service-shaped surface through the CLI rather than pure functions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rc_repro import cli, errors, jsonout, ui
from rc_repro.services import lifecycle as lc

runner_ = CliRunner()


@pytest.fixture(autouse=True)
def _reset_json_mode():
    """JSON mode is process-global on purpose (see jsonout._ACTIVE), and a test
    process runs many commands where a real one runs a single command. Reset so a
    `--json` test cannot silently move the next test's prose to stderr."""
    yield
    jsonout.json_mode_reset()


def _lines(out: str) -> list[dict]:
    return [json.loads(ln) for ln in out.splitlines() if ln.strip().startswith("{")]


# --- the vocabulary is closed, and provably so ---------------------------------

def test_every_phase_the_code_emits_is_in_the_published_vocabulary():
    """The promise `PHASES` makes is that a caller may branch on any name in it and
    will never meet one that is not. That is only true if it is checked: the file
    is a tuple somebody has to remember to update, and the phases are string
    literals scattered over eleven modules.

    A phase that is missing is not a crash -- it silently normalises to "info", and
    a caller watching for it simply never fires. This is the test that turns a
    documented vocabulary into an enforced one.
    """
    root = Path(cli.__file__).parent
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        found |= set(re.findall(r'phase="([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert found, "no phase literals found — the regex or the layout changed"
    missing = sorted(found - set(jsonout.PHASES))
    assert not missing, (
        f"phases emitted but not published: {missing}. Add them to jsonout.PHASES "
        "(and mind that this is a wire contract, so adding one is additive but "
        "removing one is not)")


def test_the_envelope_has_the_keys_the_contract_names():
    env = jsonout.envelope("info", {"a": 1}, warnings=[{"code": "X", "message": "m"}])
    assert env["schema"] == "rc-repro.info.v1"
    assert env["contract"] == jsonout.CONTRACT
    assert env["ok"] is True and env["error"] is None
    assert env["data"] == {"a": 1}
    assert env["warnings"] == [{"code": "X", "message": "m"}]
    assert env["rc_repro_version"] and env["generated_at"]
    # Never null, so a caller iterates without a None check.
    assert jsonout.envelope("list")["warnings"] == []


def test_an_error_envelope_carries_the_stable_code_not_the_class_name():
    env = jsonout.error_envelope(errors.NotReadyError("still booting"))
    assert env["ok"] is False and env["data"] is None
    assert env["error"]["code"] == "NOT_READY"
    assert env["schema"] == "rc-repro.error.v1"
    # An exception from outside the taxonomy is reported as unclassified rather
    # than dressed up as something a caller would act on.
    assert jsonout.error_envelope(RuntimeError("boom"))["error"]["code"] == "REPRO_ERROR"


def test_the_event_writer_keeps_its_two_promises():
    class Ev:
        def __init__(self, phase, pct=None, terminal=False, data=None):
            self.phase, self.pct, self.terminal = phase, pct, terminal
            self.level, self.message, self.data = "info", "m", data

    w = jsonout.EventWriter()
    assert w.event(Ev("boot", 40))["pct"] == 40
    # Monotonic: a bar that goes backwards reads as a new attempt starting.
    assert w.event(Ev("wait", 10))["pct"] == 40
    assert w.event(Ev("done", 90))["pct"] == 90
    # A phase outside the vocabulary normalises, and loses nothing doing it.
    odd = w.event(Ev("k6", data={"x": 1}))
    assert odd["phase"] == "info"
    assert odd["detail"] == {"x": 1, "phase_raw": "k6"}


def test_terminal_events_are_dropped_so_exactly_one_envelope_is_written(capsys):
    class Ev:
        phase, pct, level, message, data = "done", 100, "info", "finished", None
        terminal = True

    jsonout.EventWriter().emit(Ev())
    assert capsys.readouterr().out == "", \
        "a terminal event would be a second final object on a stream that promises one"


# --- the commands ---------------------------------------------------------------

def test_list_json_reports_an_empty_box_as_a_success(tmp_path, monkeypatch):
    """`No repros yet.` is the right answer for a person and the wrong shape for a
    script: a caller asking what is on this box and finding nothing has its answer,
    and it is not an error."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    res = runner_.invoke(cli.app, ["list", "--json"])
    assert res.exit_code == 0
    env = _lines(res.stdout)[-1]
    assert env["schema"] == "rc-repro.list.v1"
    assert env["ok"] is True and env["data"] == {"count": 0, "repros": []}
    assert "No repros yet" not in res.stdout


def test_list_json_answers_a_failure_with_an_envelope_not_a_traceback(
        tmp_path, monkeypatch):
    """`list_repros()` reads every repro.json and asks docker for project states, so
    it is not the obviously-safe line it looks like. Unguarded, a ReproError here
    reached a --json caller as a traceback with nothing on stdout -- which is the
    one thing an envelope exists to prevent."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def boom():
        raise errors.DockerError("docker is not running")

    monkeypatch.setattr(lc, "list_repros", boom)
    res = runner_.invoke(cli.app, ["list", "--json"])
    env = _lines(res.stdout)[-1]
    assert env["ok"] is False
    assert env["error"]["code"] == "ENGINE_UNAVAILABLE"
    assert res.exit_code == errors.DockerError.exit_code


def test_info_json_on_a_missing_name_is_an_envelope_with_the_right_exit(
        tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    res = runner_.invoke(cli.app, ["info", "--name", "nope", "--json"])
    env = _lines(res.stdout)[-1]
    assert env["error"]["code"] == "NOT_FOUND"
    assert res.exit_code == 4, "so a caller can tell 'no such repro' from 'not ready'"


def test_doctor_json_gives_every_row_a_declared_check_id(tmp_path, monkeypatch):
    """The published half of the report. Without an id a caller can count failures
    and nothing else; it cannot watch ONE check, which is the thing a CI step or a
    dashboard actually wants."""
    from rc_repro.services import doctor as doctorsvc

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    res = runner_.invoke(cli.app, ["doctor", "--json"])
    env = _lines(res.stdout)[-1]
    assert env["schema"] == "rc-repro.doctor.v1"
    rows = env["data"]["checks"]
    assert rows, "an empty report would pass every assertion below"
    for row in rows:
        assert row["check"] in doctorsvc.CHECKS, row
        assert row["status"] in ("ok", "warn", "fail")


def test_a_doctor_check_id_does_not_change_with_its_own_outcome(monkeypatch):
    """The trap this was written against: deriving the id from the MESSAGE gives
    `port-3000-free` when the port is free and `port-3000-in` when it is not, so an
    agent watching one id never sees the other state. One check, one id, whatever it
    finds."""
    from rc_repro.services import doctor as doctorsvc

    seen: dict[str, set[str]] = {}
    for free in (True, False):
        monkeypatch.setattr(doctorsvc.runner, "port_free", lambda *_a, **_k: free)
        for row in doctorsvc.run_checks()["checks"]:
            seen.setdefault(row["check"], set()).add(row["status"])
    ports = [c for c in seen if c == "ports"]
    assert ports == ["ports"], "the port check reported under more than one id"
    # And no id has a runtime value baked into it.
    for cid in seen:
        assert not re.search(r"\d", cid), f"check id {cid!r} carries a value"


def test_down_volumes_json_refuses_rather_than_keeping_the_data_quietly(
        tmp_path, monkeypatch):
    """There is nobody to prompt on this path. The two ways out of that are deleting
    the data unasked and quietly doing something other than what was typed; for an
    irreversible verb neither is acceptable, so it asks for the confirmation to be
    explicit."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    ws = tmp_path / "repros" / "r"
    ws.mkdir(parents=True)
    (ws / "repro.json").write_text(json.dumps({
        "name": "r", "project": "p", "rc_version": "8.5.1", "rc_image": "i",
        "mongo_tag": "8.0", "mongo_flavor": "official", "preset": "default",
        "root_url": "http://localhost:3001", "host_port": 3001,
        "version_source": "x", "extra": {}}))
    (ws / "docker-compose.yml").write_text("services: {}\n")

    res = runner_.invoke(cli.app, ["down", "--name", "r", "--volumes", "--json"])
    env = _lines(res.stdout)[-1]
    assert env["ok"] is False
    assert env["error"]["code"] == "VALIDATION_FAILED"
    assert "--yes" in env["error"]["message"]
    assert res.exit_code == 2
    # And the workspace is still there: a refusal that deleted anything would be
    # worse than the behaviour it replaced.
    assert (ws / "repro.json").exists()


def test_json_mode_keeps_stdout_a_document_and_moves_prose_to_stderr(
        tmp_path, monkeypatch):
    """`rc-repro info --json > case.json` has to produce a valid document, and the
    person who ran it should still see what happened. Two streams answer both;
    silence would answer only the first.

    Asserted on a command whose HUMAN output is a panel of boxes, so anything left
    behind on stdout would be unmissable.
    """
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    res = runner_.invoke(cli.app, ["info", "--name", "nope", "--json"])
    for line in res.stdout.splitlines():
        json.loads(line)          # raises if a single prose line got through
    assert "error:" in res.stderr, "the human line went missing entirely"


def test_the_error_paths_that_predate_the_contract_still_answer_in_it(
        tmp_path, monkeypatch):
    """Forty-eight call sites report through `_err` -- a flat exit 1 with a red
    line, written long before this module existed. In JSON mode they have to be
    envelopes too, or the contract holds right up until something goes wrong, which
    is the moment a caller needed it.

    `up --set` with no `=` is one of them, and it never touches a service.
    """
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    res = runner_.invoke(cli.app, ["up", "--version", "8.5.1", "--set", "nonsense",
                                   "--json"])
    env = _lines(res.stdout)[-1]
    assert env["ok"] is False
    assert env["error"]["code"] == "REPRO_ERROR", "unclassified, and honest about it"
    assert res.exit_code == 1, "unchanged from the human path"


def test_ui_json_mode_moves_every_helper_to_stderr(capsys):
    """The seam ui.py's docstring has always promised. It exists because the prose
    is written several frames below the flag: a panel printed by
    `_render_create_result` cannot see the `--json` its command took."""
    ui.json_mode(True)
    try:
        ui.ok("green"); ui.note("cyan"); ui.hint("dim")
        ui.panel("t", [("k", "v")])
        out = capsys.readouterr()
        assert out.out == "", "stdout must stay a JSON document"
        for word in ("green", "cyan", "dim"):
            assert word in out.err
    finally:
        ui.json_mode(False)


# --- capabilities: the build describing itself ----------------------------------

def test_capabilities_reports_exactly_the_commands_that_really_speak_json():
    """The invariant that makes the document trustworthy, and the bug it exists for.

    A caller reads this to avoid hardcoding a flag list; if it can advertise a
    command that does not answer in JSON, it has moved the same guess one level up
    and made it authoritative. The version borrowed from elsewhere advertised
    `rc-repro.loadtest.v1` and `rc-repro.capacity.v1` for two commands that printed
    a bare payload, so an agent reaching for `.data` got nothing.

    Derived from the registered app on BOTH sides -- the truth is the `--json`
    parameter on the callback, and this asserts the document agrees with it.
    """
    import inspect

    doc = jsonout.capabilities(cli.app)
    advertised = {c["name"] for c in doc["commands"] if c["json"]}

    real = set()
    def scan(cmd, prefix=""):
        cb = getattr(cmd, "callback", None)
        if cb is None:
            return
        name = (cmd.name or cb.__name__.replace("_", "-")).removesuffix("-cmd")
        for param in inspect.signature(cb).parameters.values():
            for decl in (getattr(param.default, "param_decls", None) or ()):
                if decl == "--json":
                    real.add((prefix + name).strip())

    for cmd in cli.app.registered_commands:
        scan(cmd)
    for group in getattr(cli.app, "registered_groups", []):
        for cmd in getattr(group.typer_instance, "registered_commands", []):
            scan(cmd, prefix=f"{group.name} ")

    # THE FLAG IS A PROXY, AND FOR ONE COMMAND IT IS THE WRONG ONE. `capabilities`
    # answers in an envelope and always has -- it has no `--json` because it has no
    # other mode -- so deriving purely from the parameter made the one document whose
    # purpose is saying which commands speak JSON report that this one does not, while
    # speaking it. Named here rather than special-cased silently, and the claim is not
    # taken on trust: every name in JSON_COMMANDS, this one included, is driven to
    # completion by test_every_json_command_answers_with_exactly_one_envelope.
    ALWAYS_JSON = {"capabilities"}
    assert advertised == real | ALWAYS_JSON, (
        f"capabilities advertises {sorted(advertised - real - ALWAYS_JSON)} that do "
        f"not take --json, and omits {sorted((real | ALWAYS_JSON) - advertised)} "
        f"that do")
    assert ALWAYS_JSON <= set(JSON_COMMANDS), (
        "a command declared always-JSON must be exercised by the envelope test")
    assert advertised, "no command speaks JSON, which cannot be right"


def test_capabilities_lists_a_grouped_subcommand():
    """`skill install` lives under a group, and the obvious implementation walks
    only `registered_commands` -- so grouped subcommands vanish from the document
    that claims to list everything. Silently, and only once a group exists."""
    names = {c["name"] for c in jsonout.capabilities(cli.app)["commands"]}
    assert "skill install" in names and "skill status" in names


def test_capabilities_does_not_advertise_an_error_code_nothing_can_raise():
    """`errors.GATE_CODES` names seven authority gates and this build raises none of
    them -- `AuthorityGateError` is defined and never used. Publishing them would
    teach a caller to write a branch that never runs, which is the same class of
    wrongness as advertising a flag that does not exist."""
    codes = set(jsonout.capabilities(cli.app)["error_codes"])
    assert "VALIDATION_FAILED" in codes and "NOT_READY" in codes
    assert not (codes & set(errors.GATE_CODES)), sorted(codes & set(errors.GATE_CODES))


def test_capabilities_says_which_commands_stream_by_looking_at_them():
    """`up` and `ready` build an EventWriter; `down` does not. A hardcoded list of
    verb names had all three, which is wrong today and would rot anyway."""
    streams = {c["name"]: c.get("streams") for c in jsonout.capabilities(cli.app)["commands"]
               if c["json"]}
    assert streams["up"] is True and streams["ready"] is True
    assert streams["down"] is False, "down is synchronous; claiming a stream is a lie"


def test_capabilities_needs_no_engine_and_no_home(tmp_path, monkeypatch):
    """It is asked BEFORE anybody knows whether the environment works."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(cli.runner, "docker_available",
                        lambda **_k: pytest.fail("capabilities asked docker"))
    res = runner_.invoke(cli.app, ["capabilities"])
    assert res.exit_code == 0
    assert json.loads(res.stdout.strip())["data"]["rc_repro_version"]


# --- the invariant that closes the contract -------------------------------------

#: How to drive each `--json` command so it terminates without a workspace, and
#: whether that run should succeed. The TABLE is checked against the app below, so a
#: command that gains `--json` and is not listed here fails this file rather than
#: shipping unexercised -- which is the whole point: the contract is "every --json
#: reply is exactly one envelope, last", and a command nobody drove is a command
#: nobody knows that about.
JSON_COMMANDS: dict[str, tuple[list[str], bool]] = {
    "list": (["list", "--json"], True),
    "doctor": (["doctor", "--json"], True),
    "skill status": (["skill", "status", "--json"], True),
    "skill install": (["skill", "install", "--json"], True),
    "info": (["info", "--name", "nope", "--json"], False),
    "ready": (["ready", "--name", "nope", "--json"], False),
    "down": (["down", "--name", "nope", "--json"], False),
    "up": (["up", "--version", "8.5.1", "--set", "nonsense", "--json"], False),
    "loadtest": (["loadtest", "--name", "nope", "--json"], False),
    "capacity": (["capacity", "--name", "nope", "--json"], False),
    # No `--json`: it has no other mode. See ALWAYS_JSON above.
    "capabilities": (["capabilities"], True),
}


def test_the_table_below_covers_every_command_that_takes_json():
    """Derived from the app, not from a list somebody remembers to update."""
    advertised = {c["name"] for c in jsonout.capabilities(cli.app)["commands"] if c["json"]}
    assert advertised == set(JSON_COMMANDS), (
        f"not exercised: {sorted(advertised - set(JSON_COMMANDS))}; "
        f"listed but gone: {sorted(set(JSON_COMMANDS) - advertised)}")


@pytest.mark.parametrize("name", sorted(JSON_COMMANDS))
def test_every_json_command_answers_with_exactly_one_envelope(
        name, tmp_path, monkeypatch):
    """The single promise the whole contract rests on.

    A caller reads to end of stream and parses the LAST line. That only works if
    there is exactly one envelope and it is last -- so this drives every `--json`
    command to completion and checks the stream itself, on the success paths and,
    more importantly, on the failure paths, where a contract usually breaks: the
    error happens somewhere nobody remembered to wrap, and the caller gets an empty
    stdout with a non-zero exit.
    """
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RC_REPRO_SKILL_HOME", str(tmp_path / "skills"))
    # THE ENGINE, MADE DETERMINISTIC. `want_ok` below is a fact about the contract,
    # not about this machine -- and `doctor` and `capacity` legitimately answer
    # ok=false/exit 3 on a box with no Docker, so the table was only true where one
    # happened to be installed. CLAUDE.md says nothing in the suite needs Docker;
    # this is what makes that so for these two rather than hoping for it. Stubbed at
    # the seam every path funnels through, so the command still runs end to end.
    from rc_repro import runner as _runner
    monkeypatch.setattr(_runner, "docker_available", lambda **k: True)
    monkeypatch.setattr(_runner, "compose_version", lambda **k: "2.29.0")
    argv, want_ok = JSON_COMMANDS[name]
    res = runner_.invoke(cli.app, argv)

    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert lines, f"{name} wrote nothing to stdout (exit {res.exit_code})"
    objs = []
    for ln in lines:
        objs.append(json.loads(ln))          # every line is JSON, or this raises
    envelopes = [o for o in objs if o.get("schema", "").startswith("rc-repro.")
                 and "ok" in o]
    assert len(envelopes) == 1, f"{name} wrote {len(envelopes)} envelopes"
    assert objs[-1] is envelopes[0], f"{name}'s envelope is not the last line"

    env = envelopes[0]
    assert env["contract"] == jsonout.CONTRACT
    assert env["ok"] is want_ok, f"{name}: ok={env['ok']} (exit {res.exit_code})"
    if want_ok:
        assert env["error"] is None and res.exit_code == 0
    else:
        # A failure names a code from the published set, and exits non-zero.
        assert env["error"]["code"] in jsonout.capabilities(cli.app)["error_codes"]
        assert res.exit_code != 0


def test_a_workspace_that_is_not_answering_yet_exits_five_not_one(monkeypatch, tmp_path):
    """Every REST command's failure message told the reader to run `rc-repro ready`, and
    every one of them exited 1 -- the code that tells a script not to bother polling. And
    `services/monitor.py` raises NotReadyError for the identical condition, so an
    unreachable workspace answered differently depending on which verb you reached for.

    Measured: `api` against a workspace whose Rocket.Chat container was still coming back
    from an `env` change exited 1 while saying "ready?".
    """
    import json as _json

    import requests
    from typer.testing import CliRunner

    from rc_repro import cli, errors, rcapi

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    (tmp_path / "repros" / "w").mkdir(parents=True)
    (tmp_path / "repros" / "w" / "repro.json").write_text(_json.dumps({
        "name": "w", "project": "p", "rc_version": "8.5.1", "rc_image": "i",
        "mongo_tag": "8.0", "mongo_flavor": "official", "preset": "default",
        "root_url": "http://localhost:3001", "host_port": 3001, "version_source": "x",
        "extra": {}}))
    # `_require_engine`, not `_require_docker`: the gate is runtime-aware now, so a
    # stub on the old name is inert and this asserted exit 5 while getting 3 from a
    # real Docker check -- invisible on any box that happens to have Docker running.
    monkeypatch.setattr(cli, "_require_engine", lambda target: None)

    def refuse(*_a, **_k):
        raise requests.ConnectionError("connection refused")
    monkeypatch.setattr(rcapi, "login", refuse)

    runner = CliRunner()
    res = runner.invoke(cli.app, ["api", "--name", "w", "GET", "/api/v1/me"])
    assert res.exit_code == 5, f"exit {res.exit_code}: {res.output}"
    assert errors.EXIT_CODES[5] == "not_ready", "the code this asserts must keep meaning"
    assert "not answering yet" in res.output, res.output

    # A 429 is the other "wait and it will work": Rocket.Chat rate-limits token
    # creation, so the `--save baseline` / `--compare baseline` pair the README prints
    # had its second half refused, and it read like a broken workspace.
    def limited(*_a, **_k):
        raise RuntimeError("429 Client Error: Too Many Requests for url: ...")
    monkeypatch.setattr(rcapi, "login", limited)
    res = runner.invoke(cli.app, ["api", "--name", "w", "GET", "/api/v1/me"])
    assert res.exit_code == 5, f"exit {res.exit_code}: {res.output}"
    assert "rate-limiting" in res.output and "about a minute" in res.output, res.output

    # A refused LOGIN is not something waiting fixes, so it stays unclassified -- and
    # it no longer tells the reader to run `ready`, which could not have helped.
    def deny(*_a, **_k):
        raise RuntimeError("Unauthorized")
    monkeypatch.setattr(rcapi, "login", deny)
    res = runner.invoke(cli.app, ["api", "--name", "w", "GET", "/api/v1/me"])
    assert res.exit_code == 1, res.output
    assert "ready" not in res.output.lower(), res.output

def test_no_private_function_is_registered_as_a_command():
    """A guard against a real accident. Inserting a helper directly above a decorated
    command puts it BETWEEN `@app.command(...)` and its function, so the decorator binds
    the HELPER and the real command is orphaned -- silently. It happened to `edge`:
    `rc-repro edge --help` started reporting `Usage: rc-repro edge [OPTIONS] {domain}`
    with a `--tries` option, `edge status` stopped existing, and `edge logs --tail 2`
    answered "No such option: --tail". Nothing failed; the command was simply somebody
    else's function.

    Naming is the invariant that catches it: every callback registered with Typer is a
    command somebody types, so none of them may be private."""
    from rc_repro import cli

    bound = [(c.name or c.callback.__name__, c.callback.__name__)
             for c in cli.app.registered_commands]
    private = [(name, fn) for name, fn in bound if fn.startswith("_")]
    assert not private, (
        "a private helper is registered as a CLI command, which means a decorator "
        f"is attached to the wrong function: {private}")


def test_every_documented_edge_action_is_reachable():
    """The same accident again, from the user's side: `edge` advertises five actions in
    its own help, and all five have to exist."""
    from rc_repro import cli

    edge = next(c for c in cli.app.registered_commands if (c.name or "") == "edge")
    assert edge.callback.__name__ == "edge_cmd", edge.callback.__name__
    import inspect
    src = inspect.getsource(edge.callback)
    for action in ("status", "start", "stop", "restart", "logs"):
        assert f'"{action}"' in src, f"`edge {action}` is advertised and unhandled"



def test_every_advertised_error_code_can_actually_be_raised():
    """The third registry walk. `capabilities` published a code nothing raises.

    `_error_codes()` deliberately leaves `errors.GATE_CODES` out and says why in its
    own docstring -- "advertising a code nothing can produce is the exact failure this
    document exists to prevent, and it would teach a caller to write a branch that
    never runs". The exclusion was applied to the REGISTRY and not to the class that
    carries them, so the walk picked up the base `GATE` anyway and `capabilities`
    advertised it on a build with no `raise AuthorityGateError` anywhere.

    This is the same shape as the job pools and the journal: a rule that is stated
    correctly in prose and not enforced by a walk.
    """
    import ast
    import pathlib

    from rc_repro import errors, jsonout

    root = pathlib.Path(errors.__file__).parent
    raised: set[str] = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                                   # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            exc = node.exc
            fn = exc.func if isinstance(exc, ast.Call) else exc
            name = ast.unparse(fn).rsplit(".", 1)[-1]
            cls = getattr(errors, name, None)
            if isinstance(cls, type) and getattr(cls, "code", None):
                raised.add(cls.code)

    advertised = set(jsonout._error_codes())
    # REPRO_ERROR is the base and is raised through `_err`/`_fail` wrappers rather
    # than by name, so it is always producible.
    unraisable = advertised - raised - {errors.ReproError.code}
    assert not unraisable, (
        f"capabilities advertises {sorted(unraisable)}, which nothing in rc_repro/ "
        f"raises — a caller told to expect it would write a branch that never runs")

    # And the converse guard, so the exclusion cannot quietly hide a real code: an
    # error class that IS raised must be advertised.
    assert "GATE" not in advertised, (
        "GATE is advertised again — set AuthorityGateError.advertised in the same "
        "commit that adds the first raise, not before")


def test_an_unhandled_exception_is_still_one_envelope_on_stdout():
    """"Exactly one envelope, and it is the last line" is the single promise a caller
    is told it may depend on, and four reachable states broke it.

    A workspace directory with a docker-compose.yml and no repro.json
    (FileNotFoundError -- what an interrupted create leaves between the two
    atomic_writes), a repro.json missing fields (TypeError), a malformed config.yaml
    (yaml.parser.ParserError) and a non-writable RC_REPRO_HOME (PermissionError) each
    produced a traceback, exit 1, and ZERO BYTES of stdout under `--json`. The contract
    held until the moment a caller needed it.

    Driven through `cli.main`, because that is where the handler lives and where the
    console script points -- `CliRunner` catches exceptions itself, so invoking the
    Typer app would assert against a safety net the real binary does not have.
    """
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    for label, build in (
        ("no repro.json", lambda h: (
            (h / "repros" / "half").mkdir(parents=True),
            (h / "repros" / "half" / "docker-compose.yml").write_text("services: {}"),
            ["info", "--name", "half", "--json"])[-1]),
        ("truncated repro.json", lambda h: (
            (h / "repros" / "trunc").mkdir(parents=True),
            (h / "repros" / "trunc" / "repro.json").write_text('{"name":"trunc"}'),
            (h / "repros" / "trunc" / "docker-compose.yml").write_text("services: {}"),
            ["info", "--name", "trunc", "--json"])[-1]),
        ("malformed config.yaml", lambda h: (
            (h / "config.yaml").write_text("a: b: c: ["),
            ["list", "--json"])[-1]),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            argv = build(home)
            proc = subprocess.run(
                [sys.executable, "-c",
                 "from rc_repro.cli import main; main()", *argv],
                capture_output=True, text=True, timeout=120,
                env={"PATH": "/usr/bin:/bin", "RC_REPRO_HOME": str(home),
                     "HOME": str(home), "PYTHONPATH": str(Path(__file__).parent.parent)},
            )
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            assert lines, f"{label}: stdout was EMPTY under --json (exit {proc.returncode})"
            doc = json.loads(lines[-1])
            assert doc["ok"] is False, f"{label}: {doc}"
            assert doc["error"]["code"], f"{label}: no error code"
            assert proc.returncode != 0, f"{label}: exited 0 on a failure"


def test_the_envelope_warnings_array_is_actually_populated():
    """`warnings` was in the envelope from the start and nothing ever wrote to it.

    `grep -n 'warnings=' cli.py` returned nothing and all ten `reply()` sites omitted
    it, so a caller reading the documented field for the documented purpose got `[]`
    every time. The sharpest case is asserted here because it is the one a caller
    actually hits: `doctor --json` answered `{"ok": true, "warnings": [], "data":
    {"verdict": "warn", "counts": {"warn": 6, ...}}}` -- warnings counted in `data`,
    none in `warnings`, reachable only through `data.checks[].status`, which is not
    what a caller is told to read.

    Driven through the real command rather than the helper, so this fails against the
    pre-fix tree because the ARRAY IS EMPTY, not because a function has been renamed.
    """
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, "-c", "from rc_repro.cli import app; app()",
             "doctor", "--json"],
            capture_output=True, text=True, timeout=180,
            env={"PATH": "/usr/bin:/bin", "RC_REPRO_HOME": tmp, "HOME": tmp,
                 "PYTHONPATH": str(Path(__file__).parent.parent)},
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"doctor --json wrote nothing (exit {proc.returncode})"
    doc = json.loads(lines[-1])
    counted = int(doc["data"]["counts"].get("warn", 0)) + \
        int(doc["data"]["counts"].get("fail", 0))
    assert counted > 0, (
        "this box's doctor reports no warnings at all, so the assertion below cannot "
        f"mean anything: {doc['data']['counts']}")
    assert len(doc["warnings"]) == counted, (
        f"doctor counted {counted} warn/fail checks and put {len(doc['warnings'])} in "
        f"the envelope's `warnings` — the field a caller is told to read")
    for w in doc["warnings"]:
        assert w.get("code", "").startswith("DOCTOR_"), w
        assert w.get("message"), w


def test_a_domain_error_escaping_a_command_exits_with_its_own_code_and_no_traceback():
    """`main()` guarded the BaseException branch against `typer.Exit` and left the
    `ReproError` branch three lines above it unguarded.

    `typer.Exit` is RuntimeError-derived, NOT a SystemExit:

        >>> [c.__name__ for c in typer.Exit.__mro__][:3]
        ['Exit', 'RuntimeError', 'Exception']

    Click converts it inside `app()`; nothing does out here. So `_fail(exc)` called
    from `main()` propagated as an uncaught exception -- the right message, then a
    traceback, then exit 1 where the published table says 4. The comment on the
    `except SystemExit` line claimed it covered "every `_fail`/`_err`", which is
    exactly the belief that produced the bug.

    Driven as a subprocess through `cli.main`, because CliRunner catches exceptions
    itself and would assert against a safety net the real binary does not have.
    """
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    root = str(Path(__file__).parent.parent)
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, "-c", "from rc_repro.cli import main; main()",
             "env", "--name", "definitely-not-a-repro"],
            capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "RC_REPRO_HOME": tmp, "HOME": tmp,
                 "PYTHONPATH": root},
        )
    assert proc.returncode == 4, (
        f"exited {proc.returncode}, want 4 (not_found)\n{proc.stderr[-400:]}")
    assert "Traceback" not in proc.stderr, proc.stderr[-600:]


def test_a_failure_before_any_command_still_writes_one_envelope():
    """`--json` is a command PARAMETER, so `jsonout.activate()` runs inside the command
    -- and some failures happen before any command does.

    `_before_any_command` is the sharp one: it reads the accounts file on EVERY
    invocation, so an unreadable or wrong-type `~/.rc-repro/users` -- a stale
    directory, or a file owned by another user on the shared box this tool is built
    for -- failed there and left a `--json` caller with a traceback and ZERO BYTES of
    stdout. That is the fifth member of the crash-class family v0.73.0 closed four of.
    """
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    root = str(Path(__file__).parent.parent)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "users").mkdir()          # a DIRECTORY where a file belongs
        proc = subprocess.run(
            [sys.executable, "-c", "from rc_repro.cli import main; main()",
             "list", "--json"],
            capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "RC_REPRO_HOME": tmp, "HOME": tmp,
                 "PYTHONPATH": root},
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"stdout EMPTY under --json (exit {proc.returncode})\n{proc.stderr[-400:]}"
    doc = json.loads(lines[-1])
    assert doc["ok"] is False and doc["error"]["code"], doc
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr, proc.stderr[-600:]


def test_no_json_command_writes_a_payload_outside_the_envelope():
    """The failure path was covered and the SUCCESS path was the broken one.

    `JSON_COMMANDS` drives `loadtest` and `capacity` with `--name nope`, which fails at
    `_resolve_name` and exits through `_fail` -- a correct envelope. So the table proved
    the contract on the only path where it already held, while both commands printed
    `typer.echo(json.dumps(result, indent=2))` on success: no `schema`, no `contract`,
    no `ok`, no `error`, over many lines. `jsonout`'s module docstring names these two
    as the bug it was written for, and it had been fixed for the error path only.

    README sells `--slo` as the thing that "drops into CI" and tells that CI step to
    read to end of stream and parse the last line. The last line of `capacity --json`
    was `}`.

    ASSERTED STRUCTURALLY, and here is the honest reason: driving either command to its
    success path needs k6, a live workspace and a PAT, so a unit test can only get there
    through a stub chain deep enough that it would be asserting against the stubs. What
    can be checked without Docker is that no command in this file's own table writes a
    machine payload outside `jsonout` -- which is the mistake that was actually made,
    and which a bare `json.dumps` cannot satisfy. The end-to-end success path stays a
    live-verification item.
    """
    import ast
    import pathlib as _pl

    from rc_repro import cli

    tree = ast.parse(_pl.Path(cli.__file__).read_text())
    offenders: list[str] = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        body = ast.unparse(fn)
        # Only commands that speak JSON at all can break this contract.
        if "jsonout.activate()" not in body:
            continue
        for call in [c for c in ast.walk(fn) if isinstance(c, ast.Call)]:
            rendered = ast.unparse(call)
            if rendered.startswith("typer.echo(json.dumps"):
                offenders.append(f"{fn.name}: {rendered[:70]}")
            # `print` would bypass `ui`'s stderr redirection too.
            if rendered.startswith("print(json.dumps"):
                offenders.append(f"{fn.name}: {rendered[:70]}")

    assert not offenders, (
        "these write a JSON payload straight to stdout, outside the envelope, in a "
        "command that has told the caller stdout is a machine document:\n  "
        + "\n  ".join(offenders))

    # And both DO reach `jsonout.reply`, so the fix is present rather than the bad call
    # merely having been deleted.
    for name in ("loadtest", "capacity"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        assert "jsonout.reply(" in ast.unparse(fn), f"{name} emits no envelope"
