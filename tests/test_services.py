"""Unit tests for the service layer (pure; no Docker).

These lock in the behaviour both the CLI and the web API depend on: naming,
error typing/HTTP mapping, port validation, and the event model.
"""

from __future__ import annotations

import json
from pathlib import Path

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
                 mem_gib=8.0, cpus=4, engine="docker", cluster_owned=True):
        self.tools, self.clusters, self.kernel, self.labels = tools, clusters, kernel, labels
        self.mongo_ready, self.rs_ok = mongo_ready, rs_ok
        self.engine, self.cluster_owned = engine, cluster_owned
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

    def docker_server_platform(self):
        return "Podman Engine" if self.engine == "podman" else "Docker Engine - Community"

    def run(self, argv, *, check=True):
        import subprocess
        self.calls.append(argv)
        out = ""
        if argv[:3] == ["kind", "get", "clusters"]:
            out = self.clusters
        elif argv[:3] == ["kind", "delete", "cluster"]:
            self.clusters = ""
        elif "config" in argv and "current-context" in argv:
            out = "kind-rc-repro-local"
        elif argv[:2] == ["docker", "info"]:
            # two different probes share the command; the format tells them apart
            out = (f"{self.mem_bytes} {self.cpus}" if "MemTotal" in argv[-1]
                   else self.kernel)
        elif argv[:3] == ["podman", "machine", "inspect"]:
            out = "running" if self.engine == "podman" else ""
        elif "configmap" in argv and "rc-repro-cluster-owner" in argv:
            out = ('{"metadata":{"labels":{"app.kubernetes.io/managed-by":"rc-repro"}},'
                   '"data":{"cluster":"rc-repro-local"}}'
                   if self.cluster_owned else "")
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
        if "rc-repro-cluster-owner" in manifest:
            self.cluster_owned = True
        self.applied.append(manifest)

    def install(self, ctx, ns, values, chart_version=""):
        import subprocess
        self.installed.append({"values": values, "chart_version": chart_version})
        return subprocess.CompletedProcess(["helm", "install"], 0, "", "")

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
    # Every kubectl call names rc-repro's kubeconfig. Workload calls also name the
    # context; only `config current-context` reads it from that owned file.
    kubectl_calls = [c for c in fake.calls if c[0] == "kubectl"]
    assert all("--kubeconfig" in c for c in kubectl_calls)
    assert all("--context" in c for c in kubectl_calls if "current-context" not in c)
    assert any("rc-repro-cluster-owner" in manifest for manifest in fake.applied)
    assert any("replSet" in manifest for manifest in fake.applied)  # replica set, not standalone
    assert fake.installed and fake.installed[0]["values"]["mongodb"]["enabled"] is False
    # the chart is pinned, not left to helm's "latest"
    assert fake.installed[0]["chart_version"] == "7.0.2"


def test_k8s_client_state_is_owned_and_ambient_paths_are_ignored(tmp_path, monkeypatch):
    """A contaminated login home must neither break nor redirect rc-repro."""
    from rc_repro.services import k8s
    owned = tmp_path / "owned"
    monkeypatch.setenv("RC_REPRO_HOME", str(owned))
    monkeypatch.setenv("KUBECONFIG", "/ambient/.kube/config")
    monkeypatch.setenv("HELM_CACHE_HOME", "/ambient/.cache/helm")
    monkeypatch.setenv("HELM_CONFIG_HOME", "/ambient/.config/helm")
    monkeypatch.setenv("HELM_DATA_HOME", "/ambient/.local/share/helm")
    monkeypatch.setenv("HELM_REPOSITORY_CONFIG", "/ambient/repositories.yaml")
    monkeypatch.setenv("HELM_REPOSITORY_CACHE", "/ambient/repository")

    state = k8s.client_state()
    env = k8s._client_env()
    assert env["KUBECONFIG"] == str(state.kubeconfig)
    assert env["HELM_CACHE_HOME"] == str(state.helm_cache_home)
    assert env["HELM_CONFIG_HOME"] == str(state.helm_config_home)
    assert env["HELM_DATA_HOME"] == str(state.helm_data_home)
    assert env["HELM_REPOSITORY_CONFIG"] == str(state.helm_repository_config)
    assert env["HELM_REPOSITORY_CACHE"] == str(state.helm_repository_cache)
    assert all(str(owned) in env[key] for key in (
        "KUBECONFIG", "HELM_CACHE_HOME", "HELM_CONFIG_HOME", "HELM_DATA_HOME",
        "HELM_REPOSITORY_CONFIG", "HELM_REPOSITORY_CACHE"))

    fake = _FakeRun()
    k8s.create_repro("isolated", "8.6.1", offline=True, run=fake)
    kubeconfig = str(state.kubeconfig)
    repo_config = str(state.helm_repository_config)
    repo_cache = str(state.helm_repository_cache)
    assert any(c[:3] == ["kind", "create", "cluster"] and
               c[c.index("--kubeconfig") + 1] == kubeconfig for c in fake.calls)
    assert any(c[:3] == ["kind", "export", "kubeconfig"] and
               c[c.index("--kubeconfig") + 1] == kubeconfig for c in fake.calls)
    for call in (c for c in fake.calls if c[0] == "kubectl"):
        assert call[call.index("--kubeconfig") + 1] == kubeconfig
    for call in (c for c in fake.calls if c[0] == "helm"):
        assert call[call.index("--kubeconfig") + 1] == kubeconfig
        assert call[call.index("--repository-config") + 1] == repo_config
        assert call[call.index("--repository-cache") + 1] == repo_cache
    assert "/ambient/" not in "\n".join(" ".join(c) for c in fake.calls)


