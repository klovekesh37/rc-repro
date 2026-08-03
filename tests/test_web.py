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


def wait_job(c, job_id: str):
    import time
    for _ in range(100):
        state = c.get(f"/api/jobs/{job_id}", headers=H).json()
        if state["status"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {state}")


def test_health_needs_no_token():
    r = client().get("/api/health")
    assert r.status_code == 200 and "docker" in r.json()


def test_api_requires_token(monkeypatch):
    assert client().get("/api/repros").status_code == 401
    # Mock the service so the authorized half is a real assertion — it used to be
    # `== 200 or True`, which passes unconditionally.
    monkeypatch.setattr(lc, "list_repros", lambda: [])
    assert client().get("/api/repros", headers=H).status_code == 200


def test_non_localhost_host_rejected():
    r = client(host="http://evil.example").get("/api/health")
    assert r.status_code == 403


def test_host_guard_is_case_insensitive():
    """Hostnames are case-insensitive; comparing them raw is a fail-CLOSED bug.

    `curl http://LOCALHOST:7070/` and a proxy forwarding `Host: Lab.Example.Com`
    both got "host not allowed", which reads like the guard is broken rather
    than like a spelling difference.
    """
    app = create_app(token="", allow_hosts=["Lab.Example.Com"])
    c = TestClient(app, base_url="http://localhost")
    for hdr in ("LOCALHOST", "LocalHost", "localhost",
                "lab.example.com", "LAB.EXAMPLE.COM", "Lab.Example.Com:443"):
        assert c.get("/api/health", headers={"host": hdr}).status_code == 200, hdr
    # Case folding must not widen the allow-list to unrelated hosts.
    for hdr in ("evil.example", "lab.example.com.evil", "notlocalhost"):
        assert c.get("/api/health", headers={"host": hdr}).status_code == 403, hdr


def test_allow_host_permits_proxy_domain():
    # reverse-proxy access (iximiuz/Codespaces): allow the proxy host, or '*'.
    proxy = "https://x.iximiuz.com"
    assert TestClient(create_app(token=""), base_url=proxy).get("/api/health").status_code == 403
    assert TestClient(create_app(token="", allow_hosts=["x.iximiuz.com"]),
                      base_url=proxy).get("/api/health").status_code == 200
    assert TestClient(create_app(token="", allow_hosts=["*"]),
                      base_url=proxy).get("/api/health").status_code == 200


def test_missing_host_header_rejected():
    # "" used to be a member of the allow-list, so a Host-less request walked
    # straight past the DNS-rebind guard.
    assert client().get("/api/health", headers={"host": ""}).status_code == 403


def test_ipv6_loopback_host_allowed():
    # '[::1]:7070'.split(':')[0] is '[', so a naive port strip 403s the IPv6
    # loopback even though '::1' is in the allow-list. Set the header directly:
    # TestClient's transport can't parse a bracketed IPv6 base_url.
    for hdr in ("[::1]:7070", "[::1]"):
        assert client().get("/api/health", headers={"host": hdr}).status_code == 200


def test_openapi_schema_not_exposed():
    # The schema path does not start with /api/, so the token guard never covered
    # it — it must not be served at all.
    assert client().get("/openapi.json", headers=H).status_code == 404


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
    # One file per upload rather than a shared settings.json: two tabs previewing
    # different dumps used to race, and the second silently won the first's apply.
    upload_id = r.json()["upload_id"]
    assert (tmp_path / "import" / f"{upload_id}.json").exists()


def test_config_import_uploads_do_not_collide(monkeypatch, tmp_path):
    from rc_repro.services import data
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc.runner, "workspace", lambda n: tmp_path)
    seen = []
    monkeypatch.setattr(data, "import_plan", lambda name, path, only=None: seen.append(path) or {
        "counts": {"apply": 0, "redacted": 0, "denied": 0},
        "apply": [], "redacted": [], "denied": [], "oauth_services": []})
    c = client()
    ids = [c.post("/api/repros/x/config-import/plan", headers=H,
                  files={"file": ("s.json", b"[]", "application/json")},
                  data={"only": ""}).json()["upload_id"] for _ in range(2)]
    assert ids[0] != ids[1] and len(set(seen)) == 2


