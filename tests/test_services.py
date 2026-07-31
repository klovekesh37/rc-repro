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
    # DockerError, not NotReadyError: an absent engine is a preflight problem the
    # caller must fix (exit 3), not a "still starting, poll again" state (exit 5).
    # Callers catching ReproError are unaffected; the web API status for this case
    # moves 409 -> 502, which is the more accurate answer for a dependency being
    # unavailable.
    monkeypatch.setattr(lc.runner, "docker_available", lambda: False)
    with pytest.raises(errors.DockerError):
        lc.require_docker()
    assert errors.DockerError.exit_code == 3


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


def test_rc_restart_count(monkeypatch):
    import types
    from rc_repro import runner
    monkeypatch.setattr(runner, "service_container_ids", lambda n, s: ["abc"] if s == "rocketchat" else [])
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="7\n", returncode=0))
    assert runner.rc_restart_count("x") == 7
    monkeypatch.setattr(runner, "service_container_ids", lambda n, s: [])
    assert runner.rc_restart_count("x") == 0   # no container -> 0, no crash


def test_kernel_major_minor_parsing():
    from rc_repro import cli
    assert cli._kernel_major_minor("6.19.7-200.fc43.aarch64") == (6, 19)
    assert cli._kernel_major_minor("5.15.0-generic") == (5, 15)
    assert cli._kernel_major_minor("6.19") == (6, 19)
    assert cli._kernel_major_minor(None) is None
    assert cli._kernel_major_minor("not-a-kernel") is None


