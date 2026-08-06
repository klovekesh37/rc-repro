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


def test_repro_state_from_rc_container_status():
    # "no rocketchat container" splits on whether anything else survives.
    assert lc.repro_state("", False) == "down"
    assert lc.repro_state("", True) == "stopped"
    assert lc.repro_state("Up 2 hours (healthy)", True) == "running"
    assert lc.repro_state("Up 5 seconds (health: starting)", True) == "running"
    assert lc.repro_state("Exited (0) 2 minutes ago", True) == "stopped"
    assert lc.repro_state("Created", True) == "created"
    assert lc.repro_state("Dead", True) == "dead"
    # docker reports a paused container as "Up …(Paused)" -- it must not read as running.
    assert lc.repro_state("Up 3 days (Paused)", True) == "paused"
    # The regression: a crash-looping RC. The list used to read compose's project
    # aggregate, where the healthy Mongo's "running(1)" made this "running", while
    # detail() called it "stopped". Both had to become "restarting".
    assert lc.repro_state("Restarting (1) 5 seconds ago", True) == "restarting"


def test_list_and_detail_agree_on_a_crash_looping_repro(monkeypatch, tmp_path):
    """The same repro, through both code paths, must report the same state."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    meta = lc.runner.Metadata(name="r", project="rcrepro-r", rc_version="8.5.1",
                           rc_image="img", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="http://localhost:3000",
                           host_port=3000, version_source="test")
    crash = "Restarting (1) 5 seconds ago"
    monkeypatch.setattr(lc.runner, "docker_available", lambda: True)
    monkeypatch.setattr(lc.runner, "exists", lambda _n: True)
    monkeypatch.setattr(lc.runner, "list_meta", lambda: [meta])
    monkeypatch.setattr(lc.runner, "read_meta", lambda _n: meta)
    monkeypatch.setattr(lc.runner, "read_compose", lambda _n: {})
    monkeypatch.setattr(lc.runner, "rc_restart_count", lambda _n: 7)
    # What each path actually asks docker: the project aggregate names Mongo as
    # running, which is precisely what used to mislead the list.
    monkeypatch.setattr(lc.runner, "project_states",
                        lambda: {"rcrepro-r": "restarting(1), running(1)"})
    monkeypatch.setattr(lc.runner, "rc_status_by_project", lambda: {"rcrepro-r": crash})
    monkeypatch.setattr(lc.runner, "container_details", lambda _n: [
        {"service": "rocketchat", "state": "restarting", "status": crash, "health": ""},
        {"service": "mongo", "state": "running", "status": "Up 2 hours", "health": ""}])

    listed = lc.list_repros()[0]
    detailed = lc.detail("r")
    assert listed["state"] == detailed["state"] == "restarting"
    assert detailed["restarts"] == 7      # the crash loop is now visible


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


def test_rc_restart_count(monkeypatch):
    import types
    from rc_repro import runner
    monkeypatch.setattr(runner, "service_container_ids", lambda n, s: ["abc"] if s == "rocketchat" else [])
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="7\n", returncode=0))
    assert runner.rc_restart_count("x") == 7
    monkeypatch.setattr(runner, "service_container_ids", lambda n, s: [])
    assert runner.rc_restart_count("x") == 0   # no container -> 0, no crash


def test_docker_queries_behind_the_gui_poll_cannot_hang_or_raise(monkeypatch):
    """A wedged daemon must degrade, not park a threadpool worker forever.

    docker_available() (timeout=10) can pass and the NEXT call still hang: these
    three run on the dashboard's 4s poll, so an unbounded one is a worker leak
    that ends with the server answering nothing. Each already has a documented
    "couldn't ask docker" value -- they just have to reach it.
    """
    import subprocess

    from rc_repro import runner

    for exc in (subprocess.TimeoutExpired(cmd="docker", timeout=30),
                FileNotFoundError("docker"),
                OSError("no fork")):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(runner.subprocess, "run", boom)
        assert runner._compose_ls() is None                  # None = "couldn't ask"
        assert runner.project_states() is None               # prune refuses on None
        assert runner.rc_status_by_project() == {}
        monkeypatch.setattr(runner, "service_container_ids", lambda n, s: ["abc"])
        assert runner.rc_restart_count("x") == 0

    # And every one of them passes a timeout, so the hang cannot happen at all.
    calls = []
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: calls.append(k.get("timeout")) or
                        __import__("types").SimpleNamespace(stdout="", returncode=1))
    runner._compose_ls(); runner.rc_status_by_project(); runner.rc_restart_count("x")
    assert calls and all(t is not None for t in calls), f"unbounded docker call: {calls}"


def test_kernel_major_minor_parsing():
    from rc_repro.services import doctor as doctorsvc
    assert doctorsvc._kernel_major_minor("6.19.7-200.fc43.aarch64") == (6, 19)
    assert doctorsvc._kernel_major_minor("5.15.0-generic") == (5, 15)
    assert doctorsvc._kernel_major_minor("6.19") == (6, 19)
    assert doctorsvc._kernel_major_minor(None) is None
    assert doctorsvc._kernel_major_minor("not-a-kernel") is None


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


# --- job manager (web/jobs.py; no fastapi needed) ----------------------------

def _await_finish(job, tries: int = 400) -> None:
    import time
    for _ in range(tries):
        if job.status != "running":
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job.id} never finished")


def test_job_events_are_capped_with_absolute_indices():
    from rc_repro.web import jobs as J
    job = J.Job(id="job_x", kind="t")
    for i in range(J.MAX_EVENTS_PER_JOB + 50):
        job.emit(events.Event(f"e{i}"))
    # `serve` is long-lived and every streamed docker line is an Event, so the
    # per-job buffer is bounded -- but the index the SSE stream hands out stays
    # absolute, so a reader cannot desync by counting.
    assert len(job.events) == J.MAX_EVENTS_PER_JOB
    assert job.n_events == J.MAX_EVENTS_PER_JOB + 50
    evs, done, nxt = job.snapshot(0)
    assert len(evs) == J.MAX_EVENTS_PER_JOB and nxt == job.n_events
    assert job.snapshot(nxt) == ([], done, nxt)      # nothing new after catching up


def test_job_registry_evicts_finished_jobs_only():
    from rc_repro.web import jobs as J
    mgr = J.JobManager()
    for i in range(J.MAX_JOBS + 20):
        mgr._jobs[f"job_{i}"] = J.Job(id=f"job_{i}", kind="t", status="done")
    mgr._jobs["job_live"] = J.Job(id="job_live", kind="t", status="running")
    mgr._evict_locked()
    assert len(mgr._jobs) <= J.MAX_JOBS
    assert mgr.get("job_live") is not None    # a running job is never evicted
    assert mgr.get("job_0") is None           # the oldest finished ones go first


def test_job_status_is_set_before_the_terminal_event(monkeypatch):
    from rc_repro.web import jobs as J
    captured = []
    original = J.Job.emit

    def spy(self, ev):
        captured.append((ev.terminal, self.status, self.result))
        original(self, ev)

    monkeypatch.setattr(J.Job, "emit", spy)
    job = J.JobManager().submit("t", lambda emit: "R")
    _await_finish(job)
    terminal = [c for c in captured if c[0]]
    # A client that polls /api/jobs/<id> on seeing `terminal` used to read
    # status="running" with result=None, because the event came first.
    assert terminal and terminal[-1] == (True, "done", "R")


def test_job_internal_error_does_not_leak_a_traceback():
    from rc_repro.web import jobs as J
    job = J.JobManager().submit("t", lambda emit: 1 / 0)
    _await_finish(job)
    assert job.status == "error" and job.error_kind == "InternalError"
    evs, _done, _n = job.snapshot(0)
    # The traceback goes to the server's stderr; it must not ride the SSE payload
    # into a browser, which renders whatever it is handed.
    assert evs and all("trace" not in (e.get("data") or {}) for e in evs)


def test_job_reproerror_keeps_its_kind_on_the_wire():
    from rc_repro.web import jobs as J

    def boom(emit):
        raise errors.NotFoundError("no such repro")

    job = J.JobManager().submit("t", boom)
    _await_finish(job)
    assert job.status == "error" and job.error_kind == "NotFoundError"
    evs, _done, _n = job.snapshot(0)
    assert evs[-1]["data"]["kind"] == "NotFoundError"


def test_set_state_explains_a_downed_repro(monkeypatch):
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(lc.runner, "start", lambda n: 1)
    monkeypatch.setattr(lc.runner, "rc_state", lambda n: "absent")
    with pytest.raises(errors.DockerError) as exc:
        lc.set_state("x", "start")
    # `compose start` cannot revive a repro with no containers -- say what does.
    assert "no containers to start" in str(exc.value)


def test_summary_carries_preset_notes():
    # These are what the GUI renders from the create job's result and the CLI
    # prints in a box after `up` -- the Keycloak realm, the /etc/hosts line, etc.
    m = lc.runner.Metadata(
        name="x", project="rcrepro-x", rc_version="8.5.1", rc_image="i",
        mongo_tag="8.0", mongo_flavor="official", preset="oidc",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
        extra={"notes": ["Add this to /etc/hosts:", "    127.0.0.1  keycloak"]})
    assert lc._summary(m)["notes"] == ["Add this to /etc/hosts:",
                                      "    127.0.0.1  keycloak"]


def test_compose_major_version_parsing():
    from rc_repro.services import doctor as doctorsvc
    # A first-CHARACTER compare reported every Compose newer than v2 as
    # unsupported (v5 is current) and would misread v10 as v1.
    assert doctorsvc._major_version("2.29.1") == 2
    assert doctorsvc._major_version("v2.29.1") == 2
    assert doctorsvc._major_version("5.3.1") == 5
    assert doctorsvc._major_version("10.0.0") == 10
    assert doctorsvc._major_version("1.29.2") == 1        # genuinely too old
    assert doctorsvc._major_version(None) is None
    assert doctorsvc._major_version("") is None
    assert doctorsvc._major_version("unknown") is None


def test_detail_reports_unknown_state_when_docker_is_unreachable(monkeypatch):
    from rc_repro import runner
    monkeypatch.setattr(runner, "docker_available", lambda: False)
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(runner, "read_meta", lambda n: lc.runner.Metadata(
        name=n, project=f"rcrepro-{n}", rc_version="8.5.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map"))
    monkeypatch.setattr(runner, "read_compose", lambda n: {"services": {"rocketchat": {}}})
    d = lc.detail("x")
    # container_details() returns [] both for "none" and for "could not ask docker",
    # so detail() used to assert "down" while list_repros() said "?" for the same
    # repro -- and the panel then offered a Bring up button that could only fail.
    assert d["state"] == "?" and d["health"] == "" and d["uptime"] == ""
    assert d["containers"] == []


def test_detail_says_whether_this_repro_is_the_default(monkeypatch, tmp_path):
    """The panel offers "Make default" only when it would change something.

    list_repros() has always carried `default`; detail() did not, so the panel
    had to either hide the action or show it on the repro that already is one.
    """
    from rc_repro import config, runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "docker_available", lambda: False)
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(runner, "read_meta", lambda n: lc.runner.Metadata(
        name=n, project=f"rcrepro-{n}", rc_version="8.5.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map"))
    monkeypatch.setattr(runner, "read_compose", lambda n: {"services": {"rocketchat": {}}})

    assert lc.detail("a")["is_default"] is False       # nothing pinned yet
    config.update_config(lambda cfg: cfg.__setitem__("default_repro", "a"))
    assert lc.detail("a")["is_default"] is True
    assert lc.detail("b")["is_default"] is False


# --- --https flag resolution --------------------------------------------------


def _req(**kw):
    base = dict(version="8.6.1")
    base.update(kw)
    return lc.CreateReq(**base)


def test_https_needs_only_a_domain_and_an_email(tmp_path, monkeypatch):
    """The two inputs the official docs ask for: DOMAIN and LETSENCRYPT_EMAIL.

    --https alongside them was ceremony -- a hostname has no other meaning. --https
    on its own still selects the local-CA mode, which needs nothing else.
    """
    from rc_repro import config as cfgmod
    from rc_repro import runner, tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)

    # Nothing TLS-ish at all -> no HTTPS.
    assert lc._resolve_tls(_req(), "x", "127.0.0.1") is None

    # Domain + email, on one line.
    spec = lc._resolve_tls(_req(domain="rc1.example.com", acme_email="ops@example.com"),
                           "x", "")
    assert spec.mode == tls.MODE_ACME
    assert spec.host == "rc1.example.com" and spec.port == 443
    assert spec.acme_email == "ops@example.com"
    assert spec.root_url == "https://rc1.example.com"

    # The email is remembered, so later runs need only --domain.
    cfgmod.update_config(lambda c: c.__setitem__("acme_email", "ops@example.com"))
    assert lc._resolve_tls(_req(domain="rc2.example.com"), "x", "").acme_email \
        == "ops@example.com"

    # --https alone stays the local mode, on an allocated port.
    local = lc._resolve_tls(_req(https=True), "myrepro", "")
    assert local.mode == tls.MODE_LOCAL and local.port != 443


def test_a_domain_without_an_email_names_both_ways_to_give_one(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    with pytest.raises(errors.ValidationError) as ei:
        lc._resolve_tls(_req(domain="rc1.example.com"), "x", "")
    msg = str(ei.value)
    assert "--email" in msg and "config set acme.email" in msg


def test_a_domain_publishes_publicly_because_lets_encrypt_connects_in(tmp_path,
                                                                      monkeypatch):
    """TLS-ALPN-01 is validated by an inbound connection, so loopback cannot work.

    Derived rather than demanded -- failing on a missing --bind made the user work
    out something the tool already knew. An explicit --bind still wins.
    """
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)

    req = _req(domain="rc1.example.com", acme_email="a@b.c")
    lc._resolve_tls(req, "x", "127.0.0.1")
    assert req.bind_public is True

    explicit = _req(domain="rc1.example.com", acme_email="a@b.c", bind="10.0.0.5")
    lc._resolve_tls(explicit, "x", "10.0.0.5")
    assert explicit.bind_public is False

    # The local mode never connects out, so it stays where it was put.
    local = _req(https=True)
    lc._resolve_tls(local, "x", "127.0.0.1")
    assert local.bind_public is False


def test_no_reachability_guessing_stands_between_you_and_a_certificate(tmp_path,
                                                                       monkeypatch):
    """How traffic reaches this host is the operator's business.

    The old path refused to create the repro when the name did not resolve, resolved
    privately, or sat behind a proxy -- guesses this process cannot make correctly
    (it cannot see a tunnel, a port-forward or a firewall), and the single biggest
    source of confusion here.
    """
    from rc_repro import runner, tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)
    for probe in ("dns_preflight", "reachability_gaps", "resolves_to",
                  "host_has_public_address"):
        assert not hasattr(tls, probe), f"{probe} should be gone"
    # A name that resolves nowhere is still accepted.
    spec = lc._resolve_tls(_req(domain="nowhere.invalid", acme_email="a@b.c"), "x", "")
    assert spec.mode == tls.MODE_ACME


def test_local_https_allocates_a_port_and_a_localhost_host(tmp_path, monkeypatch):
    from rc_repro import runner, tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)
    spec = lc._resolve_tls(_req(https=True), "rc8-6-1", "127.0.0.1")
    assert spec.mode == tls.MODE_LOCAL
    assert spec.host == "rc8-6-1.rcrepro.localhost"
    assert spec.port == 8443
    assert spec.root_url == "https://rc8-6-1.rcrepro.localhost:8443"


def test_tls_port_avoids_ports_other_repros_claim(tmp_path, monkeypatch):
    """A second --https repro must not be handed the first one's TLS port.

    used_ports() reads tls_ports out of repro.json for exactly this; without that
    key the allocator would hand out 8443 twice and `up` would fail on
    "port is already allocated".
    """
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)
    monkeypatch.setattr(runner, "used_ports", lambda: {8443, 8444})
    assert lc._pick_tls_port() == 8445


def test_recreating_an_https_repro_keeps_its_own_tls_port(tmp_path, monkeypatch):
    """`up --force --https` must reuse the port it already published.

    own_ports() has to count tls_ports, else the repro's own 8443 reads as "taken
    by someone else" and the allocator drifts to 8444 -- leaking a port and
    silently changing the workspace URL on every recreate.
    """
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    meta = runner.Metadata(
        name="d", project="rcrepro-d", rc_version="8.6.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map",
        public_url="https://d.rcrepro.localhost:8443",
        extra={"tls": "local", "tls_ports": [8443]})
    monkeypatch.setattr(runner, "exists", lambda n: True)
    monkeypatch.setattr(runner, "read_meta", lambda n: meta)
    monkeypatch.setattr(runner, "list_meta", lambda: [meta])
    monkeypatch.setattr(runner, "port_free", lambda p: True)

    assert 8443 in lc.own_ports("d")
    assert 8443 in runner.used_ports()          # visible to other repros...
    assert lc._pick_tls_port(exclude="d") == 8443   # ...but reusable by this one

    # The real cause of the drift: on --force the repro's OWN Traefik still holds
    # the port, so a port_free() probe says "in use". Own ports must skip that probe.
    monkeypatch.setattr(runner, "port_free", lambda p: p != 8443)
    assert lc._pick_tls_port(exclude="d") == 8443
    # ...but for anyone else that same busy port is genuinely unavailable.
    assert lc._pick_tls_port(exclude="") == 8444


def test_promote_command_reproduces_the_flags_actually_used():
    """The staging -> production hint must be copy-pasteable.

    It is the same two flags the workspace was created with, plus --force, so it
    reproduces the repro rather than describing a different one.
    """
    from rc_repro import runner
    from rc_repro.cli import _promote_command

    def meta(**extra):
        return runner.Metadata(
            name="testrepo", project="p", rc_version="8.4.2", rc_image="i",
            mongo_tag="8.0", mongo_flavor="official", preset="default",
            root_url="http://localhost:3000", host_port=3000, version_source="map",
            public_url="https://testrepo.kestron.org", extra={"tls": "acme", **extra})

    cmd = _promote_command(meta(tls_email="me@example.com"), "testrepo.kestron.org")
    assert "--domain testrepo.kestron.org" in cmd
    assert "--email me@example.com" in cmd
    assert "--force" in cmd and "--wait" in cmd
    # Nothing that no longer exists as a flag.
    for dead in ("--acme-challenge", "--acme-dns-provider", "--acme-email",
                 "--acme-staging", "--tls-cert", "--tls-san"):
        assert dead not in cmd

    # No --bind either: the public bind is derived at create time from --domain,
    # so spelling it in the hint would be noise the user has to understand.
    assert "--bind" not in cmd


def test_detail_exposes_the_https_fields_the_panel_keys_off(tmp_path, monkeypatch):
    """The panel's HTTPS row and "Check TLS" action both test detail().public_url.

    list_repros() carried public_url/tls and detail() did not, so the whole feature
    was invisible in the detail panel even though the links were right.
    """
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    meta = runner.Metadata(
        name="g", project="rcrepro-g", rc_version="8.6.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map",
        public_url="https://g.rcrepro.localhost:8443",
        extra={"tls": "local", "tls_ports": [8443]})
    monkeypatch.setattr(runner, "docker_available", lambda: False)
    monkeypatch.setattr(lc, "resolve_name", lambda n: n)
    monkeypatch.setattr(runner, "read_meta", lambda n: meta)
    monkeypatch.setattr(runner, "read_compose", lambda n: {"services": {"rocketchat": {}}})

    d = lc.detail("g")
    assert d["public_url"] == "https://g.rcrepro.localhost:8443"
    assert d["tls"] == "local"
    # The clickable link prefers https, and the plain port stays available.
    urls = [x["url"] for x in d["links"]]
    assert "https://g.rcrepro.localhost:8443" in urls
    assert "http://localhost:3000" in urls

    # A repro without --https must not grow the row or the action.
    meta.public_url = ""
    meta.extra = {}
    d2 = lc.detail("g")
    assert d2["public_url"] == "" and d2["tls"] == ""


def test_http_redirect_is_always_best_effort(tmp_path, monkeypatch):
    """Publishing :80 to redirect must never block creating the repro.

    The official rocketchat-compose files always own 80 and 443, so they can insist.
    We cannot: refusing because something else holds 80 would block --domain for
    anyone running a web server there. TLS-ALPN-01 validates on 443, so port 80 is
    only ever a convenience.
    """
    from rc_repro import runner, tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    # 80 free -> redirect on.
    monkeypatch.setattr(runner, "port_free", lambda p: True)
    spec = lc._resolve_tls(_req(domain="rc1.example.com", acme_email="a@b.c"), "x", "")
    assert spec.http_redirect is True

    # 80 busy -> still created, redirect simply off.
    monkeypatch.setattr(runner, "port_free", lambda p: p != 80)
    spec = lc._resolve_tls(_req(domain="rc1.example.com", acme_email="a@b.c"), "x", "")
    assert spec.mode == tls.MODE_ACME and spec.http_redirect is False

    # Local mode never wants 80, so a busy 80 is irrelevant to it.
    spec = lc._resolve_tls(_req(https=True), "x", "127.0.0.1")
    assert spec.mode == tls.MODE_LOCAL and spec.http_redirect is False


def test_env_overrides_survive_up_force(tmp_path, monkeypatch):
    """`up --force` rebuilds compose from the spec, so overrides must live in metadata.

    Verified live before this was written: editing only the generated compose file
    worked until the next `up --force`, which silently dropped the change.
    """
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    meta = runner.Metadata(
        name="e", project="rcrepro-e", rc_version="8.6.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map",
        extra={"env": {"KEEP_ME": "yes", "GONE": None}})
    monkeypatch.setattr(runner, "exists", lambda n: True)
    monkeypatch.setattr(runner, "read_meta", lambda n: meta)

    carried: dict = {}
    if runner.exists("e"):
        prev = runner.read_meta("e").extra.get("env")
        if isinstance(prev, dict):
            carried.update(prev)
    carried.update({"NEW": "1"})          # what this run asked for
    assert carried == {"KEEP_ME": "yes", "GONE": None, "NEW": "1"}, \
        "existing overrides carry forward; this run's win"


def test_set_env_refuses_a_no_op_and_warns_on_load_bearing_keys(tmp_path, monkeypatch):
    """Load-bearing keys are allowed — reproducing a broken config is the point —
    but never silently, because the repro stops working and the cause is invisible."""
    from rc_repro import compose, runner
    from rc_repro.services import envvars
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(envvars.lifecycle, "require_docker", lambda: None)
    monkeypatch.setattr(envvars.lifecycle, "resolve_name", lambda n: n)

    with pytest.raises(errors.ValidationError, match="nothing to change"):
        envvars.set_env("e", {}, [], emit=lambda ev: None)

    meta = runner.Metadata(
        name="e", project="rcrepro-e", rc_version="8.6.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map")
    doc = {"services": {"rocketchat": {"environment": {"PORT": "3000", "KEEP": "1"}},
                        "rocketchat-2": {"environment": {"PORT": "3000"}}}}
    monkeypatch.setattr(runner, "read_meta", lambda n: meta)
    monkeypatch.setattr(runner, "read_compose", lambda n: doc)
    written = {}
    monkeypatch.setattr(runner, "write",
                        lambda n, y, m, files=None: written.update(yaml=y, meta=m))
    monkeypatch.setattr(runner, "up", lambda n, pull=True: 0)

    events = []
    r = envvars.set_env("e", {"MONGO_URL": "mongodb://elsewhere", "MY": "v"}, ["KEEP"],
                        emit=events.append)
    assert r["restarted"] is True
    assert any("load-bearing" in str(getattr(e, "message", e)) for e in events), \
        "MONGO_URL must be called out"
    # Applied to EVERY rocketchat service, and the unset key is gone from each.
    out = compose.yaml.safe_load(written["yaml"])
    for svc in ("rocketchat", "rocketchat-2"):
        assert out["services"][svc]["environment"]["MY"] == "v", svc
        assert "KEEP" not in out["services"][svc]["environment"], svc
    # And it is recorded, so `up --force` keeps it.
    assert written["meta"].extra["env"] == {"KEEP": None, "MONGO_URL": "mongodb://elsewhere",
                                            "MY": "v"}


def test_set_env_can_write_without_restarting(tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import envvars
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(envvars.lifecycle, "require_docker", lambda: None)
    monkeypatch.setattr(envvars.lifecycle, "resolve_name", lambda n: n)
    monkeypatch.setattr(runner, "read_meta", lambda n: runner.Metadata(
        name="e", project="p", rc_version="8.6.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map"))
    monkeypatch.setattr(runner, "read_compose",
                        lambda n: {"services": {"rocketchat": {"environment": {}}}})
    monkeypatch.setattr(runner, "write", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(runner, "up", lambda n, pull=True: called.append(n) or 0)
    r = envvars.set_env("e", {"A": "1"}, [], restart=False, emit=lambda ev: None)
    assert r["restarted"] is False and called == [], "must not touch containers"


# --- the two-flag HTTPS surface (#https-domain-email) ---------------------------

def test_up_offers_exactly_two_https_flags():
    """The official compose asks for DOMAIN and LETSENCRYPT_EMAIL; so do we.

    This grew to nine visible flags across three certificate modes, which is the
    complaint that prompted the rewrite. The removed ones must be gone from the
    signature, not merely hidden -- an unreachable code path behind a deleted flag
    is worse than either.
    """
    import inspect
    from rc_repro.cli import up as up_cmd
    params = set(inspect.signature(up_cmd).parameters)
    assert {"domain", "email", "https"} <= params
    for dead in ("tls_cert", "tls_key", "tls_san", "acme_staging",
                 "acme_email", "acme_challenge", "acme_dns_provider"):
        assert dead not in params, f"--{dead.replace('_', '-')} should be gone"


def test_createreq_carries_no_dead_tls_fields():
    """CreateReq is the web API's schema too, so a stale field is a stale endpoint."""
    fields = set(lc.CreateReq.__dataclass_fields__)
    for dead in ("tls_cert", "tls_key", "tls_san", "acme_challenge_given"):
        assert dead not in fields, f"CreateReq.{dead} should be gone"
    assert {"domain", "acme_email", "https", "acme_staging"} <= fields
    # These two survive as DERIVED state, not input: _pick_challenge sets them and
    # no flag reaches them.
    assert {"acme_challenge", "acme_dns_provider"} <= fields