def test_config_import_apply_rejects_a_forged_upload_id(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc.runner, "workspace", lambda n: tmp_path)
    # The id becomes a filename, so it is pattern-checked before use.
    for bad in ("../../etc/passwd", "u../../x", "nope", "", "u0123456789ab/../x"):
        r = client().post("/api/repros/x/config-import", headers=H, json={"upload_id": bad})
        assert r.status_code == 400, bad


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
                        lambda req, emit, stream_output=False:
                        seen.update(version=req.version, preset=req.preset) or {"name": "x"})
    c = client()
    r = c.post("/api/repros", headers=H,
               json={"version": "8.5.1", "preset": None, "bogus_field": "drop me"})
    assert r.status_code == 200
    assert wait_job(c, r.json()["job_id"])["status"] == "done"
    assert seen == {"version": "8.5.1", "preset": ""}


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
    def docker_server_platform(self):
        return "Docker Engine - Community"
    def run(self, argv, *, check=True):
        import subprocess
        out = ""
        if argv[:3] == ["kind", "get", "clusters"]:
            out = "rc-repro-local"
        elif "config" in argv and "current-context" in argv:
            out = "kind-rc-repro-local"
        elif argv[:2] == ["docker", "info"]:
            out = f"{8 * 1024**3} 4" if "MemTotal" in argv[-1] else "6.8.0-generic"
        elif "configmap" in argv and "rc-repro-cluster-owner" in argv:
            out = ('{"metadata":{"labels":{"app.kubernetes.io/managed-by":"rc-repro"}},'
                   '"data":{"cluster":"rc-repro-local"}}')
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
    onboarding.complete(grants=["engine-resize", "owned-cluster"])
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


def test_gui_k8s_create_consumes_env_registration_token_without_exposure(
        tmp_path, monkeypatch):
    c, _ = _k8s_client(tmp_path, monkeypatch)
    secret = "GUI-SECRET-MUST-NOT-PRINT"
    monkeypatch.setenv("RC_REPRO_REG_TOKEN", secret)

    response = c.post("/api/repros", headers=H, json={
        "version": "8.6.1", "preset": "microservices",
        "name": "gui-token", "port": 33010,
    })
    state = wait_job(c, response.json()["job_id"])

    assert state["status"] == "done", state.get("error")
    assert state["result"]["reg_token_supplied"] is True
    assert secret not in str(state)


def test_gui_k8s_create_and_seed_preserves_profile_once(tmp_path, monkeypatch):
    c, _ = _k8s_client(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(lc, "wait_and_finalize",
                        lambda meta, emit=None: {"booted_s": 1, "running_version": "8.6.1"})
    monkeypatch.setattr(lc, "run_seed_inline",
                        lambda meta, profile, stats, emit: seen.append(
                            (profile, stats)) or {"users": 3})

    response = c.post("/api/repros", headers=H, json={
        "version": "8.6.1", "preset": "microservices", "name": "gui-seed",
        "port": 33011, "seed": True, "seed_profile": "large",
    })
    state = wait_job(c, response.json()["job_id"])

    assert state["status"] == "done", state.get("error")
    assert seen == [("large", False)]
    assert state["result"]["seed"] == {"users": 3}


def test_browser_create_form_sends_the_selected_seed_profile():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "rc_repro" / "data" / "webui"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    assert 'select name="seed_profile"' in html
    assert "seed_profile: f.seed_profile.value" in script


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


def test_gui_seed_jobs_refuse_every_compose_only_k8s_mode(tmp_path, monkeypatch):
    from rc_repro.services import k8s
    c, _ = _k8s_client(tmp_path, monkeypatch)
    k8s.create_repro("gui-guards", "8.6.1", offline=True, port=33012,
                     run=k8s._Runner())
    def unexpected_reconcile(name):
        raise AssertionError(f"validation must precede port-forward repair: {name}")

    monkeypatch.setattr(lc, "ensure_reachable", unexpected_reconcile)

    requests = [
        c.post("/api/repros/gui-guards/seed", headers=H, json={"stats": True}),
        c.post("/api/repros/gui-guards/scale", headers=H,
               json={"scale": "users=5"}),
        c.delete("/api/repros/gui-guards/scale", headers=H),
    ]
    states = [wait_job(c, response.json()["job_id"]) for response in requests]

    assert all(state["status"] == "error" for state in states)
    assert all(state["error_kind"] == "ValidationError" for state in states)
    assert all("kubernetes" in (state["error"] or "").lower() for state in states)


def test_security_headers_are_set():
    csp = client().get("/api/health").headers["content-security-policy"]
    directives = dict((d.strip().split(" ", 1) + [""])[:2]
                      for d in csp.split(";") if d.strip())
    # script-src 'self' with no 'unsafe-inline' means an injected inline handler
    # (`<img onerror=...>`) cannot run even if a renderer forgets to escape.
    assert directives["script-src"].strip() == "'self'"
    # style ATTRIBUTES are used throughout the UI, so styles do need it
    assert "'unsafe-inline'" in directives["style-src"]
    assert directives["object-src"].strip() == "'none'"
    r = client().get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    # the session token rides in ?t=, so no Referer may carry it off-origin
    assert r.headers["referrer-policy"] == "no-referrer"


def test_up_endpoint_recreates_a_downed_repro_from_stored_metadata(monkeypatch):
    import time as _t
    import types
    # `down` (keep data) removes the containers, so `docker compose start` can
    # never revive it -- /state is useless and the GUI card had no way back up.
    seen = {}
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc.runner, "read_meta",
                        lambda n: types.SimpleNamespace(rc_version="8.5.1", preset="ldap"))

    def fake_create(req, emit, stream_output=False):
        seen.update(version=req.version, preset=req.preset, name=req.name,
                    wait=req.wait, offline=req.offline)
        return {"name": req.name}

    monkeypatch.setattr(lc, "create_repro", fake_create)
    r = client().post("/api/repros/x/up", headers=H)
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    for _ in range(400):                     # poll rather than a fixed sleep
        if seen:
            break
        _t.sleep(0.01)
    # nothing is re-entered by the user: version and preset come from repro.json
    assert seen == {"version": "8.5.1", "preset": "ldap", "name": "x",
                    "wait": True, "offline": True}


