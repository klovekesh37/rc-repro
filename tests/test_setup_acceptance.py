"""Cross-surface onboarding acceptance matrix (ticket 05)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rc_repro import jsonout
from rc_repro.cli import app
from rc_repro.services import onboarding

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rc_repro.web.app import create_app  # noqa: E402

TOKEN = "secret-token"
H = {"X-RC-Repro-Token": TOKEN}


def _env(**overrides):
    base = {
        "os": "TestOS", "os_version": "1", "architecture": "x86_64",
        "cpus": 4, "memory_gib": 16.0, "disk_free_gib": 80.0,
        "tools": {"docker": "ok", "compose": "ok", "kind": "ok",
                  "kubectl": "ok", "helm": "ok"},
        "docker_ready": True, "engine_provider": "podman",
        "engine_memory_gib": 15.0, "engine_cpus": 4,
        "engine_kernel_version": "6.18.0",
        "missing_kubernetes_tools": [],
        "microservices_ready": True,
        "engine_resize_supported": False, "engine_resize_relevant": False,
    }
    base.update(overrides)
    return base


def test_acceptance_compose_paths_agree_cli_json_and_gui(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: _env(
        engine_provider="docker",
        missing_kubernetes_tools=["kind"],
        microservices_ready=False,
    ))

    # Non-TTY structured CLI path.
    cli = CliRunner().invoke(app, [
        "onboard", "--accept-defaults", "--json",
        "--deployment", "default", "--scenario", "ldap",
        "--seed-profile", "small", "--teardown-by-default",
    ])
    assert cli.exit_code == 0, cli.output
    payload = json.loads(cli.stdout)
    assert payload["ok"] is True
    assert payload["data"]["selection"]["deployment"] == "default"
    assert payload["data"]["selection"]["scenarios"] == ["ldap"]
    assert payload["data"]["selection"]["topology"] == "compose"
    assert "owned_cluster" not in {
        q["id"] for q in payload["data"]["setup"]["questions"]}
    first_cmd = payload["data"]["first_run_command"]
    assert "--scenario ldap" in first_cmd or "ldap" in first_cmd

    # Service-layer patch path (what GUI POST /api/setup uses).
    snap = onboarding.setup_snapshot()
    assert snap["selection"]["deployment"] == "default"
    assert snap["selection"]["scenarios"] == ["ldap"]
    assert snap["first_run_command"] == first_cmd

    # GUI API surface.
    c = TestClient(create_app(token=TOKEN), base_url="http://localhost")
    r = c.get("/api/setup", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["selection"]["deployment"] == "default"
    assert body["selection"]["scenarios"] == ["ldap"]
    assert body["first_run_command"] == first_cmd
    assert body["schema"] == onboarding.SETUP_SCHEMA

    # Partial reconfigure via GUI-shaped patch preserves other choices.
    r2 = c.post("/api/setup", headers=H, json={"seed_profile": "standard"})
    assert r2.status_code == 200
    assert r2.json()["selection"]["seed_profile"] == "standard"
    assert r2.json()["selection"]["scenarios"] == ["ldap"]
    assert r2.json()["selection"]["deployment"] == "default"


def test_acceptance_kubernetes_provider_and_capacity_branches(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    docker_env = _env(
        engine_provider="docker", engine_memory_gib=2.0, engine_cpus=4,
        microservices_ready=False, engine_resize_supported=False)
    podman_env = _env(
        engine_provider="podman", engine_memory_gib=2.0, engine_cpus=4,
        microservices_ready=False, engine_resize_supported=True,
        engine_resize_relevant=True)

    docker_snap = onboarding.setup_snapshot(
        environment=docker_env, draft={"deployment": "microservices"})
    assert docker_snap["capacity"]["code"] == onboarding.CAPACITY_RESIZE_UNSUPPORTED
    assert "podman machine set" not in (docker_snap["capacity"]["remediation"] or "").lower()

    podman_snap = onboarding.setup_snapshot(
        environment=podman_env, draft={"deployment": "microservices"})
    assert podman_snap["capacity"]["code"] == onboarding.CAPACITY_GRANT_REQUIRED
    assert any(q["id"] == "engine_resize" for q in podman_snap["questions"])
    assert podman_snap["license"]["required"] is True
    assert podman_snap["license"]["seed_deferred"] is True
    assert "--seed" not in podman_snap["first_run_command"]

    # Settled grant is not re-asked; conflict surfaces when denied.
    onboarding.apply_setup_patch({
        "deployment": "microservices",
        "grants": {"owned-cluster": False, "engine-resize": False},
    })
    denied = onboarding.setup_snapshot(environment=podman_env)
    assert any(c["code"] == "CONFLICT_OWNED_CLUSTER_DENIED" for c in denied["conflicts"])
    second = CliRunner().invoke(app, ["onboard", "--json", "--show"])
    assert second.exit_code == 0
    shown = json.loads(second.stdout)["data"]
    assert shown["completed"] is True
    assert any(g.get("approve_with") for g in shown["gates"])


def test_acceptance_legacy_preset_and_capabilities_regression(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    # Static capabilities remain offline.
    def boom():
        raise AssertionError("capabilities must not probe the environment")
    monkeypatch.setattr(onboarding, "detect_environment", boom)
    caps = jsonout.capabilities(app)
    assert caps["selection"]["legacy_preset_alias"] is True
    assert "ldap" in caps["selection"]["scenario_names"]
    assert "onboarding" in caps
    assert "setup" not in caps
    assert onboarding.CAPACITY_GRANT_REQUIRED in caps["error_codes"]

    # Legacy complete() still works.
    monkeypatch.setattr(onboarding, "detect_environment", lambda: _env())
    st = onboarding.complete(grants=["owned-cluster"], preferences={"retain_runs": False})
    assert st["grants"]["owned_cluster"] is True
    assert st["completed"] is True

    # Alias-style CLI still records grants.
    r = CliRunner().invoke(app, [
        "onboard", "--accept-defaults", "--json", "--grant", "engine-resize",
    ])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)["data"]
    assert data["grants"]["engine_resize"] is True
    assert data["grants"]["owned_cluster"] is True  # preserved


def test_acceptance_targeted_section_reconfigure(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: _env(
        engine_provider="docker", microservices_ready=False,
        missing_kubernetes_tools=["kind"]))
    onboarding.apply_setup_patch({
        "deployment": "default",
        "scenarios": ["ldap"],
        "seed_profile": "small",
        "retain_runs": False,
    })
    # Only seed changes.
    r = CliRunner().invoke(app, [
        "onboard", "--accept-defaults", "--json",
        "--section", "seed", "--seed-profile", "large",
    ])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)["data"]
    assert data["selection"]["seed_profile"] == "large"
    assert data["selection"]["scenarios"] == ["ldap"]
    assert data["selection"]["deployment"] == "default"


def test_completed_kubernetes_setup_reopens_for_unanswered_authority(
        tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: _env())
    initial = CliRunner().invoke(app, [
        "onboard", "--json", "--deployment", "microservices",
    ])
    assert initial.exit_code == 0, initial.output
    assert onboarding.state()["completed"] is True
    assert onboarding.state()["answered_grants"]["owned_cluster"] is False

    resumed = CliRunner().invoke(app, ["onboard"], input="y\ny\n")

    assert resumed.exit_code == 0, resumed.output
    assert "May rc-repro create and later delete those owned resources?" in resumed.output
    assert "Settled choices are unchanged" not in resumed.output
    assert onboarding.state()["grants"]["owned_cluster"] is True


def test_accept_defaults_records_only_applicable_grants(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: _env(
        engine_provider="docker", missing_kubernetes_tools=["kind"],
        microservices_ready=False))

    result = CliRunner().invoke(app, [
        "onboard", "--accept-defaults", "--json", "--deployment", "default",
    ])

    assert result.exit_code == 0, result.output
    answered = onboarding.state()["answered_grants"]
    assert answered == {"owned_cluster": False, "engine_resize": False}


def test_acceptance_no_fullscreen_tui_dependency():
    """Supported onboarding uses progressive prompts, not a full-screen TUI."""
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    for banned in ("textual", "blessed", "curses", "prompt_toolkit", "npyscreen"):
        assert banned not in pyproject.lower()
    # CLI module must not import a full-screen TUI framework.
    cli_src = (root / "rc_repro" / "cli.py").read_text(encoding="utf-8")
    for banned in ("textual", "blessed", "prompt_toolkit", "npyscreen"):
        assert banned not in cli_src
    assert "_prompt_choice" in cli_src


def test_interactive_scenario_prompt_accepts_numbered_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: _env())

    result = CliRunner().invoke(app, ["onboard"], input=(
        "2\n"  # Kubernetes microservices
        "1\n"  # LDAP, the only compatible scenario
        "1\n"  # no seed
        "n\n"  # teardown by default
        "y\n"  # owned-cluster grant
        "n\n"  # decline final write; prompt behavior is the assertion
    ))

    assert result.exit_code == 0, result.output
    assert "1. ldap" in result.output
    assert "scenarios: ldap" in result.output
    assert "Traceback" not in result.output
    assert "No changes written." in result.output


def test_acceptance_first_run_never_repeats_settled_choices(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: _env())
    onboarding.apply_setup_patch({
        "deployment": "microservices",
        "scenarios": [],
        "seed_profile": "none",
        "retain_runs": False,
        "grants": {"owned-cluster": True},
    })
    again = CliRunner().invoke(app, ["onboard"], input="")
    assert again.exit_code == 0, again.output
    assert "Settled choices are unchanged" in again.output
    assert "May rc-repro" not in again.output
    assert onboarding.FIRST_RUN_COMMAND in again.output


def test_gui_setup_error_exposes_code_and_remediation(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    c = TestClient(create_app(token=TOKEN), base_url="http://localhost")
    r = c.post("/api/setup", headers=H, json={"deployment": "not-a-deployment"})
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert "error" in body