def test_hub_logged_in(monkeypatch, tmp_path):
    from rc_repro import runner
    # no auth files readable anywhere -> can't tell (None)
    monkeypatch.delenv("REGISTRY_AUTH_FILE", raising=False)
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.setattr(runner.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    assert runner.hub_logged_in() is None

    # a config with a Hub auth entry -> True
    cfg = tmp_path / "config.json"
    cfg.write_text('{"auths": {"https://index.docker.io/v1/": {"auth": "x"}}}')
    monkeypatch.setenv("REGISTRY_AUTH_FILE", str(cfg))
    assert runner.hub_logged_in() is True

    # a readable config with only a non-Hub registry -> False
    cfg.write_text('{"auths": {"ghcr.io": {"auth": "x"}}}')
    assert runner.hub_logged_in() is False


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


# --- Kubernetes topology (no cluster required) ---------------------------------


class _FakeRun:
    """Records every external command instead of running it."""

    def __init__(self, *, tools=("kind", "kubectl", "helm"), clusters="",
                 kernel="6.8.0-generic", labels='{"app.kubernetes.io/managed-by":"rc-repro"}',
                 mongo_ready="true", rs_ok="1", index=None,
                 mem_gib=8.0, cpus=4):
        self.tools, self.clusters, self.kernel, self.labels = tools, clusters, kernel, labels
        self.mongo_ready, self.rs_ok = mongo_ready, rs_ok
        self.mem_bytes, self.cpus = int(mem_gib * 1024 ** 3), cpus
        # A realistic slice of `helm search repo --versions -o json`, including the
        # sparse appVersion coverage the real index has.
        self.index = index if index is not None else [
            {"version": "7.0.2", "app_version": "8.6.1"},
            {"version": "7.0.1", "app_version": "8.6.1"},
            {"version": "7.0.0", "app_version": "8.5.0"},
            {"version": "6.32.1", "app_version": "8.2.0"},
            {"version": "6.27.1", "app_version": "7.11.0"},
        ]
        self.calls: list[list[str]] = []
        self.applied: list[str] = []
        self.installed: list[dict] = []
        self.forwards: list[tuple] = []

    def which(self, tool):
        return f"/usr/bin/{tool}" if tool in self.tools else None

    def run(self, argv, *, check=True):
        import subprocess
        self.calls.append(argv)
        out = ""
        if argv[:3] == ["kind", "get", "clusters"]:
            out = self.clusters
        elif argv[:2] == ["docker", "info"]:
            # two different probes share the command; the format tells them apart
            out = (f"{self.mem_bytes} {self.cpus}" if "MemTotal" in argv[-1]
                   else self.kernel)
        elif "jsonpath={.metadata.labels}" in argv:
            out = self.labels
        elif "jsonpath={.status.containerStatuses[0].ready}" in argv:
            out = self.mongo_ready
        elif "rs.status().ok" in argv:
            out = self.rs_ok
        elif any("rs.initiate" in a for a in argv):
            out = "{ ok: 1 }"
        elif argv[:3] == ["helm", "search", "repo"]:
            import json as _j
            out = _j.dumps(self.index)
        return subprocess.CompletedProcess(argv, 0, out, "")

    def apply(self, ctx, ns, manifest):
        self.applied.append(manifest)

    def install(self, ctx, ns, values, chart_version=""):
        self.installed.append({"values": values, "chart_version": chart_version})

    def sleep(self, seconds):
        pass          # never actually wait in tests

    def port_forward(self, ctx, ns, host_port):
        self.forwards.append((ns, host_port))
        return 424242          # a pid that is not alive, so probes report "down"


def test_k8s_requires_the_toolchain():
    from rc_repro.services import k8s
    with pytest.raises(errors.DockerError) as ei:
        k8s.require_tools(_FakeRun(tools=("kubectl",)))
    msg = str(ei.value)
    assert "kind" in msg and "helm" in msg      # names what is missing
    k8s.require_tools(_FakeRun())               # all present: no raise


def test_k8s_refuses_mongo8_on_a_619_kernel():
    # RC 8.2+ needs MongoDB 8.0, which hard-exits on kernel 6.19+ (SERVER-121912).
    # That is impossible, not slow, so it must fail preflight rather than time out.
    from rc_repro.services import k8s
    with pytest.raises(errors.ValidationError) as ei:
        k8s.check_mongo_kernel_support("8.0", _FakeRun(kernel="6.19.7-200.fc43.aarch64"))
    assert "SERVER-121912" in str(ei.value)
    # MongoDB 7.0 is unaffected on the same kernel
    k8s.check_mongo_kernel_support("7.0", _FakeRun(kernel="6.19.7-200.fc43.aarch64"))
    # and MongoDB 8.0 is fine on an older kernel
    k8s.check_mongo_kernel_support("8.0", _FakeRun(kernel="6.8.0-117-generic"))


def test_k8s_values_never_use_the_bundled_mongodb():
    # The chart's Bitnami MongoDB is amd64-only and its default tag is rejected by
    # the chart's own appVersion, so the bundled subchart is always disabled.
    from rc_repro.services import k8s
    v = k8s.build_values("8.6.1", offline=True).values
    assert v["mongodb"]["enabled"] is False
    assert v["microservices"]["enabled"] is True
    assert "replicaSet=rs0" in v["externalMongodbUrl"]   # change streams need it
    assert v["image"]["tag"] == "8.6.1"
    # RC 8.x deprecated the oplog URL; 7.x still wants it
    assert "externalMongodbOplogUrl" not in v
    assert "externalMongodbOplogUrl" in k8s.build_values("7.10.13", offline=True).values


def test_k8s_create_labels_for_ownership_and_installs_the_chart():
    from rc_repro.services import k8s
    fake = _FakeRun()
    out = k8s.create_repro("t1", "8.6.1", offline=True, run=fake)
    assert out["namespace"] == "rc-repro-t1" and out["topology"] == "kubernetes"
    flat = [" ".join(c) for c in fake.calls]
    # ownership is asserted at creation, so teardown can prove what it may delete
    assert any("label namespace rc-repro-t1" in c and "managed-by=rc-repro" in c
               for c in flat)
    # every kubectl call passes an explicit context: the ambient one is never used
    assert all("--context" in c for c in fake.calls if c[0] == "kubectl")
    assert fake.applied and "replSet" in fake.applied[0]   # replica set, not standalone
    assert fake.installed and fake.installed[0]["values"]["mongodb"]["enabled"] is False
    # the chart is pinned, not left to helm's "latest"
    assert fake.installed[0]["chart_version"] == "7.0.2"


def test_k8s_teardown_refuses_a_namespace_it_does_not_own():
    from rc_repro.services import k8s
    # No ownership label -> not ours -> nothing is deleted, and that is not an error
    fake = _FakeRun(labels="{}")
    out = k8s.teardown("t1", run=fake)
    assert out["removed"] == [] and out["residual"] == []
    assert not any("delete namespace" in " ".join(c) for c in fake.calls)
    # Labelled -> ours -> deleted, and reported
    fake2 = _FakeRun()
    out2 = k8s.teardown("t1", run=fake2)
    assert out2["removed"] == ["namespace/rc-repro-t1"]
    assert out2["residual"] == []


def test_k8s_reuses_an_existing_cluster():
    from rc_repro.services import k8s
    fake = _FakeRun(clusters=k8s.CLUSTER_NAME)
    k8s.ensure_cluster(run=fake)
    assert not any(c[:3] == ["kind", "create", "cluster"] for c in fake.calls)
    fake2 = _FakeRun(clusters="")
    k8s.ensure_cluster(run=fake2)
    assert any(c[:3] == ["kind", "create", "cluster"] for c in fake2.calls)


def test_k8s_create_persists_shared_metadata(tmp_path, monkeypatch):
    # A Kubernetes repro must be visible to list/info/resolve_name exactly like a
    # Compose one, so it uses the same Metadata record rather than a second format.
    from rc_repro import runner
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    fake = _FakeRun()
    out = k8s.create_repro("t2", "8.6.1", offline=True, port=31234, run=fake)

    assert runner.exists("t2")
    m = runner.read_meta("t2")
    assert m.preset == "microservices" and m.host_port == 31234
    assert m.root_url == "http://localhost:31234"
    assert m.extra["topology"] == "kubernetes"
    assert m.extra["k8s_namespace"] == "rc-repro-t2"
    # the workspace holds values.yaml, not docker-compose.yml
    ws = runner.workspace("t2")
    assert (ws / "values.yaml").exists()
    assert not (ws / "docker-compose.yml").exists()
    assert "microservices" in (ws / "values.yaml").read_text()
    assert fake.forwards == [("rc-repro-t2", 31234)]
    # The reported state is probed, not assumed. Started right after helm install
    # the forward often dies (the Service has no ready endpoints yet), so claiming
    # "up" would send someone debugging their network instead of waiting for a pod.
    assert out["port_forward"] == "down"     # fake pid is not alive


def test_k8s_forward_state_reports_down_without_failing_the_repro(tmp_path, monkeypatch):
    # A repro whose forward died is still running in the cluster. Reporting it as
    # broken would be wrong, so the forward gets its own state.
    from rc_repro import runner
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("t3", "8.6.1", offline=True, port=31235, run=_FakeRun())
    m = runner.read_meta("t3")
    assert k8s.forward_state(m) == "down"        # the fake pid is not alive
    assert k8s.stop_port_forward(m) is False     # nothing to kill, not an error


def test_k8s_ensure_port_forward_revives_a_dead_one(tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    fake = _FakeRun()
    k8s.create_repro("t4", "8.6.1", offline=True, port=31236, run=fake)
    m = runner.read_meta("t4")
    fake.forwards.clear()
    assert k8s.ensure_port_forward(m, run=fake) == 424242
    assert fake.forwards == [("rc-repro-t4", 31236)]   # re-established


def test_k8s_teardown_with_volumes_forgets_the_record(tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("t5", "8.6.1", offline=True, port=31237, run=_FakeRun())
    assert runner.exists("t5")
    out = k8s.teardown("t5", volumes=True, run=_FakeRun())
    assert f"record/t5" in out["removed"]
    assert not runner.exists("t5")
    assert out["residual"] == []


def test_k8s_fails_loudly_when_the_replica_set_is_not_initiated():
    """Regression: this silently produced a repro that could never become ready.

    kubectl wait was called the instant after apply, before the pod existed, so it
    failed immediately; rs.initiate then ran against nothing. Both errors were
    discarded with check=False and the repro was reported as successfully created.
    Found by running the real code against a real cluster, where MongoDB reported
    NotYetInitialized.
    """
    from rc_repro.services import k8s
    # replica set never comes up -> exit 7, known dead, rather than a slow timeout
    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.init_replica_set(_FakeRun(rs_ok="0"), "ctx", "ns")
    assert "change streams" in str(ei.value)
    assert errors.CreateFailedError.exit_code == 7


def test_k8s_fails_when_mongo_never_becomes_ready():
    from rc_repro.services import k8s
    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.init_replica_set(_FakeRun(mongo_ready=""), "ctx", "ns")
    assert "did not become ready" in str(ei.value)


def test_k8s_tolerates_an_already_initiated_replica_set():
    # Re-running create against a reused namespace must not fail on this.
    from rc_repro.services import k8s

    class AlreadyInit(_FakeRun):
        def run(self, argv, *, check=True):
            import subprocess
            if any("rs.initiate" in a for a in argv):
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 1, "", "already initialized")
            return super().run(argv, check=check)

    k8s.init_replica_set(AlreadyInit(), "ctx", "ns")   # no raise


def test_k8s_chart_resolution_prefers_an_exact_appversion():
    from rc_repro.services import k8s
    # two charts declare 8.6.1; the newest wins
    assert k8s.resolve_chart_version("8.6.1", _FakeRun()) == "7.0.2"


def test_k8s_chart_resolution_floors_when_no_exact_match():
    # Most Rocket.Chat releases have no chart declaring them, so an exact match
    # cannot be required. Never pick a chart newer than the app it deploys.
    from rc_repro.services import k8s
    # chart 7.0.0 declares appVersion 8.5.0, which is newer than 8.4.0, so the
    # floor is chart 6.32.1 (appVersion 8.2.0) rather than the highest chart number
    assert k8s.resolve_chart_version("8.4.0", _FakeRun()) == "6.32.1"
    assert k8s.resolve_chart_version("8.0.0", _FakeRun()) == "6.27.1"  # 8.2.0 too new
    # and the chart is never newer than the app it deploys
    assert k8s.resolve_chart_version("8.5.0", _FakeRun()) == "7.0.0"


def test_k8s_chart_resolution_never_hard_fails():
    from rc_repro.services import k8s
    # nothing at or below the request: newest chart, with a warning
    events_seen = []
    assert k8s.resolve_chart_version("1.0.0", _FakeRun(),
                                     emit=events_seen.append) == "7.0.2"
    assert any(e.level == "warn" for e in events_seen)
    # unreadable index: fall back to letting helm choose rather than failing
    events_seen.clear()
    assert k8s.resolve_chart_version("8.6.1", _FakeRun(index=[]),
                                     emit=events_seen.append) == ""
    assert any("not fully pinned" in e.message for e in events_seen)


def test_k8s_pods_map_to_the_compose_container_shape(tmp_path, monkeypatch):
    # The mapping is the point: a caller reads `info` identically on both
    # topologies, which is what keeps the Kubernetes path invisible to consumers.
    import json as _j
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("p1", "8.6.1", offline=True, port=31300, run=_FakeRun())

    class WithPods(_FakeRun):
        def run(self, argv, *, check=True):
            import subprocess
            if argv[-1] == "json" and "pods" in argv:
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, _j.dumps({"items": [
                    {"metadata": {"name": "rc-rocketchat-x"}, "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": True, "restartCount": 0}]}},
                    {"metadata": {"name": "rc-ddp-streamer-y"}, "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": False, "restartCount": 2}]}},
                ]}), "")
            return super().run(argv, check=check)

    got = k8s.pods("p1", WithPods())
    assert [p["service"] for p in got] == ["rc-ddp-streamer-y", "rc-rocketchat-x"]
    assert got[1] == {"service": "rc-rocketchat-x", "state": "running",
                      "status": "1/1 ready"}
    # restarts are surfaced, since they are how a transient failure shows up
    assert "2 restart(s)" in got[0]["status"]

    d = k8s.detail("p1", WithPods())
    assert d["topology"] == "kubernetes" and d["namespace"] == "rc-repro-p1"
    assert d["state"] == "starting"          # not everything is ready yet
    # the forward is reported apart from state: a dead forward does not mean the
    # repro is broken, it means the tunnel needs re-establishing
    assert d["port_forward"] == "down"
    assert d["links"][0]["url"] == "http://localhost:31300"


