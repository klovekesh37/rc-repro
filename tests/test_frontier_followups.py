"""Acceptance tests for wayfinder follow-up tickets #36-#41."""

from __future__ import annotations

import os
import stat

import pytest

from rc_repro import config, jsonout, runner
from rc_repro.errors import ValidationError
from rc_repro.services import access, evidence, lifecycle
from rc_repro.services import data as datasvc


def test_first_admin_contract_has_no_initial_user_yes():
    env = config.first_admin_env()
    assert "INITIAL_USER" not in env
    assert env["OVERWRITE_SETTING_Show_Setup_Wizard"] == "completed"
    assert env["ADMIN_USERNAME"] == config.ADMIN_USERNAME
    assert env["ADMIN_PASS"] == config.ADMIN_PASSWORD


def test_access_local_has_no_tunnel(monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    info = access.handoff(3042, "http://localhost:3042", remote=False)
    assert info["mode"] == "local"
    assert info["tunnel_command"] is None
    assert info["browser_url"] == "http://localhost:3042"
    assert info["bind"] == "loopback"


def test_access_ssh_tunnel_is_copy_safe(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 22 10.0.0.5 22")
    monkeypatch.setenv("USER", "op")
    monkeypatch.setenv("RC_REPRO_SSH_HOST", "repro.example")
    info = access.handoff(3100, remote=True, env=dict(os.environ))
    assert info["mode"] == "remote_ssh"
    assert info["tunnel_command"] == "ssh -N -L 3100:127.0.0.1:3100 op@repro.example"
    assert info["browser_url"] == "http://127.0.0.1:3100"
    assert "loopback" in (info["note"] or "").lower()


def test_access_occupied_local_port_walks_up():
    info = access.handoff(
        3000, remote=True, preferred_local=3000,
        local_port_in_use=lambda p: p in {3000, 3001},
        env={"SSH_CONNECTION": "x", "USER": "u", "HOSTNAME": "h"},
    )
    assert info["local_port"] == 3002
    assert "-L 3002:127.0.0.1:3000" in info["tunnel_command"]


def test_retention_defaults_to_teardown():
    r = evidence.resolve_retention(preferences={})
    assert r == {"retained": False, "reason": None}
    r = evidence.resolve_retention(preferences={"retain_runs": "yes"})
    assert r["retained"] is False
    r = evidence.resolve_retention(preferences={"retain_runs": True})
    assert r == {"retained": True, "reason": "persisted preference"}
    r = evidence.resolve_retention(retained=True, reason="explicit task")
    assert r == {"retained": True, "reason": "explicit task"}
    r = evidence.resolve_retention(retained=True, reason="because i want")
    assert r["reason"] == "explicit task"  # unknown reasons are not invented prose


def test_evidence_retention_reason_and_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    meta = runner.Metadata(
        name="e1", project="rcrepro-e1", rc_version="8.6.1", rc_image="i",
        mongo_tag="7.0", mongo_flavor="official", preset="default",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
    )
    runner.write("e1", "services: {}\n", meta)
    payload = evidence.record("e1")
    assert payload["retention"]["retained"] is False
    assert payload["retention"]["reason"] is None
    assert "down --name e1 --volumes --yes" in payload["retention"]["cleanup"]
    payload = evidence.record("e1", retained=True, reason="explicit task")
    assert payload["retention"]["retained"] is True
    assert payload["retention"]["reason"] == "explicit task"


def test_capabilities_topology_features_and_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    from rc_repro.cli import app
    caps = jsonout.capabilities(app)
    assert caps["topology_features"]["compose"]["scale"] is True
    assert caps["topology_features"]["kubernetes"]["scale"] is False
    assert caps["topology_features"]["kubernetes"]["seed"] is True
    assert caps["topology_features"]["kubernetes"]["seed_stats"] is False
    assert "retain_runs" in caps["onboarding"]["preferences"]
    # Skill body must not hardcode retain; capabilities is the authority.
    from pathlib import Path
    skill = Path(__file__).resolve().parents[1] / "rc_repro" / "data" / "skill" / "SKILL.md"
    text = skill.read_text()
    assert "onboarding.preferences.retain_runs" in text
    assert "topology_features" in text


def test_scale_refused_on_kubernetes_before_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    meta = runner.Metadata(
        name="kscale", project="rc-repro-kscale", rc_version="8.6.1", rc_image="i",
        mongo_tag="7.0", mongo_flavor="official", preset="microservices",
        root_url="http://localhost:3010", host_port=3010, version_source="map",
        extra={"topology": "kubernetes", "k8s_namespace": "rc-repro-kscale"},
    )
    runner.write("kscale", "microservices: {enabled: true}\n", meta,
                 artifact_name="values.yaml")
    with pytest.raises(ValidationError) as ei:
        datasvc.run_scale("kscale", "users=10")
    assert "not supported" in str(ei.value).lower() or "kubernetes" in str(ei.value).lower()
    with pytest.raises(ValidationError):
        datasvc.clear_scale("kscale")


def test_seed_stats_refused_on_kubernetes(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    meta = runner.Metadata(
        name="kseed", project="rc-repro-kseed", rc_version="8.6.1", rc_image="i",
        mongo_tag="7.0", mongo_flavor="official", preset="microservices",
        root_url="http://localhost:3011", host_port=3011, version_source="map",
        extra={"topology": "kubernetes", "k8s_namespace": "rc-repro-kseed",
               "k8s_context": "kind-rc-repro-local", "k8s_forward_pid": 0},
    )
    runner.write("kseed", "microservices: {enabled: true}\n", meta,
                 artifact_name="values.yaml")
    # Patch ensure_reachable and login so the stats guard is the first real check.
    monkeypatch.setattr(lifecycle, "ensure_reachable", lambda *a, **k: None)
    with pytest.raises(ValidationError) as ei:
        lifecycle.run_seed_inline(meta, "small", stats=True, emit=lambda e: None)
    assert "stats" in str(ei.value).lower() or "resource" in str(ei.value).lower()


def test_compose_env_includes_first_admin_without_initial_user():
    from rc_repro import compose, presets, versions
    r = versions.resolve("8.6.1", offline=True)
    pre = presets.load("default")
    spec = compose.Spec.from_resolved(
        r, project_name="rcrepro-x", root_url="http://localhost:3000",
        host_port=3000, reg_token=None, preset=pre, bind_host="127.0.0.1")
    doc = compose.build(spec)
    env = doc["services"]["rocketchat"]["environment"]
    assert "INITIAL_USER" not in env
    assert env["ADMIN_USERNAME"] == config.ADMIN_USERNAME
    assert env["OVERWRITE_SETTING_Show_Setup_Wizard"] == "completed"
