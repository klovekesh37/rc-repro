"""Acceptance tests for wayfinder follow-up tickets #36-#41."""

from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

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


def test_access_public_local_port_override_and_ipv6_target():
    info = access.handoff(
        3000, "http://[::1]:3000", remote=True,
        env={
            "SSH_CONNECTION": "2001:db8::2 55100 2001:db8::1 22",
            "USER": "op",
            "RC_REPRO_SSH_LOCAL_PORT": "43000",
        },
    )
    assert info["local_port"] == 43000
    assert info["browser_url"] == "http://127.0.0.1:43000"
    assert info["tunnel_command"] == \
        "ssh -N -L 43000:[::1]:3000 op@2001:db8::1"


def test_access_invalid_public_local_port_is_refused():
    with pytest.raises(ValidationError) as ei:
        access.handoff(
            3000, remote=True,
            env={"SSH_CONNECTION": "a 1 b 2", "USER": "op",
                 "RC_REPRO_SSH_LOCAL_PORT": "not-a-port"},
        )
    assert "RC_REPRO_SSH_LOCAL_PORT" in str(ei.value)


def test_human_info_prints_remote_access_handoff(tmp_path, monkeypatch):
    from rc_repro.cli import app
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 55100 203.0.113.8 22")
    monkeypatch.setenv("USER", "operator")
    meta = runner.Metadata(
        name="remote-info", project="rcrepro-remote-info", rc_version="8.6.1",
        rc_image="i", mongo_tag="7.0", mongo_flavor="official", preset="default",
        root_url="http://localhost:3123", host_port=3123, version_source="map",
    )
    runner.write("remote-info", "services: {}\n", meta)

    result = CliRunner().invoke(app, ["info", "--name", "remote-info"])

    assert result.exit_code == 0, result.output
    assert "ssh -N -L 3123:127.0.0.1:3123 operator@203.0.113.8" in result.output
    assert "browser: http://127.0.0.1:3123" in result.output