def test_config_import_apply_uses_the_previewed_filter(monkeypatch, tmp_path):
    from rc_repro.services import data
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc.runner, "workspace", lambda n: tmp_path)
    seen = {}
    monkeypatch.setattr(data, "import_plan", lambda name, path, only=None: {
        "counts": {"apply": 0, "redacted": 0, "denied": 0},
        "apply": [], "redacted": [], "denied": [], "oauth_services": []})
    monkeypatch.setattr(data, "import_apply",
                        lambda name, path, only=None, emit=None: seen.setdefault("only", only) or {"applied": 0})
    c = client()
    r = c.post("/api/repros/x/config-import/plan", headers=H,
               files={"file": ("s.json", b"[]", "application/json")},
               data={"only": "Livechat,LDAP"})
    upload_id = r.json()["upload_id"]
    # The client asks for a DIFFERENT filter at apply time. It must be ignored: the
    # whole point of the preview step is that what was reviewed is what runs.
    c.post("/api/repros/x/config-import", headers=H,
           json={"upload_id": upload_id, "only": "Accounts"})
    for _ in range(400):
        if "only" in seen:
            break
        __import__("time").sleep(0.01)
    assert seen["only"] == {"Livechat", "LDAP"}


def test_config_import_prunes_stale_uploads(monkeypatch, tmp_path):
    from rc_repro.services import data
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc.runner, "workspace", lambda n: tmp_path)
    monkeypatch.setattr(data, "import_plan", lambda name, path, only=None: {
        "counts": {"apply": 0, "redacted": 0, "denied": 0},
        "apply": [], "redacted": [], "denied": [], "oauth_services": []})
    c = client()
    for _ in range(8):
        c.post("/api/repros/x/config-import/plan", headers=H,
               files={"file": ("s.json", b"[]", "application/json")}, data={"only": ""})
    # A previewed-but-never-applied dump has nothing to delete it, and these are
    # customers' config files -- they must not accumulate without bound.
    assert len(list((tmp_path / "import").glob("u*.json"))) == 5


def test_version_preview_endpoint(monkeypatch):
    monkeypatch.setattr(lc.runner, "docker_kernel_version", lambda: "6.1.167")
    r = client().get("/api/versions/8.5.1?offline=true", headers=H)
    assert r.status_code == 200
    b = r.json()
    # Lets the create dialog show the pairing before committing to a multi-GB pull.
    assert b["mongo_tag"] == "8.0" and b["mongo_flavor"] == "official"
    assert "warning" not in b                      # kernel 6.1 runs Mongo 8 fine
    assert client().get("/api/versions/nonsense", headers=H).status_code == 400