def test_k8s_real_helm_install_receives_owned_flags_and_environment(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "owned"))
    monkeypatch.setenv("KUBECONFIG", "/ambient/config")
    seen = {}

    def fake_run(argv, **kwargs):
        import subprocess
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s.subprocess, "run", fake_run)
    out = k8s._Runner().install("ctx", "ns", {"image": {"tag": "8.6.1"}}, "7.0.2")
    state = k8s.client_state()
    assert out.returncode == 0
    assert seen["argv"][seen["argv"].index("--kubeconfig") + 1] == str(state.kubeconfig)
    assert seen["argv"][seen["argv"].index("--repository-config") + 1] == \
        str(state.helm_repository_config)
    assert seen["argv"][seen["argv"].index("--repository-cache") + 1] == \
        str(state.helm_repository_cache)
    assert seen["kwargs"]["env"]["KUBECONFIG"] == str(state.kubeconfig)


def test_k8s_create_reports_unwritable_owned_state_as_preflight(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "owned"))

    def denied():
        raise PermissionError("owned client directory is read-only")

    monkeypatch.setattr(k8s, "prepare_client_state", denied)
    with pytest.raises(errors.DockerError) as ei:
        k8s.create_repro("unwritable", "8.6.1", offline=True, run=_FakeRun())
    assert ei.value.code == "ENGINE_UNAVAILABLE" and ei.value.exit_code == 3
    assert "owned client directory is read-only" in str(ei.value)


def test_k8s_repository_failure_is_terminal_and_preserves_the_cause():
    from rc_repro.services import k8s

    class RepoFail(_FakeRun):
        def run(self, argv, *, check=True):
            import subprocess
            if argv[:3] == ["helm", "repo", "update"]:
                self.calls.append(argv)
                noise = "progress\n" * 400
                return subprocess.CompletedProcess(
                    argv, 1, noise, "permission denied writing repositories.lock")
            return super().run(argv, check=check)

    fake = RepoFail()
    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.create_repro("repo-fail", "8.6.1", offline=True, run=fake)
    assert ei.value.code == "CREATE_FAILED" and ei.value.exit_code == 7
    assert "permission denied writing repositories.lock" in str(ei.value)
    assert not any(c[:3] == ["kind", "create", "cluster"] for c in fake.calls)


def test_k8s_chart_install_failure_is_structured():
    from rc_repro.services import k8s

    class InstallFail(_FakeRun):
        def install(self, ctx, ns, values, chart_version=""):
            import subprocess
            self.installed.append({"values": values, "chart_version": chart_version})
            return subprocess.CompletedProcess(
                ["helm", "install"], 1, "", "chart rendering failed")

    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.create_repro("install-fail", "8.6.1", offline=True, run=InstallFail())
    assert "chart rendering failed" in str(ei.value)


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


def test_k8s_refuses_a_same_named_cluster_without_ownership_proof(tmp_path, monkeypatch):
    from rc_repro import errors
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    with pytest.raises(errors.ConflictError) as exc:
        k8s.ensure_cluster(run=_FakeRun(
            clusters=k8s.CLUSTER_NAME, cluster_owned=False))

    assert "ownership marker" in str(exc.value)


def test_k8s_prune_refuses_a_same_named_cluster_without_ownership_proof(
        tmp_path, monkeypatch):
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    fake = _FakeRun(clusters=k8s.CLUSTER_NAME, cluster_owned=False)

    out = k8s.prune_cluster(run=fake)

    assert out["deleted"] is False
    assert "ownership marker" in out["reason"]
    assert not any(c[:3] == ["kind", "delete", "cluster"] for c in fake.calls)


def test_k8s_rolls_back_a_new_cluster_if_ownership_cannot_be_marked(
        tmp_path, monkeypatch):
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class MarkerFails(_FakeRun):
        def apply(self, ctx, ns, manifest):
            import subprocess
            if "rc-repro-cluster-owner" in manifest:
                return subprocess.CompletedProcess(
                    ["kubectl", "apply"], 1, "", "marker rejected")
            return super().apply(ctx, ns, manifest)

    fake = MarkerFails(clusters="")
    with pytest.raises(errors.CreateFailedError) as exc:
        k8s.ensure_cluster(run=fake)

    assert "marker rejected" in str(exc.value)
    assert "rolled back" in str(exc.value)
    assert any(call[:3] == ["kind", "delete", "cluster"] for call in fake.calls)


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
    assert m.extra["chart_version"] == "7.0.2"
    from datetime import datetime
    created = datetime.fromisoformat(m.created_at)
    assert created.tzinfo is not None
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