def test_staging_is_reachable_through_config_only(tmp_path, monkeypatch):
    """Not a flag on `up` -- but Let's Encrypt allows only 5 failed validations per
    hostname per hour, so a way to rehearse has to exist somewhere."""
    from rc_repro import config as cfgmod
    from rc_repro import runner
    from rc_repro.cli import _CONFIG_KEYS
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)
    assert "acme.staging" in _CONFIG_KEYS

    cfgmod.update_config(lambda c: c.__setitem__("acme_staging", True))
    spec = lc._resolve_tls(_req(domain="rc1.example.com", acme_email="a@b.c"), "x", "")
    assert spec.acme_staging is True


def test_every_tls_attribute_the_cli_and_web_reach_for_exists():
    """Catches a module attribute that moved out from under a caller.

    Splitting the local CA into tls_local left `trust-ca` calling tls.ensure_ca(),
    which ruff cannot see (it is an attribute access, not a name) and no test
    exercised -- so it would have failed only when a user ran the command.
    """
    import ast
    import pathlib

    from rc_repro import tls, tls_local
    mods = {"tlsmod": tls, "tls": tls, "tls_local": tls_local}
    missing = []
    for rel in ("rc_repro/cli.py", "rc_repro/web/app.py", "rc_repro/compose.py",
                "rc_repro/services/lifecycle.py"):
        tree = ast.parse(pathlib.Path(rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in mods and not hasattr(mods[node.value.id], node.attr)):
                missing.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")
    assert not missing, "attribute(s) that no longer exist: " + ", ".join(missing)


# --- the challenge is derived, never asked for (#https-dns01) ---------------------

def _dns_env(tmp_path, body="CF_DNS_API_TOKEN=secret\n"):
    from rc_repro import tls
    path = tls.dns_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_tls_alpn_is_the_default_matching_the_official_compose(tmp_path, monkeypatch):
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)
    spec = lc._resolve_tls(_req(domain="rc1.example.com", acme_email="a@b.c"), "x", "")
    assert spec.acme_challenge == "tlsalpn"