def test_version_preview_warns_about_the_mongo8_kernel_trap(monkeypatch):
    # SERVER-121912: mongod 8.0 hard-exits on kernel >= 6.19, and the failure reads
    # like a volume/permission problem. Surface it before the pull, not minutes in.
    monkeypatch.setattr(lc.runner, "docker_kernel_version", lambda: "6.19.7-200.fc43")
    b = client().get("/api/versions/8.5.1?offline=true", headers=H).json()
    assert "SERVER-121912" in b["warning"]
    # an older RC pairs with an older Mongo, so no warning
    older = client().get("/api/versions/7.10.13?offline=true", headers=H).json()
    assert "warning" not in older and older["mongo_tag"] == "7.0"


def test_monitor_endpoint_toggles(monkeypatch):
    from rc_repro.services import monitor as monitorsvc
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    seen = []
    monkeypatch.setattr(monitorsvc, "attach", lambda n, emit=None: seen.append(("attach", n)) or {})
    monkeypatch.setattr(monitorsvc, "detach", lambda n, emit=None: seen.append(("detach", n)) or {})
    c = client()
    for off, want in ((False, "attach"), (True, "detach")):
        r = c.post(f"/api/repros/x/monitor?off={'true' if off else 'false'}", headers=H)
        assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    for _ in range(400):
        if len(seen) == 2:
            break
        __import__("time").sleep(0.01)
    assert sorted(k for k, _ in seen) == ["attach", "detach"]


def test_jobs_list_carries_label_and_survives_the_dialog(monkeypatch):
    """The activity list is the only way back to a job whose dialog was closed."""
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    from rc_repro.services import data
    monkeypatch.setattr(data, "run_scale", lambda name, spec, emit=None: {"users": 5})
    c = client()
    job_id = c.post("/api/repros/rc8-5-1/scale", headers=H,
                    json={"scale": "users=5"}).json()["job_id"]

    r = c.get("/api/jobs", headers=H)
    assert r.status_code == 200
    rows = r.json()["jobs"]
    row = next(j for j in rows if j["id"] == job_id)
    # `kind` alone cannot tell two concurrent seeds apart -- the label names the target.
    assert row["kind"] == "scale" and row["label"] == "rc8-5-1"
    assert row["started_at"] > 0
    # The list must stay compact: a benchmark's `result` is a large nested document.
    assert "result" not in row and "events" not in row


def test_jobs_list_needs_a_token():
    assert client().get("/api/jobs").status_code == 401


def test_doctor_endpoint_returns_the_same_report_the_cli_renders(monkeypatch):
    from rc_repro.services import doctor as doctorsvc
    monkeypatch.setattr(doctorsvc, "run_checks", lambda: {
        "checks": [{"status": "fail", "message": "Docker daemon not running"}],
        "counts": {"ok": 0, "warn": 0, "fail": 1}, "verdict": "fail", "repros": None})
    r = client().get("/api/doctor", headers=H)
    assert r.status_code == 200
    body = r.json()
    # The dashboard needs the verdict to colour its banner, not just the rows.
    assert body["verdict"] == "fail"
    assert body["checks"][0]["status"] == "fail"


def test_doctor_endpoint_needs_a_token():
    assert client().get("/api/doctor").status_code == 401


def test_set_default_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    r = client().post("/api/repros/rc8-5-1/default", headers=H)
    assert r.status_code == 200 and r.json()["default"] == "rc8-5-1"
    from rc_repro import config
    assert config.load_config()["default_repro"] == "rc8-5-1"


def test_pat_endpoint_maps_a_failure_to_not_ready(monkeypatch):
    from rc_repro import rcapi, runner as runner_mod
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(runner_mod, "read_meta",
                        lambda n: type("M", (), {"name": n, "root_url": "http://x", "extra": {}})())
    monkeypatch.setattr(lc, "login", lambda m: type("A", (), {"user_id": "u1"})())
    monkeypatch.setattr(rcapi, "generate_pat", lambda *a, **k: "tok_abc")
    r = client().post("/api/repros/x/pat", headers=H, json={})
    assert r.status_code == 200
    assert r.json()["token"] == "tok_abc" and r.json()["user_id"] == "u1"

    # A workspace that is up but not finalized cannot mint one -> 409, not 500.
    monkeypatch.setattr(rcapi, "generate_pat", lambda *a, **k: None)
    assert client().post("/api/repros/x/pat", headers=H, json={}).status_code == 409