def test_k8s_chart_resolution_refuses_an_unreadable_or_empty_index():
    from rc_repro.services import k8s
    # nothing at or below the request: newest chart, with a warning
    events_seen = []
    assert k8s.resolve_chart_version("1.0.0", _FakeRun(),
                                     emit=events_seen.append) == "7.0.2"
    assert any(e.level == "warn" for e in events_seen)
    # An empty index must not silently turn the install into "latest".
    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.resolve_chart_version("8.6.1", _FakeRun(index=[]))
    assert "refusing an unpinned install" in str(ei.value)


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
    assert rec["repro"]["created_at"]
    assert rec["retention"]["created_at"] == rec["repro"]["created_at"]
    assert rec["runtime"]["engine"]["chart_version"] == "7.0.2"
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
        k8s.check_capacity(_FakeRun(
            mem_gib=2.0, cpus=5, engine="podman",
            tools=("kind", "kubectl", "helm", "podman")))
    msg = str(ei.value)
    assert "podman machine set --memory 6144" in msg
    assert "rc-repro onboard" in msg
    assert "--grant engine-resize" not in msg
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

    fake = Resizes(mem_gib=2.0, cpus=4, engine="podman",
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


def test_k8s_capacity_will_not_resize_a_separate_podman_install(tmp_path, monkeypatch):
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize"])
    fake = _FakeRun(
        mem_gib=2.0, cpus=4, engine="docker",
        tools=("kind", "kubectl", "helm", "podman"))

    with pytest.raises(errors.ValidationError):
        k8s.check_capacity(fake)

    assert not any(call[:2] == ["podman", "machine"] for call in fake.calls)


def test_k8s_wait_ready_revives_the_forward_and_persists_the_new_pid(tmp_path, monkeypatch):
    # Readiness is an HTTP fact, so success is Rocket.Chat answering (api_info),
    # while the forward is revived first because it may have died with its starter.
    from rc_repro import rcapi, runner
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    fake = _FakeRun()
    k8s.create_repro("r1", "8.6.1", offline=True, port=31600, run=fake)
    before = runner.read_meta("r1").extra["k8s_forward_pid"]

    monkeypatch.setattr(rcapi, "api_info", lambda url, timeout=5.0: {"version": "8.6.1"})
    out = k8s.wait_ready("r1", run=fake)
    assert out["version"] == "8.6.1" and out["name"] == "r1"
    # the dead forward was re-established and the new pid recorded, so a later
    # `down` kills the forward that is actually running
    assert runner.read_meta("r1").extra["k8s_forward_pid"] == before
    assert fake.forwards[-1] == ("rc-repro-r1", 31600)


def test_k8s_wait_ready_reconciles_a_forward_that_dies_inside_the_loop(
        tmp_path, monkeypatch):
    """Regression from fresh-VPS acceptance: both the create-time forward and the
    first ready-time replacement can exit before the Service has an endpoint. The
    wait loop must keep reconciling instead of polling the dead port until timeout.
    """
    from rc_repro import rcapi, runner
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class EarlyDeaths(_FakeRun):
        def port_forward(self, ctx, ns, host_port):
            self.forwards.append((ns, host_port))
            return 41000 + len(self.forwards)

    fake = EarlyDeaths()
    k8s.create_repro("r1-loop", "8.6.1", offline=True, port=31609, run=fake)
    # Create starts 41001 and the first ready tick starts 41002; both die early.
    # The second ready tick starts 41003 after the Service gains an endpoint.
    monkeypatch.setattr(k8s, "_pid_alive", lambda pid: pid == 41003)

    def api_info(url, timeout=5.0):
        pid = runner.read_meta("r1-loop").extra["k8s_forward_pid"]
        return {"version": "8.6.1"} if pid == 41003 else None

    monkeypatch.setattr(rcapi, "api_info", api_info)
    out = k8s.wait_ready("r1-loop", timeout=15, run=fake)

    assert out["version"] == "8.6.1"
    assert len(fake.forwards) == 3
    assert runner.read_meta("r1-loop").extra["k8s_forward_pid"] == 41003


def test_k8s_wait_ready_aborts_on_a_terminal_pod_failure(tmp_path, monkeypatch):
    """Regression for #11: a stuck pull must abort with exit 7, not sit out the
    timeout. This is the arm64 case the measurement work observed."""
    from rc_repro import errors, rcapi
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    fake = _FakeRun()
    k8s.create_repro("r1b", "8.6.1", offline=True, port=31607, run=fake)
    monkeypatch.setattr(rcapi, "api_info", lambda url, timeout=5.0: None)  # never serves
    monkeypatch.setattr(k8s, "detect_terminal_pod_failure",
                        lambda name, run=None: ("mongo-0", "IMAGE_PLATFORM_MISMATCH: no match for platform"))
    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.wait_ready("r1b", timeout=30, run=fake)
    assert errors.CreateFailedError.exit_code == 7
    assert "terminal condition" in str(ei.value)


def test_detect_terminal_pod_failure_reads_the_waiting_reason(tmp_path, monkeypatch):
    # ImagePullBackOff alone is not terminal (a slow registry looks the same); it is
    # the reason string that discriminates, exactly as the decision said.
    import json as _j
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class WithPods(_FakeRun):
        def __init__(self, waiting_msg, **kw):
            super().__init__(**kw); self.waiting_msg = waiting_msg
        def run(self, argv, *, check=True):
            import subprocess
            if argv[-1] == "json" and "pods" in argv:
                return subprocess.CompletedProcess(argv, 0, _j.dumps({"items": [
                    {"metadata": {"name": "mongo-0"}, "status": {"containerStatuses": [
                        {"state": {"waiting": {"reason": "ImagePullBackOff",
                                               "message": self.waiting_msg}}}]}}]}), "")
            return super().run(argv, check=check)

    k8s.create_repro("r1c", "8.6.1", offline=True, port=31608, run=_FakeRun())
    # a platform mismatch is terminal
    hit = k8s.detect_terminal_pod_failure("r1c", WithPods("no match for platform in manifest"))
    assert hit and "IMAGE_PLATFORM_MISMATCH" in hit[1]
    # a plain transient backoff is not
    assert k8s.detect_terminal_pod_failure("r1c", WithPods("Back-off pulling image")) is None


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
    try:
        onboarding.require_grant("owned-cluster")
    except errors.AuthorityGateError as exc:
        raised.add(exc.code)
    assert raised <= set(errors.GATE_CODES), raised - set(errors.GATE_CODES)
    assert raised == {
        "GATE_NOT_ONBOARDED", "GATE_ENGINE_RESIZE", "GATE_OWNED_CLUSTER"
    }


def test_k8s_exec_uses_the_compose_service_word(tmp_path, monkeypatch):
    # A caller says "rocketchat", the same word it would use on the Compose path,
    # and does not need to know the chart's release prefix.
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("x1", "8.6.1", offline=True, port=31700, run=_FakeRun())
    seen = {}

    def fake_call(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return 0

    monkeypatch.setattr(k8s.subprocess, "call", fake_call)
    assert k8s.exec_in("x1", "rocketchat", ["sh", "-c", "echo hi"]) == 0
    argv = seen["argv"]
    assert "deployment/rc-rocketchat" in argv
    assert argv[argv.index("--") + 1:] == ["sh", "-c", "echo hi"]
    assert "--context" in argv          # never the ambient context
    assert "--kubeconfig" in argv
    assert seen["env"]["KUBECONFIG"] == argv[argv.index("--kubeconfig") + 1]


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


def test_k8s_prune_refuses_when_namespace_state_is_ambiguous():
    from rc_repro.services import k8s

    class NamespaceQueryFails(_FakeRun):
        def __init__(self):
            super().__init__(clusters=k8s.CLUSTER_NAME)

        def run(self, argv, *, check=True):
            import subprocess
            if "namespaces" in argv and "-l" in argv:
                self.calls.append(argv)
                return subprocess.CompletedProcess(
                    argv, 1, "", "the owned kubeconfig is unreadable")
            return super().run(argv, check=check)

    fake = NamespaceQueryFails()
    out = k8s.prune_cluster(run=fake)
    assert out["deleted"] is False and out["exists"] is True
    assert "refusing to delete" in out["reason"]
    assert not any(c[:3] == ["kind", "delete", "cluster"] for c in fake.calls)


def test_k8s_prune_does_not_claim_a_failed_delete_succeeded():
    from rc_repro.services import k8s

    class DeleteFails(_FakeRun):
        def __init__(self):
            super().__init__(clusters=k8s.CLUSTER_NAME)

        def run(self, argv, *, check=True):
            import subprocess
            if argv[:3] == ["kind", "delete", "cluster"]:
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 1, "", "docker refused removal")
            return super().run(argv, check=check)

    with pytest.raises(errors.DockerError) as exc:
        k8s.prune_cluster(run=DeleteFails())
    assert "docker refused removal" in str(exc.value)


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


# --- topology dispatch coverage -------------------------------------------------
#
# Every one of these was a live bug that the previous 221 tests passed straight
# through, because they exercised services/k8s.py directly and never asked whether
# the shared lifecycle verbs routed to it.


def _make_k8s_repro(name, port, monkeypatch, tmp_path):
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["owned-cluster"])
    k8s.create_repro(name, "8.6.1", offline=True, port=port, run=_FakeRun())


