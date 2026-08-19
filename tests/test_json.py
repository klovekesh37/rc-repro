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
