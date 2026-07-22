"""Unit tests for the service layer (pure; no Docker).

These lock in the behaviour both the CLI and the web API depend on: naming,
error typing/HTTP mapping, port validation, and the event model.
"""

from __future__ import annotations

import json

import pytest

from rc_repro import errors
from rc_repro.services import events
from rc_repro.services import lifecycle as lc


def test_error_http_status_mapping():
    assert errors.ValidationError.http_status == 400
    assert errors.ConflictError.http_status == 409
    assert errors.NotFoundError.http_status == 404
    assert errors.NotReadyError.http_status == 409
    assert errors.DockerError.http_status == 502
    assert issubclass(errors.ValidationError, errors.ReproError)


def test_sanitize_and_derive():
    assert lc.sanitize("RC 8.5.1!!") == "rc-8-5-1"
    assert lc.derive_name("8.5.1", "default") == "rc8-5-1"
    assert lc.derive_name("8.5.1", "ldap") == "rc8-5-1-ldap"
    assert lc.sanitize("...") == ""      # no usable chars -> empty (caller rejects)


def test_pretty_state():
    assert lc._pretty_state("") == "down"
    assert lc._pretty_state("running(3), exited(1)") == "running"
    assert lc._pretty_state("exited(2)") == "stopped"


def test_createreq_defaults():
    r = lc.CreateReq(version="8.5.1")
    assert r.preset == "default" and r.seed is False and r.params == {}
    # dataclass default_factory gives a fresh dict per instance
    lc.CreateReq(version="8.5.1").params["x"] = 1
    assert lc.CreateReq(version="8.5.1").params == {}


def test_require_docker_raises_when_down(monkeypatch):
    monkeypatch.setattr(lc.runner, "docker_available", lambda: False)
    with pytest.raises(errors.NotReadyError):
        lc.require_docker()


def test_resolve_name_errors(monkeypatch):
    monkeypatch.setattr(lc.runner, "exists", lambda n: False)
    with pytest.raises(errors.NotFoundError):
        lc.resolve_name("ghost")
    monkeypatch.setattr(lc.config, "load_config", lambda *a, **k: {})
    with pytest.raises(errors.ValidationError):
        lc.resolve_name("")      # no name, no default


def test_pick_host_port_conflict(monkeypatch):
    class P:  # minimal preset stand-in
        instances = 1
    monkeypatch.setattr(lc, "own_ports", lambda name: set())
    monkeypatch.setattr(lc.runner, "used_ports", lambda: {8300})
    with pytest.raises(errors.ConflictError):
        lc.pick_host_port(8300, P(), exclude="")     # claimed by another repro


def test_pick_host_port_out_of_range(monkeypatch):
    class P:
        instances = 3      # needs a 4-port block
    with pytest.raises(errors.ValidationError):
        lc.pick_host_port(65535, P(), exclude="")    # block runs past 65535


def test_project_collision_guard(monkeypatch, tmp_path):
    # our workspace's compose path (what a same-home recreate would show)
    ours = str(tmp_path / "rcrepro" / "repros" / "rc8-5-1" / "docker-compose.yml")
    monkeypatch.setattr(lc.runner, "project_name", lambda n: "rcrepro-" + n)
    monkeypatch.setattr(lc.runner, "workspace",
                        lambda n: tmp_path / "rcrepro" / "repros" / n)

    # foreign project with our derived name -> refuse
    monkeypatch.setattr(lc.runner, "project_config_files",
                        lambda: {"rcrepro-rc8-5-1": "/Users/real/.rc-repro/repros/rc8-5-1/docker-compose.yml"})
    with pytest.raises(errors.ConflictError):
        lc._guard_project_collision("rc8-5-1")

    # same project owned by OUR workspace (a --force recreate) -> allowed
    monkeypatch.setattr(lc.runner, "project_config_files",
                        lambda: {"rcrepro-rc8-5-1": ours})
    lc._guard_project_collision("rc8-5-1")   # no raise

    # docker unavailable / no such project -> skip (best-effort)
    monkeypatch.setattr(lc.runner, "project_config_files", lambda: None)
    lc._guard_project_collision("rc8-5-1")
    monkeypatch.setattr(lc.runner, "project_config_files", lambda: {})
    lc._guard_project_collision("rc8-5-1")


def test_data_scale_ok_error_mapping():
    from rc_repro.services import data
    # non-zero exit / no JSON -> DockerError (infra); {error} payload -> ValidationError (user)
    with pytest.raises(errors.DockerError):
        data._scale_ok(1, "boom", "user prefill")
    with pytest.raises(errors.DockerError):
        data._scale_ok(0, "no json here", "user prefill")
    with pytest.raises(errors.ValidationError):
        data._scale_ok(0, '{"error": "room not found"}', "message prefill", hint="make it first")
    assert data._scale_ok(0, '{"inserted": 500}', "user prefill") == {"inserted": 500}