def test_teardown_dispatches_to_kubernetes(tmp_path, monkeypatch):
    """Regression: `down` demanded Docker and ran `compose down` on a repro that has
    no compose project, so a Kubernetes repro could not be removed through the CLI."""
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("t9", 31910, monkeypatch, tmp_path)
    # docker deliberately absent: the Kubernetes path must not require it
    monkeypatch.setattr(lc.runner, "docker_available", lambda: False)
    monkeypatch.setattr(k8s, "_Runner", lambda: _FakeRun())
    out = lc.teardown("t9", volumes=True, confirm=True)
    assert out["removed_ok"] is True
    assert "namespace/rc-repro-t9" in out["removed"]
    assert not lc.runner.exists("t9")


def test_teardown_still_requires_confirmation_for_volumes(tmp_path, monkeypatch):
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("t10", 31911, monkeypatch, tmp_path)
    with pytest.raises(errors.ValidationError):
        lc.teardown("t10", volumes=True, confirm=False)


def test_prune_never_deletes_a_live_kubernetes_repro(tmp_path, monkeypatch):
    """Regression, and the most dangerous one found: a Kubernetes repro's project is
    its namespace, which is never in the compose project list, so the compose rule
    classified a RUNNING repro as prunable and would have deleted it."""
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("t11", 31912, monkeypatch, tmp_path)
    monkeypatch.setattr(lc.runner, "docker_available", lambda: True)
    monkeypatch.setattr(lc.runner, "project_states", lambda: {})

    monkeypatch.setattr(k8s, "pods", lambda name, run=None: [
        {"service": "rc-rocketchat-a", "state": "running", "status": "1/1 ready"}])
    assert lc.prunable() == []                     # live: must not be prunable

    monkeypatch.setattr(k8s, "pods", lambda name, run=None: [])
    assert lc.prunable() == ["t11"]                # genuinely empty: prunable

    def boom(name, run=None):
        raise RuntimeError("cluster unreachable")
    monkeypatch.setattr(k8s, "pods", boom)
    assert lc.prunable() == []                     # ambiguity is never prunable


def test_list_reports_real_state_for_kubernetes_repros(tmp_path, monkeypatch):
    """Regression: state came from compose, so every Kubernetes repro showed '?'."""
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("t12", 31913, monkeypatch, tmp_path)
    monkeypatch.setattr(lc.runner, "docker_available", lambda: False)
    monkeypatch.setattr(k8s, "pods", lambda name, run=None: [
        {"service": "rc-rocketchat-a", "state": "running", "status": "1/1 ready"}])
    row = next(r for r in lc.list_repros() if r["name"] == "t12")
    assert row["state"] == "running"               # not "?"
    assert row["root_url"] == "http://localhost:31913"


