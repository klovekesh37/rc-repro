"""Test isolation: every test gets a fresh RC_REPRO_HOME and a deterministic engine.

Without the home fixture, presets.load()/config.load_config() read the developer's real
~/.rc-repro (or ambient RC_REPRO_HOME) — a user preset override like presets/saml.yaml
would shadow the built-in and break unrelated tests.

The engine fixture exists because CLAUDE.md's "Nothing in the suite needs Docker" was
FALSE, by twenty-two tests across six files. Measured by building a PATH containing
every executable except `docker` and running the suite through it: an external review
saw a red gate where this box saw green, purely because that box had no Docker and this
one does — and GitHub's runner has it too, so CI never noticed either.

Two seams, because `require_docker` probes both: the daemon and the compose plugin. A
test that wants the engine DOWN monkeypatches these itself in its own body, which runs
after this fixture and therefore wins. Nothing here stubs the docker INVOCATION seams
(`runner.compose_stream`, `edge._docker`); a test that reaches those still shells out,
and both now return a non-zero result rather than raising when the binary is absent.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "rc-repro-home"))


@pytest.fixture(autouse=True)
def _deterministic_engine(monkeypatch):
    """`require_docker()` passes by default, whatever this machine has installed.

    Not a convenience: it is what makes the suite's result the same with and without a
    docker binary, which is the property the project claims and did not have.
    """
    from rc_repro import runner

    monkeypatch.setattr(runner, "docker_available", lambda **kw: True,
                        raising=False)
    monkeypatch.setattr(runner, "compose_version", lambda **kw: "2.29.0",
                        raising=False)