def _stub_call_target(monkeypatch):
    """Wire up /call's dependencies except rcapi.call, which each test sets."""
    from rc_repro import runner as runner_mod
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(runner_mod, "read_meta",
                        lambda n: type("M", (), {"name": n, "root_url": "http://x:3000",
                                                 "extra": {}})())
    monkeypatch.setattr(lc, "login", lambda m: type("A", (), {"user_id": "u1", "token": "t1"})())


def test_api_call_endpoint_relays_the_response(monkeypatch):
    from rc_repro import rcapi
    _stub_call_target(monkeypatch)
    seen = {}

    def fake_call(root_url, method, path, auth=None, data=None, extra_headers=None, **kw):
        seen.update(root_url=root_url, method=method, path=path, data=data,
                    extra=extra_headers, token=auth.token)
        return 200, '{"success":true}'

    monkeypatch.setattr(rcapi, "call", fake_call)
    r = client().post("/api/repros/x/call", headers=H,
                      json={"method": "post", "path": "/api/v1/users.update",
                            "data": '{"userId": "abc"}'})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 200 and body["text"] == '{"success":true}'
    assert body["tag"] == "admin" and body["url"] == "http://x:3000/api/v1/users.update"
    assert isinstance(body["elapsed_ms"], (int, float))
    # The method is upper-cased and the JSON body arrives parsed, not as a string.
    assert seen["method"] == "POST" and seen["data"] == {"userId": "abc"}
    assert seen["extra"] is None and seen["token"] == "t1"


def test_api_call_reports_a_rocketchat_error_as_a_result_not_a_failure(monkeypatch):
    """A 403 from Rocket.Chat is the answer the user asked for.

    Mapping it onto the endpoint's own status would have surfaced it as a red
    toast with no body -- exactly the detail needed to see WHY it was refused.
    """
    from rc_repro import rcapi
    _stub_call_target(monkeypatch)
    monkeypatch.setattr(rcapi, "call",
                        lambda *a, **k: (403, '{"error":"unauthorized"}'))
    r = client().post("/api/repros/x/call", headers=H, json={"method": "GET", "path": "/api/v1/me"})
    assert r.status_code == 200
    assert r.json()["status"] == 403 and "unauthorized" in r.json()["text"]


def test_api_call_uses_a_pat_and_the_2fa_header_when_asked(monkeypatch):
    from rc_repro import rcapi
    _stub_call_target(monkeypatch)
    monkeypatch.setattr(rcapi, "generate_pat", lambda *a, **k: "pat_xyz")
    seen = {}

    def fake_call(root_url, method, path, auth=None, data=None, extra_headers=None, **kw):
        seen.update(token=auth.token, user_id=auth.user_id, extra=extra_headers)
        return 200, "{}"

    monkeypatch.setattr(rcapi, "call", fake_call)
    r = client().post("/api/repros/x/call", headers=H,
                      json={"method": "GET", "path": "/api/v1/me", "pat": True, "two_fa": True})
    assert r.status_code == 200 and r.json()["tag"] == "PAT+2fa"
    # The PAT replaces the login token; the user id stays.
    assert seen["token"] == "pat_xyz" and seen["user_id"] == "u1"
    assert seen["extra"]["x-2fa-method"] == "password"


def test_api_call_rejects_bad_input_before_touching_the_workspace(monkeypatch):
    from rc_repro import rcapi
    _stub_call_target(monkeypatch)
    monkeypatch.setattr(rcapi, "call", lambda *a, **k: pytest.fail("should not have been called"))
    post = lambda body: client().post("/api/repros/x/call", headers=H, json=body)  # noqa: E731
    assert post({"method": "GET", "path": ""}).status_code == 400            # no path
    assert post({"method": "TRACE", "path": "/api/v1/me"}).status_code == 400  # not whitelisted
    bad_json = post({"method": "POST", "path": "/api/v1/me", "data": "{nope}"})
    assert bad_json.status_code == 400 and "valid JSON" in bad_json.json()["error"]