def test_lifecycle_detail_dispatches_to_kubernetes(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("p2", "8.6.1", offline=True, port=31301, run=_FakeRun())
    # would raise on a Compose-only path (no docker-compose.yml exists here)
    d = lc.detail("p2")
    assert d["topology"] == "kubernetes"


# --- evidence -------------------------------------------------------------------


def test_evidence_redacts_the_root_url():
    # Credentials in a URL are the classic accidental leak, and a path can carry a
    # ticket id or customer name. Only the origin is kept.
    from rc_repro.services import evidence
    assert evidence.safe_origin("http://admin:pw@localhost:3000/channel/x?t=1") == \
        "http://localhost:3000"
    assert evidence.safe_origin("https://[::1]:8443/p") == "https://[::1]:8443"
    for bad in ("ftp://x", "", "not a url", "file:///etc/passwd"):
        assert evidence.safe_origin(bad) == "REDACTED"


def test_evidence_is_backend_neutral(tmp_path, monkeypatch):
    # The repro block must have the same shape on both topologies, so a consumer
    # never branches on topology to read it.
    from rc_repro.services import evidence, k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("e1", "8.6.1", offline=True, port=31400, run=_FakeRun())
    rec = evidence.record("e1")

    assert set(rec) == {"repro", "runtime", "artifact", "ownership", "license",
                        "retention", "generated_at"}
    assert set(rec["repro"]) == {"name", "preset", "topology", "rc_version",
                                 "rc_image", "mongo_tag", "mongo_flavor",
                                 "root_url", "host_port", "version_source",
                                 "created_at"}
    assert rec["repro"]["topology"] == "kubernetes"
    assert rec["repro"]["root_url"] == "http://localhost:31400"
    # the rendered artifact is hashed, whichever one the topology produced
    assert rec["artifact"]["name"] == "values.yaml"
    assert len(rec["artifact"]["sha256"]) == 64
    # ownership is stated so a teardown decision is auditable
    assert rec["ownership"]["proof"] == "label"
    assert rec["ownership"]["namespace"] == "rc-repro-e1"


def test_evidence_records_the_licence_state_without_the_value(tmp_path, monkeypatch):
    # An unlicensed microservices repro can look healthy while not behaving as
    # licensed, so citing it as proof without this caveat is the actual harm.
    from rc_repro.services import evidence, k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("e2", "8.6.1", offline=True, port=31401, run=_FakeRun())
    rec = evidence.record("e2")
    assert rec["license"] == {"required": True, "supplied": False, "source": None}


def test_evidence_cleanup_is_a_pasteable_command(tmp_path, monkeypatch):
    # A literal command, not a descriptor: an agent relays something a human can
    # paste months later without knowing the topology.
    from rc_repro.services import evidence, k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("e3", "8.6.1", offline=True, port=31402, run=_FakeRun())
    rec = evidence.record("e3")
    assert rec["retention"]["cleanup"] == "rc-repro down --name e3 --volumes --yes"


def test_evidence_never_contains_a_secret(tmp_path, monkeypatch):
    import json as _j
    from rc_repro.services import evidence, k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RC_REPRO_REG_TOKEN", "SUPERSECRETTOKEN")
    k8s.create_repro("e4", "8.6.1", offline=True, port=31403, run=_FakeRun())
    blob = _j.dumps(evidence.record("e4"))
    assert "SUPERSECRETTOKEN" not in blob
    assert "admin123" not in blob          # the default admin password


def test_evidence_bundle_writes_manifest_and_artifact(tmp_path, monkeypatch):
    import json as _j
    from rc_repro.services import evidence, k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("e5", "8.6.1", offline=True, port=31404, run=_FakeRun())
    rec = evidence.record("e5")
    out = evidence.write_bundle("e5", tmp_path / "bundle", rec)
    assert "manifest.json" in out["files"] and "values.yaml" in out["files"]
    manifest = _j.loads((tmp_path / "bundle" / "manifest.json").read_text())
    assert manifest["repro"]["name"] == "e5"


# --- capacity preflight ----------------------------------------------------------


def test_k8s_capacity_passes_when_the_engine_is_big_enough():
    from rc_repro.services import k8s
    k8s.check_capacity(_FakeRun(mem_gib=8.0, cpus=4))          # no raise
    assert k8s.engine_capacity(_FakeRun(mem_gib=6.0, cpus=4))[1] == 4


def test_k8s_capacity_refuses_a_small_engine_without_a_grant(tmp_path, monkeypatch):
    # Podman's 2 GiB default cannot fit six Deployments and two StatefulSets. The
    # error must name the exact command, and must not re-ask a settled question.
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    with pytest.raises(errors.ValidationError) as ei:
        k8s.check_capacity(_FakeRun(mem_gib=2.0, cpus=5))
    msg = str(ei.value)
    assert "podman machine set --memory 6144" in msg
    assert "--grant engine-resize" in msg
    assert "stops unrelated containers" in msg      # the real cost, stated
    assert errors.ValidationError.exit_code == 2


def test_k8s_capacity_resizes_when_granted_and_says_so(tmp_path, monkeypatch):
    # The grant covers the action, not hiding it: the resize is reported as a warn
    # event because it restarts the engine.
    from rc_repro.services import k8s
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize"])

    class Resizes(_FakeRun):
        """Small until resized, then big enough."""
        def run(self, argv, *, check=True):
            if argv[:3] == ["podman", "machine", "set"]:
                self.mem_bytes = 8 * 1024 ** 3
            return super().run(argv, check=check)

    fake = Resizes(mem_gib=2.0, cpus=4,
                   tools=("kind", "kubectl", "helm", "podman"))
    seen = []
    k8s.check_capacity(fake, emit=seen.append)
    assert any(e.level == "warn" and "stops unrelated containers" in e.message
               for e in seen)
    flat = [" ".join(c) for c in fake.calls]
    assert any("podman machine set --memory 6144" in c for c in flat)
    assert any("podman machine stop" in c for c in flat)
    assert any("podman machine start" in c for c in flat)


def test_k8s_capacity_never_guesses_a_cpu_count(tmp_path, monkeypatch):
    # CPU is the binding constraint and cannot be fixed by the memory resize.
    # Choosing a CPU allocation for someone's machine is not rc-repro's call, so
    # this refuses even when the resize grant exists.
    from rc_repro.services import k8s
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize"])
    with pytest.raises(errors.ValidationError) as ei:
        k8s.check_capacity(_FakeRun(mem_gib=8.0, cpus=2))
    assert "CPU" in str(ei.value)


def test_k8s_capacity_skips_when_the_engine_is_unreachable():
    # require_docker reports an absent engine better than a capacity check can.
    from rc_repro.services import k8s
    k8s.check_capacity(_FakeRun(mem_gib=0.0, cpus=0))          # no raise


def test_k8s_capacity_will_not_resize_docker_desktop(tmp_path, monkeypatch):
    # Only a Podman machine can be resized from the CLI, so with Docker Desktop the
    # grant cannot be acted on and rc-repro says which knob to turn instead.
    from rc_repro.services import k8s
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize"])
    with pytest.raises(errors.ValidationError) as ei:
        k8s.check_capacity(_FakeRun(mem_gib=2.0, cpus=4))      # no podman on PATH
    assert "Docker Desktop" in str(ei.value)


def test_k8s_wait_ready_revives_the_forward_and_persists_the_new_pid(tmp_path, monkeypatch):
    # Readiness is an HTTP fact, so this reuses wait_serving rather than polling pod
    # conditions: pods being Ready is not the same as Rocket.Chat answering.
    from rc_repro import runner
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    fake = _FakeRun()
    k8s.create_repro("r1", "8.6.1", offline=True, port=31600, run=fake)
    before = runner.read_meta("r1").extra["k8s_forward_pid"]

    monkeypatch.setattr(lc, "wait_serving", lambda m, e, t: {"booted_s": 7})
    out = k8s.wait_ready("r1", run=fake)
    assert out["booted_s"] == 7 and out["name"] == "r1"
    # the dead forward was re-established and the new pid recorded, so a later
    # `down` kills the forward that is actually running
    assert runner.read_meta("r1").extra["k8s_forward_pid"] == before
    assert fake.forwards[-1] == ("rc-repro-r1", 31600)


def test_write_meta_leaves_the_rendered_artifact_alone(tmp_path, monkeypatch):
    # Updating a pid must not re-render values.yaml, or evidence's artifact hash
    # would churn for no reason.
    import hashlib
    from rc_repro import runner
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("r2", "8.6.1", offline=True, port=31601, run=_FakeRun())
    art = runner.workspace("r2") / "values.yaml"
    before = hashlib.sha256(art.read_bytes()).hexdigest()
    m = runner.read_meta("r2")
    m.extra = {**m.extra, "k8s_forward_pid": 999999}
    runner.write_meta("r2", m)
    assert hashlib.sha256(art.read_bytes()).hexdigest() == before
    assert runner.read_meta("r2").extra["k8s_forward_pid"] == 999999


def test_gate_registry_matches_what_the_code_actually_raises(tmp_path, monkeypatch):
    """A declared registry is only useful if it cannot drift from reality.

    AuthorityGateError takes its code as an argument, so nothing structural stops a
    call site inventing one that `capabilities` never advertises. This pins the gates
    that are actually raised today.
    """
    from rc_repro import errors
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    raised = set()
    try:
        onboarding.require_onboarded()
    except errors.AuthorityGateError as exc:
        raised.add(exc.code)
    onboarding.complete(grants=[])
    try:
        onboarding.require_grant("engine-resize")
    except errors.AuthorityGateError as exc:
        raised.add(exc.code)
    assert raised <= set(errors.GATE_CODES), raised - set(errors.GATE_CODES)
    assert raised == {"GATE_NOT_ONBOARDED", "GATE_ENGINE_RESIZE"}


def test_k8s_exec_uses_the_compose_service_word(tmp_path, monkeypatch):
    # A caller says "rocketchat", the same word it would use on the Compose path,
    # and does not need to know the chart's release prefix.
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("x1", "8.6.1", offline=True, port=31700, run=_FakeRun())
    seen = {}

    def fake_call(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(k8s.subprocess, "call", fake_call)
    assert k8s.exec_in("x1", "rocketchat", ["sh", "-c", "echo hi"]) == 0
    argv = seen["argv"]
    assert "deployment/rc-rocketchat" in argv
    assert argv[argv.index("--") + 1:] == ["sh", "-c", "echo hi"]
    assert "--context" in argv          # never the ambient context


def test_k8s_prune_refuses_while_repros_remain():
    from rc_repro.services import k8s

    class WithNamespaces(_FakeRun):
        def __init__(self, ns_out, **kw):
            super().__init__(clusters=k8s.CLUSTER_NAME, **kw)
            self.ns_out = ns_out

        def run(self, argv, *, check=True):
            import subprocess
            if "namespaces" in argv and "-l" in argv:
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, self.ns_out, "")
            return super().run(argv, check=check)

    busy = WithNamespaces("rc-repro-a rc-repro-b")
    out = k8s.prune_cluster(run=busy)
    assert out["deleted"] is False and out["namespaces"] == ["rc-repro-a", "rc-repro-b"]
    # deleting the cluster would take running repros with it
    assert not any(c[:3] == ["kind", "delete", "cluster"] for c in busy.calls)

    empty = WithNamespaces("")
    out2 = k8s.prune_cluster(run=empty)
    assert out2["deleted"] is True
    assert any(c[:3] == ["kind", "delete", "cluster"] for c in empty.calls)


def test_k8s_prune_does_nothing_without_an_owned_cluster():
    from rc_repro.services import k8s
    out = k8s.prune_cluster(run=_FakeRun(clusters=""))
    assert out["deleted"] is False and "no rc-repro-owned cluster" in out["reason"]


def test_evidence_bundle_captures_one_log_file_per_pod(tmp_path, monkeypatch):
    # One file per pod, not one concatenated log: nine interleaved components are
    # unreadable, and the failing one is what a reader wants.
    import json as _j
    from rc_repro.services import evidence, k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class WithLogs(_FakeRun):
        def run(self, argv, *, check=True):
            import subprocess
            if argv[-1] == "json" and "pods" in argv:
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, _j.dumps({"items": [
                    {"metadata": {"name": "rc-rocketchat-a"}, "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": True, "restartCount": 0}]}},
                    {"metadata": {"name": "mongo-0"}, "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": True, "restartCount": 0}]}},
                ]}), "")
            if "logs" in argv and "--all-containers=true" in argv:
                self.calls.append(argv)
                pod = argv[argv.index("logs") + 1]
                if pod == "mongo-0":
                    # unreadable logs must be noted, not omitted, so a reader can
                    # tell "nothing logged" from "not collected"
                    return subprocess.CompletedProcess(argv, 1, "", "container starting")
                return subprocess.CompletedProcess(argv, 0, f"log line from {pod}\n", "")
            return super().run(argv, check=check)

    fake = WithLogs()
    k8s.create_repro("b1", "8.6.1", offline=True, port=31800, run=fake)
    monkeypatch.setattr(k8s, "_Runner", lambda: fake)

    rec = evidence.record("b1")
    out = evidence.write_bundle("b1", tmp_path / "bundle", rec)
    assert "logs/rc-rocketchat-a.log" in out["files"]
    assert "logs/mongo-0.log" in out["files"]
    body = (tmp_path / "bundle" / "logs" / "rc-rocketchat-a.log").read_text()
    assert "log line from rc-rocketchat-a" in body
    note = (tmp_path / "bundle" / "logs" / "mongo-0.log").read_text()
    assert "no logs collected" in note and "container starting" in note
    # the tail is bounded, so a bundle attached to a case cannot be unbounded
    assert any("--tail=2000" in " ".join(c) for c in fake.calls)
