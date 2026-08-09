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

#: Every request now carries a session cookie, so `H` is just the Host header the
#: allow-list wants. Kept under its old name so the ~90 call sites did not have to
#: change when the credential did.
H = {"Host": "localhost"}
PASSWORD = "correct-horse-battery"


def _fresh_auth_state():
    from rc_repro.services import sessions as sessionsvc
    sessionsvc._cache.clear()
    sessionsvc._stamp = (-1, -1)
    sessionsvc._flushed.clear()
    from rc_repro.web import app as webapp
    webapp._signin_fails.clear()


def client(host="http://localhost", *, sign_in=True, role=""):
    """An app with accounts, signed in as alice unless told otherwise.

    There is no token mode any more: a session is the only way in, so the default
    client holds one. `sign_in=False` gives the signed-out view.
    """
    from rc_repro.services import users as usersvc
    _fresh_auth_state()
    if not usersvc.any_users():
        usersvc.add("alice", PASSWORD, role=role or "admin")
    c = TestClient(create_app(), base_url=host)
    if sign_in:
        r = c.post("/signin", data={"user": "alice", "password": PASSWORD},
                   follow_redirects=False, headers=H)
        assert r.status_code == 303, r.text
    return c


def test_health_needs_no_session():
    r = client(sign_in=False).get("/api/health", headers=H)
    assert r.status_code == 200 and "docker" in r.json()


def test_api_requires_a_session(monkeypatch):
    assert client(sign_in=False).get("/api/repros", headers=H).status_code == 401
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
    app = create_app(allow_hosts=["Lab.Example.Com"])
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
    assert TestClient(create_app(), base_url=proxy).get("/api/health").status_code == 403
    assert TestClient(create_app(allow_hosts=["x.iximiuz.com"]),
                      base_url=proxy).get("/api/health").status_code == 200
    assert TestClient(create_app(allow_hosts=["*"]),
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
                        lambda req, emit, stream_output=False: seen.update(v=req.version) or {"name": "x"})
    r = client().post("/api/repros", headers=H,
                      json={"version": "8.5.1", "bogus_field": "drop me"})
    assert r.status_code == 200 and seen["v"] == "8.5.1"   # unknown key ignored, no crash


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


def test_jobs_list_needs_a_session():
    assert client(sign_in=False).get("/api/jobs").status_code == 401


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


def test_doctor_endpoint_needs_a_session():
    assert client(sign_in=False).get("/api/doctor").status_code == 401


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


def test_api_call_needs_a_session():
    assert client(sign_in=False).post("/api/repros/x/call", json={"method": "GET", "path": "/"}).status_code == 401


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


def test_tls_endpoint_needs_a_session():
    assert client(sign_in=False).get("/api/repros/t/tls").status_code == 401


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


def test_env_endpoints_need_a_session():
    assert client(sign_in=False).get("/api/repros/e/env").status_code == 401
    assert client(sign_in=False).post("/api/repros/e/env", json={"set": {}}).status_code == 401


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


# --- backup / restore / upgrade (#backup) ---------------------------------------
#
# The GUI gates its Upgrade action on the SERVER's answer, so these lock in that
# the gate is a real server decision and not something the browser decides alone.

def test_backup_is_a_job_and_passes_its_note_through(monkeypatch):
    """`label` is JobManager's own keyword, so the note travels as `note`."""
    from rc_repro.services import backup as bk
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    seen = {}
    monkeypatch.setattr(bk, "create",
                        lambda name, out="", note="", live=False, emit=None:
                        seen.update(name=name, out=out, note=note, live=live)
                        or {"name": name, "path": "/x.rcbak", "bytes": 1,
                            "manifest": {}})
    r = client().post("/api/repros/rc8-5-1/backup", headers=H,
                      json={"label": "before upgrade", "live": True})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    assert seen["note"] == "before upgrade" and seen["live"] is True


def test_backups_list_and_delete(monkeypatch):
    from rc_repro.services import backup as bk
    monkeypatch.setattr(bk, "list_backups", lambda name="": [{"path": "/a.rcbak"}])
    r = client().get("/api/backups", headers=H)
    assert r.status_code == 200 and r.json()["backups"] == [{"path": "/a.rcbak"}]

    monkeypatch.setattr(bk, "delete", lambda p: {"deleted": str(p)})
    r = client().request("DELETE", "/api/backups?path=/a.rcbak", headers=H)
    assert r.status_code == 200 and r.json()["deleted"] == "/a.rcbak"


def test_compatibility_is_answerable_before_committing(monkeypatch):
    """The restore dialog enables its button from this, so a downgrade is refused
    while the user is still choosing rather than after a job has started."""
    from rc_repro.services import backup as bk
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(bk, "read_manifest", lambda b: {"rc_version": "8.5.1"})
    monkeypatch.setattr(bk, "compatibility",
                        lambda m, meta: {"allowed": False,
                                         "blocked_reason": "downgrade"})
    import rc_repro.web.app as appmod
    monkeypatch.setattr(appmod.runner, "read_meta", lambda n: object())
    r = client().post("/api/backups/compatibility", headers=H,
                      json={"bundle": "/a.rcbak", "name": "old"})
    assert r.status_code == 200
    assert r.json()["compatibility"]["allowed"] is False

    # No target named -> just the manifest, no verdict to give.
    r = client().post("/api/backups/compatibility", headers=H,
                      json={"bundle": "/a.rcbak"})
    assert r.status_code == 200 and r.json()["compatibility"] is None


def test_compatibility_and_restore_need_a_bundle():
    assert client().post("/api/backups/compatibility", headers=H,
                         json={}).status_code == 400
    assert client().post("/api/restore", headers=H, json={}).status_code == 400


def test_restore_is_a_job_carrying_its_flags(monkeypatch):
    from rc_repro.services import backup as bk
    seen = {}
    monkeypatch.setattr(bk, "restore",
                        lambda bundle, name="", new=False, allow_upgrade=False,
                        force=False, emit=None:
                        seen.update(bundle=bundle, name=name, new=new,
                                    allow_upgrade=allow_upgrade, force=force) or {})
    r = client().post("/api/restore", headers=H,
                      json={"bundle": "/a.rcbak", "new": True, "allow_upgrade": True})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    assert seen["new"] is True and seen["allow_upgrade"] is True


def test_upgrade_gate_is_a_server_decision(monkeypatch):
    """A stopped workspace reports can_upgrade=false with the reason, so the GUI
    can hide the action instead of offering one that would fail."""
    from rc_repro.services import upgrade as upsvc
    monkeypatch.setattr(upsvc, "can_upgrade",
                        lambda n: {"can_upgrade": False, "reason": "'x' is exited",
                                   "current": ""})
    r = client().get("/api/repros/x/upgrade", headers=H)
    assert r.status_code == 200
    assert r.json()["can_upgrade"] is False and "exited" in r.json()["reason"]
    assert "plan" not in r.json()


def test_upgrade_plan_is_only_resolved_for_a_running_repro(monkeypatch):
    from rc_repro.services import upgrade as upsvc
    monkeypatch.setattr(upsvc, "can_upgrade",
                        lambda n: {"can_upgrade": True, "reason": "", "current": "8.5.1"})
    monkeypatch.setattr(upsvc, "plan",
                        lambda n, to, offline=False: {"to_version": to, "allowed": True})
    r = client().get("/api/repros/x/upgrade?to=8.6.1", headers=H)
    assert r.status_code == 200 and r.json()["plan"]["to_version"] == "8.6.1"


def test_upgrade_post_requires_a_target_version(monkeypatch):
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    assert client().post("/api/repros/x/upgrade", headers=H,
                         json={}).status_code == 400


def test_upgrade_and_rollback_are_jobs(monkeypatch):
    from rc_repro.services import upgrade as upsvc
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    seen = {}
    monkeypatch.setattr(upsvc, "run",
                        lambda name, to, offline=False, force=False, no_backup=False,
                        rollback_on_failure=True, emit=None:
                        seen.update(to=to, no_backup=no_backup) or {})
    r = client().post("/api/repros/x/upgrade", headers=H, json={"to": "8.6.1"})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")
    assert seen["to"] == "8.6.1" and seen["no_backup"] is False

    monkeypatch.setattr(upsvc, "rollback", lambda name, bundle="", emit=None: {})
    r = client().post("/api/repros/x/upgrade/rollback", headers=H, json={})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")