def test_start_and_stop_are_refused_rather_than_silently_wrong(tmp_path, monkeypatch):
    # Scaling to zero is not the same as stopping a container, and doing something
    # different under the same word is worse than refusing.
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("t13", 31914, monkeypatch, tmp_path)
    for action in ("start", "stop"):
        with pytest.raises(errors.ValidationError) as ei:
            lc.set_state("t13", action)
        assert "not supported on the Kubernetes topology" in str(ei.value)


def test_restart_dispatches_to_a_rollout(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("t14", 31915, monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(k8s, "restart",
                        lambda n, emit=None, run=None: seen.setdefault("name", n) and 0 or 0)
    lc.set_state("t14", "restart")
    assert seen["name"] == "t14"


def test_create_passes_port_and_honours_wait(tmp_path, monkeypatch):
    """Regression: the dispatch dropped req.port and ignored req.wait, so --port was
    silently ignored and --wait returned an unready repro with no error."""
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    # The Kubernetes path now gates on onboarding, so satisfy it: this test is about
    # port and wait, not the gate (which has its own test below).
    onboarding.complete(grants=["engine-resize", "owned-cluster"])
    got = {}

    def fake_create(name, version, **kw):
        got.update(kw, name=name)
        return {"name": name}

    monkeypatch.setattr(k8s, "create_repro", fake_create)
    monkeypatch.setattr(k8s, "wait_ready", lambda n, emit=None: {"booted_s": 5})
    res = lc.create_repro(lc.CreateReq(version="8.6.1", preset="microservices",
                                       name="t15", port=31916, wait=True))
    assert got["port"] == 31916                    # no longer dropped
    assert res["waited"] is True and res["booted_s"] == 5


def test_require_compose_topology_refuses_rather_than_no_ops(tmp_path, monkeypatch):
    # The design's rule: a command accepted and then doing nothing is the
    # afternoon-wasting failure rc-repro exists to remove.
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("g1", 31920, monkeypatch, tmp_path)
    with pytest.raises(errors.ValidationError) as ei:
        lc.require_compose_topology("g1", "stats", "It reads container stats.")
    msg = str(ei.value)
    assert "not supported on the kubernetes topology" in msg
    assert "It reads container stats." in msg      # says why, not just no
    assert "rc-repro info" in msg                  # offers the nearest thing
    assert errors.ValidationError.exit_code == 2


def test_require_compose_topology_is_silent_for_compose(tmp_path, monkeypatch):
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(lc, "topology_of_repro", lambda n: "compose")
    lc.require_compose_topology("anything", "stats")      # no raise


def test_ensure_reachable_revives_the_forward_and_persists_it(tmp_path, monkeypatch):
    # Every HTTP-using verb needs this: on Kubernetes the URL is a port-forward that
    # dies with whatever started it, so otherwise those verbs fail for a reason
    # unrelated to what was asked.
    from rc_repro import runner
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("g2", 31921, monkeypatch, tmp_path)
    fake = _FakeRun()
    monkeypatch.setattr(k8s, "_Runner", lambda: fake)
    monkeypatch.setattr(k8s, "ensure_port_forward", lambda m, e=None, r=None: 777777)
    lc.ensure_reachable("g2")
    assert runner.read_meta("g2").extra["k8s_forward_pid"] == 777777


def test_ensure_reachable_is_a_no_op_for_compose(tmp_path, monkeypatch):
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(lc, "topology_of_repro", lambda n: "compose")
    lc.ensure_reachable("whatever")        # must not raise or touch anything


def test_logs_dispatches_to_kubectl(tmp_path, monkeypatch):
    """Regression: `logs` was in the design's parity table and never wired, so it ran
    `compose logs` against a repro with no compose project."""
    from typer.testing import CliRunner
    from rc_repro import cli
    from rc_repro.cli import app
    from rc_repro.services import k8s
    _make_k8s_repro("g3", 31922, monkeypatch, tmp_path)
    monkeypatch.setattr(cli.runner, "docker_available", lambda: True)
    seen = {}

    def fake_logs(name, *, follow=False, tail=None, run=None):
        seen.update(name=name, follow=follow, tail=tail)
        return 0

    monkeypatch.setattr(k8s, "logs", fake_logs)
    res = CliRunner().invoke(app, ["logs", "--name", "g3", "--tail", "50"])
    assert res.exit_code == 0
    assert seen == {"name": "g3", "follow": False, "tail": 50}


def test_wait_and_finalize_dispatches_for_every_caller(tmp_path, monkeypatch):
    """Regression: dispatch lived in the CLI's `ready --json` branch only, so the
    non-json CLI path and the web GUI both called the compose-shaped path. It now
    dispatches in the service function, fully to k8s.wait_ready (not just revive-then-
    compose-wait), so all three callers get terminal-pod detection."""
    from rc_repro import runner
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    _make_k8s_repro("w1", 31930, monkeypatch, tmp_path)
    seen = {}

    def fake_wait(name, timeout=600.0, emit=None):
        seen["name"] = name
        return {"booted_s": 3, "version": "8.6.1"}

    monkeypatch.setattr(k8s, "wait_ready", fake_wait)
    monkeypatch.setattr(lc, "wait_serving", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("wait_serving must not run for a Kubernetes repro")))
    out = lc.wait_and_finalize(runner.read_meta("w1"))
    assert seen["name"] == "w1"        # dispatched to the Kubernetes wait
    assert out["running_version"] == "8.6.1"


def test_web_stats_is_guarded_like_the_cli():
    # The GUI calls the same service layer, so it inherits the same class of bug.
    src = Path("rc_repro/web/app.py").read_text(encoding="utf-8")
    i = src.find("ids = runner.container_ids(target)")
    assert i > 0
    assert "require_compose_topology" in src[max(0, i - 500):i]


# --- reopened decisions, now carried out ---------------------------------------


def test_onboarding_gate_fires_on_the_kubernetes_path(tmp_path, monkeypatch):
    """#7: require_onboarded was dead code. It now gates the Kubernetes create path,
    while the Docker default stays zero-config."""
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    with pytest.raises(errors.AuthorityGateError) as ei:
        lc.create_repro(lc.CreateReq(version="8.6.1", preset="microservices", name="og"))
    assert ei.value.code == "GATE_NOT_ONBOARDED" and ei.value.exit_code == 6


def test_docker_default_needs_no_onboarding(tmp_path, monkeypatch):
    # The gate must not touch the Docker default, which has always worked with no
    # config. It should fail on the engine being down, not on onboarding.
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(lc.runner, "docker_available", lambda: False)
    with pytest.raises(errors.ReproError) as ei:
        lc.create_repro(lc.CreateReq(version="8.6.1", preset="default", name="dd"))
    assert not isinstance(ei.value, errors.AuthorityGateError)


def test_compose_only_flags_are_refused_on_kubernetes(tmp_path, monkeypatch):
    """#15: --fresh/--force/--monitor were accepted and silently ignored."""
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize", "owned-cluster"])
    for flag in ("fresh", "force", "monitor"):
        req = lc.CreateReq(version="8.6.1", preset="microservices", name="ff", **{flag: True})
        with pytest.raises(errors.ValidationError) as ei:
            lc.create_repro(req)
        assert f"--{flag}" in str(ei.value)
        assert "not supported on the Kubernetes topology" in str(ei.value)


def test_licence_warning_fires_for_an_unlicensed_ee_preset(tmp_path, monkeypatch):
    """#13: the create-time warning was never emitted."""
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    seen = []
    assert lc.warn_if_unlicensed(
        lc.CreateReq(version="8.6.1", preset="microservices"), seen.append) is True
    assert seen[0].level == "warn" and seen[0].data["code"] == "LICENSE_ABSENT_EE_PRESET"
    # silent with a token, and silent for a non-EE preset
    assert lc.warn_if_unlicensed(
        lc.CreateReq(version="8.6.1", preset="microservices", reg_token="t")) is False
    assert lc.warn_if_unlicensed(lc.CreateReq(version="8.6.1", preset="default")) is False


def test_doctor_json_is_an_envelope_and_exits_3_when_not_ready(tmp_path, monkeypatch):
    """#6: doctor had no --json. It is the agent's preflight call, so it returns the
    envelope with per-check ids and exits 3 (preflight) on any fail."""
    import json as _j
    from typer.testing import CliRunner
    from rc_repro import cli
    from rc_repro.cli import app
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.runner, "docker_available", lambda: False)
    res = CliRunner().invoke(app, ["doctor", "--json"])
    payload = _j.loads(res.stdout)
    assert payload["schema"] == "rc-repro.doctor.v1"
    assert any(c["check"] == "docker-daemon" and c["status"] == "fail"
               for c in payload["data"]["checks"])
    assert payload["data"]["ready"] is False
    assert res.exit_code == 3            # preflight, so an agent stops before `up`