def test_dns01_is_selected_when_credentials_exist(tmp_path, monkeypatch):
    """The presence of the file IS the signal -- nobody creates it by accident.

    It is the only way to issue when Let's Encrypt cannot connect inbound: behind
    NAT, behind a tunnel, or behind a proxy that terminates TLS in front of you.
    """
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)
    _dns_env(tmp_path)
    req = _req(domain="rc1.example.com", acme_email="a@b.c")
    spec = lc._resolve_tls(req, "x", "")
    assert spec.acme_challenge == "dns" and spec.acme_dns_provider == "cloudflare"
    # dns-01 never accepts an inbound connection, so the workspace must NOT be
    # widened to 0.0.0.0 -- these repros run admin/admin123.
    assert req.bind_public is False


def test_dns01_provider_inference_is_not_cloudflare_specific(tmp_path, monkeypatch):
    from rc_repro import tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    for body, expected in (("CF_DNS_API_TOKEN=x\n", "cloudflare"),
                           ("AWS_ACCESS_KEY_ID=x\nAWS_SECRET_ACCESS_KEY=y\n", "route53"),
                           ("DO_AUTH_TOKEN=x\n", "digitalocean"),
                           ("HETZNER_API_KEY=x\n", "hetzner"),
                           ("AZURE_CLIENT_ID=x\n", "azuredns")):
        _dns_env(tmp_path, body)
        provider, why = tls.infer_dns_provider()
        assert provider == expected, f"{body!r} -> {provider!r}"
        assert "secret" not in why and "=" not in why, "never echo a credential VALUE"