def test_backup_endpoints_need_a_session():
    assert client(sign_in=False).post("/api/repros/x/backup", json={}).status_code == 401
    assert client(sign_in=False).get("/api/backups").status_code == 401
    assert client(sign_in=False).post("/api/restore", json={"bundle": "/a"}).status_code == 401
    assert client(sign_in=False).get("/api/repros/x/upgrade").status_code == 401
# --- untyped JSON bodies (#audit) ------------------------------------------------
#
# A body field arrives as whatever JSON says it is. Each of these reached code that
# assumed a string and raised, escaping as an opaque 500 to any caller using the
# documented HTTP API. The shipped GUI always sends strings, so none were reachable
# from the browser -- which is exactly why they survived.

def test_create_without_a_version_is_a_400_not_a_500():
    r = client().post("/api/repros", headers=H, json={"preset": "default"})
    assert r.status_code == 400 and "version" in r.json()["error"]
    assert client().post("/api/repros", headers=H, json={}).status_code == 400


def test_create_still_accepts_a_valid_body(monkeypatch):
    monkeypatch.setattr(lc, "create_repro",
                        lambda req, emit, stream_output=False: {"name": req.version})
    r = client().post("/api/repros", headers=H, json={"version": "8.5.1", "bogus": 1})
    assert r.status_code == 200 and r.json()["job_id"].startswith("job_")


def test_benchmark_rejects_versions_that_are_not_strings():
    for bad in (-1, [1, 2], [[]], {"a": 1}, True):
        r = client().post("/api/benchmark", headers=H, json={"versions": bad})
        assert r.status_code == 400, f"{bad!r} gave {r.status_code}"
    assert client().post("/api/benchmark", headers=H,
                         json={"versions": []}).status_code == 400


def test_benchmark_still_accepts_a_list_or_a_csv_string(monkeypatch):
    from rc_repro.services import perf as perfsvc
    seen = []
    monkeypatch.setattr(perfsvc, "run_benchmark",
                        lambda v, p, o, np, emit=None: seen.append(list(v)) or {})
    assert client().post("/api/benchmark", headers=H,
                         json={"versions": ["8.4.2", "8.5.1"]}).status_code == 200
    assert client().post("/api/benchmark", headers=H,
                         json={"versions": "8.4.2, 8.5.1"}).status_code == 200
    assert seen == [["8.4.2", "8.5.1"], ["8.4.2", "8.5.1"]]


def test_state_rejects_a_non_string_action(monkeypatch):
    """`action` reached dict.get() and raised "unhashable type" for a dict/list."""
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    for bad in ({}, [], 5, None):
        r = client().post("/api/repros/x/state", headers=H, json={"action": bad})
        assert r.status_code == 400, f"{bad!r} gave {r.status_code}"


def test_settings_says_whether_an_email_is_remembered_without_leaking_it(monkeypatch,
                                                                        tmp_path):
    """The create dialog's email field is optional only if one is remembered.

    The placeholder used to say "leave blank, it is remembered" -- true for someone
    who ran `rc-repro config set acme.email`, false for everyone else, and the GUI
    has no way to set it. A blank field then produced a job that failed on a value
    the form had called optional.
    """
    from rc_repro import config as cfgmod
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    c = client()
    assert c.get("/api/settings", headers=H).json() == {"acme_email_remembered": False}

    cfgmod.update_config(lambda cfg: cfg.__setitem__("acme_email", "ops@rocket.chat"))
    r = c.get("/api/settings", headers=H)
    assert r.json() == {"acme_email_remembered": True}
    assert "ops@rocket.chat" not in r.text, "the address itself must never be returned"


def test_settings_needs_a_session():
    assert client(sign_in=False).get("/api/settings").status_code == 401


# --- named-user login (#team-auth) ------------------------------------------------
#
# With accounts present the GUI is behind a login, and every request carries it.
# Without accounts nothing changes: the session token still works exactly as before.

from pathlib import Path  # noqa: E402


def _auth(user=None, password=None):
    """Kept as the way every test names the Host it is talking to.

    It used to build an Authorization header; Basic is gone, and the session
    cookie the `signed_in` fixture holds is what authenticates now. Callers did
    not have to change.
    """
    return {"Host": "localhost"}


def _reset_auth_state():
    from rc_repro.services import sessions as sessionsvc
    sessionsvc._cache.clear()
    sessionsvc._stamp = (-1, -1)
    sessionsvc._flushed.clear()
    from rc_repro.web import app as webapp
    webapp._signin_fails.clear()


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    """An accounts-mode app with nobody signed in."""
    from rc_repro.services import users as usersvc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    _reset_auth_state()
    usersvc.add("alice", "correct-horse-battery")
    yield TestClient(create_app(), base_url="http://localhost")
    _reset_auth_state()


@pytest.fixture
def basic_client(anon_client):
    """Signed in as alice. TestClient keeps the cookie jar, so every later
    request in a test carries the session exactly as a browser would."""
    r = anon_client.post("/signin", data={"user": "alice",
                                          "password": "correct-horse-battery"},
                         follow_redirects=False)
    assert r.status_code == 303, r.text
    return anon_client


def test_an_anonymous_api_call_is_401_with_no_browser_dialog(anon_client):
    """WWW-Authenticate is what summoned the browser's own grey password box.

    Dropping it is precisely what makes a real sign-in page possible: as long as
    the header is sent, the browser prompts before the page can.
    """
    r = anon_client.get("/api/repros", headers={"Host": "localhost"})
    assert r.status_code == 401
    assert "www-authenticate" not in r.headers
    assert r.json()["kind"] == "Unauthorized"