@pytest.mark.parametrize(("version", "status", "message"), [
    ("2.40.3", "ok", "docker compose v2 (2.40.3)"),
    ("v5.3.0", "ok", "docker compose v5 (v5.3.0)"),
    ("1.29.2", "warn", "rc-repro expects Compose v2 or v5"),
])
def test_doctor_accepts_supported_compose_cli_majors(
        tmp_path, monkeypatch, version, status, message):
    import json as _j
    from typer.testing import CliRunner
    from rc_repro import cli
    from rc_repro.cli import app
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.runner, "docker_available", lambda: False)
    monkeypatch.setattr(cli.runner, "compose_version", lambda: version)

    res = CliRunner().invoke(app, ["doctor", "--json"])
    payload = _j.loads(res.stdout)
    compose_check = next(c for c in payload["data"]["checks"]
                         if c["check"] == "compose-version")
    assert compose_check["status"] == status
    assert message in compose_check["message"]


def test_doctor_reports_unwritable_k8s_client_state_without_a_traceback(tmp_path, monkeypatch):
    import json as _j
    from types import SimpleNamespace
    from typer.testing import CliRunner
    from rc_repro import cli
    from rc_repro.cli import app
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(cli.runner, "docker_available", lambda: False)
    monkeypatch.setattr(cli.requests, "get", lambda *a, **k: SimpleNamespace(status_code=200))
    monkeypatch.setattr(k8s, "engine_capacity", lambda: (8.0, 4))
    monkeypatch.setattr(
        k8s, "prepare_client_state",
        lambda: (_ for _ in ()).throw(PermissionError("repositories.lock is read-only")))

    res = CliRunner().invoke(app, ["doctor", "--json"])
    payload = _j.loads(res.stdout)
    state_check = next(c for c in payload["data"]["checks"]
                       if c["check"] == "k8s-client-state")
    assert state_check["status"] == "fail"
    assert "repositories.lock is read-only" in state_check["message"]
    assert payload["data"]["ready"] is False and res.exit_code == 3


# --- process/concurrency bugs found by charting --------------------------------