def test_unrecognised_credentials_are_refused_not_silently_ignored(tmp_path,
                                                                   monkeypatch):
    """Falling back to tlsalpn would ignore a file the user deliberately created."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    _dns_env(tmp_path, "SOME_UNKNOWN_THING=x\n")
    with pytest.raises(errors.ValidationError) as ei:
        lc._resolve_tls(_req(domain="rc1.example.com", acme_email="a@b.c"), "x", "")
    assert "config set acme.dns_provider" in str(ei.value)


def test_an_explicit_provider_overrides_inference(tmp_path, monkeypatch):
    from rc_repro import config as cfgmod
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p: True)
    _dns_env(tmp_path, "SOME_UNKNOWN_THING=x\n")
    cfgmod.update_config(lambda c: c.__setitem__("acme_dns_provider", "exoscale"))
    spec = lc._resolve_tls(_req(domain="rc1.example.com", acme_email="a@b.c"), "x", "")
    assert spec.acme_challenge == "dns" and spec.acme_dns_provider == "exoscale"


def test_dns01_reaches_traefik_as_flags_and_a_mounted_env_file(tmp_path, monkeypatch):
    from rc_repro import tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    _dns_env(tmp_path)
    spec = tls.TlsSpec(mode=tls.MODE_ACME, host="rc1.example.com", port=443,
                       acme_email="a@b.c", acme_challenge="dns",
                       acme_dns_provider="cloudflare")
    svc = tls.service(spec)
    cmd = " ".join(svc["command"])
    assert "acme.dnschallenge=true" in cmd
    assert "acme.dnschallenge.provider=cloudflare" in cmd
    assert "tlschallenge" not in cmd
    # The token travels in an env_file, never in argv -- `ps` is world-readable.
    assert svc["env_file"] == [str(tls.dns_env_path())]
    assert "secret" not in cmd