def test_data_run_scale_validates(monkeypatch):
    from rc_repro.services import data
    monkeypatch.setattr(data.lifecycle, "resolve_name", lambda n: n)
    with pytest.raises(errors.ValidationError):
        data.run_scale("x", "garbage-spec")          # parse_scale rejects
    with pytest.raises(errors.ValidationError):
        data.run_scale("x", "")                       # nothing to do


def test_data_import_plan(monkeypatch, tmp_path):
    from rc_repro.services import data
    monkeypatch.setattr(data.lifecycle, "resolve_name", lambda n: n)
    dump = tmp_path / "s.json"
    dump.write_text(json.dumps([
        {"_id": "A", "value": "new", "packageValue": "old"},          # apply
        {"_id": "Site_Url", "value": "https://c", "packageValue": ""},  # denied
        {"_id": "SMTP_Password", "value": "XXXXXXXX", "packageValue": ""},  # redacted
    ]))
    plan = data.import_plan("x", str(dump))
    assert plan["counts"] == {"apply": 1, "redacted": 1, "denied": 1}
    assert plan["apply"][0]["id"] == "A"


def test_perf_run_loadtest_validates(monkeypatch):
    from rc_repro.services import perf
    monkeypatch.setattr(perf.lifecycle.runner, "docker_available", lambda: True)
    with pytest.raises(errors.ValidationError):
        perf.run_loadtest(perf.LoadtestReq(name="x", scenario="webhook"))  # not a GUI scenario
    with pytest.raises(errors.ValidationError):
        perf.run_loadtest(perf.LoadtestReq(name="x", scenario="messages", vus=0))


def test_repro_links():
    from rc_repro import runner
    def meta(preset, extra=None):
        return runner.Metadata(name="r", project="p", rc_version="8.5.1", rc_image="i",
                               mongo_tag="8.0", mongo_flavor="official", preset=preset,
                               root_url="http://localhost:8300", host_port=8300,
                               version_source="x", extra=extra or {})
    # s3_minio -> console + api URLs surfaced
    s3 = {l["label"]: l["url"] for l in lc.repro_links(meta("s3_minio"))}
    assert s3["Rocket.Chat"] == "http://localhost:8300"
    assert "MinIO console" in s3 and "MinIO API" in s3
    # oidc -> keycloak
    oidc = {l["label"]: l["url"] for l in lc.repro_links(meta("oidc"))}
    assert "Keycloak" in oidc and oidc["Keycloak"].startswith("http://localhost:")
    # monitoring adds Grafana + Prometheus
    mon = {l["label"] for l in lc.repro_links(meta("default", {"monitoring": True}))}
    assert {"Grafana", "Prometheus"} <= mon
    # multi-instance adds instance URLs
    mi = {l["label"] for l in lc.repro_links(meta("multi-instance", {"instances": 3}))}
    assert {"instance 1", "instance 2", "instance 3"} <= mi


def test_perf_capacity_benchmark_validate(monkeypatch):
    from rc_repro.services import perf
    monkeypatch.setattr(perf.lifecycle.runner, "docker_available", lambda: True)
    with pytest.raises(errors.ValidationError):
        perf.run_capacity(perf.CapacityReq(name="x", scenario="webhook"))
    with pytest.raises(errors.ValidationError):
        perf.run_capacity(perf.CapacityReq(name="x", scenario="journey", start=0))
    with pytest.raises(errors.ValidationError):
        perf.run_benchmark(["8.5.1"])          # needs >= 2 versions


def test_uptime_health_parsing():
    assert lc._uptime_health("Up 2 hours (healthy)") == ("2 hours", "healthy")
    assert lc._uptime_health("Up 47 minutes") == ("47 minutes", "")
    assert lc._uptime_health("Exited (0) 5 minutes ago") == ("", "")
    assert lc._uptime_health("") == ("", "")


def test_event_model_and_emit():
    seen: list[events.Event] = []
    events.info(seen.append, "hello", phase="boot", pct=50, name="x")
    events.warn(seen.append, "careful", phase="wait")
    assert seen[0].phase == "boot" and seen[0].pct == 50 and seen[0].data == {"name": "x"}
    assert seen[1].level == "warn"
    d = seen[0].as_dict()
    assert d["message"] == "hello" and d["terminal"] is False
