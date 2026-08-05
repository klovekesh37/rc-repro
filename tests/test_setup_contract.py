"""Setup snapshot/patch contract tests (ticket 01 and capacity cases)."""

from __future__ import annotations

import json

import pytest

from rc_repro import config, jsonout
from rc_repro.cli import app
from rc_repro.services import onboarding


def _compose_env(**overrides):
    base = {
        "os": "TestOS", "os_version": "1", "architecture": "x86_64",
        "cpus": 4, "memory_gib": 16.0, "disk_free_gib": 80.0,
        "tools": {"docker": "ok", "compose": "ok", "kind": "missing",
                  "kubectl": "missing", "helm": "missing"},
        "docker_ready": True, "engine_provider": "docker",
        "engine_memory_gib": 8.0, "engine_cpus": 4,
        "engine_kernel_version": "6.18.0",
        "missing_kubernetes_tools": ["kind", "kubectl", "helm"],
        "microservices_ready": False,
        "engine_resize_supported": False, "engine_resize_relevant": False,
    }
    base.update(overrides)
    return base


def test_empty_setup_snapshot_separates_persisted_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    env = _compose_env()
    snap = onboarding.setup_snapshot(environment=env)

    assert snap["schema"] == onboarding.SETUP_SCHEMA
    assert snap["completed"] is False
    assert snap["persisted"]["grants"]["owned_cluster"] is False
    assert snap["persisted"]["answered_grants"]["owned_cluster"] is False
    assert snap["environment"]["engine_provider"] == "docker"
    assert snap["selection"]["deployment"] == "default"
    assert snap["selection"]["topology"] == "compose"
    # Compose: no kubernetes authority/capacity questions in the applicable list.
    ids = {q["id"] for q in snap["questions"]}
    assert "deployment" in ids
    assert "owned_cluster" not in ids
    assert "engine_resize" not in ids
    # Secret *values* never appear; the contract only names which keys were omitted.
    assert "SUPERSECRET" not in json.dumps(snap)
    assert snap["persisted"]["omitted_secret_keys"] == ["reg_token"]


def test_partial_patch_preserves_untouched_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    config.save_config({"reg_token": "SUPERSECRET", "default_repro": "keep-me"})

    first = onboarding.apply_setup_patch({
        "deployment": "default",
        "scenarios": ["ldap"],
        "seed_profile": "standard",
        "retain_runs": True,
        "grants": {"owned-cluster": False, "engine-resize": False},
    }, mark_complete=True)
    assert first["completed"] is True
    assert first["selection"]["scenarios"] == ["ldap"]
    assert first["selection"]["seed_profile"] == "standard"
    assert first["persisted"]["preferences"]["retain_runs"] is True

    second = onboarding.apply_setup_patch({"seed_profile": "small"}, mark_complete=True)
    assert second["selection"]["seed_profile"] == "small"
    assert second["selection"]["scenarios"] == ["ldap"]
    assert second["persisted"]["preferences"]["retain_runs"] is True
    assert second["persisted"]["grants"]["owned_cluster"] is False

    raw = config.load_config(with_env=False)
    assert raw["reg_token"] == "SUPERSECRET"
    assert raw["default_repro"] == "keep-me"
    assert "SUPERSECRET" not in json.dumps(second)


def test_invalid_and_empty_patches_never_mutate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    with pytest.raises(onboarding.ValidationError, match="unknown deployment"):
        onboarding.apply_setup_patch({"deployment": "not-a-deployment"})
    assert config.load_config(with_env=False) == {}
    assert onboarding.state()["completed"] is False

    onboarding.apply_setup_patch({})
    assert config.load_config(with_env=False) == {}
    assert onboarding.state()["completed"] is False


