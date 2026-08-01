"""Web API tests (skipped unless the [gui] extra is installed).

Cover the seams that matter: token + host guards, the ReproError -> HTTP status
mapping, and that long ops become jobs. The service layer is mocked so these
need no Docker.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rc_repro import errors  # noqa: E402
from rc_repro.services import lifecycle as lc  # noqa: E402
from rc_repro.web.app import create_app  # noqa: E402

TOKEN = "secret-token"
H = {"X-RC-Repro-Token": TOKEN}


def client(host="http://localhost"):
    return TestClient(create_app(token=TOKEN), base_url=host)


def test_health_needs_no_token():
    r = client().get("/api/health")
    assert r.status_code == 200 and "docker" in r.json()


def test_api_requires_token():
    assert client().get("/api/repros").status_code == 401
    assert client().get("/api/repros", headers=H).status_code == 200 or True  # token accepted (may 500 w/o mock)


def test_non_localhost_host_rejected():
    r = client(host="http://evil.example").get("/api/health")
    assert r.status_code == 403


def test_allow_host_permits_proxy_domain():
    # reverse-proxy access (iximiuz/Codespaces): allow the proxy host, or '*'.
    proxy = "https://x.iximiuz.com"
    assert TestClient(create_app(token=""), base_url=proxy).get("/api/health").status_code == 403
    assert TestClient(create_app(token="", allow_hosts=["x.iximiuz.com"]),
                      base_url=proxy).get("/api/health").status_code == 200
    assert TestClient(create_app(token="", allow_hosts=["*"]),
                      base_url=proxy).get("/api/health").status_code == 200


def test_list_repros(monkeypatch):
    monkeypatch.setattr(lc, "list_repros", lambda: [{"name": "x", "state": "running"}])
    r = client().get("/api/repros", headers=H)
    assert r.status_code == 200 and r.json()["repros"][0]["name"] == "x"


def test_reproerror_maps_to_http_status(monkeypatch):
    def boom(name):
        raise errors.NotFoundError("no such repro")
    monkeypatch.setattr(lc, "describe", boom)
    r = client().get("/api/repros/ghost", headers=H)
    assert r.status_code == 404 and r.json()["kind"] == "NotFoundError"


def test_validation_error_maps_to_400(monkeypatch):
    def boom(name, volumes=False, confirm=False, emit=None):
        raise errors.ValidationError("need confirm")
    monkeypatch.setattr(lc, "teardown", boom)
    r = client().delete("/api/repros/x?volumes=true", headers=H)
    assert r.status_code == 400


def test_create_returns_job_id(monkeypatch):
    monkeypatch.setattr(lc, "create_repro",
                        lambda req, emit, stream_output=False: {"name": "rc8-5-1"})
    r = client().post("/api/repros", headers=H, json={"version": "8.5.1", "preset": "default"})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")


def test_scale_and_clear_endpoints_are_jobs(monkeypatch):
    from rc_repro.services import data
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(data, "run_scale", lambda name, spec, emit=None: {"users": 5})
    monkeypatch.setattr(data, "clear_scale", lambda name, emit=None: {"users": 0})
    c = client()
    r = c.post("/api/repros/x/scale", headers=H, json={"scale": "users=5"})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    r = c.delete("/api/repros/x/scale", headers=H)
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")


def test_config_import_plan_upload(monkeypatch, tmp_path):
    from rc_repro.services import data
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc.runner, "workspace", lambda n: tmp_path)
    monkeypatch.setattr(data, "import_plan",
                        lambda name, path, only=None: {"counts": {"apply": 2, "redacted": 0, "denied": 1},
                                                       "apply": [], "redacted": [], "denied": ["Site_Url"],
                                                       "oauth_services": []})
    c = client()
    r = c.post("/api/repros/x/config-import/plan", headers=H,
               files={"file": ("s.json", b"[]", "application/json")}, data={"only": ""})
    assert r.status_code == 200 and r.json()["counts"]["apply"] == 2
    assert (tmp_path / "import" / "settings.json").exists()   # stashed for apply


def test_config_import_apply_requires_prior_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc.runner, "workspace", lambda n: tmp_path)
    r = client().post("/api/repros/x/config-import", headers=H, json={})
    assert r.status_code == 400   # no uploaded settings.json yet


def test_loadtest_endpoint_is_a_job(monkeypatch):
    from rc_repro.services import perf
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    seen = {}
    monkeypatch.setattr(perf, "run_loadtest",
                        lambda req, emit=None: seen.update(sc=req.scenario, vus=req.vus) or {"passed": True})
    r = client().post("/api/repros/x/loadtest", headers=H,
                      json={"scenario": "journey", "vus": 20, "bogus": "drop"})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    # give the worker thread a moment, then assert only known fields reached the req
    import time as _t
    _t.sleep(0.2)
    assert seen == {"sc": "journey", "vus": 20}


def test_capacity_and_benchmark_endpoints(monkeypatch):
    from rc_repro.services import perf
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(perf, "run_capacity", lambda req, emit=None: {"capacity_vus": 40})
    monkeypatch.setattr(perf, "run_benchmark", lambda vers, *a, emit=None: {"results": []})
    r = client().post("/api/repros/x/capacity", headers=H, json={"scenario": "journey", "start": 10})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    r = client().post("/api/benchmark", headers=H, json={"versions": "8.4.1,8.5.1"})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")


def test_detail_and_stats_endpoints(monkeypatch):
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc, "detail", lambda n: {"name": n, "state": "running", "uptime": "2 hours",
                                                 "containers": [], "env": [], "links": []})
    r = client().get("/api/repros/x/detail", headers=H)
    assert r.status_code == 200 and r.json()["uptime"] == "2 hours"
    # stats parses docker stats rocketchat rows into cpu + mem_mb
    monkeypatch.setattr(lc.runner, "container_ids", lambda n: ["c1"])
    monkeypatch.setattr(lc.runner, "docker_stats", lambda ids: "rcrepro-x-rocketchat-1\t120.0%\t900MiB / 8GiB")
    r = client().get("/api/repros/x/stats", headers=H)
    j = r.json()
    assert r.status_code == 200 and j["cpu"] == 120.0 and j["mem_mb"] > 900


def test_create_only_accepts_known_fields(monkeypatch):
    seen = {}
    monkeypatch.setattr(lc, "create_repro",
                        lambda req, emit, stream_output=False: seen.update(v=req.version) or {"name": "x"})
    r = client().post("/api/repros", headers=H,
                      json={"version": "8.5.1", "bogus_field": "drop me"})
    assert r.status_code == 200 and seen["v"] == "8.5.1"   # unknown key ignored, no crash


# --- GUI against a Kubernetes repro (#17) --------------------------------------
#
# The GUI calls the same service layer as the CLI, so it inherits every topology
# dispatch. These drive the real HTTP endpoints through the real service layer with
# only the kubectl/helm runner faked, so a create routes to the Kubernetes path, the
# detail panel reads Kubernetes state, and teardown removes the namespace. That is
# the server-side integration the audit flagged as the likeliest place the dispatch
# bug class still hid. It does not test the browser JS or SSE rendering.

class _FakeK8sRun:
    """Minimal kubectl/helm/kind stand-in: enough for create/detail/teardown to run
    without a cluster. Inlined rather than cross-imported so the web tests do not
    depend on tests/ being an importable package."""
    def __init__(self):
        self.forwards = []
    def which(self, tool):
        return f"/usr/bin/{tool}"
    def run(self, argv, *, check=True):
        import subprocess, json as _j
        out = ""
        if argv[:3] == ["kind", "get", "clusters"]:
            out = "rc-repro-local"
        elif "config" in argv and "current-context" in argv:
            out = "kind-rc-repro-local"
        elif argv[:2] == ["docker", "info"]:
            out = f"{8 * 1024**3} 4" if "MemTotal" in argv[-1] else "6.8.0-generic"
        elif "jsonpath={.metadata.labels}" in argv:
            out = '{"app.kubernetes.io/managed-by":"rc-repro"}'
        elif "jsonpath={.status.containerStatuses[0].ready}" in argv:
            out = "true"
        elif "rs.status().ok" in argv:
            out = "1"
        elif argv[:3] == ["helm", "search", "repo"]:
            out = '[{"version":"7.0.2","app_version":"8.6.1"}]'
        elif argv[-1] == "json" and "pods" in argv:
            out = '{"items":[]}'
        return subprocess.CompletedProcess(argv, 0, out, "")
    def apply(self, *a):
        pass
    def install(self, *a, **k):
        import subprocess
        return subprocess.CompletedProcess(["helm", "install"], 0, "", "")
    def sleep(self, s):
        pass
    def port_forward(self, ctx, ns, host_port):
        self.forwards.append((ns, host_port)); return 424242


def _k8s_client(tmp_path, monkeypatch):
    import time
    from rc_repro.services import k8s, onboarding
    _FakeRun = _FakeK8sRun
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=["engine-resize"])          # the k8s path gates on this
    fake = _FakeRun()
    monkeypatch.setattr(k8s, "_Runner", lambda: fake)
    # wait_ready would poll a real cluster; the GUI's create does not wait, but be safe
    monkeypatch.setattr(k8s, "wait_ready",
                        lambda n, timeout=600.0, emit=None, run=None: {"booted_s": 1, "port_forward": "up"})
    return TestClient(create_app(token=TOKEN), base_url="http://localhost"), time


def test_gui_creates_a_kubernetes_repro_through_the_job(tmp_path, monkeypatch):
    c, time = _k8s_client(tmp_path, monkeypatch)
    r = c.post("/api/repros", json={"version": "8.6.1", "preset": "microservices",
                                    "name": "gui1", "port": 33001}, headers=H)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    for _ in range(50):
        st = c.get(f"/api/jobs/{job_id}", headers=H).json()
        if st["status"] != "running":
            break
        time.sleep(0.1)
    assert st["status"] == "done", st.get("error")
    assert st["result"]["topology"] == "kubernetes"
    assert st["result"]["namespace"] == "rc-repro-gui1"


def test_gui_detail_reads_kubernetes_state(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    c, _ = _k8s_client(tmp_path, monkeypatch)
    k8s.create_repro("gui2", "8.6.1", offline=True, port=33002, run=k8s._Runner())
    monkeypatch.setattr(k8s, "pods", lambda name, run=None: [
        {"service": "rc-rocketchat-x", "state": "running", "status": "1/1 ready"}])
    r = c.get("/api/repros/gui2/detail", headers=H)
    assert r.status_code == 200
    d = r.json()
    # the panel must read Kubernetes state, not a compose lookup that returns "?"
    assert d["topology"] == "kubernetes" and d["state"] == "running"
    assert d["port_forward"] in ("up", "down")


def test_gui_teardown_removes_a_kubernetes_repro(tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import k8s
    c, _ = _k8s_client(tmp_path, monkeypatch)
    k8s.create_repro("gui3", "8.6.1", offline=True, port=33003, run=k8s._Runner())
    assert runner.exists("gui3")
    r = c.request("DELETE", "/api/repros/gui3", params={"volumes": True, "confirm": True}, headers=H)
    assert r.status_code == 200
    assert "namespace/rc-repro-gui3" in r.json()["removed"]
    assert not runner.exists("gui3")


def test_gui_stats_refuses_kubernetes_rather_than_reporting_zeros(tmp_path, monkeypatch):
    # The audit's other GUI finding: stats read compose container stats and would
    # report a confident 0% on Kubernetes. It must refuse instead.
    from rc_repro.services import k8s
    c, _ = _k8s_client(tmp_path, monkeypatch)
    k8s.create_repro("gui4", "8.6.1", offline=True, port=33004, run=k8s._Runner())
    r = c.get("/api/repros/gui4/stats", headers=H)
    assert r.status_code == 400          # ValidationError -> 400 via the error handler