def test_forward_pid_identity_guards_against_reuse(tmp_path, monkeypatch):
    """#19: a recycled pid must not be trusted or killed. os.kill(pid,0) alone
    would report a stranger as our forward; the cmdline identity check prevents it."""
    from rc_repro.services import k8s
    # a live pid whose command line is NOT a kubectl port-forward: not ours
    monkeypatch.setattr(k8s.os, "kill", lambda pid, sig: None)   # "alive"
    monkeypatch.setattr(k8s, "_cmdline_is_kubectl_forward", lambda pid: False)
    assert k8s._pid_alive(4242) is False
    # a live pid that IS our forward: ours
    monkeypatch.setattr(k8s, "_cmdline_is_kubectl_forward", lambda pid: True)
    assert k8s._pid_alive(4242) is True
    # a dead pid: not ours, and identity is never consulted
    def boom(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(k8s.os, "kill", boom)
    assert k8s._pid_alive(4242) is False


def test_cmdline_identity_reads_proc_then_falls_back(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    # a fake /proc cmdline for a kubectl port-forward
    argv = "kubectl\x00--context\x00kind-x\x00-n\x00ns\x00port-forward\x00svc/rc\x00"
    import io
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.BytesIO(argv.encode()))
    assert k8s._cmdline_is_kubectl_forward(1) is True
    # an unrelated process
    argv2 = "sshd\x00-D\x00"
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.BytesIO(argv2.encode()))
    assert k8s._cmdline_is_kubectl_forward(1) is False


def test_ensure_cluster_tolerates_a_lost_creation_race(tmp_path, monkeypatch):
    """#20: two concurrent creates must not crash. The loser sees 'already exist'
    and that is success, not a raise."""
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class RaceLoser(_FakeRun):
        def __init__(self):
            super().__init__(clusters="")     # we see no cluster...
        def run(self, argv, *, check=True):
            import subprocess
            if argv[:3] == ["kind", "create", "cluster"]:
                # ...but another process created it first
                return subprocess.CompletedProcess(argv, 1, "",
                    'ERROR: node(s) already exist for a cluster with the name "rc-repro-local"')
            return super().run(argv, check=check)

    ctx = k8s.ensure_cluster(run=RaceLoser())    # must not raise
    assert ctx == "kind-rc-repro-local"


def test_ensure_cluster_still_raises_on_a_real_creation_failure(tmp_path, monkeypatch):
    from rc_repro import errors
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class RealFail(_FakeRun):
        def __init__(self):
            super().__init__(clusters="")
        def run(self, argv, *, check=True):
            import subprocess
            if argv[:3] == ["kind", "create", "cluster"]:
                noise = "creating node\n" * 300
                return subprocess.CompletedProcess(
                    argv, 1, noise,
                    "permission denied while writing the rc-repro kubeconfig")
            return super().run(argv, check=check)

    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.ensure_cluster(run=RealFail())
    assert "permission denied while writing the rc-repro kubeconfig" in str(ei.value)


def test_ensure_cluster_rejects_an_unusable_exported_kubeconfig(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class BadExport(_FakeRun):
        def __init__(self):
            super().__init__(clusters="rc-repro-local")

        def run(self, argv, *, check=True):
            import subprocess
            if "config" in argv and "current-context" in argv:
                self.calls.append(argv)
                return subprocess.CompletedProcess(
                    argv, 1, "", "could not parse the owned kubeconfig")
            return super().run(argv, check=check)

    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.ensure_cluster(run=BadExport())
    assert "could not parse the owned kubeconfig" in str(ei.value)


def test_ensure_cluster_reconciles_a_nonzero_create_that_becomes_ready(
        tmp_path, monkeypatch):
    """kind may create a healthy node, then fail during host-side kubeconfig export."""
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class ConvergedAfterError(_FakeRun):
        def run(self, argv, *, check=True):
            import subprocess
            if argv[:3] == ["kind", "create", "cluster"]:
                self.calls.append(argv)
                self.clusters = "rc-repro-local"
                return subprocess.CompletedProcess(
                    argv, 1, "Creating cluster...", "late kubeconfig export failure")
            return super().run(argv, check=check)

    events_seen = []
    ctx = k8s.ensure_cluster(emit=events_seen.append, run=ConvergedAfterError())
    assert ctx == "kind-rc-repro-local"
    assert any("recovered" in event.message for event in events_seen)


def test_ensure_cluster_refuses_to_reuse_an_unready_node(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    class Unready(_FakeRun):
        def __init__(self):
            super().__init__(clusters="rc-repro-local")

        def run(self, argv, *, check=True):
            import subprocess
            if "wait" in argv and "condition=Ready" in " ".join(argv):
                self.calls.append(argv)
                return subprocess.CompletedProcess(
                    argv, 1, "", "timed out waiting for node readiness")
            return super().run(argv, check=check)

    with pytest.raises(errors.CreateFailedError) as ei:
        k8s.ensure_cluster(run=Unready())
    assert "existing cluster rc-repro-local is not usable" in str(ei.value)
    assert "timed out waiting for node readiness" in str(ei.value)


# --- #21/#22/#23: reclaim, collision, and the last two guards -------------------


def test_second_up_on_an_existing_repro_is_refused_clearly(tmp_path, monkeypatch):
    """#22: a repeat would fail deep inside helm with a raw error; refuse early."""
    from rc_repro import errors
    from rc_repro.services import k8s
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("dup", "8.6.1", offline=True, port=34200, run=_FakeRun())
    with pytest.raises(errors.ConflictError) as ei:
        k8s.create_repro("dup", "8.6.1", offline=True, port=34201, run=_FakeRun())
    assert "already exists" in str(ei.value)
    assert "rc-repro down" in str(ei.value)          # names how to proceed


def test_prune_dispatches_kubernetes_teardown_not_compose(tmp_path, monkeypatch):
    """#21: prune called runner.down (compose) on a k8s repro, leaking the forward
    and namespace. It now dispatches to k8s.teardown."""
    from rc_repro import runner
    from rc_repro.services import k8s, onboarding
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["owned-cluster"])
    k8s.create_repro("pr", "8.6.1", offline=True, port=34202, run=_FakeRun())
    monkeypatch.setattr(lc, "prunable", lambda: ["pr"])
    torn = {}
    monkeypatch.setattr(k8s, "teardown",
                        lambda name, volumes=False, emit=None: torn.setdefault("name", name)
                        or {"removed": [f"namespace/rc-repro-{name}"], "residual": []})
    monkeypatch.setattr(k8s, "cluster_prune_status", lambda: {
        "cluster": k8s.CLUSTER_NAME, "exists": True, "prunable": False,
        "namespaces": ["rc-repro-pr"], "reason": "repros still present"})
    monkeypatch.setattr(k8s, "prune_cluster", lambda emit=None: {
        "cluster": k8s.CLUSTER_NAME, "exists": False, "prunable": False,
        "namespaces": [], "reason": "deleted", "deleted": True})
    # runner.down must NOT be called for a k8s repro
    monkeypatch.setattr(runner, "down", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("runner.down called on a Kubernetes repro")))
    out = lc.prune(confirm=True)
    assert torn["name"] == "pr" and out["removed"] == ["pr"]
    assert out["cluster"]["deleted"] is True


