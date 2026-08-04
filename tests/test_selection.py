"""Public deployment/scenario selector contract tests."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from rc_repro import config, jsonout, presets, runner
from rc_repro.errors import ValidationError
from rc_repro.services import lifecycle


def test_legacy_aliases_resolve_to_the_existing_aggregate():
    ldap = presets.resolve_selection(preset="ldap", params={"users": "3"})
    microservices = presets.resolve_selection(preset="microservices")

    assert ldap.deployment == "default"
    assert ldap.scenarios == ("ldap",)
    assert ldap.preset.topology == "compose"
    assert ldap.preset.scenario_params["users"] == 3
    assert microservices.deployment == "microservices"
    assert microservices.scenarios == ()
    assert microservices.preset.topology == "kubernetes"


def test_independent_selectors_render_ldap_for_kubernetes():
    selected = presets.resolve_selection(
        deployment="microservices", scenarios=["ldap"], params={"users": "4"})

    assert selected.label == "microservices-ldap"
    assert selected.preset.topology == "kubernetes"
    assert selected.preset.requires_license is True
    assert selected.preset.scenario == "ldap"
    assert selected.preset.scenario_params["users"] == 4


def test_scenario_set_and_unsupported_pair_fail_before_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    with pytest.raises(ValidationError, match="does not support scenario 'email'"):
        lifecycle.create_repro(lifecycle.CreateReq(
            version="8.6.1", preset="", deployment="microservices",
            scenario=["email"], name="should-not-exist"))
    assert not runner.exists("should-not-exist")

    with pytest.raises(ValidationError, match=r"scenario set \[ldap, email\]"):
        lifecycle.create_repro(lifecycle.CreateReq(
            version="8.6.1", preset="", deployment="microservices",
            scenario=["ldap", "email"], name="also-should-not-exist"))
    assert not runner.exists("also-should-not-exist")


def test_public_kubernetes_selector_reaches_the_native_adapter(tmp_path, monkeypatch):
    from rc_repro.services import k8s, onboarding

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize", "owned-cluster"])
    seen = {}

    def fake_create(name, version, **kwargs):
        seen.update(name=name, version=version, **kwargs)
        return {"name": name, "topology": "kubernetes"}

    monkeypatch.setattr(k8s, "create_repro", fake_create)
    lifecycle.create_repro(lifecycle.CreateReq(
        version="8.6.1", preset="", deployment="microservices",
        scenario=["ldap"], params={"users": "3"}, name="public-ldap-k8s"))

    assert seen["preset"].scenario == "ldap"
    assert seen["preset"].scenario_params["users"] == 3
    assert seen["preset"].topology == "kubernetes"


def test_saved_selector_defaults_are_additive_and_legacy_config_survives(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    config.save_config({
        "default_repro": "old",
        "default_deployment": "microservices",
        "default_scenarios": ["ldap"],
        "reg_token": "redacted-in-test-config",
    })

    selected = presets.resolve_selection(preset="", params={"users": "2"})
    raw = config.load_config(with_env=False)

    assert selected.deployment == "microservices"
    assert selected.scenarios == ("ldap",)
    assert selected.preset.scenario_params["users"] == 2
    assert raw["default_repro"] == "old"
    assert raw["reg_token"] == "redacted-in-test-config"

    selected_scenario_only = presets.resolve_selection(
        preset="", scenarios=["ldap"], params={"users": "1"})
    assert selected_scenario_only.deployment == "microservices"
    assert selected_scenario_only.scenarios == ("ldap",)


def test_cli_exposes_selectors_and_structured_refusal(tmp_path, monkeypatch):
    from rc_repro.cli import app

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(app, [
        "up", "--version", "8.6.1", "--deployment", "microservices",
        "--scenario", "email", "--json",
    ])

    assert result.exit_code == ValidationError.exit_code
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == ValidationError.code
    assert not list((tmp_path / "home").glob("repros/*"))

    caps = jsonout.capabilities(app)
    assert caps["selection"]["deployment_presets"] == [
        "default", "microservices", "multi-instance"]
    assert "ldap" in caps["selection"]["scenario_names"]
    up = next(c for c in caps["commands"] if c["name"] == "up")
    assert "--deployment" in up["flags"] and "--scenario" in up["flags"]


def test_custom_yaml_remains_a_legacy_preset(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    config.preset_dir().mkdir(parents=True)
    (config.preset_dir() / "ldap.yaml").write_text(
        "name: ldap\ndescription: local override\nenv: {CUSTOM: value}\n",
        encoding="utf-8",
    )

    selected = presets.resolve_selection(preset="ldap")
    assert selected.preset.description == "local override"
    assert selected.scenarios == ()
    assert selected.label == "ldap"

    with pytest.raises(ValueError, match="custom preset 'ldap' cannot be combined"):
        presets.resolve_selection(preset="ldap", deployment="microservices")
    with pytest.raises(ValueError, match="shadows the built-in scenario"):
        presets.resolve_selection(preset="", deployment="microservices",
                                  scenarios=["ldap"])
