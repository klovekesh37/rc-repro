"""Test isolation: every test gets a fresh RC_REPRO_HOME.

Without this, presets.load()/config.load_config() read the developer's real
~/.rc-repro (or ambient RC_REPRO_HOME) — a user preset override like
presets/saml.yaml would shadow the built-in and break unrelated tests.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "rc-repro-home"))