def test_prune_reaches_an_empty_cluster_after_all_records_are_gone(tmp_path, monkeypatch):
    from rc_repro.services import k8s, onboarding
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["owned-cluster"])

    state = {"cluster": k8s.CLUSTER_NAME, "exists": True, "prunable": True,
             "namespaces": [], "reason": "empty rc-repro-owned cluster"}
    monkeypatch.setattr(lc, "prunable", lambda: [])
    monkeypatch.setattr(k8s, "cluster_prune_status", lambda: state)
    deleted = {**state, "exists": False, "prunable": False,
               "reason": "deleted", "deleted": True}
    monkeypatch.setattr(k8s, "prune_cluster", lambda emit=None: deleted)

    with pytest.raises(errors.ValidationError):
        lc.prune(confirm=False)
    out = lc.prune(confirm=True)
    assert out["targets"] == [] and out["removed"] == []
    assert out["cluster"]["deleted"] is True


def test_prune_cli_reports_empty_cluster_cleanup(monkeypatch):
    from typer.testing import CliRunner
    from rc_repro import cli
    from rc_repro.services import k8s

    state = {"cluster": k8s.CLUSTER_NAME, "exists": True, "prunable": True,
             "namespaces": [], "reason": "empty rc-repro-owned cluster"}
    monkeypatch.setattr(cli.lcsvc, "prune_plan", lambda: {
        "targets": [], "cluster": state})
    monkeypatch.setattr(cli.lcsvc, "prune", lambda confirm=False, emit=None: {
        "targets": [], "removed": [], "cluster": {
            **state, "exists": False, "prunable": False,
            "reason": "deleted", "deleted": True}})

    res = CliRunner().invoke(cli.app, ["prune", "--yes"])
    assert res.exit_code == 0
    assert "deleted empty Kind cluster 'rc-repro-local'" in res.stdout


def test_stale_forwards_reports_a_dead_tunnel_not_a_stranger(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("sf", "8.6.1", offline=True, port=34203, run=_FakeRun())
    # the fake forward pid is not alive-and-ours, so forward_state is "down"
    rows = lc.stale_forwards()
    assert [r["name"] for r in rows] == ["sf"]


def test_monitor_command_is_refused_on_kubernetes(tmp_path, monkeypatch):
    """#23."""
    from rc_repro import errors
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("mon", "8.6.1", offline=True, port=34204, run=_FakeRun())
    with pytest.raises(errors.ValidationError):
        lc.require_compose_topology("mon", "monitor", "compose-only.")


def test_offline_is_refused_on_the_kubernetes_path(tmp_path, monkeypatch):
    """#23: --offline promises no network but the k8s path must pull chart + images."""
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize", "owned-cluster"])
    with pytest.raises(errors.ValidationError) as ei:
        lc.create_repro(lc.CreateReq(version="8.6.1", preset="microservices",
                                     name="off", offline=True))
    assert "offline cannot work on the Kubernetes topology" in str(ei.value)


def test_wait_and_finalize_dispatches_fully_to_k8s_wait(tmp_path, monkeypatch):
    """Final-sweep regression: wait_and_finalize revived the forward but then called
    the compose-shaped wait_serving, whose is_alive reads compose state and which has
    no terminal-pod detection. The non-json `ready` and the GUI use this, so they must
    get k8s.wait_ready, not a compose wait."""
    from rc_repro import runner
    from rc_repro.services import k8s
    from rc_repro.services import lifecycle as lc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    k8s.create_repro("wf", "8.6.1", offline=True, port=34300, run=_FakeRun())
    called = {}

    def fake_wait(name, timeout=600.0, emit=None):
        called["name"] = name
        return {"booted_s": 9, "version": "8.6.1"}

    monkeypatch.setattr(k8s, "wait_ready", fake_wait)
    # wait_serving must NOT be used for a k8s repro
    monkeypatch.setattr(lc, "wait_serving", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("wait_serving called on a Kubernetes repro")))
    out = lc.wait_and_finalize(runner.read_meta("wf"))
    assert called["name"] == "wf"
    assert out == {"booted_s": 9, "running_version": "8.6.1"}