def test_api_call_cannot_be_pointed_at_another_host(monkeypatch):
    """`path` must stay a path on this repro, never a new destination.

    This endpoint takes a caller-supplied URL fragment and has the server fetch
    it, which is the shape of an SSRF. It is safe only because rcapi.call()
    joins `root_url + "/" + path.lstrip("/")` -- pin that, because a "cleaner"
    urljoin() here would silently start honouring an absolute URL.
    """
    from urllib.parse import urlparse

    from rc_repro import rcapi
    _stub_call_target(monkeypatch)
    seen = []
    monkeypatch.setattr(rcapi, "call",
                        lambda root, m, p, **k: (seen.append(root), (200, "{}"))[1])
    for hostile in ("http://evil.example/x", "//evil.example/x", "/../../evil"):
        r = client().post("/api/repros/x/call", headers=H,
                          json={"method": "GET", "path": hostile})
        assert r.status_code == 200
        # The host is what matters, not the spelling: "http://evil.example/x"
        # survives as a path *segment* ("http://x:3000/http://evil.example/x"),
        # which is fine. It reaching evil.example is not.
        assert urlparse(r.json()["url"]).netloc == "x:3000", hostile
    assert seen == ["http://x:3000"] * 3


def test_api_call_maps_a_dead_workspace_to_not_ready(monkeypatch):
    import requests

    from rc_repro import rcapi
    _stub_call_target(monkeypatch)

    def boom(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(rcapi, "call", boom)
    r = client().post("/api/repros/x/call", headers=H, json={"method": "GET", "path": "/api/v1/me"})
    assert r.status_code == 409 and "connection refused" in r.json()["error"]


def test_api_call_needs_a_token():
    assert client().post("/api/repros/x/call", json={"method": "GET", "path": "/"}).status_code == 401


def test_tls_endpoint_probes_this_host_not_the_public_name(monkeypatch):
    """The GUI check must not let a proxy in front answer for us.

    Probing the public hostname reported Cloudflare's valid edge certificate as the
    repro's own while its Traefik had none. It dials 127.0.0.1 with the domain as
    SNI, and reports the public name separately.
    """
    from rc_repro import runner as runner_mod
    from rc_repro import tls as tlsmod
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    meta = runner_mod.Metadata(
        name="t", project="p", rc_version="8.4.2", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map",
        public_url="https://rc1.example.com",
        extra={"tls": "acme", "tls_ports": [443]})
    monkeypatch.setattr(runner_mod, "read_meta", lambda n: meta)

    seen = []

    def fake_verify(host, port=443, timeout=10.0, cafile=None, sni=None):
        seen.append((host, port, sni))
        if host == "127.0.0.1":
            return {"serving": True, "issuer": "CN = YR2", "subject": "CN = rc1.example.com",
                    "dates": "notAfter=Nov  1 2026", "fallback": False,
                    "trusted": True, "trusted_via_ca": True, "error": ""}
        return {"serving": True, "issuer": "CN = Cloudflare Inc ECC CA-3", "subject": "",
                "dates": "", "fallback": False, "trusted": True,
                "trusted_via_ca": True, "error": ""}

    monkeypatch.setattr(tlsmod, "verify", fake_verify)
    r = client().get("/api/repros/t/tls", headers=H)
    assert r.status_code == 200
    b = r.json()
    assert b["issuer"] == "CN = YR2", "must report what THIS host serves"
    assert b["public_issuer"] == "CN = Cloudflare Inc ECC CA-3"
    assert b["mode"] == "acme" and b["public_url"] == "https://rc1.example.com"
    # The local probe carries the domain as SNI; the public probe uses the name.
    assert ("127.0.0.1", 443, "rc1.example.com") in seen
    assert ("rc1.example.com", 443, None) in seen


def test_tls_endpoint_refuses_a_repro_without_https(monkeypatch):
    from rc_repro import runner as runner_mod
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(runner_mod, "read_meta", lambda n: runner_mod.Metadata(
        name="t", project="p", rc_version="8.4.2", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map"))
    r = client().get("/api/repros/t/tls", headers=H)
    assert r.status_code == 400 and "not created with --https" in r.json()["error"]


def test_tls_endpoint_needs_a_token():
    assert client().get("/api/repros/t/tls").status_code == 401


def test_env_endpoints(monkeypatch):
    """GET reports the effective env; POST is a job because the recreate is slow."""
    from rc_repro.services import envvars as envsvc
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(envsvc, "current", lambda n: {
        "name": n, "overrides": ["MY"],
        "env": [{"key": "MY", "value": "v", "override": True},
                {"key": "ADMIN_PASS", "value": "********", "override": False}]})
    r = client().get("/api/repros/e/env", headers=H)
    assert r.status_code == 200
    assert r.json()["overrides"] == ["MY"]
    assert [e["key"] for e in r.json()["env"]] == ["MY", "ADMIN_PASS"]

    seen = {}
    monkeypatch.setattr(envsvc, "set_env",
                        lambda name, sets, unset, restart=True, emit=None:
                        seen.update(name=name, sets=sets, unset=unset, restart=restart)
                        or {"name": name, "restarted": restart, "overrides": []})
    r = client().post("/api/repros/e/env", headers=H,
                      json={"set": {"A": "1"}, "unset": ["B"]})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    assert seen["sets"] == {"A": "1"} and seen["unset"] == ["B"] and seen["restart"] is True


def test_env_post_rejects_the_wrong_shapes(monkeypatch):
    """`set` must be an object and `unset` a list — a string for either would reach
    the service layer and produce something incoherent."""
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    for body, msg in [({"set": ["A=1"]}, "must be an object"),
                      ({"unset": "B"}, "must be a list")]:
        r = client().post("/api/repros/e/env", headers=H, json=body)
        assert r.status_code == 400 and msg in r.json()["error"], body


def test_env_endpoints_need_a_token():
    assert client().get("/api/repros/e/env").status_code == 401
    assert client().post("/api/repros/e/env", json={"set": {}}).status_code == 401


def test_env_post_keeps_settings_and_plain_vars_apart(monkeypatch):
    """`setting` is prefixed by the SERVER, not the caller.

    The two kinds are not interchangeable — a Rocket.Chat setting only works with
    OVERWRITE_SETTING_, a plain env var only works without it — so the rule lives in
    one place and every front-end gets it.
    """
    from rc_repro.services import envvars as envsvc
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    seen = {}
    monkeypatch.setattr(envsvc, "set_env",
                        lambda name, sets, unset, restart=True, emit=None:
                        seen.update(sets=sets, unset=unset)
                        or {"name": name, "restarted": True, "overrides": []})

    r = client().post("/api/repros/e/env", headers=H, json={
        "set": {"MY_PLAIN": "raw"},
        "setting": {"Message_AllowStarring": "false"},
        "unset": ["OLD"]})
    assert r.status_code == 200
    assert seen["sets"] == {"MY_PLAIN": "raw",
                            "OVERWRITE_SETTING_Message_AllowStarring": "false"}
    assert seen["unset"] == ["OLD"]

    # An already-prefixed setting id is not double-prefixed.
    client().post("/api/repros/e/env", headers=H,
                  json={"setting": {"OVERWRITE_SETTING_Foo_Bar": "1"}})
    assert seen["sets"] == {"OVERWRITE_SETTING_Foo_Bar": "1"}

    # And the shape is validated.
    r = client().post("/api/repros/e/env", headers=H, json={"setting": ["A=1"]})
    assert r.status_code == 400 and "must be an object" in r.json()["error"]


def test_static_assets_must_revalidate_so_an_upgrade_is_not_masked():
    """StaticFiles sends ETag/Last-Modified but no Cache-Control.

    With no freshness directive a browser may reuse a cached copy WITHOUT
    revalidating (heuristic freshness, RFC 9111 4.2.2), so after upgrading rc-repro
    the old app.js keeps running and new UI simply is not there — which looks
    exactly like a missing feature. `no-cache` means "revalidate first", not
    "don't store", so the ETag still answers 304.
    """
    c = client()
    for path in ("/", "/app.js", "/app.css"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path
        assert r.headers.get("etag"), f"{path} must still carry an ETag for the 304"

    # A matching ETag stays cheap.
    etag = c.get("/app.js").headers["etag"]
    r = c.get("/app.js", headers={"If-None-Match": etag})
    assert r.status_code == 304 and not r.content

    # API responses are not given a blanket directive here; the SSE stream sets its
    # own, and the rest are dynamic anyway.
    assert c.get("/api/health").headers.get("cache-control") is None