def test_k8s_info_json_exposes_remote_access_and_token_boolean(
        tmp_path, monkeypatch):
    from rc_repro.cli import app
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 55100 203.0.113.8 22")
    monkeypatch.setenv("USER", "operator")
    meta = runner.Metadata(
        name="remote-k8s-info", project="rc-repro-remote-k8s-info",
        rc_version="8.6.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="microservices",
        root_url="http://localhost:3124", host_port=3124,
        version_source="map", extra={
            "topology": "kubernetes",
            "k8s_namespace": "rc-repro-remote-k8s-info",
            "k8s_context": "kind-rc-repro-local",
            "reg_token_supplied": True,
        },
    )
    runner.write("remote-k8s-info", "microservices: {enabled: true}\n", meta,
                 artifact_name="values.yaml")
    monkeypatch.setattr(k8s, "pods", lambda name, run=None: [])
    monkeypatch.setattr(k8s, "forward_state", lambda metadata: "up")

    result = CliRunner().invoke(
        app, ["info", "--name", "remote-k8s-info", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["reg_token_supplied"] is True
    assert payload["data"]["access"] == {
        "mode": "remote_ssh",
        "bind": "loopback",
        "host_port": 3124,
        "local_port": 3124,
        "browser_url": "http://127.0.0.1:3124",
        "tunnel_command": (
            "ssh -N -L 3124:127.0.0.1:3124 operator@203.0.113.8"),
        "note": payload["data"]["access"]["note"],
    }


def test_retention_defaults_to_teardown():
    r = evidence.resolve_retention(preferences={})
    assert r == {"retained": False, "reason": None}
    r = evidence.resolve_retention(preferences={"retain_runs": "yes"})
    assert r["retained"] is False
    r = evidence.resolve_retention(preferences={"retain_runs": True})
    assert r == {"retained": True, "reason": "persisted preference"}
    r = evidence.resolve_retention(retained=True, reason="explicit task")
    assert r == {"retained": True, "reason": "explicit task"}
    with pytest.raises(ValidationError):
        evidence.resolve_retention(retained=True, reason="because i want")


def test_evidence_retention_reason_and_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    meta = runner.Metadata(
        name="e1", project="rcrepro-e1", rc_version="8.6.1", rc_image="i",
        mongo_tag="7.0", mongo_flavor="official", preset="default",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
    )
    runner.write("e1", "services: {}\n", meta)
    monkeypatch.setattr(runner, "docker_available", lambda: True)
    monkeypatch.setattr(runner, "container_details", lambda _name: [
        {"service": "mongo", "state": "running", "status": "Up 2 minutes"},
        {"service": "rocketchat", "state": "restarting",
         "status": "Restarting (1) 5 seconds ago"},
    ])
    payload = evidence.record("e1")
    assert payload["runtime"]["state"] == "restarting"
    assert payload["retention"]["retained"] is False
    assert payload["retention"]["reason"] is None
    assert "down --name e1 --volumes --yes" in payload["retention"]["cleanup"]
    payload = evidence.record("e1", retained=True, reason="explicit task")
    assert payload["retention"]["retained"] is True
    assert payload["retention"]["reason"] == "explicit task"


def test_evidence_cli_exposes_explicit_task_retention(monkeypatch):
    from rc_repro import cli, jsonout
    from rc_repro.cli import app
    seen = {}

    def fake_record(name, *, retained=None, reason=None):
        seen.update(name=name, retained=retained, reason=reason)
        return {
            "repro": {"name": name or "x", "rc_version": "8.6.1",
                      "topology": "kubernetes"},
            "runtime": {"state": "running"},
            "artifact": {"name": "values.yaml", "sha256": "a" * 64},
            "license": {"required": True, "supplied": False},
            "retention": {"retained": True, "reason": "explicit task",
                          "cleanup": "rc-repro down --name x --volumes --yes"},
        }

    monkeypatch.setattr(cli.evidencesvc, "record", fake_record)
    result = CliRunner().invoke(
        app, ["evidence", "--name", "x", "--retain-for-task", "--json"])
    assert result.exit_code == 0, result.output
    assert seen == {"name": "x", "retained": True, "reason": "explicit task"}
    assert json.loads(result.stdout)["data"]["retention"]["reason"] == "explicit task"
    caps = jsonout.capabilities(app)
    evidence_command = next(c for c in caps["commands"] if c["name"] == "evidence")
    assert "--retain-for-task" in evidence_command["flags"]


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


def test_k8s_registration_token_sources_reach_the_service(tmp_path, monkeypatch):
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize", "owned-cluster"])
    stored = config.load_config(with_env=False)
    stored["reg_token"] = "from-config"
    config.save_config(stored)
    seen = []

    def fake_create(name, version, **kwargs):
        seen.append(kwargs["reg_token"])
        return {"name": name, "topology": "kubernetes"}

    monkeypatch.setattr(k8s, "create_repro", fake_create)
    lifecycle.create_repro(lifecycle.CreateReq(
        version="8.6.1", preset="microservices", name="from-config"))
    monkeypatch.setenv("RC_REPRO_REG_TOKEN", "from-env")
    lifecycle.create_repro(lifecycle.CreateReq(
        version="8.6.1", preset="microservices", name="from-env"))
    lifecycle.create_repro(lifecycle.CreateReq(
        version="8.6.1", preset="microservices", name="from-option",
        reg_token="from-option"))

    assert seen == ["from-config", "from-env", "from-option"]


def test_k8s_cli_json_dispatches_token_without_exposing_it(tmp_path, monkeypatch):
    from rc_repro.cli import app
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize", "owned-cluster"])
    seen = {}

    def fake_create(name, version, **kwargs):
        seen["token"] = kwargs["reg_token"]
        return {"name": name, "topology": "kubernetes",
                "reg_token_supplied": True, "waited": False}

    monkeypatch.setattr(k8s, "create_repro", fake_create)
    secret = "CLI-SECRET-MUST-NOT-PRINT"
    result = CliRunner().invoke(app, [
        "up", "--version", "8.6.1", "--preset", "microservices",
        "--name", "token-cli", "--reg-token", secret, "--json",
    ])

    assert result.exit_code == 0, result.output
    assert seen["token"] == secret
    assert secret not in result.output
    assert json.loads(result.stdout.splitlines()[-1])["data"]["reg_token_supplied"] is True


def test_k8s_create_and_seed_uses_one_shared_lifecycle(tmp_path, monkeypatch):
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize", "owned-cluster"])
    meta = runner.Metadata(
        name="seed-create", project="rc-repro-seed-create", rc_version="8.6.1",
        rc_image="i", mongo_tag="7.0", mongo_flavor="official",
        preset="microservices", root_url="http://localhost:3200", host_port=3200,
        version_source="map", extra={"topology": "kubernetes"},
    )
    calls = []
    monkeypatch.setattr(k8s, "create_repro", lambda *a, **k: {
        "name": "seed-create", "topology": "kubernetes"})
    monkeypatch.setattr(lifecycle.runner, "read_meta", lambda name: meta)
    monkeypatch.setattr(lifecycle, "wait_and_finalize",
                        lambda m, emit=None: calls.append("wait") or {"booted_s": 1})
    monkeypatch.setattr(lifecycle, "run_seed_inline",
                        lambda m, profile, stats, emit: calls.append(
                            ("seed", profile, stats)) or {"users": 2})

    result = lifecycle.create_repro(lifecycle.CreateReq(
        version="8.6.1", preset="microservices", name="seed-create",
        seed=True, seed_profile="standard"))

    assert calls == ["wait", ("seed", "standard", False)]
    assert result["seed"] == {"users": 2}


def test_k8s_cli_json_create_and_seed_runs_once(tmp_path, monkeypatch):
    from rc_repro import cli
    from rc_repro.cli import app
    meta = runner.Metadata(
        name="seed-cli", project="rc-repro-seed-cli", rc_version="8.6.1",
        rc_image="i", mongo_tag="7.0", mongo_flavor="official",
        preset="microservices", root_url="http://localhost:3201", host_port=3201,
        version_source="map", extra={"topology": "kubernetes"},
    )
    seen = []

    def fake_create(req, emit=None, stream_output=False):
        assert req.wait is True and req.seed is False
        return {"name": "seed-cli", "topology": "kubernetes", "waited": True}

    monkeypatch.setattr(cli.lcsvc, "create_repro", fake_create)
    monkeypatch.setattr(cli.runner, "read_meta", lambda name: meta)
    monkeypatch.setattr(cli.lcsvc, "ensure_reachable", lambda name: None)
    monkeypatch.setattr(cli.lcsvc, "run_seed_inline",
                        lambda m, profile, stats, emit: seen.append(profile) or {
                            "users": 2, "channels": 1, "messages": 3, "total_s": 0.2})

    result = CliRunner().invoke(app, [
        "up", "--version", "8.6.1", "--preset", "microservices",
        "--name", "seed-cli", "--seed", "--seed-profile", "large", "--json",
    ])

    assert result.exit_code == 0, result.output
    assert seen == ["large"]
    assert json.loads(result.stdout.splitlines()[-1])["data"]["seed"]["users"] == 2


def test_k8s_human_cli_create_and_seed_preserves_profile(monkeypatch):
    from rc_repro import cli
    from rc_repro.cli import app
    meta = runner.Metadata(
        name="seed-human", project="rc-repro-seed-human", rc_version="8.6.1",
        rc_image="i", mongo_tag="7.0", mongo_flavor="official",
        preset="microservices", root_url="http://localhost:3203", host_port=3203,
        version_source="map", extra={"topology": "kubernetes"},
    )
    seen = []

    def fake_create(req, emit=None, stream_output=False):
        assert req.wait is True and req.seed is False
        return {"name": "seed-human", "topology": "kubernetes",
                "waited": True, "booted_s": 1}

    monkeypatch.setattr(cli.lcsvc, "create_repro", fake_create)
    monkeypatch.setattr(cli.runner, "read_meta", lambda name: meta)
    monkeypatch.setattr(cli, "_run_seed",
                        lambda m, profile, **kwargs: seen.append(profile))

    result = CliRunner().invoke(app, [
        "up", "--version", "8.6.1", "--preset", "microservices",
        "--name", "seed-human", "--seed", "--seed-profile", "standard",
    ])

    assert result.exit_code == 0, result.output
    assert seen == ["standard"]


def test_existing_k8s_seed_reconciles_before_login(monkeypatch):
    meta = runner.Metadata(
        name="seed-existing", project="rc-repro-seed-existing", rc_version="8.6.1",
        rc_image="i", mongo_tag="7.0", mongo_flavor="official",
        preset="microservices", root_url="http://localhost:3202", host_port=3202,
        version_source="map", extra={"topology": "kubernetes"},
    )
    order = []
    monkeypatch.setattr(lifecycle, "ensure_reachable",
                        lambda name: order.append("reconcile"))
    monkeypatch.setattr(lifecycle, "login",
                        lambda m: order.append("login") or object())
    monkeypatch.setattr(lifecycle.seeder, "plan_from",
                        lambda profile: order.append("plan") or object())
    monkeypatch.setattr(lifecycle.seeder, "seed",
                        lambda *a, **k: order.append("seed") or {"users": 1})

    result = lifecycle.run_seed_inline(meta, "small", False, lambda e: None)

    assert order == ["reconcile", "login", "plan", "seed"]
    assert result["users"] == 1


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
    with pytest.raises(ValidationError) as scale_error:
        datasvc.run_scale("kscale", "users=10")
    assert "yet. bulk Mongo prefill" in str(scale_error.value)
    with pytest.raises(ValidationError) as clear_error:
        datasvc.clear_scale("kscale")
    assert "yet. clear-scale removes" in str(clear_error.value)


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


def test_k8s_seed_cli_refuses_compose_only_modes(tmp_path, monkeypatch):
    from rc_repro import cli
    from rc_repro.cli import app
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    meta = runner.Metadata(
        name="kcli", project="rc-repro-kcli", rc_version="8.6.1", rc_image="i",
        mongo_tag="7.0", mongo_flavor="official", preset="microservices",
        root_url="http://localhost:3210", host_port=3210, version_source="map",
        extra={"topology": "kubernetes", "k8s_namespace": "rc-repro-kcli"},
    )
    runner.write("kcli", "microservices: {enabled: true}\n", meta,
                 artifact_name="values.yaml")
    def unexpected_reconcile(name):
        raise AssertionError(f"validation must precede port-forward repair: {name}")

    monkeypatch.setattr(cli.lcsvc, "ensure_reachable", unexpected_reconcile)

    for args in (["--scale", "users=10"], ["--clear-scale"], ["--stats"]):
        result = CliRunner().invoke(app, ["seed", "--name", "kcli", *args])
        assert result.exit_code == ValidationError.exit_code, result.output
        assert "not supported on the kubernetes topology" in result.output.lower()


def test_k8s_up_seed_stats_refuses_before_provisioning_in_human_and_json_modes(
        tmp_path, monkeypatch):
    from rc_repro.cli import app
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RC_REPRO_REG_TOKEN", "non-secret-test-token")
    onboarding.complete(grants=["engine-resize", "owned-cluster"])

    def unexpected_create(*args, **kwargs):
        raise AssertionError("validation must precede Kubernetes provisioning")

    monkeypatch.setattr(k8s, "create_repro", unexpected_create)
    base = [
        "up", "--version", "8.6.1", "--preset", "microservices",
        "--name", "kstats-up", "--seed", "--stats",
    ]

    human = CliRunner().invoke(app, base)
    assert human.exit_code == ValidationError.exit_code, human.output
    assert "not supported on the kubernetes topology" in human.output.lower()

    structured = CliRunner().invoke(app, [*base, "--json"])
    assert structured.exit_code == ValidationError.exit_code, structured.output
    payload = json.loads(structured.stdout.splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == ValidationError.code


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