def test_an_anonymous_page_is_sent_to_the_sign_in_page(anon_client):
    """A browser navigating to the app gets the page that fixes the problem,
    carrying where it was going. Answering an HTML navigation with JSON is how
    the old design ended up with no login screen at all."""
    r = anon_client.get("/", headers={"Host": "localhost"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/signin?e=required")


def test_the_sign_in_page_and_its_stylesheet_are_reachable_signed_out(anon_client):
    """Otherwise the login is an unstyled wall of text -- or worse, itself a
    redirect loop."""
    r = anon_client.get("/signin", headers={"Host": "localhost"})
    assert r.status_code == 200
    assert "<form method=\"post\" action=\"/signin\"" in r.text
    assert anon_client.get("/app.css", headers={"Host": "localhost"}).status_code == 200


def test_signing_in_sets_a_session_cookie_and_admits(anon_client, monkeypatch):
    monkeypatch.setattr(lc, "list_repros", lambda: [])
    r = anon_client.post("/signin", follow_redirects=False,
                         data={"user": "alice", "password": "correct-horse-battery"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "rc_repro_session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("samesite", "SameSite")
    assert anon_client.get("/api/repros", headers=_auth()).status_code == 200


def test_the_cookie_is_host_prefixed_and_secure_only_when_the_browser_hop_is_https(tmp_path, monkeypatch):
    """__Host- requires Secure, and http://localhost cannot have it -- so the name
    is decided by the transport, and exactly one name is live per scheme."""
    from rc_repro.services import users as usersvc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    _reset_auth_state()
    usersvc.add("alice", "correct-horse-battery")
    c = TestClient(create_app(public_https=True),
                   base_url="http://localhost")
    r = c.post("/signin", follow_redirects=False,
               data={"user": "alice", "password": "correct-horse-battery"})
    assert r.status_code == 303
    assert "__Host-rc_repro_session=" in r.headers["set-cookie"]
    assert "Secure" in r.headers["set-cookie"]
    _reset_auth_state()


def test_a_wrong_password_or_unknown_user_is_refused(anon_client):
    for user, pw in (("alice", "wrong-x-x-x-x"), ("ghost", "correct-horse-battery")):
        r = anon_client.post("/signin", data={"user": user, "password": pw},
                             follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/signin?e=bad")
        assert "set-cookie" not in r.headers, "a failed sign-in must mint nothing"


def test_signing_out_ends_the_session_on_the_server(basic_client, monkeypatch):
    """Not just a cleared cookie: the point of a server-side session is that the
    server can end it. Replaying the same cookie afterwards must fail."""
    monkeypatch.setattr(lc, "list_repros", lambda: [])
    jar = dict(basic_client.cookies)
    assert basic_client.get("/api/repros", headers=_auth()).status_code == 200
    r = basic_client.post("/signout", headers=_auth(), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin?e=signedout"
    basic_client.cookies.clear()
    basic_client.cookies.update(jar)          # replay the exact cookie
    assert basic_client.get("/api/repros", headers=_auth()).status_code == 401


def test_a_second_sign_in_does_not_reuse_the_first_token(anon_client):
    """Session fixation: every sign-in mints a fresh id."""
    first = anon_client.post("/signin", follow_redirects=False,
                             data={"user": "alice", "password": "correct-horse-battery"})
    anon_client.cookies.clear()
    second = anon_client.post("/signin", follow_redirects=False,
                              data={"user": "alice", "password": "correct-horse-battery"})
    assert first.headers["set-cookie"] != second.headers["set-cookie"]


def test_who_am_i_replaces_the_actor_field_on_health(basic_client):
    """/api/health stopped reading credentials: an endpoint left open for uptime
    checks should not be in the identity business."""
    assert "actor" not in basic_client.get("/api/health", headers=_auth()).json()
    me = basic_client.get("/api/session", headers=_auth()).json()
    assert me["user"] == "alice" and me["accounts"] is True


def test_your_sessions_can_be_listed_and_revoked_individually(basic_client):
    """The answer to "I signed in on the customer's laptop", which only exists
    because the session lives on the server."""
    rows = basic_client.get("/api/sessions", headers=_auth()).json()["sessions"]
    assert len(rows) == 1 and rows[0]["current"] is True
    assert len(rows[0]["sid"]) == 8, "the full verifier must never be handed out"
    r = basic_client.delete(f"/api/sessions?sid={rows[0]['sid']}", headers=_auth())
    assert r.status_code == 200
    assert basic_client.get("/api/sessions", headers=_auth()).status_code == 401


def test_a_malformed_session_cookie_does_not_crash(anon_client):
    for bad in ("", "!!!", "x" * 5000, "a.b.c"):
        anon_client.cookies.set("rc_repro_session", bad)
        r = anon_client.get("/api/repros", headers={"Host": "localhost"})
        assert r.status_code == 401, bad
    anon_client.cookies.clear()


def test_a_password_change_ends_every_session_it_minted(basic_client, monkeypatch):
    """Changing a compromised password must not leave the intruder signed in for
    the next seven days."""
    from rc_repro.services import sessions as sessionsvc
    from rc_repro.services import users as usersvc
    monkeypatch.setattr(lc, "list_repros", lambda: [])
    assert basic_client.get("/api/repros", headers=_auth()).status_code == 200
    # set_password does the revoking ITSELF now. This used to read
    # `usersvc.set_password(...)` then `assert sessionsvc.revoke_user("alice") == 1`
    # -- which asserted the opposite: that the caller still had to remember. Any
    # caller that forgot left a replaced credential working for seven days.
    ended = usersvc.set_password("alice", "a-brand-new-password")
    assert ended == 1, "the service, not the caller, ends the sessions"
    assert sessionsvc.revoke_user("alice") == 0, "there is nothing left to revoke"
    assert basic_client.get("/api/repros", headers=_auth()).status_code == 401


def test_health_stays_open_for_uptime_checks(basic_client):
    assert basic_client.get("/api/health",
                            headers={"Host": "localhost"}).status_code == 200


def test_the_sign_in_endpoint_is_throttled_per_address(anon_client, monkeypatch):
    """scrypt now runs on exactly ONE endpoint, which is what finally makes a
    guessing bound possible: services/users.py cannot refuse on a counter without
    refusing correct passwords too (that was B2), but a login endpoint can decline
    to spend the CPU at all. Keyed on the ADDRESS, so nobody can throttle a
    colleague by guessing at their name."""
    from rc_repro.web import app as webapp
    for _ in range(webapp.SIGNIN_MAX_FAILURES):
        anon_client.post("/signin", data={"user": "alice", "password": "nope-nope-nope"},
                         follow_redirects=False)
    r = anon_client.post("/api/session", json={"user": "alice", "password": "nope-nope-nope"})
    assert r.status_code == 429

    # ...and the throttle is on the ADDRESS, not on alice: a different client
    # signs her in normally while the attacker is still blocked.
    monkeypatch.setattr(lc, "list_repros", lambda: [])
    other = TestClient(anon_client.app, base_url="http://localhost")
    webapp._signin_fails.pop("testclient", None)
    ok = other.post("/api/session", json={"user": "alice",
                                          "password": "correct-horse-battery"})
    assert ok.status_code == 200 and ok.json()["user"] == "alice"


def test_a_job_records_who_ran_it(basic_client, monkeypatch, tmp_path):
    """Attribution is the point of named accounts — without it they are just a
    shared password with extra steps."""
    monkeypatch.setattr(lc, "create_repro",
                        lambda req, emit, stream_output=False: {"name": "x"})
    hdr = _auth("alice", "correct-horse-battery")
    assert basic_client.post("/api/repros", headers=hdr,
                             json={"version": "8.5.1"}).status_code == 200
    rows = basic_client.get("/api/jobs", headers=hdr).json()["jobs"]
    assert rows[0]["actor"] == "alice"


def test_the_audit_log_survives_a_restart(basic_client, monkeypatch, tmp_path):
    """journald is not always where somebody will look, and an in-memory job list
    is gone the moment the service restarts."""
    monkeypatch.setattr(lc, "create_repro",
                        lambda req, emit, stream_output=False: {"name": "x"})
    basic_client.post("/api/repros", headers=_auth("alice", "correct-horse-battery"),
                      json={"version": "8.5.1"})
    from rc_repro.web.jobs import AUDIT_FILE
    # Account changes are audited too now, so pick the line under test rather
    # than assuming the file has exactly one.
    lines = [ln for ln in (tmp_path / AUDIT_FILE).read_text().splitlines()
             if "\tcreate\t" in ln]
    assert len(lines) == 1, lines
    when, who, kind, label, origin, outcome = lines[0].split("\t")
    assert who == "alice" and kind == "create" and label == "8.5.1"
    assert when.startswith("20")
    # The two new columns are APPENDED, so the four an existing `cut -f2` reads
    # are still in the same positions.
    assert origin == "session", "a signed-in GUI request, so the identity is checked"
    assert outcome == "ok"


def test_an_unwritable_audit_log_does_not_break_the_job(monkeypatch):
    from rc_repro import config as cfgmod
    from rc_repro.web import jobs as jobs_mod
    # audit() imports config lazily, so patch the source module, not jobs.
    monkeypatch.setattr(cfgmod, "home",
                        lambda: Path("/proc/definitely-not-writable"))
    jobs_mod.audit("alice", "create", "8.5.1")     # must not raise


def test_the_actor_comes_from_the_session_not_the_body(basic_client, monkeypatch):
    """Otherwise anyone could create a workspace in a colleague's namespace."""
    seen = {}
    monkeypatch.setattr(lc, "create_repro",
                        lambda req, emit, stream_output=False:
                        seen.update(actor=req.actor) or {"name": "x"})
    basic_client.post("/api/repros", headers=_auth("alice", "correct-horse-battery"),
                      json={"version": "8.5.1", "actor": "bob"})
    assert seen["actor"] == "alice", "the body's actor must be ignored"


def test_health_says_nothing_about_identity(anon_client):
    """It used to carry an `actor` field, which existed only because there was
    nowhere else to ask who was signed in. `GET /api/session` is that place now,
    and an endpoint left open for uptime checks has no business reading
    credentials -- deriving there let an anonymous caller spend scrypt per request
    just by attaching a header."""
    body = anon_client.get("/api/health", headers={"Host": "localhost"}).json()
    assert body["ok"] is True
    assert "actor" not in body


def test_who_am_i_is_answerable_without_being_signed_in(anon_client):
    """So the page can tell "signed out" from "this server has no accounts"."""
    me = anon_client.get("/api/session", headers={"Host": "localhost"}).json()
    assert me == {"user": "", "role": "", "accounts": True}


# --- CSRF (F2) ------------------------------------------------------------------
# In token mode the unguessable ?t= doubles as a CSRF token. A Basic credential
# does not: the browser attaches it automatically, `Host:` is whatever this server
# answers to, and Basic is not a cookie so SameSite never applies. Confirmed
# reachable before the fix -- a body-less POST to /upgrade/rollback, which drops
# the workspace database.

_CROSS = {"Host": "localhost", "Sec-Fetch-Site": "cross-site",
          "Origin": "https://evil.example"}


def test_a_cross_site_state_change_is_refused(basic_client):
    creds = _auth("alice", "correct-horse-battery")
    for path in ("/api/repros/x/upgrade/rollback", "/api/repros/x/pat",
                 "/api/repros/x/default", "/api/repros/x/monitor"):
        r = basic_client.post(path, headers={**creds, **_CROSS})
        assert r.status_code == 403, f"{path} -> {r.status_code}"


def test_a_cross_site_delete_is_refused(basic_client):
    r = basic_client.delete("/api/repros/x?volumes=true&confirm=true",
                            headers={**_auth("alice", "correct-horse-battery"), **_CROSS})
    assert r.status_code == 403


def test_the_apps_own_requests_still_work(basic_client):
    """Sec-Fetch-Site: same-origin is what the SPA's own fetch() sends."""
    creds = _auth("alice", "correct-horse-battery")
    r = basic_client.post("/api/repros/x/default",
                          headers={**creds, "Host": "localhost",
                                   "Sec-Fetch-Site": "same-origin",
                                   "Origin": "http://localhost"})
    assert r.status_code != 403


def test_non_browser_clients_are_unaffected(basic_client):
    """curl and CI send neither header; refusing them would break every script."""
    r = basic_client.post("/api/repros/x/default",
                          headers={**_auth("alice", "correct-horse-battery"),
                                   "Host": "localhost"})
    assert r.status_code != 403


def test_a_cross_site_read_is_refused_too(basic_client):
    """Reads used to be waved through as the milder case. They are not.

    `GET /api/repros/{name}/logs` hands over a workspace's container log, which
    carries LDAP bind passwords and OAuth client secrets, and `/api/repros`
    discloses every ticket number on the box. The guard therefore covers every
    /api/ path, not just the state-changing methods.
    """
    creds = _auth("alice", "correct-horse-battery")
    for path in ("/api/repros", "/api/repros/x/logs", "/api/repros/x/detail",
                 "/api/jobs"):
        r = basic_client.get(path, headers={**creds, **_CROSS})
        assert r.status_code == 403, f"{path} -> {r.status_code}"


def test_the_spa_itself_is_still_reachable_cross_site(basic_client):
    """Only /api/ is guarded. A link to the GUI from a wiki or a chat message has
    to keep working -- refusing the document would break every bookmark."""
    r = basic_client.get("/", headers={**_auth("alice", "correct-horse-battery"),
                                       **_CROSS})
    assert r.status_code == 200


# --- B1: the logs WebSocket (confirmed exploitable before this) ------------------

def _ws_headers(**over):
    h = {"Host": "localhost"}
    h.update(over)
    return h


def test_a_cross_site_websocket_is_refused_before_it_is_accepted(basic_client):
    """The confirmed exploit. A WebSocket handshake is exempt from CORS, so the
    browser makes it cross-origin and attaches the cached Basic credential itself
    -- and this handler re-implemented `guard`'s checks while omitting the
    cross-site one. Measured before the fix: a forged Origin was refused 403 on a
    POST and ACCEPTED here, then streamed the workspace's log.
    """
    from starlette.websockets import WebSocketDisconnect
    creds = _auth("alice", "correct-horse-battery")
    with pytest.raises(WebSocketDisconnect):
        with basic_client.websocket_connect(
                "/api/repros/w1/logs/stream",
                headers={**creds, **_ws_headers(**{"Origin": "https://evil.example",
                                                   "Sec-Fetch-Site": "cross-site"})}):
            pass


def test_a_websocket_with_no_origin_is_refused_when_the_credential_is_ambient(basic_client):
    """A browser ALWAYS sends Origin on a WS upgrade, so an absent one cannot be
    read as same-origin the way it can for a plain fetch. With Basic in play the
    credential is attached by the browser, so the handshake has to prove it came
    from us."""
    from starlette.websockets import WebSocketDisconnect
    creds = _auth("alice", "correct-horse-battery")
    with pytest.raises(WebSocketDisconnect):
        with basic_client.websocket_connect("/api/repros/w1/logs/stream",
                                            headers={**creds, **_ws_headers()}):
            pass


# --- backup `out` is confined over HTTP (F9) ------------------------------------

def test_a_backup_cannot_be_written_outside_the_managed_directory(basic_client, monkeypatch):
    """`out` chose a path on the SERVER. Unconfined it wrote a tar.gz anywhere the
    server user could -- a systemd drop-in, a webroot -- and with a forged
    cross-site request the attacker chose the path too."""
    creds = _auth("alice", "correct-horse-battery")
    monkeypatch.setattr(lc, "resolve_name", lambda n, actor="": n)
    for bad in ("/etc/cron.d/pwn", "../../../../tmp/pwn.tar.gz", "~/../../tmp/x"):
        r = basic_client.post("/api/repros/x/backup",
                              headers={**creds, "Host": "localhost"},
                              json={"out": bad})
        assert r.status_code == 400, f"{bad} -> {r.status_code}"
        assert "must be inside" in r.json()["error"]


def test_a_backup_inside_the_managed_directory_is_allowed(basic_client, monkeypatch, tmp_path):
    from rc_repro.services import backup as backupsvc

    creds = _auth("alice", "correct-horse-battery")
    monkeypatch.setattr(lc, "resolve_name", lambda n, actor="": n)
    seen = {}
    monkeypatch.setattr(backupsvc, "create",
                        lambda t, out="", note="", live=False, emit=None:
                        seen.update(out=out) or {"ok": True})
    dest = backupsvc.backups_dir() / "mine.tar.gz"
    r = basic_client.post("/api/repros/x/backup", headers={**creds, "Host": "localhost"},
                          json={"out": str(dest)})
    assert r.status_code == 200, r.text
    assert seen["out"] == str(dest.resolve())


# --- graceful shutdown (F15) ----------------------------------------------------

def test_shutdown_waits_for_a_running_job():
    """A backup killed inside _Quiesced leaves Rocket.Chat STOPPED with nothing to
    restart it; a load test killed mid-run leaves CPU caps applied. Daemon threads
    skip every `finally`, and `Restart=always` makes that routine."""
    import time as _t

    from rc_repro.web.jobs import JobManager

    jobs = JobManager()
    cleaned = []

    def slow(emit=None):
        try:
            _t.sleep(0.4)
            return {"ok": True}
        finally:
            cleaned.append("restored")

    jobs.submit("seed", slow, label="x")
    _t.sleep(0.05)
    left = jobs.drain(timeout=5)
    assert left == [], "the job should have finished before the deadline"
    assert cleaned == ["restored"], "its cleanup must have run"


def test_shutdown_is_bounded_and_names_what_it_abandoned():
    """A capacity search can run for an hour; a shutdown that never completes is
    its own failure, and systemd would SIGKILL us anyway."""
    import threading
    import time as _t

    from rc_repro.web.jobs import JobManager

    jobs = JobManager()
    release = threading.Event()
    job = jobs.submit("seed", lambda emit=None: release.wait(timeout=10), label="x")
    _t.sleep(0.05)
    left = jobs.drain(timeout=0.3)
    assert left == [job.id], "an abandoned job must be named"
    release.set()


def test_new_work_is_refused_once_shutting_down():
    from rc_repro.errors import ConflictError
    from rc_repro.web.jobs import JobManager

    jobs = JobManager()
    jobs.drain(timeout=0)
    with pytest.raises(ConflictError):
        jobs.submit("seed", lambda emit=None: None, label="x")


def test_the_app_drains_on_shutdown(monkeypatch):
    """The hook has to be wired to the app, not just exist."""
    drained = []
    app = create_app()
    monkeypatch.setattr(type(app.state.jobs), "drain",
                        lambda self, timeout=25.0: drained.append(True) or [])
    with TestClient(app, base_url="http://localhost") as c:
        c.get("/api/health")
    assert drained == [True], "lifespan shutdown never ran drain()"


# --- the edge, in the GUI (its own endpoint) ------------------------------------

def test_edge_status_is_not_on_the_health_endpoint(basic_client):
    """/api/health is the cheap, unauthenticated one that every tab polls every
    four seconds; the edge status shells out to docker twice."""
    body = basic_client.get("/api/health",
                            headers=_auth("alice", "correct-horse-battery")).json()
    assert "routes" not in body and "edge" not in body


def test_edge_endpoint_reports_each_route_and_whether_it_is_reachable(
        basic_client, monkeypatch):
    """A route the edge cannot reach answers 502 rather than erroring -- the one
    failure nothing else in the UI would surface."""
    from rc_repro.services import edge as edgesvc

    edgesvc.write(edgesvc.Edge(domain="gui.example.com", acme_email="o@e.com"))
    monkeypatch.setattr(edgesvc, "_docker", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": ""})())
    edgesvc.register("alice-rc8-5-1", "t1.example.com")
    edgesvc.register("bob-rc8-5-1", "t2.example.com")
    monkeypatch.setattr(edgesvc, "running", lambda: True)
    monkeypatch.setattr(edgesvc, "attached_networks",
                        lambda: {edgesvc.workspace_network("alice-rc8-5-1")})

    body = basic_client.get("/api/edge",
                            headers=_auth("alice", "correct-horse-battery")).json()
    assert body["installed"] is True and body["running"] is True
    assert body["domain"] == "gui.example.com"
    by_name = {r["name"]: r for r in body["routes"]}
    assert by_name["alice-rc8-5-1"]["host"] == "t1.example.com"
    assert by_name["alice-rc8-5-1"]["reachable"] is True
    assert by_name["bob-rc8-5-1"]["reachable"] is False, "attached to neither"


def test_edge_endpoint_says_so_when_there_is_none(basic_client):
    body = basic_client.get("/api/edge",
                            headers=_auth("alice", "correct-horse-battery")).json()
    assert body["installed"] is False and body["routes"] == []


# --- roles (M10) ------------------------------------------------------------------
# The role column has been parsed, displayed and enforced NOWHERE since it was
# added. These lock in what it now means.

def _as(client, name, password):
    """Sign `client` in as somebody else, replacing whatever session it holds."""
    client.cookies.clear()
    r = client.post("/signin", data={"user": name, "password": password},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


def test_every_api_route_declares_a_minimum_role():
    """DEFAULT DENY covers the runtime; this covers the reviewer.

    An endpoint shipping unguarded is structurally the same mistake as the audit
    gap -- added in one place, not registered in the other -- so it fails the
    build rather than waiting to be noticed.
    """
    from rc_repro.web import app as webapp
    app = create_app()
    missing = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not (path.startswith("/api/") or path in ("/signin", "/signout")):
            continue
        methods = sorted(getattr(route, "methods", None) or ["WS"])
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            if webapp.route_requirement(m, path) is None:
                missing.append(f"{m} {path}")
    assert not missing, ("these routes declare no minimum role, so they are "
                         f"refused at runtime: {missing}")


def test_a_readonly_user_may_look_but_not_touch(basic_client, monkeypatch):
    from rc_repro.services import users as usersvc
    monkeypatch.setattr(lc, "list_repros", lambda: [])
    usersvc.add("ronly", "read-only-password", role="readonly")
    _as(basic_client, "ronly", "read-only-password")

    assert basic_client.get("/api/repros", headers=_auth()).status_code == 200
    assert basic_client.get("/api/doctor", headers=_auth()).status_code == 200
    for path in ("/api/repros/x/state", "/api/repros/x/seed", "/api/repros/x/pat"):
        r = basic_client.post(path, json={}, headers=_auth())
        assert r.status_code == 403, f"{path} -> {r.status_code}"
    assert basic_client.delete("/api/repros/x?volumes=true", headers=_auth()).status_code == 403


def test_a_readonly_user_cannot_read_logs_or_env(basic_client, monkeypatch):
    """Not an oversight. A workspace log carries LDAP bind passwords and OAuth
    client secrets, so "read-only" cannot mean "may read those"."""
    from rc_repro.services import users as usersvc
    usersvc.add("ronly", "read-only-password", role="readonly")
    _as(basic_client, "ronly", "read-only-password")
    assert basic_client.get("/api/repros/x/logs", headers=_auth()).status_code == 403
    assert basic_client.get("/api/repros/x/env", headers=_auth()).status_code == 403


def test_a_member_cannot_manage_people(basic_client, monkeypatch):
    from rc_repro.services import users as usersvc
    usersvc.add("bob", "bobs-good-password", role="member")
    _as(basic_client, "bob", "bobs-good-password")
    assert basic_client.get("/api/users", headers=_auth()).status_code == 403
    assert basic_client.post("/api/users", json={"name": "eve"},
                             headers=_auth()).status_code == 403
    # ...but may still change their OWN password
    r = basic_client.post("/api/me/password", headers=_auth(),
                          json={"old": "bobs-good-password", "new": "another-good-one"})
    assert r.status_code == 200


def test_an_admin_creates_an_account_and_the_server_mints_the_password(basic_client):
    """An admin who TYPES a colleague's password also knows it, which makes every
    audit line signed with that name deniable."""
    r = basic_client.post("/api/users", json={"name": "carol", "role": "member"},
                          headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "member" and len(body["password"]) >= 12
    # and it works
    _as(basic_client, "carol", body["password"])
    assert basic_client.get("/api/session", headers=_auth()).json()["user"] == "carol"


def test_the_last_admin_is_protected_through_the_api(basic_client):
    r = basic_client.post("/api/users/alice/role", json={"role": "member"},
                          headers=_auth())
    assert r.status_code == 409 and "only admin" in r.json()["error"]
    assert basic_client.delete("/api/users/alice", headers=_auth()).status_code == 409


def test_a_blank_role_resolves_to_admin_over_http(basic_client):
    """The upgrade path: every account created before roles existed has a blank
    column. If blank did not mean admin, an install whose only account predates
    this would have zero admins and no way to make one."""
    from rc_repro.services import users as usersvc
    # A line written the way a version before roles did it: no role column.
    stored = usersvc._read()
    stored["legacy"] = (usersvc.hash_password("legacy-password-here"), "2026-01-01", "")
    usersvc._write(stored)
    rows = basic_client.get("/api/users", headers=_auth()).json()
    legacy = next(u for u in rows["users"] if u["name"] == "legacy")
    assert legacy["role"] == "admin" and legacy["implicit"] is True
    assert rows["implicit_admins"] == ["legacy"]


def test_changing_a_role_ends_that_users_sessions(basic_client):
    from rc_repro.services import sessions as sessionsvc
    from rc_repro.services import users as usersvc
    usersvc.add("bob", "bobs-good-password", role="member")
    token = sessionsvc.create("bob")
    assert sessionsvc.verify(token) is not None
    basic_client.post("/api/users/bob/role", json={"role": "readonly"}, headers=_auth())
    assert sessionsvc.verify(token) is None, "a demotion must reach a live session"


def test_only_an_admin_may_choose_the_image_or_the_interface(basic_client, monkeypatch):
    """`rc_image` runs an arbitrary container as the serve user and `bind` can
    publish a workspace with fixed admin/admin123 credentials to the network.
    Those decide what code runs and where it listens, which is not the same
    question as "make me a workspace"."""
    from rc_repro.services import users as usersvc
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    usersvc.add("bob", "bobs-good-password", role="member")
    _as(basic_client, "bob", "bobs-good-password")
    for field, value in (("rc_image", "evil/image"), ("bind", "0.0.0.0"),
                         ("reg_token", "x"), ("port", 3999)):
        r = basic_client.post("/api/repros", headers=_auth(),
                              json={"version": "8.5.1", field: value})
        assert r.status_code == 400, f"{field} -> {r.status_code}"
        assert "admin" in r.json()["error"]
    # the ordinary body still works for a member
    assert basic_client.post("/api/repros", headers=_auth(),
                             json={"version": "8.5.1"}).status_code == 200


# --- the activity trail gets readers (H5) -----------------------------------------

def test_the_audit_log_can_finally_be_read(basic_client, monkeypatch):
    """It has been written since accounts landed and read by nothing -- no CLI
    command, no endpoint, no view. "Who tore down TICKET-1234?" meant ssh and cat."""
    monkeypatch.setattr(lc, "create_repro",
                        lambda req, emit, stream_output=False: {"name": "x"})
    basic_client.post("/api/repros", headers=_auth(), json={"version": "8.5.1"})
    out = basic_client.get("/api/audit", headers=_auth()).json()
    kinds = [r["kind"] for r in out["lines"]]
    assert "create" in kinds
    row = next(r for r in out["lines"] if r["kind"] == "create")
    assert row["actor"] == "alice" and row["origin"] == "session"
    assert row["outcome"] == "ok"


def test_a_non_admin_only_ever_sees_their_own_lines(basic_client):
    """Self-audit rather than admin-only: "wait, did I do that?" is a legitimate
    question, and a 403 there teaches people the log is not for them."""
    from rc_repro.services import users as usersvc
    usersvc.add("bob", "bobs-good-password", role="member")
    # alice (admin) generates a line of her own
    basic_client.post("/api/users/bob/role", json={"role": "member"}, headers=_auth())
    _as(basic_client, "bob", "bobs-good-password")
    out = basic_client.get("/api/audit?actor=alice", headers=_auth()).json()
    assert out["scope"] == "bob", "the actor filter is forced, not honoured"
    assert all(r["actor"] == "bob" for r in out["lines"])


def test_a_refusal_is_recorded_so_the_role_lines_can_be_reviewed(basic_client):
    """Whether `readonly` is drawn in the right place is an open question in the
    design; `grep denied` is the evidence to settle it with."""
    from rc_repro.services import users as usersvc
    usersvc.add("ronly", "read-only-password", role="readonly")
    _as(basic_client, "ronly", "read-only-password")
    assert basic_client.post("/api/repros", json={"version": "8.5.1"},
                             headers=_auth()).status_code == 403
    out = basic_client.get("/api/audit", headers=_auth()).json()
    denied = [r for r in out["lines"] if r["outcome"] == "denied"]
    assert denied and "needs member" in denied[0]["label"]


def test_minting_a_token_is_audited_at_the_one_place_all_callers_pass_through():
    """generate_pat has eight non-test call sites, so a fix at one handler would
    have missed six -- and a bypass-2FA admin token is one of the two most
    sensitive things this tool can do."""
    import inspect
    from rc_repro import rcapi
    src = inspect.getsource(rcapi.generate_pat)
    assert "auditsvc.record(\"pat\"" in src


def test_a_legacy_four_field_line_still_parses(tmp_path, monkeypatch):
    """The two new columns are APPENDED, so an old log stays readable rather than
    being silently skipped by the reader that was supposed to make it useful."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.services import audit as auditsvc
    auditsvc.audit_path().parent.mkdir(parents=True, exist_ok=True)
    auditsvc.audit_path().write_text(
        "2026-01-01T00:00:00+00:00\talice\tdown-volumes\tticket-1234\n", encoding="utf-8")
    (row,) = auditsvc.read()["lines"]
    assert row["actor"] == "alice" and row["kind"] == "down-volumes"
    assert row["origin"] == "" and row["outcome"] == "ok"


# --- first run (D8): creating the very first account ------------------------------

def _first_run_client(tmp_path, monkeypatch):
    from rc_repro.services import firstrun as frsvc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    _fresh_auth_state()
    key = frsvc.mint()
    return TestClient(create_app(first_run=True), base_url="http://localhost"), key


def test_the_first_account_is_created_by_spending_the_key(tmp_path, monkeypatch):
    """One step, not two. A key-for-session exchange followed by an account
    creation would mean a privileged session with no account behind it existing
    in between -- the anonymous admin mode this design removes."""
    c, key = _first_run_client(tmp_path, monkeypatch)
    assert c.get("/setup", headers=H).status_code == 200
    r = c.post("/api/session/first-run", headers=H,
               json={"key": key, "user": "alice", "password": "alice-good-password"})
    assert r.status_code == 200 and r.json() == {"user": "alice", "role": "admin"}
    assert "rc_repro_session=" in r.headers["set-cookie"]
    assert c.get("/api/session", headers=H).json()["role"] == "admin"


def test_the_key_is_single_use_and_setup_disappears_with_it(tmp_path, monkeypatch):
    c, key = _first_run_client(tmp_path, monkeypatch)
    c.post("/api/session/first-run", headers=H,
           json={"key": key, "user": "alice", "password": "alice-good-password"})
    again = c.post("/api/session/first-run", headers=H,
                   json={"key": key, "user": "eve", "password": "eves-good-password"})
    assert again.status_code == 409
    # and the door itself is gone, so a stale bookmark is not a second way in
    assert c.get("/setup", headers=H).status_code == 404
    assert c.get("/setup.js", headers=H).status_code == 404


def test_a_wrong_key_creates_nothing(tmp_path, monkeypatch):
    from rc_repro.services import users as usersvc
    c, _key = _first_run_client(tmp_path, monkeypatch)
    r = c.post("/api/session/first-run", headers=H,
               json={"key": "not-the-key", "user": "eve", "password": "eves-good-password"})
    assert r.status_code == 401
    assert usersvc.any_users() is False, "a failed attempt must create no account"


def test_setup_is_closed_when_the_flow_was_never_opened(tmp_path, monkeypatch):
    """`serve` sets first_run only on a loopback bind with no accounts. Anywhere
    else these must not exist at all."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    _fresh_auth_state()
    c = TestClient(create_app(first_run=False), base_url="http://localhost")
    assert c.get("/setup", headers=H).status_code == 404
    assert c.post("/api/session/first-run", headers=H,
                  json={"key": "x", "user": "a", "password": "b"}).status_code == 409


# --- ownership: handover, and a destruction gate (phase 5) ------------------------

def _workspace(name, owner, tmp_path):
    """A workspace record on disk, owned by somebody."""
    from rc_repro import runner
    ws = runner.workspace(name)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "docker-compose.yml").write_text("services: {}\n")
    meta = runner.Metadata(name=name, project=f"rcrepro-{name}", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="http://localhost:3000",
                           host_port=3000, version_source="map",
                           extra={"created_by": owner})
    import json as _j
    from dataclasses import asdict
    (ws / "repro.json").write_text(_j.dumps(asdict(meta)))
    return meta


def test_handing_over_moves_the_owner_and_keeps_who_made_it(basic_client, tmp_path):
    """Who CREATED it is a fact about the past and stays; who is responsible for
    it today is what a handover changes. Before this, "belongs to alice" kept
    warning bob about data that had been his for a week."""
    from rc_repro.services import users as usersvc
    from rc_repro import runner
    usersvc.add("bob", "bobs-good-password", role="member")
    _workspace("t4471", "alice", tmp_path)
    r = basic_client.post("/api/repros/t4471/owner", json={"to": "bob"}, headers=_auth())
    assert r.status_code == 200 and r.json()["to"] == "bob"
    extra = runner.read_meta("t4471").extra
    assert extra["owner"] == "bob"
    assert extra["created_by"] == "alice", "creation is immutable"
    assert extra["owner_history"][-1]["from"] == "alice"
    assert lc.owner_of("t4471") == "bob"


def test_you_cannot_hand_a_workspace_to_somebody_who_cannot_sign_in(basic_client, tmp_path):
    _workspace("t4471", "alice", tmp_path)
    r = basic_client.post("/api/repros/t4471/owner", json={"to": "ghost"}, headers=_auth())
    assert r.status_code == 404 and "no account" in r.json()["error"]


def test_a_member_cannot_destroy_somebody_elses_workspace(basic_client, tmp_path, monkeypatch):
    """§3.3's guardrail was a confirm dialog, which stops being a guardrail around
    the twentieth time you click through it for your own workspaces."""
    from rc_repro.services import users as usersvc
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    usersvc.add("bob", "bobs-good-password", role="member")
    _workspace("alices-box", "alice", tmp_path)
    _as(basic_client, "bob", "bobs-good-password")
    r = basic_client.delete("/api/repros/alices-box?volumes=true&confirm=true",
                            headers=_auth())
    assert r.status_code == 409
    assert "belongs to alice" in r.json()["error"]


def test_help_on_a_colleagues_workspace_stays_open(basic_client, tmp_path, monkeypatch):
    """Only DESTRUCTION is gated. Covering somebody's ticket is the workflow this
    exists for, so start/stop/logs/seed on their workspace stay allowed."""
    from rc_repro.services import users as usersvc
    monkeypatch.setattr(lc, "set_state", lambda n, a: None)
    usersvc.add("bob", "bobs-good-password", role="member")
    _workspace("alices-box", "alice", tmp_path)
    _as(basic_client, "bob", "bobs-good-password")
    r = basic_client.post("/api/repros/alices-box/state", json={"action": "restart"},
                          headers=_auth())
    assert r.status_code == 200


def test_an_admin_can_still_force_it(basic_client, tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr("rc_repro.runner.down", lambda n, volumes=False: 0)
    monkeypatch.setattr("rc_repro.runner.remove", lambda n: None)
    _workspace("bobs-box", "bob", tmp_path)
    allowed, _why = lc.may_destroy("bobs-box", "alice")   # alice is admin
    assert allowed is True


def test_the_gate_is_one_config_line_away_from_the_old_behaviour(tmp_path, monkeypatch):
    """A team that preferred confirm-and-proceed should not have to fork."""
    from rc_repro import config
    from rc_repro.services import users as usersvc
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    usersvc.add("alice", "correct-horse-battery", role="admin")
    usersvc.add("bob", "bobs-good-password", role="member")
    _workspace("alices-box", "alice", tmp_path)
    assert lc.may_destroy("alices-box", "bob")[0] is False
    config.update_config(lambda c: c.__setitem__(lc.DESTROY_POLICY_KEY, "anyone"))
    assert lc.may_destroy("alices-box", "bob")[0] is True


def test_a_same_origin_form_post_with_a_null_origin_is_allowed(anon_client):
    """The regression that only a real browser found.

    A same-origin form POST from a page served with `Referrer-Policy: no-referrer`
    arrives as `Sec-Fetch-Site: same-origin` with `Origin: null`. Treating that
    "null" as a foreign host refused the sign-in form in every real browser, while
    every HTTP test passed because TestClient sends neither header.
    """
    r = anon_client.post("/signin", follow_redirects=False,
                         data={"user": "alice", "password": "correct-horse-battery"},
                         headers={"Host": "localhost", "Origin": "null",
                                  "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_a_null_origin_without_sec_fetch_site_is_still_refused(basic_client):
    """An opaque origin -- a sandboxed iframe, a data: URL -- cannot be matched
    against the allow-list, so with no Sec-Fetch-Site to appeal to it counts as
    cross-site."""
    r = basic_client.post("/api/repros/x/state", json={"action": "stop"},
                          headers={"Host": "localhost", "Origin": "null"})
    assert r.status_code == 403


# --- --trust-proxy (P12): whose X-Forwarded-* is believed -------------------------

_FWD = {"Host": "localhost", "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "203.0.113.9", "Sec-Fetch-Site": "same-origin"}


def _app(**kw):
    """A client whose peer address is a real IP.

    TestClient reports its peer as the literal string "testclient", which is not
    an address and is therefore never trusted -- correct behaviour, but it makes
    the trusted case untestable unless the client is given one.
    """
    from rc_repro.services import users as usersvc
    _fresh_auth_state()
    if not usersvc.any_users():
        usersvc.add("alice", PASSWORD, role="admin")
    peer = kw.pop("peer", "127.0.0.1")
    # `base` matters once a test follows a cookie the proxy's scheme made Secure:
    # httpx, like a browser, will not send one back over http://.
    base = kw.pop("base", "http://localhost")
    return TestClient(create_app(**kw), base_url=base, client=(peer, 5555))


def test_forwarded_headers_are_ignored_from_an_untrusted_peer():
    """uvicorn believes X-Forwarded-* from 127.0.0.1 by DEFAULT, so on the default
    bind any other local user could rewrite the client address and the scheme --
    both of which decide security here. `serve` passes proxy_headers=False and
    this resolves it explicitly instead."""
    c = _app()                      # no trust_proxy at all
    r = c.post("/signin", data={"user": "alice", "password": PASSWORD},
               headers=_FWD, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "Secure" not in cookie, "a client must not be able to claim https"
    assert "__Host-" not in cookie


def test_a_trusted_proxy_makes_the_cookie_secure_and_host_prefixed():
    """The live symptom this fixes: behind a TLS-terminating proxy the session
    cookie was issued without Secure and without __Host-, on a public https URL,
    and the sign-in page warned about a connection that was actually encrypted."""
    c = _app(trust_proxy=["127.0.0.1"])
    r = c.post("/signin", data={"user": "alice", "password": PASSWORD},
               headers=_FWD, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "__Host-rc_repro_session=" in cookie and "Secure" in cookie


def test_the_transport_warning_follows_the_same_decision():
    """Deriving the banner and the cookie independently is how a misconfigured
    proxy ends up minting a cookie the browser then sends in clear.

    The peer is non-loopback in both halves: on loopback the warning is suppressed
    regardless, because there the password crosses no network.
    """
    warned = _app(peer="10.0.0.9").get("/signin", headers=_FWD)
    assert "not encrypted" in warned.text, "an untrusted X-Forwarded-Proto is ignored"
    quiet = _app(peer="10.0.0.9", trust_proxy=["10.0.0.0/8"]).get("/signin", headers=_FWD)
    assert "not encrypted" not in quiet.text


def test_the_throttle_keys_on_the_real_client_behind_a_trusted_proxy():
    """Without this every user shares one bucket -- the proxy's address -- so one
    person mistyping their password ten times locks out the whole team."""
    from rc_repro.web import app as webapp
    c = _app(trust_proxy=["127.0.0.1"])
    for _ in range(webapp.SIGNIN_MAX_FAILURES):
        c.post("/api/session", json={"user": "alice", "password": "wrong-wrong-x"},
               headers=_FWD)
    assert c.post("/api/session", json={"user": "alice", "password": "wrong-wrong-x"},
                  headers=_FWD).status_code == 429
    # a DIFFERENT client behind the same proxy is unaffected
    other = {**_FWD, "X-Forwarded-For": "203.0.113.77"}
    assert c.post("/api/session", json={"user": "alice", "password": PASSWORD},
                  headers=other).status_code == 200


def test_changing_your_own_password_behind_a_proxy_keeps_you_signed_in():
    """The cookie's NAME and its Secure flag follow the BROWSER's hop, not the bind.

    Behind --trust-proxy that hop is https while public_https is False (rc-repro
    did not arrange the TLS), so the guard reads `__Host-rc_repro_session`.
    me_password was the one call site of four that did not pass the resolved
    scheme, so it wrote the plain name: every session was revoked, the replacement
    went out under a name nothing reads, and changing your own password signed you
    out having just answered {"ok": true}. The replacement also went out WITHOUT
    Secure and without the prefix that stops a sibling workspace -- which the
    design puts on a neighbouring host, running admin/admin123 -- shadowing it.

    Nothing covered me_password behind a proxy, which is why it shipped.
    """
    c = _app(trust_proxy=["127.0.0.1"], base="https://localhost")
    r = c.post("/api/session", json={"user": "alice", "password": PASSWORD},
               headers=_FWD)
    assert r.status_code == 200
    assert r.headers["set-cookie"].startswith("__Host-rc_repro_session=")

    r = c.post("/api/me/password",
               json={"old": PASSWORD, "new": "a-brand-new-password"}, headers=_FWD)
    assert r.status_code == 200, r.text
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-rc_repro_session="), \
        f"the replacement cookie is under a name the guard does not read: {cookie}"
    assert "Secure" in cookie, f"a session token went out without Secure: {cookie}"

    who = c.get("/api/session", headers=_FWD)
    assert who.json()["user"] == "alice", "changing my own password signed me out"


def test_a_cidr_is_accepted_and_a_typo_simply_trusts_nothing():
    from rc_repro.web.app import parse_trusted
    assert len(parse_trusted(["10.0.0.0/8", "127.0.0.1"])) == 2
    assert parse_trusted(["not-an-address", ""]) == [], \
        "a typo must mean 'not trusted', not a server that will not start"


def test_the_job_queue_is_bounded_at_submit_not_at_the_slot():
    """The measurement and heavy pools bound how many jobs RUN at once, and they
    are acquired inside the worker thread -- so the thread already exists by the
    time it waits. Measured before this: 40 capacity submissions against a pool of
    size 1 produced 40 live OS threads and 40 retained Job objects, which
    _evict_locked() will not drop because a queued job is correctly active.
    """
    import threading

    from rc_repro.errors import ConflictError
    from rc_repro.web.jobs import MAX_QUEUED_PER_KIND, JobManager

    jm = JobManager()
    gate = threading.Event()
    before = threading.active_count()

    def slow(emit=None):
        gate.wait(timeout=30)
        return {"ok": True}

    try:
        for _ in range(MAX_QUEUED_PER_KIND + 1):     # +1 is the one that runs
            jm.submit("capacity", slow, label="q")
        with pytest.raises(ConflictError, match="waiting for a free slot"):
            jm.submit("capacity", slow, label="one-too-many")
        grew = threading.active_count() - before
        assert grew <= MAX_QUEUED_PER_KIND + 1, \
            f"threads are still unbounded: {grew} new ones"
    finally:
        gate.set()

    # An UNPOOLED kind is untouched -- reads and state changes were never the
    # problem and must not start refusing.
    jm2 = JobManager()
    for _ in range(MAX_QUEUED_PER_KIND + 5):
        jm2.submit("state", lambda emit=None: None, label="s")


def test_changing_your_own_password_is_throttled_like_a_login():
    """It takes a password and derives scrypt on it, so it is a login endpoint
    whatever it is called. _do_signin claims "scrypt runs HERE and nowhere else,
    which is what makes a guessing bound possible at all" -- this endpoint quietly
    made that false, and any signed-in account, readonly included, could spend the
    threadpool on it without limit.
    """
    from rc_repro.web import app as webapp

    c = client()
    for _ in range(webapp.SIGNIN_MAX_FAILURES):
        r = c.post("/api/me/password",
                   json={"old": "not-my-password", "new": "a-brand-new-password"},
                   headers=H)
        assert r.status_code == 400, r.text        # ValidationError, still counted
    r = c.post("/api/me/password",
               json={"old": "not-my-password", "new": "a-brand-new-password"},
               headers=H)
    assert r.status_code == 429
    assert r.headers.get("Retry-After"), "a 429 that does not say when is not useful"


def test_a_finished_job_releases_its_result_but_keeps_its_summary():
    """A capacity result is ~118 KB of nested loadtest documents, and MAX_JOBS is a
    hundred -- 68.8 MB held for the life of the process, measured. The Activity list
    only ever renders summary(), which excludes the result, so a hundred summaries
    and ten results is the right shape.
    """
    from rc_repro.web.jobs import KEEP_RESULTS, JobManager

    jm = JobManager()
    for i in range(KEEP_RESULTS + 5):
        jm.submit("state", lambda emit=None, n=i: {"payload": "x" * 1000, "n": n},
                  label=f"j{i}")
    for jid in list(jm._jobs):
        jm._threads[jid].join(timeout=10)
    jm.submit("state", lambda emit=None: None, label="trigger")   # trims on submit

    kept = [j for j in jm._jobs.values() if j.result is not None]
    dropped = [j for j in jm._jobs.values() if j.result_dropped]
    assert len(dropped) == 5, f"expected 5 released, got {len(dropped)}"
    assert len(kept) <= KEEP_RESULTS

    # Everything the list view needs survives on a job whose result went.
    gone = dropped[0]
    assert gone.summary()["status"] == "done"
    assert gone.summary()["label"].startswith("j")
    assert gone.n_events > 0, "the progress trail is not what costs memory"


def test_releasing_a_result_drops_BOTH_references_to_it():
    """`job.result` and the terminal event's data["result"] are the SAME object,
    not a copy -- which is easy to misread, and clearing only one frees nothing."""
    from rc_repro.services.events import Event
    from rc_repro.web.jobs import Job

    job = Job(id="j", kind="capacity")
    payload = {"big": ["x"] * 100}
    job.result = payload
    job.status = "done"
    job.emit(Event("done", phase="done", terminal=True, data={"result": payload}))
    assert job.result is job.events[-1].data["result"], "premise of the test"

    job.forget_result()
    assert job.result is None
    assert "result" not in job.events[-1].data, "the second reference still held it"
    assert job.result_dropped is True


def test_the_job_endpoint_says_when_a_result_was_released(monkeypatch):
    c = client()
    jm = c.app.state.jobs
    job = jm.submit("state", lambda emit=None: {"ok": True}, label="x")
    jm._threads[job.id].join(timeout=10)
    assert c.get(f"/api/jobs/{job.id}", headers=H).json()["result"] == {"ok": True}

    job.forget_result()
    body = c.get(f"/api/jobs/{job.id}", headers=H).json()
    assert body["result"] is None
    assert body["result_dropped"] is True, \
        "an empty panel and a discarded one are indistinguishable without this"


def test_the_log_stream_drops_the_oldest_line_not_the_newest():
    """Which end gets dropped is the whole behaviour of the log panel.

    It used to discard the NEWEST: once a chatty container filled the queue, every
    line arriving from then on was thrown away while the viewer sat rendering a
    window from minutes ago, on a panel with "follow" ticked. And the cap was
    10,000 against a browser (app.js LOG_MAX) that keeps 3,000 -- so 7,000 of those
    held lines could never have been displayed even if they had survived.
    """
    import asyncio

    from rc_repro.web.app import WS_QUEUE_MAX, make_log_offer

    assert WS_QUEUE_MAX == 3_000, "keep this in step with LOG_MAX in app.js"

    q = asyncio.Queue(maxsize=WS_QUEUE_MAX)
    dropped = [0]
    offer = make_log_offer(q, dropped)

    for i in range(WS_QUEUE_MAX + 500):
        offer(f"line-{i}")

    assert q.qsize() == WS_QUEUE_MAX, "the bound still holds"
    assert dropped[0] == 500, "and it counts what it threw away, so it can say so"
    assert q.get_nowait() == "line-500", "the OLDEST went, not the newest"
    rest = [q.get_nowait() for _ in range(q.qsize())]
    assert rest[-1] == f"line-{WS_QUEUE_MAX + 499}", "the newest line survived"


def test_the_end_of_stream_sentinel_is_never_dropped():
    """It is what closes the socket. Losing it leaves the browser waiting on a
    stream that has already ended."""
    import asyncio

    from rc_repro.web.app import WS_QUEUE_MAX, make_log_offer

    q = asyncio.Queue(maxsize=WS_QUEUE_MAX)
    offer = make_log_offer(q, [0])
    for i in range(WS_QUEUE_MAX):
        offer(f"line-{i}")
    assert q.qsize() == WS_QUEUE_MAX, "full"
    offer(None)
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert items[-1] is None, "the sentinel was dropped by the overflow policy"