def test_deployment_change_clears_only_an_incompatible_saved_scenario(
        tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.apply_setup_patch({"deployment": "default", "scenarios": ["ldap"]})

    snap = onboarding.apply_setup_patch({"deployment": "multi-instance"})

    assert snap["selection"]["deployment"] == "multi-instance"
    assert snap["selection"]["scenarios"] == []
    assert "--scenario" not in snap["first_run_command"]
    assert config.load_config(with_env=False)["default_scenarios"] == []


def test_explicit_incompatible_selector_pair_is_rejected_before_any_write(
        tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.apply_setup_patch({"deployment": "default", "scenarios": ["ldap"]})
    before = config.load_config(with_env=False)

    with pytest.raises(onboarding.ValidationError):
        onboarding.apply_setup_patch({
            "deployment": "multi-instance", "scenarios": ["ldap"],
            "seed_profile": "large",
        })

    assert config.load_config(with_env=False) == before


def test_compose_patch_never_requires_kubernetes_grants(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    snap = onboarding.apply_setup_patch({
        "deployment": "default",
        "scenarios": [],
        "seed_profile": "none",
        "retain_runs": False,
    })
    assert snap["selection"]["topology"] == "compose"
    assert snap["capacity"]["status"] == "not_applicable"
    assert not any(g["kind"] == "capacity" for g in snap["gates"])
    assert not any(g.get("code") == "GATE_OWNED_CLUSTER" for g in snap["gates"])
    assert "owned_cluster" not in {q["id"] for q in snap["questions"]}


def test_kubernetes_snapshot_exposes_capacity_and_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    env = _compose_env(
        tools={"docker": "ok", "compose": "ok", "kind": "ok",
               "kubectl": "ok", "helm": "ok"},
        engine_provider="docker",
        engine_memory_gib=2.0, engine_cpus=4,
        missing_kubernetes_tools=[],
        microservices_ready=False,
        engine_resize_supported=False, engine_resize_relevant=False,
    )
    snap = onboarding.setup_snapshot(
        environment=env, draft={"deployment": "microservices"})

    assert snap["selection"]["topology"] == "kubernetes"
    assert snap["capacity"]["provider"] == "docker"
    assert snap["capacity"]["code"] == onboarding.CAPACITY_RESIZE_UNSUPPORTED
    assert snap["capacity"]["ready"] is False
    remediation = snap["capacity"]["remediation"] or ""
    assert "Docker Desktop" in remediation or "Docker-compatible" in remediation
    assert "podman machine set" not in remediation.lower()
    ids = {q["id"] for q in snap["questions"]}
    assert "owned_cluster" in ids
    assert snap["capacity"]["observed_memory_gib"] == 2.0
    assert snap["capacity"]["required_memory_gib"] == 6.0
    assert snap["capacity"]["supported_action"] == "manual_memory"


def test_unlicensed_kubernetes_first_run_defers_seed_without_exposing_token(
        tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    env = _compose_env(
        tools={"docker": "ok", "compose": "ok", "kind": "ok",
               "kubectl": "ok", "helm": "ok"},
        engine_provider="docker-compatible",
        engine_memory_gib=15.0, engine_cpus=4,
        missing_kubernetes_tools=[], microservices_ready=True,
    )
    draft = {
        "deployment": "microservices",
        "scenarios": ["ldap"],
        "seed_profile": "small",
        "grants": {"owned-cluster": True},
    }

    unlicensed = onboarding.setup_snapshot(
        cfg={}, environment=env, draft=draft)
    assert unlicensed["license"] == {
        "required": True,
        "supplied": False,
        "status": "required",
        "code": "LICENSE_ABSENT_EE_PRESET",
        "seed_deferred": True,
        "remediation": unlicensed["license"]["remediation"],
    }
    assert unlicensed["review"]["seed_status"] == "deferred_license_required"
    assert "--seed" not in unlicensed["first_run_command"]

    licensed = onboarding.setup_snapshot(
        cfg={"reg_token": "SUPERSECRET"}, environment=env, draft=draft)
    assert licensed["license"]["supplied"] is True
    assert licensed["license"]["seed_deferred"] is False
    assert "--seed --seed-profile small" in licensed["first_run_command"]
    assert "SUPERSECRET" not in json.dumps(licensed)


def test_podman_memory_shortfall_offers_resize_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    env = _compose_env(
        tools={"docker": "ok", "compose": "ok", "kind": "ok",
               "kubectl": "ok", "helm": "ok"},
        engine_provider="podman",
        engine_memory_gib=2.0, engine_cpus=4,
        missing_kubernetes_tools=[],
        microservices_ready=False,
        engine_resize_supported=True, engine_resize_relevant=True,
    )
    snap = onboarding.setup_snapshot(
        environment=env, draft={"deployment": "microservices"})
    assert snap["capacity"]["code"] == onboarding.CAPACITY_GRANT_REQUIRED
    assert snap["capacity"]["supported_action"] == "engine_resize"
    assert any(q["id"] == "engine_resize" and q["applicable"] for q in snap["questions"])

    onboarding.apply_setup_patch({
        "deployment": "microservices",
        "grants": {"owned-cluster": True, "engine-resize": True},
    })
    snap2 = onboarding.setup_snapshot(environment=env)
    assert snap2["capacity"]["code"] == onboarding.CAPACITY_INSUFFICIENT_MEMORY
    assert snap2["persisted"]["grants"]["engine_resize"] is True


def test_first_run_is_blocked_by_mongodb_kernel_compatibility_gate(
        tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    env = _compose_env(
        engine_provider="podman",
        engine_kernel_version="6.19.7-200.fc43.aarch64",
    )

    snap = onboarding.setup_snapshot(
        environment=env, draft={"deployment": "default"})

    assert snap["capacity"]["status"] == "not_applicable"
    assert snap["compatibility"]["code"] == (
        onboarding.COMPATIBILITY_MONGODB_KERNEL_UNSUPPORTED)
    assert snap["compatibility"]["ready"] is False
    assert snap["compatibility"]["mongo_version"] == "8.0"
    assert snap["first_run_command"] == ""
    assert snap["review"]["first_run_status"] == "blocked_compatibility"
    gate = next(g for g in snap["gates"] if g["kind"] == "compatibility")
    assert gate["code"] == onboarding.COMPATIBILITY_MONGODB_KERNEL_UNSUPPORTED
    assert "SERVER-121912" in gate["remediation"]


def test_insufficient_cpu_is_not_a_successful_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    env = _compose_env(
        tools={"docker": "ok", "compose": "ok", "kind": "ok",
               "kubectl": "ok", "helm": "ok"},
        engine_provider="podman",
        engine_memory_gib=8.0, engine_cpus=2,
        missing_kubernetes_tools=[],
        microservices_ready=False,
        engine_resize_supported=True, engine_resize_relevant=False,
    )
    cap = onboarding.capacity_assessment(env, deployment="microservices")
    assert cap["ready"] is False
    assert cap["code"] == onboarding.CAPACITY_INSUFFICIENT_CPU
    assert cap["status"] == "insufficient"


def test_settled_denial_and_new_authority_conflict_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.apply_setup_patch({
        "deployment": "microservices",
        "grants": {"owned-cluster": False},
    })
    env = _compose_env(
        tools={"docker": "ok", "compose": "ok", "kind": "ok",
               "kubectl": "ok", "helm": "ok"},
        engine_provider="podman",
        engine_memory_gib=15.0, engine_cpus=4,
        missing_kubernetes_tools=[],
        microservices_ready=True,
        engine_resize_supported=False, engine_resize_relevant=False,
    )
    snap = onboarding.setup_snapshot(environment=env)
    assert any(c["code"] == "CONFLICT_OWNED_CLUSTER_DENIED" for c in snap["conflicts"])
    assert any(g["approve_with"] == onboarding.RECONFIGURE_COMMAND for g in snap["gates"]
               if g.get("code") == "GATE_OWNED_CLUSTER")


def test_capabilities_stays_offline_without_setup_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    def boom():
        raise AssertionError("detect_environment must not run for capabilities")

    monkeypatch.setattr(onboarding, "detect_environment", boom)
    cap = jsonout.capabilities(app)
    assert "presets" in cap
    assert "onboarding" in cap
    assert "setup" not in cap  # dynamic setup is a separate contract


def test_legacy_selector_defaults_survive_setup_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    config.save_config({
        "default_deployment": "microservices",
        "default_scenarios": ["ldap"],
    })
    snap = onboarding.setup_snapshot(environment=_compose_env(
        tools={"docker": "ok", "compose": "ok", "kind": "ok",
               "kubectl": "ok", "helm": "ok"},
        engine_provider="podman",
        engine_memory_gib=15.0, engine_cpus=4,
        missing_kubernetes_tools=[],
        microservices_ready=True,
    ))
    assert snap["selection"]["deployment"] == "microservices"
    assert snap["selection"]["scenarios"] == ["ldap"]
    assert snap["legacy_preset_alias"] is True


def test_section_filter_limits_questions(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    snap = onboarding.setup_snapshot(environment=_compose_env(), section="seed")
    assert [q["id"] for q in snap["questions"]] == ["seed_profile"]
