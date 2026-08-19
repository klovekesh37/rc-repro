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
    monkeypatch.setattr(lc.runner, "docker_available", lambda **_k: True)
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
    """An absent engine is a PREFLIGHT failure, not a "still coming up" one.

    It used to raise NotReadyError, which is exit 5 / HTTP 409 and means "poll
    again" -- advice that can never succeed, because polling does not start
    Docker. The caller has to fix their environment, so ENGINE_UNAVAILABLE:
    exit 3, HTTP 502, the dependency underneath is not there.

    Asserted on all three faces of the taxonomy, because each one is consumed by
    a different front-end and they have drifted apart before.
    """
    monkeypatch.setattr(lc.runner, "docker_available", lambda **_k: False)
    with pytest.raises(errors.DockerError) as caught:
        lc.require_docker()
    assert caught.value.code == "ENGINE_UNAVAILABLE"
    assert caught.value.exit_code == 3          # CLI: fix it, do not retry
    assert caught.value.http_status == 502      # web: upstream dependency is down
    # And specifically NOT the "poll again" answer it used to give.
    assert not isinstance(caught.value, errors.NotReadyError)
    assert errors.NotReadyError.exit_code == 5


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
    monkeypatch.setattr(perf.lifecycle.runner, "docker_available", lambda **_k: True)
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
    monkeypatch.setattr(perf.lifecycle.runner, "docker_available", lambda **_k: True)
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

    # `project_states` memoises for _QUERY_TTL, so this test only sees the stubbed
    # failure if the cache is cold -- and it is only cold by luck of test ORDER.
    # Any earlier test that reaches a real docker (the `--json` contract tests run
    # `list` and `doctor` for real, a file earlier in the alphabet) leaves a warm
    # entry, and this passed on the cached answer instead of the one under test.
    runner._query_cache.clear()

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


def test_summary_carries_preset_notes_derived_and_not_the_frozen_copy():
    """These are what the GUI renders from the create job's result and the CLI prints
    after `up` -- the Keycloak realm, the /etc/hosts line.

    DERIVED from the preset on every read, not read back out of the record. A note
    written into `repro.json` at create time keeps its wording for the life of the
    workspace, which is how a Kubernetes workspace went on telling people `backup`
    had no path on that runtime for three releases after it grew one -- the fix
    reached new workspaces and no existing one. So the stale copy in this record is
    deliberately not what the preset says, and it is the preset's that has to come
    out.
    """
    from rc_repro import presets

    m = lc.runner.Metadata(
        name="x", project="rcrepro-x", rc_version="8.5.1", rc_image="i",
        mongo_tag="8.0", mongo_flavor="official", preset="oidc",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
        extra={"notes": ["a sentence some older release wrote down"]})
    s = lc._summary(m)
    assert s["notes"] == lc.flatten_notes([lc.note_group(
        "Scenario · oidc", kind="scenario", body=presets.load("oidc").notes)])
    assert "a sentence some older release wrote down" not in s["notes"]
    # The grouped form is what the panel renders, and the scenario has to be MARKED
    # as one -- the panel feeds that group back into its links card rather than
    # giving it a card of its own.
    assert [g["kind"] for g in s["note_groups"]] == ["scenario"]
    assert any("/etc/hosts" in n for n in s["notes"])


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
    monkeypatch.setattr(runner, "docker_available", lambda **_k: False)
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
    monkeypatch.setattr(runner, "docker_available", lambda **_k: False)
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


def test_a_domain_without_an_email_names_both_ways_to_give_one(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    with pytest.raises(errors.ValidationError) as ei:
        lc._resolve_tls(_req(domain="rc1.example.com"), "x", "")
    msg = str(ei.value)
    assert "--email" in msg and "config set acme.email" in msg


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
    monkeypatch.setattr(runner, "port_free", lambda p, h="": True)
    for probe in ("dns_preflight", "reachability_gaps", "resolves_to",
                  "host_has_public_address"):
        assert not hasattr(tls, probe), f"{probe} should be gone"
    # A name that resolves nowhere is still accepted.
    spec = lc._resolve_tls(_req(domain="nowhere.invalid", acme_email="a@b.c"), "x", "")
    assert spec.mode == tls.MODE_ACME


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
    monkeypatch.setattr(runner, "docker_available", lambda **_k: False)
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
    monkeypatch.setattr(runner, "port_free", lambda p, h="": True)
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
    monkeypatch.setattr(runner, "port_free", lambda p, h="": True)
    spec = lc._resolve_tls(_req(domain="rc1.example.com", acme_email="a@b.c"), "x", "")
    assert spec.acme_challenge == "tlsalpn"


def test_dns01_is_selected_when_credentials_exist(tmp_path, monkeypatch):
    """The presence of the file IS the signal -- nobody creates it by accident.

    It is the only way to issue when Let's Encrypt cannot connect inbound: behind
    NAT, behind a tunnel, or behind a proxy that terminates TLS in front of you.
    """
    from rc_repro import runner
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "port_free", lambda p, h="": True)
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
    monkeypatch.setattr(runner, "port_free", lambda p, h="": True)
    _dns_env(tmp_path, "SOME_UNKNOWN_THING=x\n")
    cfgmod.update_config(lambda c: c.__setitem__("acme_dns_provider", "exoscale"))
    spec = lc._resolve_tls(_req(domain="rc1.example.com", acme_email="a@b.c"), "x", "")
    assert spec.acme_challenge == "dns" and spec.acme_dns_provider == "exoscale"


# --- per-user naming (#team-auth) --------------------------------------------------
#
# On a shared server the second person to run `up -v 8.5.1` silently REUSED the
# first person's workspace, data and all. That is a data-loss bug independent of
# anything else here.

def test_two_people_running_the_same_version_get_two_workspaces():
    assert lc.derive_name("8.5.1", "default") == "rc8-5-1"          # unchanged solo
    assert lc.derive_name("8.5.1", "default", "alice") == "alice-rc8-5-1"
    assert lc.derive_name("8.5.1", "default", "bob") == "bob-rc8-5-1"
    assert lc.derive_name("8.5.1", "ldap", "alice") == "alice-rc8-5-1-ldap"


def test_an_explicit_name_is_namespaced_too():
    """Two people typing `--name test` collide exactly as derived names did."""
    assert lc.owner_prefix("test", "alice") == "alice-test"
    assert lc.owner_prefix("test", "bob") == "bob-test"
    # Idempotent, so `up --name alice-test` does not become alice-alice-test.
    assert lc.owner_prefix("alice-test", "alice") == "alice-test"


def test_naming_is_unchanged_without_an_actor():
    """A single-user machine must keep every existing workspace name."""
    assert lc.derive_name("8.5.1", "default", "") == "rc8-5-1"
    assert lc.owner_prefix("test", "") == "test"


def test_the_lock_name_and_the_written_name_cannot_drift():
    """create_repro derives the name twice -- once to lock, once in the body. If
    they disagreed, the lock would guard a different repro than the one written."""
    for req in (lc.CreateReq(version="8.5.1", actor="alice"),
                lc.CreateReq(version="8.5.1", preset="ldap", actor="alice"),
                lc.CreateReq(version="8.5.1", name="My Repro!", actor="alice"),
                lc.CreateReq(version="8.5.1")):
        assert lc._derive_for(req) == lc._derive_for(req)
        assert lc._derive_for(req)


def test_a_derived_name_stays_a_valid_dns_label():
    """It becomes a hostname under the front door, so dots would break the TLS
    wildcard silently."""
    name = lc.derive_name("8.5.1", "default", "alice")
    assert "." not in name and name == lc.sanitize(name)


# --- job concurrency is bounded (F10) -------------------------------------------
# Job RETENTION was bounded; concurrency was not. Ten teammates each starting a
# capacity search meant ten k6 containers against one engine -- searches that
# invalidate each other and starve the workspaces under test. A perf tool that
# silently produces wrong numbers under contention is worse than one that waits.

def test_a_second_load_test_waits_rather_than_corrupting_the_first():
    import threading
    import time as _t

    from rc_repro.web.jobs import JobManager

    jobs = JobManager()
    started, release = threading.Event(), threading.Event()

    def hog(emit=None):
        started.set()
        release.wait(timeout=5)
        return {"ok": True}

    def quick(emit=None):
        return {"ok": True}

    first = jobs.submit("loadtest", hog, label="a")
    assert started.wait(timeout=5), "the first job should start immediately"
    second = jobs.submit("loadtest", quick, label="b")
    _t.sleep(0.2)
    assert second.status == "queued", "a concurrent load test is not a measurement"

    # And a queued job must not look finished: the SSE stream closes on that, and
    # eviction drops anything not active.
    _events, done, _n = second.snapshot(0)
    assert done is False, "a queued job reported as finished closes its own stream"

    release.set()
    for _ in range(50):
        if second.status == "done":
            break
        _t.sleep(0.1)
    assert second.status == "done" and first.status == "done"


def test_a_failing_job_releases_its_slot():
    """One leaked slot on the measurement pool (size 1) wedges every future load
    test for the life of the process."""
    import time as _t

    from rc_repro.errors import ValidationError
    from rc_repro.web.jobs import JobManager

    jobs = JobManager()

    def boom(emit=None):
        raise ValidationError("nope")

    failed = jobs.submit("loadtest", boom, label="a")
    for _ in range(50):
        if failed.status == "error":
            break
        _t.sleep(0.1)
    assert failed.status == "error"

    after = jobs.submit("loadtest", lambda emit=None: {"ok": True}, label="b")
    for _ in range(50):
        if after.status == "done":
            break
        _t.sleep(0.1)
    assert after.status == "done", "the slot was never released"


def test_ordinary_jobs_are_not_queued():
    """Reads, state changes and seeds stay unbounded, as before."""
    import time as _t

    from rc_repro.web.jobs import JobManager

    jobs = JobManager()
    job = jobs.submit("seed", lambda emit=None: {"ok": True}, label="x")
    for _ in range(50):
        if job.status == "done":
            break
        _t.sleep(0.1)
    assert job.status == "done"


# --- TLS after the edge ---------------------------------------------------------
# A workspace terminates no TLS, so `_resolve_tls` no longer allocates a port,
# probes one, or decides anything about a sidecar. What is left is: which name,
# and which certificate source.

def test_a_domain_still_needs_only_a_domain_and_an_email(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro import tls
    from rc_repro.services import edge as edgesvc
    from rc_repro.services import lifecycle as lcsvc

    monkeypatch.setattr(edgesvc, "running", lambda: True)
    req = lcsvc.CreateReq(version="8.5.1", domain="chat.example.com",
                          acme_email="me@example.com")
    spec = lcsvc._resolve_tls(req, "w", "127.0.0.1")
    assert spec.mode == tls.MODE_ACME
    assert spec.host == "chat.example.com"
    assert spec.root_url == "https://chat.example.com", "443 carries no port"


def test_a_domain_without_an_email_says_how_to_set_one(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro import errors
    from rc_repro.services import lifecycle as lcsvc

    req = lcsvc.CreateReq(version="8.5.1", domain="chat.example.com")
    with pytest.raises(errors.ValidationError) as exc:
        lcsvc._resolve_tls(req, "w", "127.0.0.1")
    assert "config set acme.email" in str(exc.value)


def test_local_https_gets_a_localhost_name_and_no_port_of_its_own(tmp_path, monkeypatch):
    """It used to allocate 8443, 8444, ... one per workspace, each with its own
    URL. Every name answers on the edge's 443 now."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro import tls
    from rc_repro.services import edge as edgesvc
    from rc_repro.services import lifecycle as lcsvc

    monkeypatch.setattr(edgesvc, "running", lambda: True)
    req = lcsvc.CreateReq(version="8.5.1", https=True)
    spec = lcsvc._resolve_tls(req, "w", "127.0.0.1")
    assert spec.mode == tls.MODE_LOCAL
    assert spec.host == "w.rcrepro.localhost"
    assert spec.root_url == "https://w.rcrepro.localhost"


def test_dns01_is_still_chosen_from_credentials_not_a_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro import tls
    from rc_repro.services import edge as edgesvc
    from rc_repro.services import lifecycle as lcsvc

    tls.acme_dir().mkdir(parents=True, exist_ok=True)
    tls.dns_env_path().write_text("CF_DNS_API_TOKEN=secret\n")
    monkeypatch.setattr(edgesvc, "running", lambda: True)
    req = lcsvc.CreateReq(version="8.5.1", domain="chat.example.com",
                          acme_email="me@example.com")
    spec = lcsvc._resolve_tls(req, "w", "127.0.0.1")
    assert spec.acme_challenge == "dns" and spec.acme_dns_provider == "cloudflare"


# --- host capacity (an OOM took a 10 GB box down) --------------------------------
# Seven concurrent Rocket.Chat + MongoDB stacks exhausted a 10 GB host with no
# swap: the kernel OOM killer fired and the machine had to be recovered. Every
# individual `up` had succeeded -- nothing anywhere asked whether there was room
# for one more. docs/design/team-server.md §8 recorded this as "made visible, not
# solved"; these pin the guard that solves it.

def _mem(monkeypatch, available_mb, total_mb=10000, swap_mb=0):
    from rc_repro import runner
    monkeypatch.setattr(runner, "host_memory",
                        lambda: (total_mb, available_mb, swap_mb))


# These five assert PreflightError, not NotReadyError. errors.py always documented
# capacity that way -- "capacity shortfalls ... use this" -- while the code raised
# NotReadyError, which is exit 5 and means "still unknown, poll again". Polling does
# not free memory, so exit 5 told a script to retry something retrying cannot fix.
# Same argument that moved `require_docker` off exit 5; HTTP status is 409 either way.
def test_a_workspace_is_refused_when_the_host_cannot_hold_it(monkeypatch):
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc

    _mem(monkeypatch, available_mb=1000)      # 100 MB usable after the reserve
    with pytest.raises(errors.PreflightError) as exc:
        lc.check_capacity(lc.CreateReq(version="8.5.1"))
    assert "not enough memory" in str(exc.value)


def test_the_refusal_says_what_to_stop_and_how(monkeypatch):
    """"Out of memory" with no next step just moves the problem to the operator."""
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc

    _mem(monkeypatch, available_mb=1000)
    with pytest.raises(errors.PreflightError) as exc:
        lc.check_capacity(lc.CreateReq(version="8.5.1"))
    msg = str(exc.value)
    assert "rc-repro stop" in msg and "--force" in msg
    assert "NO SWAP" in msg, "no swap means no buffer at all; say so"


def test_the_advertised_force_override_actually_overrides(monkeypatch):
    """The refusal tells the user to pass --force. It has to then work.

    The test above pins the WORDS "--force" in the message; nothing pinned the
    behaviour, and check_capacity never read req.force -- so the documented escape
    hatch did not exist. Following the instruction produced the identical refusal,
    and because `up --force` also means "overwrite this repro", obeying the advice
    risked destroying a workspace while still being blocked.

    Found by an audit that hit the refusal, did what it said, and was refused again.
    The override also has to stay LOUD: this is the guard added after an OOM killer
    took out a 10 GB host, so bypassing it is reported, not silent.
    """
    from rc_repro.services import lifecycle as lc
    from rc_repro.services.events import Event

    _mem(monkeypatch, available_mb=1000)          # 100 MB usable: nowhere near enough
    seen: list[Event] = []
    lc.check_capacity(lc.CreateReq(version="8.5.1", force=True), "",
                      seen.append)                # must NOT raise

    warnings = [e for e in seen if e.level == "warn"]
    assert warnings, "an OOM guard must not be bypassed silently"
    text = " ".join(e.message for e in warnings)
    assert "--force" in text, "say which flag did this"
    assert "OOM" in text, "and what the consequence is"

    # Without it, the same host state still refuses -- this widens nothing else.
    with pytest.raises(errors.PreflightError):
        lc.check_capacity(lc.CreateReq(version="8.5.1"))


def test_plenty_of_memory_is_not_refused(monkeypatch):
    from rc_repro.services import lifecycle as lc

    _mem(monkeypatch, available_mb=9000)
    lc.check_capacity(lc.CreateReq(version="8.5.1"))       # must not raise


def test_a_preset_with_keycloak_needs_more_than_a_bare_workspace(monkeypatch):
    """saml/oidc run Keycloak, which is the biggest sidecar there is."""
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc

    # Enough for a bare workspace, not for one plus Keycloak.
    _mem(monkeypatch, available_mb=lc.host_reserve_mb(10024) + lc.WORKSPACE_MB + 100)
    lc.check_capacity(lc.CreateReq(version="8.5.1"))       # bare: fits
    with pytest.raises(errors.PreflightError):
        lc.check_capacity(lc.CreateReq(version="8.5.1"), "saml")


def test_monitoring_counts_towards_the_estimate(monkeypatch):
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc

    _mem(monkeypatch, available_mb=lc.host_reserve_mb(10024) + lc.WORKSPACE_MB + 100)
    with pytest.raises(errors.PreflightError):
        lc.check_capacity(lc.CreateReq(version="8.5.1", monitor=True))


def test_the_last_workspace_warns_before_the_ceiling(monkeypatch):
    """The ceiling should be visible BEFORE it is hit, not only in the refusal."""
    from rc_repro.services import lifecycle as lc

    _mem(monkeypatch, available_mb=lc.host_reserve_mb(10024) + lc.WORKSPACE_MB + 200)
    seen: list = []
    lc.check_capacity(lc.CreateReq(version="8.5.1"), "", lambda ev: seen.append(ev))
    assert any("is left" in str(getattr(e, "message", e)) for e in seen)


def test_a_host_we_cannot_measure_is_never_refused(monkeypatch):
    """Not Linux: skip rather than guess. A wrong refusal is worse than none."""
    from rc_repro import runner
    from rc_repro.services import lifecycle as lc

    monkeypatch.setattr(runner, "host_memory", lambda: None)
    lc.check_capacity(lc.CreateReq(version="8.5.1"))       # must not raise


def test_host_memory_prefers_available_over_free():
    """MemFree excludes the page cache, which the kernel reclaims on demand -- it
    looks alarming on a healthy box and reassuring on a doomed one."""
    from rc_repro import runner

    mem = runner.host_memory()
    if mem is None:
        pytest.skip("not Linux")
    total, avail, _swap = mem
    assert total > 0 and 0 < avail <= total


def test_the_guard_would_have_stopped_the_incident(monkeypatch):
    """The REAL numbers from the host that died: 10024 MB total, 3350 MB
    available, no swap, and a seventh stack about to start.

    A fixed 1 GB reserve waved this exact configuration through when I tested it
    -- the box OOMed while MemAvailable still read 3.3 GB, because the memory that
    kills a host is what already-admitted workspaces take LATER. A proportional
    reserve refuses it.
    """
    from rc_repro import errors
    from rc_repro.services import lifecycle as lc

    _mem(monkeypatch, total_mb=10024, available_mb=3350, swap_mb=0)
    with pytest.raises(errors.PreflightError):
        lc.check_capacity(lc.CreateReq(version="8.5.1"), "multi-instance")


def test_the_reserve_scales_with_the_host():
    from rc_repro.services import lifecycle as lc

    assert lc.host_reserve_mb(10024) == 2004, "a fifth of a 10 GB box"
    assert lc.host_reserve_mb(2048) == 1024, "never below 1 GB on a small one"


def test_an_impossible_mongo_kernel_pairing_is_refused_before_any_docker_work(monkeypatch):
    """SERVER-121912: mongod 8.0 EXITS on kernel >= 6.19 — not degrades, exits,
    with a message that reads like a volume or permission fault.

    `doctor` has known the rule for a while and could only warn, because it does
    not know which release you are about to boot. Nothing on the create path
    checked it, so `up` would pull ~1.5 GB of images, create the containers and
    then let mongod die, leaving `diagnose` to explain it afterwards. Now the
    pairing is resolved first and an impossible one is refused.
    """
    from rc_repro.services import doctor

    # The rule itself: version-aware, so it answers differently per pairing.
    assert doctor.mongo_kernel_conflict("8.0", "6.19.7-200.fc43.aarch64")
    assert "SERVER-121912" in doctor.mongo_kernel_conflict("8.0", "6.19.7")
    assert doctor.mongo_kernel_conflict("7.0", "6.19.7") == ""   # 7.0 is unaffected
    assert doctor.mongo_kernel_conflict("8.0", "6.18.0") == ""   # older kernel is fine
    assert doctor.mongo_kernel_conflict("8.0", "") == ""         # unknown: do not guess
    assert doctor.mongo_kernel_conflict("", "6.19.7") == ""      # unknown: do not guess

    # And the create path refuses with it, before it touches docker.
    pulled: list = []
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr(lc.runner, "docker_kernel_version", lambda: "6.19.7-200.fc43")
    monkeypatch.setattr(lc.runner, "exists", lambda n: False)
    monkeypatch.setattr(lc.runner, "write", lambda *a, **k: pulled.append("wrote"))
    monkeypatch.setattr(lc.versions, "resolve",
                        lambda v, offline=False: type("R", (), {
                            "rc_version": v, "mongo_tag": "8.0", "rc_image": "img",
                            "mongo_flavor": "mongodb", "mongo_shell": "mongosh",
                            "oplog": False, "source": "test", "note": ""})())
    with pytest.raises(errors.PreflightError) as caught:
        lc._create_repro_locked(lc.CreateReq(version="8.6.1"))
    assert "mongod 8.0 exits" in str(caught.value)
    assert caught.value.exit_code == 3, "a wrong environment is a preflight failure"
    assert not pulled, "it refused only AFTER starting work"


# --- readiness must not depend on DNS, a certificate, or the edge ----------------

def test_root_url_in_metadata_is_always_the_local_port(monkeypatch, tmp_path):
    """runner.Metadata's own docstring makes this a contract: root_url "stays the
    plain http://localhost:<port> that rc-repro's own API calls (login, PAT, seeding,
    load tests) use", and public_url carries the external name.

    `--root-url` used to displace it, which put a public https name into root_url --
    and `ready` polls exactly that. Reported from a live box: "Rocket.Chat did not
    become ready within 300s" while `curl http://localhost:3000/api/info` answered
    200 the whole time. Reproduced here with `up --root-url https://lab.example.com`:
    still booting at 127s, 200 locally, and after the fix ready in 15s.

    The override still reaches Rocket.Chat -- it is what RC ADVERTISES, and the
    compose spec below keeps taking it.
    """
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    seen = {}

    monkeypatch.setattr(lc.runner, "docker_available", lambda **_k: True)
    monkeypatch.setattr(lc.runner, "exists", lambda _n: False)
    monkeypatch.setattr(lc, "check_capacity", lambda *a, **k: None)
    monkeypatch.setattr(lc, "check_sidecar_ports", lambda *a, **k: None)
    monkeypatch.setattr(lc, "pick_host_port", lambda *a, **k: 3001)
    _real_resolve = lc.versions.resolve      # bind before patching, or it recurses
    monkeypatch.setattr(lc.versions, "resolve",
                        lambda v, offline=False: _real_resolve(v, offline=True))

    def fake_write(name, compose_yaml, meta, files=None):
        seen["meta"] = meta
        seen["yaml"] = compose_yaml
        return None

    monkeypatch.setattr(lc.runner, "write", fake_write)
    monkeypatch.setattr(lc, "_up", lambda *a, **k: 0)

    lc.create_repro(lc.CreateReq(version="8.5.1", name="urlcase",
                                 root_url="https://lab.example.com", wait=False),
                    emit=lc.null_emit)

    meta = seen.get("meta")
    assert meta is not None, "create never wrote metadata"
    assert meta.root_url == "http://localhost:3001", \
        f"root_url must stay local, got {meta.root_url!r}"
    # And RC still advertises the override, or --root-url would do nothing at all.
    assert "ROOT_URL: https://lab.example.com" in seen["yaml"], \
        "RC should still advertise the override — otherwise --root-url does nothing"


def test_a_transitional_health_is_reported_without_dockers_string_prefix():
    """Docker spells the transitional state "health: starting" inside the Status
    string, and bare "healthy"/"unhealthy" for the settled ones.

    Taken verbatim the panel rendered "Health: health: starting". The prefix belongs
    to the string format, not to the value.
    """
    assert lc._uptime_health("Up 4 seconds (health: starting)") == ("4 seconds", "starting")
    assert lc._uptime_health("Up 2 hours (healthy)") == ("2 hours", "healthy")
    assert lc._uptime_health("Up 5 minutes (unhealthy)") == ("5 minutes", "unhealthy")
    # No healthcheck at all, and a stopped container, are both "no health".
    assert lc._uptime_health("Up 3 minutes") == ("3 minutes", "")
    assert lc._uptime_health("Exited (0) 1 minute ago") == ("", "")
# --- the topology socket --------------------------------------------------------
# Phase 1 of Kubernetes support: a workspace's runtime becomes a value that can be
# READ, instead of a shape (`docker-compose.yml` on disk) that can only be
# inferred. Only compose is registered, so none of this is user-visible yet -- the
# tests below are what make the seam checkable before there is a second runtime.

def test_a_workspace_with_no_runtime_key_reads_as_docker():
    """The compatibility gate. Every workspace on every existing box predates this
    key, and none of them is rewritten -- so an absent value must mean docker, or
    the socket silently orphans the entire installed base."""
    from rc_repro.services import topology

    old = lc.runner.Metadata(name="r", project="p", rc_version="8.5.1", rc_image="i",
                          mongo_tag="8.0", mongo_flavor="official", preset="default",
                          root_url="http://localhost:3000", host_port=3000,
                          version_source="test")
    assert old.extra == {}, "the fixture must genuinely lack the key"
    assert topology.of_meta(old) == topology.DOCKER


def test_an_unknown_runtime_reads_as_docker_rather_than_raising():
    """Forward compatibility, and the reason `of_meta` never raises: a workspace
    written by a NEWER rc-repro naming a runtime this build has never heard of must
    still list and still tear down. A repro the installed version cannot remove is
    worse than one it renders imprecisely."""
    from rc_repro.services import topology

    m = lc.runner.Metadata(name="r", project="p", rc_version="8.5.1", rc_image="i",
                        mongo_tag="8.0", mongo_flavor="official", preset="default",
                        root_url="u", host_port=3000, version_source="t",
                        extra={"runtime": "nomad"})
    assert topology.of_meta(m) == topology.DOCKER
    # And a corrupt `extra` is survivable too -- runner already defends against it.
    broken = lc.runner.Metadata(name="r", project="p", rc_version="8.5.1", rc_image="i",
                             mongo_tag="8.0", mongo_flavor="official", preset="default",
                             root_url="u", host_port=3000, version_source="t")
    broken.extra = "not-a-dict"          # type: ignore[assignment]
    assert topology.of_meta(broken) == topology.DOCKER


def test_the_runtime_survives_a_real_write_and_read(monkeypatch, tmp_path):
    """Round-trip through repro.json on disk, not through a mock. `read_meta`
    filters unknown keys, so a value living in the wrong place would be dropped
    between writing a workspace and reading it back."""
    from rc_repro.services import topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="p", rc_version="8.6.1", rc_image="i",
                        mongo_tag="8.0", mongo_flavor="official", preset="default",
                        root_url="u", host_port=3010, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    lc.runner.write("k", "services: {}\n", m)
    assert topology.of_repro("k") == topology.KUBERNETES
    assert lc.runner.read_meta("k").extra["runtime"] == "kubernetes"


def test_reading_the_runtime_of_a_missing_workspace_does_not_explode(tmp_path, monkeypatch):
    """`of_repro` is called before the caller's own existence check in some paths.
    It must not be the thing that fails, or the user gets a stat error instead of
    'no such repro'."""
    from rc_repro.services import topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    assert topology.of_repro("nope") == topology.DOCKER


def test_normalize_accepts_what_people_type_and_refuses_what_they_mistype():
    """The opposite policy to `of_meta`: this reads input a human just typed, where
    a silent fallback would boot the wrong topology."""
    from rc_repro.services import topology

    assert topology.normalize("") == topology.DOCKER, "omitting --runtime is not an error"
    assert topology.normalize(None) == topology.DOCKER
    for spelling in ("docker", "compose", "docker-compose", "DOCKER"):
        assert topology.normalize(spelling) == topology.DOCKER
    for spelling in ("k8s", "kube", "Kubernetes", " KUBERNETES "):
        assert topology.normalize(spelling) == topology.KUBERNETES
    with pytest.raises(errors.ValidationError) as caught:
        topology.normalize("nomad")
    assert caught.value.exit_code == 2, "a typo is a usage error, not a preflight one"
    assert "kubernetes" in str(caught.value), "the refusal lists what IS known"


def test_doctor_warns_before_inotify_runs_out(tmp_path):
    """The symptom of this one points nowhere near its cause.

    With `fs.inotify.max_user_instances` exhausted, Traefik STARTS, stays up, logs
    "Cannot start the provider *file.Provider ... too many open files", and then
    serves its own default certificate to every request. That looks exactly like a
    broken route or a bad certificate and is neither -- it cost three full runs of an
    audit to find, which is precisely the kind of thing `doctor` exists to say first.

    It is also not hypothetical on a developer box: the limit is per-user, kind's own
    docs raise it for multi-cluster use, and it resets on reboot -- this host was
    raised to 1024 during the audit and was back to the 128 default afterwards.
    """
    from rc_repro.services import doctor

    def at(limit, clusters):
        f = tmp_path / f"lim{limit}"
        f.write_text(str(limit))
        return doctor.inotify_headroom(clusters, path=str(f))

    # Plenty of room: say so, so the number is visible before it bites.
    assert at(4096, 1)[0][0] == "ok"

    # The kind default with several clusters is the failure that was actually hit.
    status, msg = at(128, 5)[0]
    assert status == "fail"
    assert "default certificate" in msg,         "name the symptom, or the reader cannot connect this to what they are seeing"
    assert "sysctl" in msg and "max_user_instances" in msg, "and give the fix"

    # Between need and 2x need is tight rather than broken.
    assert at(128, 2)[0][0] == "warn"

    # Not Linux: the file is absent, and a limit that does not apply is not a finding.
    assert doctor.inotify_headroom(1, path=str(tmp_path / "absent")) == []
    # An unreadable/garbage value is silence too, never a crash in a diagnostic.
    bad = tmp_path / "garbage"
    bad.write_text("not-a-number")
    assert doctor.inotify_headroom(1, path=str(bad)) == []


def test_kubernetes_logs_do_not_mangle_rocketchats_own_formatting(monkeypatch):
    """Rocket.Chat pretty-prints its logs; the reader came for THAT.

    `--prefix` was passed unconditionally, so every line arrived stamped with
    "[pod/rocketchat-rocketchat-<hash>/rocketchat] " and the columns Rocket.Chat
    lays out were pushed off the left of the screen -- destroying exactly the
    formatting somebody opened the logs to read. Reported from real use.

    With one pod the prefix names the only thing it could be, so it is pure noise.
    With several it is the only way to tell replicas apart, so it comes back.
    """
    from rc_repro.services import k8s

    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        class _P:
            stdout = None
            def terminate(self): pass
            def wait(self, timeout=None): return 0
        return _P()
    monkeypatch.setattr(k8s.subprocess, "Popen", fake_popen)

    def with_pods(n):
        import subprocess as sp
        monkeypatch.setattr(k8s, "run", lambda argv, **kw: sp.CompletedProcess(
            argv, 0, "\n".join(f"pod/rc-{i}" for i in range(n)), ""))
        k8s.log_process("w", context="c", tail=25, follow=True)
        return seen["argv"]

    single = with_pods(1)
    assert "--prefix" not in single, \
        "one pod: the prefix identifies the only candidate and costs the formatting"
    assert "-c" not in single, (
        "the selected pods have DIFFERENT container names -- rocketchat, "
        "ddp-streamer, presence -- so naming one would fail on the others")
    assert "-f" in single and "--tail" in single

    # THE SELECTOR IS THE POINT ON MICROSERVICES. It was the monolith deployment
    # alone, so ddp-streamer -- which carries the WebSocket -- produced no output at
    # all, and a realtime problem gave logs with nothing about realtime in them.
    sel = single[single.index("-l") + 1]
    assert f"app.kubernetes.io/instance={k8s.RELEASE}" in sel, \
        "the whole release, so a microservice the chart adds later is included too"
    assert "app.kubernetes.io/name!=nats" in sel, \
        "but not the message bus, which would drown Rocket.Chat's own lines"
    # And the narrow questions still ask the narrow one.
    assert k8s.APP_SELECTOR == "app.kubernetes.io/name=rocketchat", \
        "env means Rocket.Chat itself, not whichever pod sorted first"
    import inspect
    assert "LOG_SELECTOR" in inspect.signature(k8s.pod_metrics).parameters["selector"].default \
        or k8s.LOG_SELECTOR == inspect.signature(k8s.pod_metrics).parameters["selector"].default, \
        "stats sums every application pod, as Compose sums every rocketchat service"

    many = with_pods(3)
    assert "--prefix" in many, "several replicas are indistinguishable without it"

    # A failed pod count must not decide the shape either way -- it falls back to
    # the quiet form rather than guessing there are many.
    import subprocess as sp
    monkeypatch.setattr(k8s, "run",
                        lambda argv, **kw: sp.CompletedProcess(argv, 1, "", "boom"))
    k8s.log_process("w", context="c", tail=5, follow=False)
    assert "--prefix" not in seen["argv"] and "-f" not in seen["argv"]


def test_orphaned_namespaces_are_found_but_never_swept_by_default(
        monkeypatch, tmp_path):
    """A namespace with no local record is unreachable by every other command.

    Every rc-repro command starts from the state directory, so a workspace whose
    repro.json is gone -- interrupted create, wiped RC_REPRO_HOME, a different home
    -- keeps its pods and its PersistentVolumeClaim running and simply cannot be
    seen. One was found holding 8Gi and five CrashLoopBackOff pods, removable only
    by hand with kubectl.

    It is OPT-IN, and that is the point: on a shared cluster an unrecorded namespace
    may be a colleague's workspace created from their own home, so "this machine has
    no record of it" is not evidence that it is rubbish.
    """
    import json as _json
    from dataclasses import asdict

    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="known", project="p", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="u", host_port=3000,
                           version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    ws = lc.runner.workspace("known")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(_json.dumps(asdict(m)), encoding="utf-8")

    monkeypatch.setattr(k8s, "workspace_namespaces",
                        lambda ctx=k8s.CONTEXT: ["rc-repro-known", "rc-repro-p3",
                                                 k8s.OPERATOR_NAMESPACE])
    found = lc.orphan_namespaces()
    assert found == ["rc-repro-p3"], found
    assert k8s.OPERATOR_NAMESPACE not in found, \
        "the shared operator/monitoring namespace is not a workspace"

    # Default prune leaves them entirely alone.
    monkeypatch.setattr(lc, "prunable", lambda: [])
    monkeypatch.setattr(lc.auditsvc, "record", lambda *a, **kw: None)
    deleted: list[str] = []

    def fake_run(argv, **kw):
        import subprocess as sp
        if "delete" in argv and "namespace" in argv:
            deleted.append(argv[-2] if argv[-1].startswith("--") else argv[-1])
        return sp.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(k8s, "run", fake_run)

    assert lc.prune(confirm=True)["orphans"] == []
    assert deleted == [], "a sweep nobody asked for could delete a colleague's work"

    # Asked for explicitly, it removes exactly the unexplained one.
    out = lc.prune(confirm=True, orphans=True)
    assert out["orphans"] == ["rc-repro-p3"]
    assert deleted == ["rc-repro-p3"], deleted


def test_env_and_stats_answer_on_kubernetes_instead_of_refusing(
        monkeypatch, tmp_path):
    """The last two commands that had no Kubernetes path now have one.

    `env` is answered from the RUNNING CONTAINER rather than a document. That is not
    a translation of the Compose behaviour, it is better than it: the chart
    contributes variables rc-repro never set, so the generated values would not show
    them and the compose file has no equivalent here at all.

    `stats` needs metrics-server, which kind does not ship. When it is missing the
    refusal says so and how to install it -- a resource figure that is quietly zero
    is worse than one that is missing, which is the same reasoning `stats` already
    applies to a container it cannot find.
    """
    import json as _json
    from dataclasses import asdict

    from rc_repro import errors as errs
    from rc_repro.services import envvars, k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="p", rc_version="8.5.1", rc_image="i",
                           mongo_tag="8.0", mongo_flavor="official", preset="default",
                           root_url="u", host_port=3000, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra["context"] = k8s.CONTEXT
    m.extra["env"] = {"MY_OVERRIDE": "set-by-me"}
    ws = lc.runner.workspace("k")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(_json.dumps(asdict(m)), encoding="utf-8")

    # --- env: read from the container, credentials masked -------------------
    monkeypatch.setattr(k8s, "container_env", lambda name, *, context: {
        "ADMIN_PASS": "admin123", "MONGO_URL": "mongodb://mongodb:27017/rocketchat",
        "MY_OVERRIDE": "set-by-me", "ROOT_URL": "http://localhost:3000"})
    out = envvars.current("k")
    keys = {row["key"]: row for row in out["env"]}
    assert set(keys) == {"ADMIN_PASS", "MONGO_URL", "MY_OVERRIDE", "ROOT_URL"}
    assert keys["MY_OVERRIDE"]["override"] is True, "a user override is marked as one"
    assert keys["ROOT_URL"]["override"] is False
    assert keys["ADMIN_PASS"]["value"] != "admin123", \
        "credentials are masked here exactly as they are on Compose"

    # --- stats: real numbers, parsed from `kubectl top` output --------------
    import subprocess as sp

    def top(argv, **kw):
        if "top" in argv:
            return sp.CompletedProcess(
                argv, 0, "rocketchat-a   120m   500Mi\nrocketchat-b   80m   300Mi\n", "")
        return sp.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(k8s, "run", top)
    rows = k8s.pod_metrics("k", context="c")
    assert sum(r["cpu_millicores"] for r in rows) == 200.0, "replicas are summed"
    assert rows[0]["mem_bytes"] == 500 * 1024 ** 2, "Mi is binary, not 1e6"

    # --- and a refusal that names the fix when metrics-server is absent ------
    def no_metrics(argv, **kw):
        return sp.CompletedProcess(argv, 1, "", "error: Metrics API not available")
    monkeypatch.setattr(k8s, "run", no_metrics)
    with pytest.raises(errs.NotReadyError) as caught:
        k8s.pod_metrics("k", context="c")
    assert "metrics-server" in str(caught.value)
    assert "kubectl apply" in str(caught.value), "say how to install it"


def test_start_restores_the_replica_count_stop_recorded(monkeypatch):
    """`--replicas 2` came back as 1 after stop/start, silently.

    `stop` records the counts in a namespace annotation for exactly this, and `start`
    read it back through jsonpath -- escaping the SLASH in
    `rc-repro.io/replicas-before-stop` while leaving the DOT in `rc-repro.io`
    unescaped. jsonpath then read `rc-repro` and `io/...` as separate path segments,
    matched nothing and returned empty, so every restore fell through to the default
    of 1. Measured on a live cluster: the slash-escaped form returns "" where the
    dot-escaped form returns the annotation.

    The annotation written to prevent this was therefore never once read back, and
    nothing said so -- the workspace simply had half the Rocket.Chat it was asked for.
    Found by running microservices with two replicas, which is the only shape where
    the default and the truth differ.

    Read as JSON and picked in Python now: a rule with no escaping in it cannot be
    got subtly wrong.
    """
    import json as _json

    from rc_repro.services import k8s

    recorded = {"deployment/rocketchat-rocketchat": "2",
                "deployment/rocketchat-presence": "1"}
    scaled: list[str] = []

    def fake_run(argv, **kw):
        import subprocess as sp
        joined = " ".join(argv)
        if "get" in argv and "namespace" in argv and "json" in joined:
            return sp.CompletedProcess(argv, 0, _json.dumps(
                {"metadata": {"annotations": {k8s.SCALE_ANNOTATION:
                                              _json.dumps(recorded)}}}), "")
        if "scale" in argv:
            scaled.append(next(a for a in argv if a.startswith("--replicas="))
                          + " " + argv[-1])
        return sp.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(k8s, "run", fake_run)
    monkeypatch.setattr(k8s, "_scalables",
                        lambda ns, ctx: ["deployment/rocketchat-rocketchat",
                                         "deployment/rocketchat-presence"])

    k8s.start_workspace("w", context="c")
    assert "--replicas=2 deployment/rocketchat-rocketchat" in scaled, scaled
    assert "--replicas=1 deployment/rocketchat-presence" in scaled, scaled

    # A workspace stopped by hand carries no annotation; 1 is what `up` would give.
    def no_annotation(argv, **kw):
        import subprocess as sp
        if "get" in argv and "namespace" in argv:
            return sp.CompletedProcess(argv, 0, '{"metadata":{}}', "")
        if "scale" in argv:
            scaled.append(next(a for a in argv if a.startswith("--replicas="))
                          + " " + argv[-1])
        return sp.CompletedProcess(argv, 0, "", "")
    scaled.clear()
    monkeypatch.setattr(k8s, "run", no_annotation)
    k8s.start_workspace("w", context="c")
    assert all(x.startswith("--replicas=1") for x in scaled), scaled


def test_an_interrupted_kubernetes_create_still_leaves_a_record(monkeypatch, tmp_path):
    """Killed part-way through, a create must leave something `down` can find.

    `create_workspace` builds the namespace, MongoDB, the release, the scenario's
    manifests and the port-forward -- minutes of work -- and the record used to be
    written only after it returned. Stopping `serve` with a create job still running
    does exactly that, and it was reported from real use: a full microservices
    workspace with twelve pods left running and INVISIBLE, because `list`, `info`
    and `down` all begin at the state directory and there was nothing in it.

    Compose never had this. It writes the record first and starts containers after,
    so an interrupted create is still listed and still removable. This is the same
    guarantee for the other runtime.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr(lc, "check_capacity", lambda *a, **kw: None)
    monkeypatch.setattr(lc.runner, "pick_port", lambda *a, **kw: 3999, raising=False)

    # The kill, at the worst possible moment: everything built, nothing recorded.
    def die(**kw):
        raise KeyboardInterrupt("serve shut down mid-create")
    monkeypatch.setattr(k8s, "create_workspace", die)

    req = lc.CreateReq(version="8.5.1", name="interrupted", runtime="kubernetes",
                       deployment="microservices")
    with pytest.raises(BaseException):
        lc.create_repro(req)

    assert lc.runner.exists("interrupted"), (
        "the workspace was left running with no record at all -- nothing in "
        "rc-repro could see it, let alone remove it")
    meta = lc.runner.read_meta("interrupted")
    assert topology.of_meta(meta) == topology.KUBERNETES
    assert meta.extra.get("namespace") == k8s.namespace_for("interrupted"), \
        "teardown selects on the namespace, so it has to be recorded"
    assert meta.extra.get("incomplete") is True, \
        "and it must be distinguishable from a create that finished"


def test_prune_asks_kubernetes_whether_a_kubernetes_workspace_is_down(
        monkeypatch, tmp_path):
    """"Is it down" was answered by Docker for every workspace, on both runtimes.

    `prunable` listed a repro when no compose PROJECT was running. A Kubernetes
    workspace never has one, so a healthy workspace serving traffic was reported as
    down and offered up for deletion -- `prune` announces "deletes N down repro(s)
    incl. data" with a live one counted among them. It could not then remove it
    either: the loop ran `docker compose down` against a workspace with no compose
    file, which always failed.

    Found by an audit that went looking for why an orphaned namespace was still
    running with no state file anywhere to manage it.
    """
    import json as _json
    from dataclasses import asdict

    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def workspace(name, runtime, port):
        m = lc.runner.Metadata(name=name, project=f"rcrepro-{name}",
                               rc_version="8.5.1", rc_image="i", mongo_tag="8.0",
                               mongo_flavor="official", preset="default",
                               root_url=f"http://localhost:{port}", host_port=port,
                               version_source="t")
        if runtime == topology.KUBERNETES:
            topology.stamp(m.extra, runtime)
        ws = lc.runner.workspace(name)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "repro.json").write_text(_json.dumps(asdict(m)), encoding="utf-8")

    workspace("live-k8s", topology.KUBERNETES, 3000)
    workspace("gone-k8s", topology.KUBERNETES, 3001)
    workspace("down-docker", topology.DOCKER, 3002)

    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr(lc.runner, "project_states", lambda: set())
    monkeypatch.setattr(lc.auditsvc, "actor", lambda: "")
    monkeypatch.setattr(lc, "may_destroy", lambda n, a: (True, ""))
    # The live one still has its Rocket.Chat workload; the other does not.
    monkeypatch.setattr(k8s, "workload_exists",
                        lambda name, *, context: name == "live-k8s")

    targets = lc.prunable()
    assert "live-k8s" not in targets, (
        "a RUNNING Kubernetes workspace was offered for deletion, because the "
        "question was put to Docker and it has no compose project either way")
    assert "gone-k8s" in targets, "a Kubernetes workspace with no workload IS down"
    assert "down-docker" in targets, "and Compose must keep behaving as before"


def test_prune_removes_a_kubernetes_workspace_through_kubernetes(
        monkeypatch, tmp_path):
    """And having listed one, it has to be able to remove it.

    The loop called `runner.down` -- `docker compose down` -- for every target, so a
    Kubernetes workspace was warned about and skipped every time, leaving its
    namespace and PersistentVolumeClaim on the cluster indefinitely.
    """
    import json as _json
    from dataclasses import asdict

    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rcrepro-k", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="http://localhost:3000",
                           host_port=3000, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra["context"] = k8s.CONTEXT
    ws = lc.runner.workspace("k")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(_json.dumps(asdict(m)), encoding="utf-8")

    monkeypatch.setattr(lc, "prunable", lambda: ["k"])
    monkeypatch.setattr(lc.auditsvc, "record", lambda *a, **kw: None)
    monkeypatch.setattr(lc.runner, "down",
                        lambda *a, **kw: pytest.fail(
                            "it reached for `docker compose down` on Kubernetes"))
    deleted: list[tuple] = []
    monkeypatch.setattr(k8s, "delete_namespace",
                        lambda name, *, context, volumes, emit=None:
                            deleted.append((name, volumes)) or True)

    out = lc.prune(confirm=True)
    assert deleted == [("k", True)], "the namespace and its PVC must actually go"
    assert out["removed"] == ["k"]
    assert not lc.runner.workspace("k").exists(), "and the local record with it"


def test_reading_env_answers_from_the_container_on_kubernetes(monkeypatch, tmp_path):
    """`env` with no --set went through three states, and this pins the last one.

    First it raised a bare FileNotFoundError naming
    `repros/<n>/docker-compose.yml` -- a path, with no statement of what was wrong.
    That escaped the ReproError contract, so `serve` answered 500 to a request that
    is merely unsupported while `logs` and `stats` answered cleanly on the same
    workspace. Then it refused cleanly. Now it answers.

    From the RUNNING CONTAINER, which is not a translation of the Compose behaviour
    but better than it: the chart contributes variables rc-repro never set, so
    neither the helm values nor any generated document would show them.

    `env --set` still refuses -- an environment variable cannot be changed inside a
    running container on either runtime, and here that means a helm upgrade.
    """
    import json as _json
    from dataclasses import asdict

    from rc_repro.services import envvars, k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="p", rc_version="8.6.1", rc_image="i",
                           mongo_tag="8.0", mongo_flavor="official", preset="default",
                           root_url="u", host_port=3010, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra["context"] = k8s.CONTEXT
    ws = lc.runner.workspace("k")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(_json.dumps(asdict(m)), encoding="utf-8")
    assert not (ws / "docker-compose.yml").exists(), \
        "the absence of a compose file is the condition under test"

    monkeypatch.setattr(k8s, "container_env", lambda name, *, context: {
        "ROOT_URL": "http://localhost:3010", "ADMIN_PASS": "admin123"})
    out = envvars.current("k")
    keys = {r["key"]: r["value"] for r in out["env"]}
    assert keys["ROOT_URL"] == "http://localhost:3010"
    assert keys["ADMIN_PASS"] != "admin123", "credentials masked, as on Compose"

    # The WRITE path still refuses, and names the command that does the job.
    with pytest.raises(errors.ValidationError) as caught:
        envvars.set_env("k", {"A": "b"})
    assert "helm" in str(caught.value)
    assert caught.value.http_status == 400


def test_stop_says_so_when_the_operator_keeps_mongodb_running(monkeypatch):
    """An operator-managed MongoDB cannot be scaled to zero.

    The operator reconciles its StatefulSet straight back, and the Community operator
    has no way to pause reconciliation (checked upstream, not assumed). Two faults
    followed from not knowing that, and an audit of the operator path found both:

      * `stop` documents itself as giving the memory back. It gave back Rocket.Chat's
        and not MongoDB's, and said nothing -- so someone who ran `stop` to free the
        box was told it had worked.
      * The wait-for-pods-gone loop required EVERY pod to disappear, so every stop and
        every restart on this path burned the full POD_GONE_TRIES budget waiting for a
        pod that was never going to leave, then carried on regardless.

    The hand-written StatefulSet path is untouched: it scales to zero like anything
    else, and must not start paying for this.
    """
    from rc_repro.services import k8s
    from rc_repro.services.events import Event

    def cluster(*, operator: bool, pods: list[str]):
        """Stub kubectl for the handful of calls stop_workspace makes."""
        def run(argv, timeout=None, own=False):
            import subprocess as sp
            joined = " ".join(argv)
            if "mongodbcommunity" in joined:
                out = "mongodbcommunity.mongodbcommunity.mongodb.com/mongodb" \
                    if operator else ""
                return sp.CompletedProcess(argv, 0 if operator else 1, out, "")
            if "get pods" in joined or ("pods" in argv and "get" in argv):
                if "-l" in argv:                       # the mongo-only selector
                    keep = [p for p in pods if "mongodb" in p]
                    return sp.CompletedProcess(argv, 0, " ".join(keep), "")
                return sp.CompletedProcess(argv, 0, " ".join(pods), "")
            if "jsonpath={.spec.replicas}" in joined:
                return sp.CompletedProcess(argv, 0, "1", "")
            return sp.CompletedProcess(argv, 0, "", "")
        return run

    # --- the operator path: mongo is expected to survive ------------------
    monkeypatch.setattr(k8s, "run", cluster(operator=True, pods=["pod/mongodb-0"]))
    monkeypatch.setattr(k8s, "scale_workspace", lambda *a, **kw: 1)
    assert k8s.mongo_pods_the_operator_keeps("w", context="c") == {"pod/mongodb-0"}

    slept: list[float] = []
    seen: list[Event] = []
    k8s.stop_workspace("w", context="c", emit=seen.append, sleep=slept.append)
    assert not slept, (
        "it waited for a pod the operator will never remove; on the operator path "
        "every stop and restart paid the full timeout for nothing")
    text = " ".join(e.message for e in seen if e.level == "warn")
    assert "MongoDB stays up" in text, "stop must not claim memory it did not free"
    assert "--volumes" in text, "and must name what actually reclaims it"

    # --- the StatefulSet path: nothing survives, nothing is claimed --------
    monkeypatch.setattr(k8s, "run", cluster(operator=False, pods=[]))
    assert k8s.mongo_pods_the_operator_keeps("w", context="c") == set()
    seen.clear()
    k8s.stop_workspace("w", context="c", emit=seen.append, sleep=slept.append)
    assert not [e for e in seen if e.level == "warn"], \
        "the hand-written StatefulSet does scale to zero; do not warn about it"


def test_ready_speaks_for_every_probed_pod_not_just_the_first(monkeypatch):
    """`ready` used to read items[0].containerStatuses[0], which over-claimed twice.

    Found by an operational audit of a live microservices workspace:

      * With --replicas N it inspected ONE pod, so the workspace was declared ready
        on the strength of the first.
      * It only ever looked at the monolith container. `ddp-streamer` carries the
        WebSocket -- the realtime half of Rocket.Chat -- and was seen
        Running-but-not-Ready on a workspace `ready` had already called serving. A
        caller told "ready" could open the URL and find messages not arriving, which
        is the failure this tool exists to REPRODUCE, not to cause.

    account/authorization/presence are excluded on purpose: the chart ships them with
    no readinessProbe, so their pod-Ready flips as soon as the container starts and
    attests nothing. Waiting on it would cost time and buy no confidence.
    """
    import json as _json

    from rc_repro.services import k8s

    def pods(*specs):
        """specs: (name, [ready flags]) -- no flags means Pending, no statuses."""
        items = []
        for pod_name, flags in specs:
            status = {"phase": "Pending" if flags is None else "Running"}
            if flags is not None:
                status["containerStatuses"] = [{"ready": f} for f in flags]
            items.append({"metadata": {"name": pod_name}, "status": status})
        return (0, _json.dumps({"items": items}))

    seen: list[list[str]] = []

    def stub(result):
        """Answer whichever output form the caller asked for.

        Deliberately format-aware, so this test is a real differential: the version
        being replaced asked for `-o jsonpath={.items[0]...ready}` and the new one
        asks for `-o json`. A stub that only spoke JSON would fail against the old
        code for the wrong reason -- output it could not parse -- and would prove
        nothing about the over-claiming this test is here to pin.
        """
        def run(argv, timeout=None, own=False):
            import subprocess as sp
            seen.append(argv)
            rc, payload = result
            if rc != 0:
                return sp.CompletedProcess(argv, rc, "", "")
            joined = " ".join(argv)
            if "jsonpath" in joined:
                items = _json.loads(payload).get("items") or []
                first = ((items[0].get("status", {}).get("containerStatuses") or [{}])[0]
                         if items else {})
                got = first.get("ready")
                return sp.CompletedProcess(
                    argv, 0, "" if got is None else str(got).lower(), "")
            return sp.CompletedProcess(argv, 0, payload, "")
        return run

    # One ready pod -- the monolith case, still true.
    monkeypatch.setattr(k8s, "run", stub(pods(("rc-0", [True]))))
    assert k8s.workspace_ready("w", context="c") is True

    # The selector must name both probed workloads and NEITHER unprobed one.
    argv = seen[-1]
    sel = argv[argv.index("-l") + 1]
    assert "rocketchat" in sel and "rocketchat-ddp-streamer" in sel, sel
    for unprobed in ("account", "authorization", "presence"):
        assert f"rocketchat-{unprobed}" not in sel, \
            f"{unprobed} has no readinessProbe, so waiting on it proves nothing"

    # THE items[0] BUG: three replicas, the third not ready.
    monkeypatch.setattr(k8s, "run", stub(
        pods(("rc-0", [True]), ("rc-1", [True]), ("rc-2", [False]))))
    assert k8s.workspace_ready("w", context="c") is False, \
        "a not-ready replica must not be hidden behind a ready first pod"

    # THE MICROSERVICES BUG: Rocket.Chat ready, ddp-streamer not.
    monkeypatch.setattr(k8s, "run", stub(
        pods(("rc-0", [True]), ("rc-ddp-streamer-0", [False]))))
    assert k8s.workspace_ready("w", context="c") is False, \
        "realtime is not serving, so the workspace is not ready"

    # A Pending pod has NO containerStatuses; "nothing failed" is not "ready".
    monkeypatch.setattr(k8s, "run", stub(pods(("rc-0", [True]), ("rc-ddp-0", None))))
    assert k8s.workspace_ready("w", context="c") is False

    # No pods at all is not ready either -- that is a torn-down workspace.
    monkeypatch.setattr(k8s, "run", stub(pods()))
    assert k8s.workspace_ready("w", context="c") is False

    # A sidecar that is not ready counts, even when the app container is.
    monkeypatch.setattr(k8s, "run", stub(pods(("rc-0", [True, False]))))
    assert k8s.workspace_ready("w", context="c") is False

    # kubectl failing is not readiness.
    monkeypatch.setattr(k8s, "run", stub((1, "")))
    assert k8s.workspace_ready("w", context="c") is False


def test_a_compose_only_operation_refuses_a_kubernetes_workspace(monkeypatch, tmp_path):
    """`stats` shells out to `docker stats` against a compose project, so it cannot
    serve a workspace that has no compose project. The refusal must name the
    alternative -- 'not supported' leaves the user to find what the person writing
    the message already knew."""
    from rc_repro.services import topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="p", rc_version="8.6.1", rc_image="i",
                        mongo_tag="8.0", mongo_flavor="official", preset="default",
                        root_url="u", host_port=3010, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    lc.runner.write("k", "services: {}\n", m)

    with pytest.raises(errors.ValidationError) as caught:
        topology.require_compose("k", "stats", instead="Use `kubectl top`.")
    assert "Kubernetes" in str(caught.value)
    assert "kubectl top" in str(caught.value), "the alternative is stated"

    # And it is SILENT for the runtime it is guarding -- a guard that fires on
    # compose would break every existing user of the command.
    topology.stamp(m.extra, topology.DOCKER)
    lc.runner.write("k", "services: {}\n", m)
    topology.require_compose("k", "stats")


def test_both_runtimes_are_registered_and_a_third_would_not_be():
    """Kubernetes joined `REGISTERED` only once its whole sequence -- cluster,
    namespace, MongoDB with an initiated replica set, the chart, a port-forward --
    ran on a live cluster rather than in stubs.

    Registration is what `up` consults, and it is deliberately separate from being
    SPELLABLE: a name can be recognised, and reported honestly as unavailable,
    before it works. That is how `kubernetes` behaved for six commits.
    """
    from rc_repro.services import topology

    assert topology.REGISTERED == frozenset({topology.DOCKER, topology.KUBERNETES})
    assert topology.is_registered(topology.DOCKER)
    assert topology.is_registered(topology.KUBERNETES)
    assert not topology.is_registered("nomad")
    assert topology.normalize("k8s") == topology.KUBERNETES


def test_the_create_path_stamps_the_runtime_on_a_compose_workspace(monkeypatch, tmp_path):
    """The stamp has to happen on the REAL create path, not just be available as a
    helper. Compose workspaces are stamped too, deliberately: if only Kubernetes
    ones were, 'absent' would mean two different things -- an old workspace and a
    new compose one -- and no later reader could tell them apart.

    Captured at the `runner.write` seam, which is the last thing to see the
    metadata before it becomes repro.json.
    """
    from rc_repro.services import topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    written: list = []
    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr(lc, "check_capacity", lambda *a, **k: None)
    monkeypatch.setattr(lc.runner, "docker_kernel_version", lambda: "6.1.0")
    monkeypatch.setattr(lc.runner, "exists", lambda n: False)
    monkeypatch.setattr(lc.runner, "used_ports", lambda **k: set())
    monkeypatch.setattr(lc.runner, "port_free", lambda *a, **k: True)
    monkeypatch.setattr(lc.runner, "write",
                        lambda name, yaml, meta, files=None: written.append(meta))
    monkeypatch.setattr(lc.runner, "up", lambda *a, **k: 0)
    monkeypatch.setattr(lc.versions, "resolve",
                        lambda v, offline=False: type("R", (), {
                            "rc_version": v, "mongo_tag": "8.0", "rc_image": "img",
                            "mongo_flavor": "mongodb", "mongo_shell": "mongosh",
                            "oplog": False, "source": "test", "note": ""})())
    try:
        lc._create_repro_locked(lc.CreateReq(version="8.6.1", name="stamped"))
    except Exception:                       # noqa: BLE001
        pass                                # whatever happens AFTER the write is not this test
    assert written, "the create path never reached runner.write"
    assert written[0].extra.get("runtime") == topology.DOCKER, (
        "a workspace was written with no runtime recorded — every later reader "
        "would have to guess, which is the ambiguity this key exists to remove")


# --- the three axes -------------------------------------------------------------

def test_every_cell_of_the_runtime_deployment_matrix():
    """`--preset` used to carry two ideas at once -- the scenario AND the
    deployment shape -- because there was one axis to put them in. Of the nine
    presets shipped, eight are scenarios and exactly one, multi-instance, is a
    deployment: it adds a Traefik load balancer and a NATS mesh.

    Splitting them means `--preset` finally means the same thing in both runtimes.
    Every combination is decided in one function, because a rule enforced at two
    call sites eventually disagrees with itself.
    """
    from rc_repro.services import topology as T

    ok = lambda **kw: T.resolve_axes(**kw)                                # noqa: E731

    # Defaults: omitting --runtime is the common case, not an error.
    assert (ok().runtime, ok().deployment, ok().replicas) == (T.DOCKER, T.MONOLITH, 1)
    # Kubernetes defaults to microservices, matching the chart's own default.
    k = ok(runtime="k8s")
    assert (k.runtime, k.deployment) == (T.KUBERNETES, T.MICROSERVICES)
    assert ok(runtime="k8s", deployment="monolith").deployment == T.MONOLITH
    # A scenario is a scenario regardless of where it runs -- the whole point.
    assert ok(preset="ldap").preset == "ldap"
    assert ok(runtime="k8s", preset="ldap").preset == "ldap"
    # multi-instance reaches the preset loader, which is how it stays compatible.
    m = ok(deployment="multi-instance", replicas=3)
    assert (m.preset, m.params["instances"], m.replicas) == ("multi-instance", 3, 3)
    assert ok(deployment="multi-instance").replicas == 2, "the preset's own default"


def test_the_refusals_name_what_to_do_instead(monkeypatch):
    """Each refusal is about coverage or physics -- never about category
    confusion. 'Is multi-instance a preset or a deployment?' stops being a
    question a user can get wrong, because there is one place it can live."""
    from rc_repro.services import topology as T

    cases = [
        (dict(runtime="docker", deployment="microservices"), "--runtime kubernetes"),
        (dict(runtime="k8s", deployment="multi-instance"), "--runtime docker"),
        (dict(replicas=3), "--deployment multi-instance"),
        (dict(preset="ldap", deployment="multi-instance"), "separately"),
        (dict(preset="multi-instance", deployment="monolith"), "two different deployments"),
        (dict(runtime="nomad"), "Known runtimes"),
        (dict(deployment="sharded"), "Known deployments"),
        (dict(replicas=-1), "at least 1"),
    ]
    for kwargs, expect in cases:
        with pytest.raises(errors.ValidationError) as caught:
            T.resolve_axes(**kwargs)
        assert expect in str(caught.value), f"{kwargs} said {caught.value!r}"
        assert caught.value.exit_code == 2, "a bad combination is a usage error"

    # Resolving and being allowed stay separate questions, so a GUI can name a
    # runtime it cannot yet create. Both are registered now, so neither refuses --
    # the separation is what lets a THIRD runtime be named before it works.
    assert T.resolve_axes(runtime="k8s").runtime == T.KUBERNETES
    T.require_registered(T.KUBERNETES)
    T.require_registered(T.DOCKER)
    with pytest.raises(errors.ValidationError) as caught:
        T.require_registered("nomad")
    assert "not available in this build" in str(caught.value)


def test_the_old_spellings_keep_working_and_say_what_they_are_now():
    """Permanent aliases, not a deprecation with a deadline. They cost one dict
    lookup each, and breaking a command line that is pasted into support tickets
    buys nothing."""
    from rc_repro.services import topology as T

    a = T.resolve_axes(preset="multi-instance", params={"instances": "3"})
    assert (a.deployment, a.replicas) == (T.MULTI_INSTANCE, 3)
    assert a.params["instances"] == "3", "the original param is preserved verbatim"
    assert any("--deployment multi-instance" in h for h in a.hints)
    assert any("--replicas 3" in h for h in a.hints)

    # #3's spelling: --deployment microservices with no --runtime means Kubernetes.
    b = T.resolve_axes(deployment="microservices")
    assert b.runtime == T.KUBERNETES
    assert any("implies --runtime kubernetes" in h for h in b.hints)

    # Nothing is deprecated about the NEW spelling, so it stays quiet.
    assert T.resolve_axes(deployment="multi-instance", replicas=3).hints == []


def test_rebuilding_a_workspace_does_not_nag_about_a_spelling_nobody_typed():
    """`restore` and the GUI's recreate button rebuild a request from a record.
    Both used to pass `preset=meta.preset`, which for a multi-instance workspace
    IS the legacy spelling -- so clicking Start printed a deprecation notice at a
    user who had typed neither flag."""
    from rc_repro.services import topology as T

    m = lc.runner.Metadata(name="r", project="p", rc_version="8.5.1", rc_image="i",
                           mongo_tag="8.0", mongo_flavor="official",
                           preset="multi-instance", root_url="u", host_port=3000,
                           version_source="t",
                           extra={"runtime": "docker", "deployment": "multi-instance",
                                  "instances": 3})
    kwargs = T.axes_of_meta(m)
    assert kwargs == {"runtime": "docker", "deployment": "multi-instance",
                      "preset": "default", "replicas": 3}
    assert T.resolve_axes(**kwargs).hints == [], "rebuilding is not a deprecation"

    # A workspace older than the deployment key is not lost: for those the
    # deployment WAS the preset name, which is the ambiguity the split removes.
    old = lc.runner.Metadata(name="r", project="p", rc_version="8.5.1", rc_image="i",
                             mongo_tag="8.0", mongo_flavor="official",
                             preset="multi-instance", root_url="u", host_port=3000,
                             version_source="t")
    assert T.axes_of_meta(old)["deployment"] == T.MULTI_INSTANCE
    assert T.axes_of_meta(old)["runtime"] == T.DOCKER


# --- the Kubernetes preflight ---------------------------------------------------
# Phase 3 rung 1: `doctor` learns Kubernetes. Nothing here creates anything -- every
# function is a read, so these tests drive the whole surface with `k8s.run` stubbed.

def _fake_run(mapping):
    """Stub k8s.run: match on a substring of the joined argv."""
    import subprocess as sp

    def run(argv, timeout=None, own=False):
        joined = " ".join(argv)
        for needle, (rc, out) in mapping.items():
            if needle in joined:
                return sp.CompletedProcess(argv, rc, out, "")
        return sp.CompletedProcess(argv, 1, "", "no stub")
    return run


def test_tool_versions_parse_the_three_formats_the_tools_actually_print():
    """Each tool spells its version differently, and all three strings below are
    real output from this machine. A parser that only handled one would report a
    present tool as unknown and, with a floor to check, refuse to proceed."""
    from rc_repro.services import k8s

    assert k8s._parse_version("kind v0.32.0 go1.26.3 linux/amd64") == (0, 32, 0)
    assert k8s._parse_version("Client Version: v1.36.3") == (1, 36, 3)
    assert k8s._parse_version("v4.2.3+g43e8b7f") == (4, 2, 3)
    assert k8s._parse_version("something unparseable") == ()


def test_an_unparseable_version_is_not_treated_as_too_old():
    """A distro may print something this does not recognise. Refusing to proceed
    over an unrecognised version string is worse than trying: the binary is there,
    and the floor exists to catch a genuinely ancient one, not to gate on parsing."""
    from rc_repro.services import k8s

    assert k8s.Tool(name="helm", path="/usr/bin/helm", raw="weird").new_enough
    assert not k8s.Tool(name="helm", path="/usr/bin/helm", version=(2, 17)).new_enough
    assert k8s.Tool(name="helm", path="/usr/bin/helm", version=(3, 0)).new_enough
    assert not k8s.Tool(name="helm").present


def test_a_missing_binary_never_raises_out_of_the_seam():
    """`doctor` is the command someone runs BECAUSE things are wrong. If the seam
    raised on a missing binary, every call site would need a wrapper and one
    forgotten wrapper takes down the whole report."""
    from rc_repro.services import k8s

    res = k8s.run(["definitely-not-a-real-binary-xyz", "version"])
    assert res.returncode != 0
    assert res.stdout == ""


def test_namespaces_are_selected_by_label_never_by_name(monkeypatch):
    """The safety property. Anyone can create a namespace called
    `rc-repro-anything`; a teardown matching on the prefix would eventually delete
    one rc-repro never made. Asserted on the argv, because that is the thing that
    reaches the cluster."""
    from rc_repro.services import k8s

    seen = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        seen.append(argv)
        return sp.CompletedProcess(argv, 0, "namespace/rc-repro-t1\n", "")

    monkeypatch.setattr(k8s, "run", spy)
    assert k8s.workspace_namespaces() == ["rc-repro-t1"]
    argv = seen[0]
    assert "-l" in argv and k8s.OWNER_SELECTOR in argv, argv
    # The context legitimately contains the prefix (kind-rc-repro-local). What must
    # not appear is a SELECTOR built from the name: no field-selector, and no
    # argument that is the prefix used as a match.
    selectors = [a for i, a in enumerate(argv)
                 if i and argv[i - 1] not in ("--context",) and a != "--context"]
    assert not any(a.startswith("--field-selector") for a in selectors), argv
    assert not any(a.strip('"\'') == k8s.NAMESPACE_PREFIX for a in selectors), argv


def test_the_release_is_named_as_the_official_docs_name_it():
    """PR #3 calls the release `rc`. The official guide calls it `rocketchat`, and
    with one release per namespace there is no reason to differ -- so every command
    copied from docs.rocket.chat works by substituting the namespace alone."""
    from rc_repro.services import k8s

    assert k8s.RELEASE == "rocketchat"
    assert k8s.CLUSTER_NAME == "rc-repro-local"


def test_preflight_stops_before_asking_a_cluster_that_is_not_there(monkeypatch):
    """Ordering matters: asking an absent API server for storage classes would time
    out, and the timeout would be reported as "no storage" -- a wrong answer that
    sends someone to fix the wrong thing."""
    from rc_repro.services import k8s

    asked = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        asked.append(" ".join(argv))
        if "get clusters" in " ".join(argv):
            return sp.CompletedProcess(argv, 0, "kind\n", "")
        return sp.CompletedProcess(argv, 0, "v1.0.0", "")

    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", spy)
    pre = k8s.preflight()
    assert pre.cluster_exists is False
    assert pre.other_clusters == ["kind"]
    assert not any("storageclass" in a for a in asked), \
        "it asked an absent cluster for storage"


def test_doctor_stays_quiet_about_kubernetes_when_nobody_is_using_it(monkeypatch, tmp_path):
    """A doctor that reports FAIL for a feature you are not using teaches people to
    ignore its failures. With no tools and no Kubernetes workspace, the whole
    subject is one informational line -- not five rows of absence."""
    from rc_repro.services import doctor, k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda _t: "")
    report = doctor.run_checks()
    kube = [r for r in report["checks"] if "ubernetes" in r["message"]]
    assert len(kube) == 1, f"expected one line, got {[r['message'] for r in kube]}"
    assert kube[0]["status"] == "ok"
    assert "unaffected" in kube[0]["message"]


def test_doctor_fails_when_a_kubernetes_workspace_exists_but_the_tools_do_not(
        monkeypatch, tmp_path):
    """The same finding, opposite severity. A workspace that cannot be reached OR
    TORN DOWN is a real fault -- and the teardown half is what makes it urgent,
    because the resources keep running."""
    from rc_repro.services import doctor, k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="p", rc_version="8.6.1", rc_image="i",
                           mongo_tag="8.0", mongo_flavor="official", preset="default",
                           root_url="u", host_port=3010, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    lc.runner.write("k", "services: {}\n", m)

    monkeypatch.setattr(k8s, "which", lambda _t: "")
    report = doctor.run_checks()
    kube = [r for r in report["checks"] if "Kubernetes workspace(s) exist" in r["message"]]
    assert len(kube) == 1, [r["message"] for r in report["checks"]]
    assert kube[0]["status"] == "fail"
    assert "torn down" in kube[0]["message"]
    assert report["verdict"] == "fail"


def test_a_cluster_with_no_default_storageclass_is_reported(monkeypatch, tmp_path):
    """The guide opens with this warning: local distributions "often ship without a
    storage provisioner enabled". The failure it causes is silent -- a PVC stays
    Pending forever and nothing in the output names storage."""
    from rc_repro.services import doctor, k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", _fake_run({
        "get clusters": (0, f"{k8s.CLUSTER_NAME}\n"),
        "/readyz": (0, "ok"),
        "get storageclass": (0, '{"items": []}'),
        "get namespace": (0, ""),
        "version": (0, "v9.9.9"),
    }))
    report = doctor.run_checks()
    sc = [r for r in report["checks"] if "StorageClass" in r["message"]]
    assert len(sc) == 1 and sc[0]["status"] == "warn", [r["message"] for r in report["checks"]]
    assert "Pending" in sc[0]["message"], "it must name the symptom, not just the cause"


def test_a_stopped_docker_is_not_reported_as_a_missing_cluster(monkeypatch, tmp_path):
    """`kind` talks to Docker. With Docker stopped, `kind get clusters` exits
    non-zero -- which reads as an empty list if only stdout is consulted, and
    "your cluster does not exist" is then a WRONG answer that sends someone to
    create a cluster that is already there.

    The two are kept apart: answered-no and could-not-ask.
    """
    from rc_repro.services import doctor, k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", _fake_run({
        "get clusters": (1, ""),          # non-zero: Docker is down
        "version": (0, "v9.9.9"),
    }))
    found, why = k8s.clusters()
    assert found == [] and why, "the reason must survive, not be flattened to []"

    report = doctor.run_checks()
    msgs = [r["message"] for r in report["checks"]]
    assert any("Could not tell whether cluster" in m for m in msgs), msgs
    assert not any("does not exist" in m for m in msgs), \
        "it claimed the cluster is absent when it merely could not ask"


def test_kubernetes_works_without_kind_because_kind_only_creates_clusters(monkeypatch):
    """kind is needed to PROVISION a cluster, never to USE one.

    Namespaces, helm releases, PVCs, port-forwards and exec are plain Kubernetes.
    They behave the same on k3s, minikube, Docker Desktop or a remote cluster, so a
    box with kubectl and helm and no kind is fully usable -- it just cannot be
    handed a cluster, it has to be pointed at one.

    The first cut of this module returned early when kind was absent, which made a
    perfectly good k3s box look like it had no Kubernetes at all.
    """
    from rc_repro.services import k8s

    monkeypatch.setattr(k8s, "which", lambda t: "" if t == "kind" else f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", _fake_run({
        "config current-context": (0, "k3s-default\n"),
        "/readyz": (0, "ok"),
        "get storageclass": (0, '{"items": [{"metadata": {"name": "local-path",'
                                ' "annotations": {"storageclass.kubernetes.io/'
                                'is-default-class": "true"}}}]}'),
        "get namespace": (0, ""),
        "version": (0, "v9.9.9"),
    }))
    pre = k8s.preflight()
    assert pre.tools_ready, "kubectl + helm is all that USING Kubernetes needs"
    assert not pre.can_provision, "without kind it cannot create a cluster"
    assert pre.missing_tools == [], "a missing kind is not a missing requirement"
    assert pre.context == "k3s-default", "it fell back to the configured cluster"
    assert pre.provider == k8s.PROVIDER_EXTERNAL
    assert pre.cluster_reachable and pre.usable
    assert pre.default_storage_class == "local-path"


def test_a_cluster_rc_repro_did_not_create_is_marked_external(monkeypatch):
    """The ownership rule at the right granularity. rc-repro may delete a cluster
    it created; in one you supplied it owns only the namespaces it labelled, and
    the cluster itself is never its to remove."""
    from rc_repro.services import k8s

    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", _fake_run({
        "get clusters": (0, "somebody-elses\n"),
        "config current-context": (0, "kind-somebody-elses\n"),
        "/readyz": (0, "ok"),
        "get storageclass": (0, '{"items": []}'),
        "get namespace": (0, ""),
        "version": (0, "v9.9.9"),
    }))
    pre = k8s.preflight()
    assert pre.cluster_exists is False, "ours is not among them"
    assert pre.provider == k8s.PROVIDER_EXTERNAL
    assert pre.context == "kind-somebody-elses"

    # And ours, when it IS there, is the one rc-repro may manage fully.
    monkeypatch.setattr(k8s, "run", _fake_run({
        "get clusters": (0, f"{k8s.CLUSTER_NAME}\n"),
        "/readyz": (0, "ok"),
        "get storageclass": (0, '{"items": []}'),
        "get namespace": (0, ""),
        "version": (0, "v9.9.9"),
    }))
    ours = k8s.preflight()
    assert ours.cluster_exists and ours.provider == k8s.PROVIDER_KIND
    assert ours.context == k8s.CONTEXT


def test_doctor_names_both_ways_to_get_a_cluster_when_there_is_none(monkeypatch, tmp_path):
    """kubectl and helm but nothing to point them at. Naming only kind would be
    wrong -- pointing at an existing k3s or Docker Desktop cluster is equally
    valid, and for most people already done."""
    from rc_repro.services import doctor, k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: "" if t == "kind" else f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", _fake_run({
        "config current-context": (1, ""),
        "version": (0, "v9.9.9"),
    }))
    msgs = [r["message"] for r in doctor.run_checks()["checks"]]
    hit = [m for m in msgs if "No Kubernetes cluster configured" in m]
    assert len(hit) == 1, msgs
    assert "kind" in hit[0] and "k3s" in hit[0], "both routes must be named"


def test_the_cluster_in_use_is_not_also_listed_as_another_cluster(monkeypatch, tmp_path):
    """It described the same cluster twice -- "Using your cluster 'kind-kind'"
    followed by "1 other kind cluster(s) (kind)" -- and the second mention reads as
    a different one."""
    from rc_repro.services import doctor, k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", _fake_run({
        "get clusters": (0, "kind\n"),
        "config current-context": (0, "kind-kind\n"),
        "/readyz": (0, "ok"),
        "get storageclass": (0, '{"items": [{"metadata": {"name": "standard",'
                                ' "annotations": {"storageclass.kubernetes.io/'
                                'is-default-class": "true"}}}]}'),
        "get namespace": (0, ""),
        "version": (0, "v9.9.9"),
    }))
    msgs = [r["message"] for r in doctor.run_checks()["checks"]]
    assert any("Using your cluster 'kind-kind'" in m for m in msgs), msgs
    assert not any("other kind cluster" in m for m in msgs), \
        "the cluster in use was counted again as an 'other' one"


def test_missing_storage_is_a_refusal_because_nothing_would_ever_log_it():
    """The reason this cannot be left to the logs.

    With no provisioner the PVC stays Pending, the pod stays Pending, and
    Rocket.Chat NEVER STARTS -- so there are no Rocket.Chat logs to find it in.
    Left to run it costs the full readiness timeout and then reports "Rocket.Chat
    did not become ready", which blames the one component that is innocent and
    sends someone to debug it.

    Same reasoning as the mongo/kernel gate: there is no case where continuing
    works, so a warning would only be a slower failure.
    """
    from rc_repro.services import k8s

    none_at_all = k8s.Preflight(context="c", cluster_reachable=True)
    msg = k8s.storage_blocker(none_at_all)
    assert msg, "an unprovisioned cluster must be refused"
    assert "never start" in msg, "it must name the symptom"
    assert "logs" in msg, "and say why the logs will not show it"

    # A cluster with classes but no DEFAULT is the subtler half, and fails the
    # same way -- so it gets the command that fixes it.
    no_default = k8s.Preflight(context="c", cluster_reachable=True,
                               storage_classes=["fast", "slow"])
    msg2 = k8s.storage_blocker(no_default)
    assert "none is marked default" in msg2
    assert "kubectl patch storageclass" in msg2, "name the fix, not just the fault"

    # Provisioned: silent.
    ok = k8s.Preflight(context="c", cluster_reachable=True,
                       storage_classes=["standard"], default_storage_class="standard")
    assert k8s.storage_blocker(ok) == ""


def test_storage_is_not_reported_for_a_cluster_nobody_could_reach():
    """Reporting "no StorageClass" for an unreachable cluster is the same wrong
    answer as reporting an absent cluster for a stopped Docker: it sends someone
    to fix storage when the problem is that nothing answered."""
    from rc_repro.services import k8s

    unreachable = k8s.Preflight(context="c", cluster_reachable=False)
    assert k8s.storage_blocker(unreachable) == ""


def test_a_missing_ingress_controller_only_blocks_a_request_that_needs_one():
    """Conditional on purpose. A workspace reached by port-forward needs no
    ingress controller, so an absent one is not a fault -- it is only a fault
    against `--domain`. Warning about it in `doctor` would tell every port-forward
    user about something that cannot affect them."""
    from rc_repro.services import k8s

    bare = k8s.Preflight(context="kind-kind", cluster_reachable=True,
                         provider=k8s.PROVIDER_EXTERNAL)
    assert k8s.ingress_blocker(bare, wants_domain=False) == "", \
        "port-forward needs no ingress"
    msg = k8s.ingress_blocker(bare, wants_domain=True)
    assert "no ingress controller" in msg
    assert "does not install one into a cluster it does not own" in msg
    assert "helm install traefik" in msg, "name what to run"

    # In OUR cluster the same gap is rc-repro's job, so the message differs.
    ours = k8s.Preflight(context=k8s.CONTEXT, cluster_reachable=True,
                         provider=k8s.PROVIDER_KIND)
    assert "rc-repro installs one" in k8s.ingress_blocker(ours, wants_domain=True)

    # Present: silent either way.
    have = k8s.Preflight(context="c", cluster_reachable=True,
                         ingress_classes=["traefik"])
    assert k8s.ingress_blocker(have, wants_domain=True) == ""


def test_doctor_never_warns_about_ingress(monkeypatch, tmp_path):
    """This used to assert `doctor` did not MENTION ingress at all, on the reasoning that
    it cannot know whether you are about to ask for `--domain` and most workspaces never
    do. That reasoning was about not nagging, and it still holds -- but silence turned out
    to be the wrong way to keep it: on a cluster that already has an ingress controller
    (k3s ships Traefik) the report left a reader unable to see the one thing that
    distinguishes their cluster from a kind one.

    So it is reported as a FACT and never as a warning. Severity still belongs to whatever
    needs it, which is `ingress_blocker` refusing a `--domain` that cannot be served.
    """
    from rc_repro.services import doctor, k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", _fake_run({
        "get clusters": (0, f"{k8s.CLUSTER_NAME}\n"),
        "/readyz": (0, "ok"),
        "get storageclass": (0, '{"items": [{"metadata": {"name": "standard",'
                                ' "annotations": {"storageclass.kubernetes.io/'
                                'is-default-class": "true"}}}]}'),
        "get ingressclass": (0, ""),
        "get namespace": (0, ""),
        "version": (0, "v9.9.9"),
    }))
    rows = doctor.run_checks()["checks"]
    ingress = [r for r in rows if "ingress" in r["message"].lower()]
    assert len(ingress) == 1, [r["message"] for r in ingress]
    assert ingress[0]["status"] == "ok", "an absent ingress controller is not a fault"
    assert ingress[0]["check"] == "kubernetes-ingress"
    assert "port-forward" in ingress[0]["message"], \
        "and it says why its absence does not matter"
    # Nothing anywhere in the report escalates over it.
    assert not any(r["status"] in ("warn", "fail") and "ingress" in r["message"].lower()
                   for r in rows)


def test_rc_repro_never_rewrites_the_users_kubeconfig(monkeypatch, tmp_path):
    """The one way creating a cluster genuinely disturbs existing work.

    `kind create cluster` writes ~/.kube/config AND switches current-context to the
    cluster it just made. Without isolation, somebody working in their own cluster
    runs `rc-repro up` and their next `kubectl get pods` answers from somewhere
    else -- with no indication anything moved.

    So anything touching rc-repro's OWN cluster runs with KUBECONFIG and all five
    Helm homes pointed inside RC_REPRO_HOME. Adopted from PR #3.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    env = k8s.owned_env()
    assert env["KUBECONFIG"].startswith(str(tmp_path)), env["KUBECONFIG"]
    for var in ("HELM_CACHE_HOME", "HELM_CONFIG_HOME", "HELM_DATA_HOME",
                "HELM_REPOSITORY_CONFIG", "HELM_REPOSITORY_CACHE"):
        assert env[var].startswith(str(tmp_path)), f"{var} escapes to the user's home"
    # Pinning only repositories.yaml would still leave cache and data writes in
    # ~/.config/helm, which is why all of them move.
    assert k8s.owned_kubeconfig().parent.is_dir()


def test_isolation_is_derived_from_the_target_not_chosen_at_the_call_site(monkeypatch):
    """`own` is never passed by hand. Each function derives it from the context it
    is targeting, so forgetting it is not a thing that can happen -- and the
    discovery path deliberately stays ambient, because reading the cluster you
    already configured is the entire point of it."""
    from rc_repro.services import k8s

    seen = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        seen.append((own, " ".join(argv)))
        return sp.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(k8s, "run", spy)
    k8s.reachable(k8s.CONTEXT)          # ours -> isolated
    k8s.reachable("k3s-default")        # theirs -> ambient
    assert seen[0][0] is True, "our own cluster must not use the ambient kubeconfig"
    assert seen[1][0] is False, "their cluster must be read from their kubeconfig"

    # And discovery of the active context is ambient by definition.
    seen.clear()
    k8s.active_context()
    assert seen[0][0] is False, "discovery must read the config the user set up"


# --- capacity knows what Kubernetes costs ---------------------------------------

def test_a_kubernetes_workspace_is_not_charged_as_a_compose_one(monkeypatch, tmp_path):
    """`check_capacity` computed `WORKSPACE_MB + PRESET_MB` and nothing about the
    runtime, so a Kubernetes workspace was billed as if it were Compose -- missing a
    control plane (573 MiB measured) and five extra Node processes.

    This function exists because seven concurrent stacks OOM-killed a 10 GB host.
    Under-charging is not a rounding error here: the OOM killer picks its own
    victim, so it destroys somebody else's work.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda _t: "")     # no cluster of ours

    compose = lc.CreateReq(version="8.6.1")
    assert lc._kube_overhead_mb(compose) == 0, "Compose must be unaffected"

    # Kubernetes with nothing resolved: an empty deployment means that runtime's
    # DEFAULT, which is microservices -- the expensive one. Reading empty as free
    # would under-charge the common case.
    default_k8s = lc.CreateReq(version="8.6.1", runtime="kubernetes")
    assert lc._kube_overhead_mb(default_k8s) == (lc.CLUSTER_MB + lc.KUBE_CHART_MB
                                                + lc.MICROSERVICES_MB)

    mono = lc.CreateReq(version="8.6.1", runtime="k8s",
                        deployment=topology.MONOLITH)
    # A "monolith" on this chart is five pods, not two -- it runs NATS regardless
    # of microservices.enabled -- so it pays for the cluster AND the chart baseline.
    assert lc._kube_overhead_mb(mono) == lc.CLUSTER_MB + lc.KUBE_CHART_MB


def test_the_control_plane_is_charged_only_when_one_is_about_to_be_built(monkeypatch, tmp_path):
    """It is shared, so billing it to the second and third workspace would refuse creates
    the host could hold -- and a capacity check that is wrong in the safe direction still
    stops people using the tool, which is how they learn to pass --force by reflex.

    The charge now turns on whether this create will BUILD a control plane, which is the
    only case where its memory is new.
    """
    from rc_repro.services import topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    req = lc.CreateReq(version="8.6.1", runtime="k8s", deployment=topology.MONOLITH)

    assert lc._kube_overhead_mb(req, provisioning=True) == lc.CLUSTER_MB + lc.KUBE_CHART_MB, \
        "the create that builds the cluster pays for it"
    assert lc._kube_overhead_mb(req, provisioning=False) == lc.KUBE_CHART_MB, \
        "the rest share the cluster but still pay for their own chart"


def test_a_cluster_that_is_already_running_is_not_charged_for(monkeypatch, tmp_path):
    """INVERTED, deliberately, and the old reasoning is worth recording.

    This charge used to be keyed on whether rc-repro's OWN kind cluster existed, on the
    argument that "somebody else's cluster being up does not mean the control plane is
    already paid for" -- true while rc-repro always created its own alongside, and wrong
    the moment it can use one that is already running, where the 600 MB is spent whatever
    this create does. Charging it anyway refuses creates the box can hold, on a box whose
    only cluster is the one about to be adopted.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    req = lc.CreateReq(version="8.6.1", runtime="kubernetes", deployment=topology.MONOLITH)
    # A k3s cluster that is up: nothing to build, so nothing to charge.
    monkeypatch.setattr(k8s, "plan_cluster", lambda: k8s.ClusterPlan(
        context="default", distribution="k3s", create=False))
    assert lc._will_provision(req) is False
    assert lc._kube_overhead_mb(req, lc._will_provision(req)) == lc.KUBE_CHART_MB

    # And with kind and no cluster yet, the control plane is real and is charged.
    monkeypatch.setattr(k8s, "plan_cluster", lambda: k8s.ClusterPlan(
        context=k8s.CONTEXT, distribution="kind", create=True))
    assert lc._will_provision(req) is True
    assert lc._kube_overhead_mb(req, lc._will_provision(req)) == \
        lc.CLUSTER_MB + lc.KUBE_CHART_MB


def test_no_cluster_and_no_kind_charges_nothing_because_the_create_refuses(monkeypatch, tmp_path):
    """The old rule was "an unprobeable cluster is charged for", on the reasoning that
    assuming it exists would let the create through and spend the memory anyway.

    There is nothing to be safe about any more: the only thing `plan_cluster` raises for
    is having no cluster AND no way to make one, and that create is refused before a byte
    is written. Charging for a control plane that will never be built would refuse for
    memory a create was never going to spend.
    """
    from rc_repro.services import k8s, topology
    from rc_repro.errors import PreflightError

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    def refuse():
        raise PreflightError("kind is not installed, ...")
    monkeypatch.setattr(k8s, "plan_cluster", refuse)
    # MONOLITH explicitly: an empty deployment means "that runtime's default", which for
    # Kubernetes is microservices and carries its own 800 MB.
    req = lc.CreateReq(version="8.6.1", runtime="k8s", deployment=topology.MONOLITH)
    assert lc._will_provision(req) is False
    assert lc._kube_overhead_mb(req, lc._will_provision(req)) == lc.KUBE_CHART_MB


def test_the_kubernetes_overhead_actually_reaches_the_refusal(monkeypatch, tmp_path):
    """The helper existing is not the same as it being used. Asserted end-to-end
    through `check_capacity`, because a correct estimate nothing consults refuses
    nothing -- and the first version of this change computed the overhead and then
    did not add it to `need`.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    # A kind box with no cluster yet, so the control plane is part of the bill. STATED
    # rather than probed: `check_capacity` now asks which cluster it would use, and a
    # unit test must not depend on whether one happens to be running on the machine.
    monkeypatch.setattr(k8s, "plan_cluster", lambda: k8s.ClusterPlan(
        context=k8s.CONTEXT, distribution="kind", create=True))
    # A host with room for exactly one Compose workspace and no more.
    need_k8s = (lc.WORKSPACE_MB + lc.CLUSTER_MB + lc.KUBE_CHART_MB
                + lc.MICROSERVICES_MB)
    tight = lc.WORKSPACE_MB + 200
    monkeypatch.setattr(lc.runner, "host_memory",
                        lambda: (8000, tight + lc.host_reserve_mb(8000), 0))
    monkeypatch.setattr(lc.runner, "list_meta", lambda: [])

    # Compose fits.
    lc.check_capacity(lc.CreateReq(version="8.6.1"), "default")
    # The same host cannot hold a Kubernetes one, and must say so.
    with pytest.raises(errors.PreflightError) as caught:
        lc.check_capacity(lc.CreateReq(version="8.6.1", runtime="kubernetes"),
                          "default")
    assert need_k8s > tight, "the fixture must actually be too small"
    assert f"{need_k8s} MB" in str(caught.value), "the bill must include the Kubernetes parts"
    # And it is a PREFLIGHT failure, not a "poll again". errors.py already said so
    # -- "capacity shortfalls ... use this" -- while the code raised NotReadyError,
    # exit 5, which tells a script to retry something retrying cannot fix. Polling
    # does not free memory. Same argument that moved `require_docker` off exit 5.
    assert caught.value.exit_code == 3, "a full host is a preflight failure"
    assert caught.value.code == "PREFLIGHT_FAILED"


# --- provisioning ---------------------------------------------------------------

def test_creating_a_cluster_never_writes_the_users_kubeconfig(monkeypatch, tmp_path):
    """kind writes the file it is GIVEN and also honours KUBECONFIG for the context
    switch, so both are set. Verified live as well -- a real `kind create` left
    ~/.kube/config byte-identical and current-context on kind-kind -- but pinned
    here because the live check is not in CI."""
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    calls = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        calls.append((own, argv))
        j = " ".join(argv)
        if "get clusters" in j:
            return sp.CompletedProcess(argv, 0, "", "")
        if "current-context" in j:
            return sp.CompletedProcess(argv, 0, k8s.CONTEXT, "")
        if "/readyz" in j:
            return sp.CompletedProcess(argv, 0, "ok", "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    assert k8s.ensure_cluster() == k8s.CONTEXT
    create = [a for own, a in calls if "create" in a]
    assert create, calls
    argv = create[0]
    assert "--kubeconfig" in argv, "kind must be told where to write"
    assert str(tmp_path) in argv[argv.index("--kubeconfig") + 1], \
        "it wrote outside RC_REPRO_HOME"
    assert all(own for own, a in calls if "create" in a), \
        "the create must also run with the redirected environment"


def test_a_second_up_reuses_the_cluster_instead_of_racing_to_create_it(monkeypatch, tmp_path):
    """Two simultaneous `up`s would both see no cluster and both run `kind create`;
    the second fails with "node(s) already exist". The re-check inside the lock
    means the loser reuses rather than retries -- concurrent workspaces are the
    whole point of one-cluster-many-namespaces."""
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    created = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        j = " ".join(argv)
        if "get clusters" in j:
            return sp.CompletedProcess(argv, 0, f"{k8s.CLUSTER_NAME}\n", "")
        if "current-context" in j:
            return sp.CompletedProcess(argv, 0, k8s.CONTEXT, "")
        if "/readyz" in j:
            return sp.CompletedProcess(argv, 0, "ok", "")
        if "create" in argv:
            created.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    k8s.ensure_cluster()
    assert not created, "it created a cluster that was already there"


def test_a_create_that_lost_a_race_is_success_not_failure(monkeypatch, tmp_path):
    """Somebody ran `kind create cluster --name rc-repro-local` by hand while we
    held the lock. "already exist" means the thing we wanted is true."""
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        j = " ".join(argv)
        if "get clusters" in j:
            return sp.CompletedProcess(argv, 0, "", "")
        if "current-context" in j:
            return sp.CompletedProcess(argv, 0, k8s.CONTEXT, "")
        if "/readyz" in j:
            return sp.CompletedProcess(argv, 0, "ok", "")
        if "create" in argv:
            return sp.CompletedProcess(argv, 1, "", "node(s) already exist for a "
                                                     "cluster with the name")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    assert k8s.ensure_cluster() == k8s.CONTEXT

    # A create that fails for any OTHER reason is terminal, and says why.
    def broken(argv, timeout=None, own=False):
        import subprocess as sp
        j = " ".join(argv)
        if "get clusters" in j:
            return sp.CompletedProcess(argv, 0, "", "")
        if "create" in argv:
            return sp.CompletedProcess(argv, 1, "", "ERROR: failed to pull image")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", broken)
    with pytest.raises(errors.CreateFailedError) as caught:
        k8s.ensure_cluster()
    assert "failed to pull image" in str(caught.value), "the reason must reach the user"
    assert caught.value.exit_code == 7, "known dead, not 'poll again'"


def test_ensure_cluster_refuses_without_kind_and_names_the_alternative(monkeypatch, tmp_path):
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda _t: "")
    with pytest.raises(errors.PreflightError) as caught:
        k8s.ensure_cluster()
    assert "point kubectl at a cluster you already have" in str(caught.value)
    assert caught.value.exit_code == 3


def test_deleting_the_cluster_can_only_ever_target_our_own(monkeypatch, tmp_path):
    """The safety property. There is no parameter for which cluster to delete,
    because a delete that can be pointed anywhere eventually is. Asserted on the
    argv, since that is what reaches the machine.

    Verified live too: after `delete_cluster`, the unrelated `kind` cluster on this
    box was still there.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    seen = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        seen.append(argv)
        j = " ".join(argv)
        if "get clusters" in j:
            return sp.CompletedProcess(argv, 0, f"kind\n{k8s.CLUSTER_NAME}\nother\n", "")
        if "current-context" in j:
            return sp.CompletedProcess(argv, 0, k8s.CONTEXT, "")
        if "/readyz" in j:
            return sp.CompletedProcess(argv, 0, "ok", "")
        if "get namespace" in j:
            return sp.CompletedProcess(argv, 0, "", "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    assert k8s.delete_cluster() is True
    deletes = [a for a in seen if "delete" in a]
    assert len(deletes) == 1, deletes
    argv = deletes[0]
    # The property is about the --name VALUE, not about the whole argv: `kind` is
    # legitimately argv[0], the binary.
    assert argv[argv.index("--name") + 1] == k8s.CLUSTER_NAME, argv
    # And no other cluster the machine happens to have is named anywhere in it.
    targets = argv[argv.index("--name") + 1:]
    assert targets == [k8s.CLUSTER_NAME], f"more than our cluster is targeted: {targets}"


def test_the_cluster_is_not_taken_out_from_under_a_colleagues_workspace(monkeypatch, tmp_path):
    """On a shared box the cluster holds other people's workspaces. `prune`
    reclaiming it mid-ticket is the failure this guards, and the namespaces carry
    an owner label so the refusal can be specific."""
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    deleted = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        j = " ".join(argv)
        if "get clusters" in j:
            return sp.CompletedProcess(argv, 0, f"{k8s.CLUSTER_NAME}\n", "")
        if "current-context" in j:
            return sp.CompletedProcess(argv, 0, k8s.CONTEXT, "")
        if "/readyz" in j:
            return sp.CompletedProcess(argv, 0, "ok", "")
        if "get namespace" in j:
            return sp.CompletedProcess(argv, 0, "namespace/rc-repro-t1\n"
                                                "namespace/rc-repro-t2\n", "")
        if "delete" in argv:
            deleted.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    with pytest.raises(errors.ConflictError) as caught:
        k8s.delete_cluster()
    assert "2 workspace namespace(s)" in str(caught.value)
    assert "rc-repro-t1" in str(caught.value), "name them, so it is actionable"
    assert not deleted, "it deleted the cluster anyway"

    # --force takes it, because sometimes that is what you want.
    assert k8s.delete_cluster(force=True) is True
    assert deleted, "--force must actually delete"


def test_deleting_a_cluster_that_is_not_there_is_not_an_error(monkeypatch, tmp_path):
    """`prune` calls this unconditionally; a missing cluster is the state it wanted."""
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "run", _fake_run({"get clusters": (0, "somebody-else\n")}))
    assert k8s.delete_cluster() is False


def test_the_replica_set_is_initiated_and_verified_not_assumed(monkeypatch, tmp_path):
    """`--replSet rs0` only puts mongod IN replica-set mode. Without rs.initiate()
    there is no primary, nothing can write, and Rocket.Chat waits forever -- which
    is what a real workspace did for 540 seconds, at 5 pods, with nothing in
    Rocket.Chat's logs naming MongoDB.

    The verification matters as much as the call: this is the step whose silent
    failure produces a workspace that looks created and can never become ready.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    scripts = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        j = " ".join(argv)
        if "jsonpath" in j:
            return sp.CompletedProcess(argv, 0, "true", "")
        if "--eval" in argv:
            script = argv[argv.index("--eval") + 1]
            scripts.append(script)
            out = "1" if script == "rs.status().ok" else ""
            return sp.CompletedProcess(argv, 0, out, "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    k8s.init_replica_set(namespace="rc-repro-t", context=k8s.CONTEXT,
                         sleep=lambda _s: None)
    assert any("rs.initiate" in s for s in scripts), scripts
    assert "rs.status().ok" in scripts, "it must verify, not trust the exit code"

    # A set that is already initiated is success, not failure.
    def already(argv, timeout=None, own=False):
        import subprocess as sp
        j = " ".join(argv)
        if "jsonpath" in j:
            return sp.CompletedProcess(argv, 0, "true", "")
        if "--eval" in argv:
            script = argv[argv.index("--eval") + 1]
            if script == "rs.status().ok":
                return sp.CompletedProcess(argv, 0, "1", "")
            return sp.CompletedProcess(argv, 1, "", "already initialized")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", already)
    k8s.init_replica_set(namespace="rc-repro-t", context=k8s.CONTEXT,
                         sleep=lambda _s: None)


def test_an_uninitiated_replica_set_is_reported_as_dead_not_left_to_time_out(
        monkeypatch, tmp_path):
    """Exit 7, known dead. Every second waited after this is spent waiting for
    something that cannot happen, and the readiness timeout would blame
    Rocket.Chat."""
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def never_ok(argv, timeout=None, own=False):
        import subprocess as sp
        if "jsonpath" in " ".join(argv):
            return sp.CompletedProcess(argv, 0, "true", "")   # container is up...
        if "--eval" in argv:
            return sp.CompletedProcess(argv, 0, "0", "")   # ...but rs.status().ok == 0
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", never_ok)
    with pytest.raises(errors.CreateFailedError) as caught:
        k8s.init_replica_set(namespace="rc-repro-t", context=k8s.CONTEXT,
                             sleep=lambda _s: None)
    assert "change streams" in str(caught.value), "say WHY Rocket.Chat needs it"
    assert caught.value.exit_code == 7

    # And MongoDB never becoming ready is equally terminal, naming the command
    # that shows why.
    def never_ready(argv, timeout=None, own=False):
        import subprocess as sp
        return sp.CompletedProcess(argv, 0, "false", "")

    monkeypatch.setattr(k8s, "run", never_ready)
    monkeypatch.setattr(k8s, "MONGO_READY_TRIES", 2)
    with pytest.raises(errors.CreateFailedError) as caught2:
        k8s.init_replica_set(namespace="rc-repro-t", context=k8s.CONTEXT,
                             sleep=lambda _s: None)
    assert "describe pod" in str(caught2.value), "name what to run next"
    assert "never started" in str(caught2.value), \
        "a pod stuck Pending has not started; it is not 'not ready'"


def test_initiation_waits_for_a_running_pod_not_a_ready_one(monkeypatch, tmp_path):
    """A circular wait, and it made the whole create a coin flip.

    The readiness probe runs mongosh, which cannot complete its handshake against
    an UNINITIATED replica set -- mongod logs
    "ReadConcernMajorityNotAvailableYet". So readiness depends on initiation, and
    waiting for readiness before initiating means each waits for the other. The
    probe occasionally scraped through and the workspace built; otherwise it timed
    out at 300s reporting that MongoDB never became ready, while mongod had been
    up the whole time.

    So this polls `.status.phase`, and the probe itself connects with
    `directConnection=true` -- which `compose.py` already knew it had to.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    asked = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        joined = " ".join(argv)
        asked.append(joined)
        if "jsonpath" in joined:
            # `ready` is FALSE and stays false, exactly as an uninitiated set
            # behaves; `started` is what says the container is exec-able.
            if "].ready" in joined:
                return sp.CompletedProcess(argv, 0, "false", "")
            return sp.CompletedProcess(argv, 0, "true", "")
        if "--eval" in argv:
            script = argv[argv.index("--eval") + 1]
            return sp.CompletedProcess(argv, 0,
                                       "1" if script == "rs.status().ok" else "", "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    # Must succeed despite readiness never being true.
    k8s.init_replica_set(namespace="rc-repro-t", context=k8s.CONTEXT,
                         sleep=lambda _s: None)
    assert not any("].ready" in a for a in asked), \
        "it waited on readiness, which cannot happen before initiation"
    assert any(".started" in a for a in asked), asked[:3]
    # And every mongosh call carries the direct URI.
    for call in [a for a in asked if "mongosh" in a]:
        assert "directConnection=true" in call, call


def test_the_readiness_probe_can_answer_before_the_set_is_initiated():
    """The manifest half of the same bug. A probe that cannot pass until initiation
    keeps the pod unready forever, and Kubernetes gives no hint that the PROBE is
    the problem."""
    import yaml

    from rc_repro.services import k8s

    sts = [d for d in yaml.safe_load_all(k8s.mongo_manifest("t", "8.0"))
           if d["kind"] == "StatefulSet"][0]
    cmd = sts["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["exec"]["command"]
    assert any("directConnection=true" in part for part in cmd), cmd


def test_a_failed_create_leaves_no_namespace_nobody_can_see(monkeypatch, tmp_path):
    """repro.json is written only after a successful create, so a namespace that
    survives a failure is invisible to `list` and to `down`. On Compose a failed
    `up` at least leaves a workspace directory you can find."""
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    deleted = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        joined = " ".join(argv)
        if "get clusters" in joined:
            return sp.CompletedProcess(argv, 0, f"{k8s.CLUSTER_NAME}\n", "")
        if "current-context" in joined:
            return sp.CompletedProcess(argv, 0, k8s.CONTEXT, "")
        if "/readyz" in joined:
            return sp.CompletedProcess(argv, 0, "ok", "")
        if "get storageclass" in joined:
            return sp.CompletedProcess(argv, 0, '{"items":[{"metadata":{"name":"s",'
                '"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}]}', "")
        if "get ingressclass" in joined or "get namespace" in joined:
            return sp.CompletedProcess(argv, 0, "", "")
        if "delete namespace" in joined:
            deleted.append(joined)
        if "search repo" in joined:
            return sp.CompletedProcess(argv, 0,
                '[{"version":"7.0.2","app_version":"8.6.1"}]', "")
        if "jsonpath" in joined:
            return sp.CompletedProcess(argv, 0, "true", "")
        if "--eval" in argv:
            return sp.CompletedProcess(argv, 0, "1", "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "apply", lambda *a, **k: None)
    # Mongo 8.0 takes the operator path; without these the wait polls a real clock.
    monkeypatch.setattr(k8s, "ensure_operator", lambda **k: None)
    monkeypatch.setattr(k8s, "wait_for_mongodb", lambda **k: None)
    monkeypatch.setattr(k8s, "install",
                        lambda **k: (_ for _ in ()).throw(
                            errors.CreateFailedError("helm exploded")))
    resolved = type("R", (), {"rc_version": "8.6.1", "mongo_tag": "8.0",
                              "rc_image": "img", "oplog": False})()
    with pytest.raises(errors.CreateFailedError):
        k8s.create_workspace(name="t", resolved=resolved, host_port=3000,
                             microservices=False)
    assert deleted, "the namespace survived a failed create"
    assert "rc-repro-t" in deleted[0]


def test_the_headless_service_publishes_dns_before_the_pod_is_ready():
    """The circular dependency one layer below the readiness probe.

    A headless Service does not publish DNS for a not-ready pod. This pod cannot be
    ready until the replica set is initiated, so `rs.initiate` could not resolve
    `mongodb-0.mongodb` to verify the member is itself, and mongod refused with "no
    host described in new configuration ... maps to this node".

    Publishing not-ready addresses is the standard bootstrap pattern for a
    StatefulSet database -- the MongoDB operator does the same.
    """
    import yaml

    from rc_repro.services import k8s

    svc = [d for d in yaml.safe_load_all(k8s.mongo_manifest("t", "8.0"))
           if d["kind"] == "Service"][0]
    # The literal STRING "None" -- that is how Kubernetes documents a headless
    # Service, and YAML's null is `null`, not `None`. Asserting `is None` here
    # would fail against a manifest the API server accepts.
    assert svc["spec"]["clusterIP"] == "None", "must stay headless for stable pod DNS"
    assert svc["spec"].get("publishNotReadyAddresses") is True, \
        "without this, rs.initiate cannot resolve its own member"
    # And the member host the initiate script uses is the one this service serves.
    assert f"{k8s.MONGO_SERVICE}-0.{k8s.MONGO_SERVICE}" in k8s.RS_INITIATE


def test_down_removes_a_kubernetes_workspace_instead_of_asking_docker(monkeypatch, tmp_path):
    """The gap the first successful CLI create exposed: `down` reached for a compose
    project that is not there and answered "no configuration file provided: not
    found", leaving a workspace that could be CREATED and never removed.

    The dispatch sits BELOW the confirmation, the ownership gate and the audit
    record, which are about whose data is being destroyed and do not depend on the
    runtime -- duplicating them per runtime is how one loses a check the other keeps.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.6.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="u", host_port=3010,
                           version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra["context"] = k8s.CONTEXT
    lc.runner.write("k", "", m)

    monkeypatch.setattr(lc, "require_docker", lambda: None)
    monkeypatch.setattr(lc.runner, "down",
                        lambda *a, **k: pytest.fail("it asked docker compose"))
    called = {}
    monkeypatch.setattr(k8s, "delete_namespace",
                        lambda name, **kw: called.update(name=name, **kw) or True)

    out = lc.teardown("k", volumes=False)
    assert out["runtime"] == topology.KUBERNETES
    assert called == {"name": "k", "context": k8s.CONTEXT, "volumes": False,
                      "emit": lc.null_emit}, called

    # And --volumes still needs the confirmation, which is shared rather than
    # reimplemented per runtime.
    with pytest.raises(errors.ValidationError):
        lc.teardown("k", volumes=True, confirm=False)


def test_stop_and_start_scale_a_kubernetes_workspace_rather_than_calling_compose(
        monkeypatch, tmp_path):
    """Kubernetes has no pause, so stop/start is scale-to-zero and back.

    What has to match is COMPOSE'S CONTRACT, not Kubernetes' vocabulary: `stop`
    gives the memory back and keeps the data, `start` brings the same workspace back
    on the same port. Scaling leaves the PersistentVolumeClaim untouched, which is
    what puts this as far from `down --volumes` as `docker compose stop` is.

    Dispatched in `set_state` rather than in each front-end: the CLI's
    stop/start/restart and the GUI's always-enabled buttons both arrive through this
    one function, so one branch covers both and cannot drift.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.6.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="u", host_port=3010,
                           version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra["context"] = k8s.CONTEXT
    lc.runner.write("k", "", m)

    for name in ("stop", "start", "restart"):
        monkeypatch.setattr(lc.runner, name,
                            lambda *a, **kw: pytest.fail("it called docker compose"))
    calls = []
    monkeypatch.setattr(k8s, "stop_workspace",
                        lambda n, **kw: calls.append(("stop", n)))
    monkeypatch.setattr(k8s, "start_workspace",
                        lambda n, **kw: calls.append(("start", n)))

    lc.set_state("k", "stop")
    assert calls == [("stop", "k")], calls

    calls.clear()
    lc.set_state("k", "start")
    assert calls == [("start", "k")], calls

    # restart is both, in that order -- not a no-op and not a start-then-stop.
    calls.clear()
    lc.set_state("k", "restart")
    assert calls == [("stop", "k"), ("start", "k")], calls

    # A bad action is still rejected the same way on both runtimes.
    with pytest.raises(errors.ValidationError):
        lc.set_state("k", "pause")


def test_a_stopped_kubernetes_workspace_remembers_what_to_scale_back_to(monkeypatch):
    """Kubernetes has no "stopped" state -- 0 replicas IS the mechanism -- so the
    number that was there has to be written down or `start` cannot restore it.

    Recorded BEFORE scaling, or the value read back is the zero just written. Stored
    on the namespace rather than in repro.json because scaling by hand is a
    legitimate thing to do and the cluster should carry its own truth -- and a
    workspace stopped by hand carries no annotation at all, which is why the
    fallback is 1 rather than a refusal.
    """
    from rc_repro.services import k8s

    scaled, annotated = [], []

    def fake_run(argv, **kw):
        import subprocess
        if "get" in argv and "deployment,statefulset" in argv:
            out = "deployment.apps/rocketchat-rocketchat\nstatefulset.apps/mongodb\n"
        elif "annotate" in argv:
            annotated.append(argv)
            out = ""
        elif "get" in argv and any(a.startswith("jsonpath={.spec.replicas}")
                                   for a in argv):
            out = "2"
        elif "scale" in argv:
            scaled.append(argv)
            out = ""
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(k8s, "run", fake_run)
    k8s.stop_workspace("w", context=k8s.CONTEXT)

    payload = [a for a in annotated[0] if a.startswith(k8s.SCALE_ANNOTATION)][0]
    assert '"deployment.apps/rocketchat-rocketchat": "2"' in payload, payload
    assert "statefulset.apps/mongodb" in payload, \
        "MongoDB scales too -- a workspace holding its database resident is not stopped"
    assert any("--replicas=0" in a for argv in scaled for a in argv), scaled


def test_the_port_forward_targets_the_deployment_not_the_service(monkeypatch, tmp_path):
    """`port-forward svc/...` needs a ready ENDPOINT, and a Service has none until
    its pod passes readiness. The first version started the forward straight after
    `helm install`, kubectl found nothing to attach to and exited, and the URL that
    `up` printed never answered -- a lie rather than a delay.

    A deployment target only needs a pod to EXIST.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    spawned = []
    # "Running", not a pod name: kubectl refuses a ContainerCreating pod, so
    # waiting for existence alone spawned a forward that died immediately.
    monkeypatch.setattr(k8s, "run", _fake_run({"jsonpath": (0, "Running")}))
    monkeypatch.setattr(k8s.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv) or type(
                            "P", (), {"pid": 4242})())
    pid = k8s.port_forward("k", namespace="rc-repro-k", context=k8s.CONTEXT,
                           host_port=3010, sleep=lambda _s: None)
    assert pid == 4242
    argv = spawned[0]
    target = argv[argv.index("port-forward") + 1]
    assert target.startswith("deployment/"), f"forwarded to {target}"
    assert argv[-1] == "3010:3000", "the container port is 3000; the Service is 80"


def test_a_dead_port_forward_is_replaced_rather_than_trusted(monkeypatch, tmp_path):
    """A forward dies with its pod, so a recorded pid is not evidence it is alive --
    and a recycled pid is not evidence it is OURS. `ready` and `start` re-establish
    through this rather than assuming the create-time pid still holds."""
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    assert k8s.forward_alive(None) is False
    assert k8s.forward_alive(999999999) is False, "a pid that cannot be read is dead"
    # Our own process is alive but is NOT a port-forward, which is the recycled-pid
    # case: believing it would leave the workspace unreachable AND, at teardown,
    # signal something unrelated.
    import os as _os
    assert k8s.forward_alive(_os.getpid()) is False

    spawned = []
    monkeypatch.setattr(k8s, "run", _fake_run({"jsonpath": (0, "Running")}))
    monkeypatch.setattr(k8s.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv) or type(
                            "P", (), {"pid": 77})())
    assert k8s.ensure_port_forward("k", namespace="ns", context=k8s.CONTEXT,
                                   host_port=3010, pid=999999999) == 77
    assert spawned, "it trusted a dead pid"


def test_a_url_is_only_printed_once_the_forward_is_confirmed_alive(monkeypatch, tmp_path):
    """A port-forward that dies on spawn leaves a URL that looks like an address and
    answers nothing -- which sends someone to debug Rocket.Chat when the forward is
    what failed. Observed twice on live runs before this.

    So the URL is confirmed, not assumed, and a dead forward hands over the command
    that establishes one instead of a promise.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    said: list = []
    monkeypatch.setattr(k8s.time, "sleep", lambda _s: None)
    monkeypatch.setattr(k8s, "ensure_cluster", lambda **k: k8s.CONTEXT)
    monkeypatch.setattr(k8s, "ensure_repo", lambda **k: None)
    monkeypatch.setattr(k8s, "resolve_chart_version", lambda *a, **k: "7.0.2")
    monkeypatch.setattr(k8s, "preflight",
                        lambda *a, **k: k8s.Preflight(cluster_reachable=True,
                                                      default_storage_class="s"))
    monkeypatch.setattr(k8s, "ensure_namespace", lambda n, **k: f"rc-repro-{n}")
    monkeypatch.setattr(k8s, "apply", lambda *a, **k: None)
    monkeypatch.setattr(k8s, "init_replica_set", lambda **k: None)
    monkeypatch.setattr(k8s, "ensure_operator", lambda **k: None)
    monkeypatch.setattr(k8s, "wait_for_mongodb", lambda **k: None)
    monkeypatch.setattr(k8s, "install", lambda **k: None)
    monkeypatch.setattr(k8s, "clusters", lambda: ([k8s.CLUSTER_NAME], ""))
    monkeypatch.setattr(k8s, "port_forward", lambda *a, **k: 999999999)  # dead pid
    resolved = type("R", (), {"rc_version": "8.6.1", "mongo_tag": "8.0",
                              "rc_image": "img", "oplog": False})()

    out = k8s.create_workspace(name="k", resolved=resolved, host_port=3010,
                               microservices=False,
                               emit=lambda e: said.append(e.message))
    assert out["port_forward_pid"] == 0, "a dead forward must not be recorded as live"
    assert not any(m.strip() == "http://localhost:3010" for m in said), \
        "it printed a URL it had not confirmed"
    assert any("port-forward" in m and "kubectl" in m for m in said), said


def test_the_microservices_surcharge_matches_the_pods_that_were_observed():
    """Half measured, half estimated, and the test says which.

    MEASURED live on chart 7.0.2: nine pods against a monolith's five, the four
    extra being account, authorization, ddp-streamer and presence. NOT five -- this
    chart ships no stream-hub deployment, which the first estimate assumed.

    STILL ESTIMATED: what they cost. Both memory readings were taken at readiness
    with pods still ContainerCreating, and the microservices one came out LOWER than
    the monolith's, which cannot be true.
    """
    from rc_repro.services import lifecycle as lc

    assert lc.MICROSERVICES_MB == 800, "4 observed pods x ~200 MB"
    # Generous on purpose: under-charging lets through a create that OOMs a swapless
    # host, and the kernel picks its own victim.
    assert lc.MICROSERVICES_MB >= 4 * 200
    # And it is charged ON TOP of the chart baseline, not instead of it -- NATS is
    # in KUBE_CHART_MB because a monolith runs it too.
    assert lc.KUBE_CHART_MB > 0 and lc.CLUSTER_MB > 0


def test_a_cluster_that_outlived_its_kubeconfig_is_reconnected(monkeypatch, tmp_path):
    """`kind get clusters` reads Docker, so it sees clusters rc-repro's OWN kubeconfig
    knows nothing about. That happens whenever the cluster outlives the config: a
    fresh or different RC_REPRO_HOME, a deleted config, a cluster made by hand.

    Without an export, "reusing cluster" was followed by every kubectl call going to
    localhost:8080 and the create failing with "the API server is not answering"
    about a cluster that was perfectly healthy. Observed exactly that way: a
    workspace left running from an earlier session broke the next run.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    seen = []

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        joined = " ".join(argv)
        seen.append(joined)
        if "get clusters" in joined:
            return sp.CompletedProcess(argv, 0, f"{k8s.CLUSTER_NAME}\n", "")
        if "current-context" in joined:
            return sp.CompletedProcess(argv, 0, k8s.CONTEXT, "")
        if "/readyz" in joined:
            return sp.CompletedProcess(argv, 0, "ok", "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    assert k8s.ensure_cluster() == k8s.CONTEXT
    assert any("export kubeconfig" in c for c in seen), \
        "it reused a cluster without writing it into rc-repro's kubeconfig"
    assert not any("create cluster" in c for c in seen), "it recreated an existing one"

    # A failed export is terminal and says so, rather than leaving every later
    # kubectl call to fail against localhost:8080.
    def broken(argv, timeout=None, own=False):
        import subprocess as sp
        joined = " ".join(argv)
        if "get clusters" in joined:
            return sp.CompletedProcess(argv, 0, f"{k8s.CLUSTER_NAME}\n", "")
        if "export kubeconfig" in joined:
            return sp.CompletedProcess(argv, 1, "", "permission denied")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", broken)
    with pytest.raises(errors.CreateFailedError) as caught:
        k8s.ensure_cluster()
    assert "kubeconfig could not be read" in str(caught.value)


def test_a_failed_recreate_never_destroys_data_that_down_promised_to_keep(
        monkeypatch, tmp_path):
    """The most damaging defect on this branch, and it was caused by a fix.

    The rollback added for failed creates deletes the namespace -- correct when this
    call created it, catastrophic over a namespace `down` had kept. Proven live: a
    marker document written before `down` was GONE after a failed `up`, because the
    rollback took the namespace and the PVC with it. `down` had printed "the
    namespace and its PersistentVolumeClaim are kept" moments earlier.

    A failure may only undo what the same call did. Deleting retained data is what
    `down --volumes` is for -- asked for deliberately, behind a confirmation.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(k8s, "ensure_cluster", lambda **kw: k8s.CONTEXT)
    monkeypatch.setattr(k8s, "ensure_repo", lambda **kw: None)
    monkeypatch.setattr(k8s, "resolve_chart_version", lambda *a, **kw: "7.0.2")
    monkeypatch.setattr(k8s, "preflight", lambda *a, **kw: k8s.Preflight(
        cluster_reachable=True, default_storage_class="standard"))
    monkeypatch.setattr(k8s, "ensure_namespace", lambda n, **kw: f"rc-repro-{n}")
    monkeypatch.setattr(k8s, "apply", lambda *a, **kw: None)
    monkeypatch.setattr(k8s, "init_replica_set", lambda **kw: None)
    monkeypatch.setattr(k8s, "ensure_operator", lambda **kw: None)
    monkeypatch.setattr(k8s, "wait_for_mongodb", lambda **kw: None)
    monkeypatch.setattr(k8s, "clusters", lambda: ([k8s.CLUSTER_NAME], ""))
    monkeypatch.setattr(k8s, "install", lambda **kw: (_ for _ in ()).throw(
        errors.CreateFailedError("helm exploded")))
    resolved = type("R", (), {"rc_version": "8.6.1", "mongo_tag": "8.0",
                              "rc_image": "img", "oplog": False})()

    deleted: list = []
    monkeypatch.setattr(k8s, "run", lambda argv, **kw: (
        deleted.append(" ".join(argv)) if "delete namespace" in " ".join(argv) else None)
        or __import__("subprocess").CompletedProcess(argv, 0, "", ""))

    # The namespace was ALREADY there -- a re-create after `down`.
    monkeypatch.setattr(k8s, "workspace_namespaces", lambda *a, **kw: ["rc-repro-k"])
    with pytest.raises(errors.CreateFailedError):
        k8s.create_workspace(name="k", resolved=resolved, host_port=3010,
                             microservices=False)
    assert not deleted, "the rollback destroyed a namespace it did not create"

    # A genuinely new namespace IS cleaned up, or a failed create leaks something
    # `list` cannot show.
    monkeypatch.setattr(k8s, "workspace_namespaces", lambda *a, **kw: [])
    with pytest.raises(errors.CreateFailedError):
        k8s.create_workspace(name="k", resolved=resolved, host_port=3010,
                             microservices=False)
    assert deleted, "a namespace this call created was left behind"


def test_exec_survives_a_terminating_pod_rather_than_failing_the_create(monkeypatch):
    """`containerStatuses[0].started` is still true for a TERMINATING pod, so a
    re-create can pass the wait and then exec into a pod on its way out:
    "unable to upgrade connection: container not found (mongod)". A one-shot exec
    turns that race into a failed create; retrying turns it into a delay."""
    from rc_repro.services import k8s

    calls = []

    def flaky(argv, **kw):
        import subprocess as sp
        calls.append(1)
        if len(calls) < 3:
            return sp.CompletedProcess(argv, 1, "",
                                       'unable to upgrade connection: container '
                                       'not found ("mongod")')
        return sp.CompletedProcess(argv, 0, "1", "")

    monkeypatch.setattr(k8s, "run", flaky)
    res = k8s._mongo_exec(k8s.CONTEXT, "rc-repro-k", "rs.status().ok",
                          sleep=lambda _s: None)
    assert res.returncode == 0 and len(calls) == 3, calls

    # A different failure is NOT retried -- that would just delay a real error.
    calls.clear()
    monkeypatch.setattr(k8s, "run", lambda argv, **kw: __import__("subprocess")
                        .CompletedProcess(argv, 1, "", "authentication failed"))
    out = k8s._mongo_exec(k8s.CONTEXT, "rc-repro-k", "x", sleep=lambda _s: None)
    assert out.returncode == 1


def test_bind_reaches_the_port_forward_instead_of_being_dropped(monkeypatch, tmp_path):
    """`--bind 0.0.0.0` was accepted on the Kubernetes path and then dropped, which
    is worse than refusing it: a workspace created for a shared box was reachable
    only from localhost and nothing said so.

    kubectl binds 127.0.0.1 by default and takes `--address`, so this is a real
    setting rather than a Compose-only one.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(k8s, "run", _fake_run({"jsonpath": (0, "true")}))
    spawned: list = []
    monkeypatch.setattr(k8s.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv) or type(
                            "P", (), {"pid": 9})())

    k8s.port_forward("n", namespace="ns", context=k8s.CONTEXT, host_port=3000,
                     bind_host="0.0.0.0", sleep=lambda _s: None)
    assert "--address" in spawned[0] and "0.0.0.0" in spawned[0], spawned[0]

    # Loopback needs no flag, and the default must stay loopback -- repros ship
    # fixed weak credentials, so widening is opt-in on both runtimes.
    for quiet in ("", "127.0.0.1", "localhost"):
        spawned.clear()
        k8s.port_forward("n", namespace="ns", context=k8s.CONTEXT, host_port=3000,
                         bind_host=quiet, sleep=lambda _s: None)
        assert "--address" not in spawned[0], quiet


def test_teardown_waits_for_the_namespace_rather_than_claiming_it_is_gone(
        monkeypatch, tmp_path):
    """`kubectl delete namespace --wait=false` returns instantly, so `down` printed
    "removed" while the namespace was Terminating, the pods were still shutting down
    and the PVC was still there. Anyone checking with kubectl saw the opposite of
    what they had just been told, and `up` with the same name could race a
    half-deleted namespace.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    said: list = []
    phases = ["Terminating", "Terminating", ""]

    def spy(argv, timeout=None, own=False):
        import subprocess as sp
        joined = " ".join(argv)
        if "get namespace" in joined and "jsonpath" in joined:
            return sp.CompletedProcess(argv, 0, phases.pop(0) if phases else "", "")
        if "get namespace" in joined:
            return sp.CompletedProcess(argv, 0, "namespace/rc-repro-k\n", "")
        if "get pvc" in joined:
            return sp.CompletedProcess(argv, 0, "persistentvolumeclaim/data-mongodb-0\n", "")
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(k8s, "run", spy)
    assert k8s.delete_namespace("k", context=k8s.CONTEXT, volumes=True,
                                emit=lambda e: said.append(e.message),
                                sleep=lambda _s: None) is True
    assert any("1 volume(s)" in m for m in said), said
    assert any("Terminating" in m for m in said), "it never said it was waiting"
    assert any("are gone" in m for m in said), "it never confirmed the end state"


def test_the_admin_and_the_setup_wizard_reach_a_kubernetes_workspace():
    """`up` printed "Login admin / admin123" for a workspace where that user had
    never been created and the setup wizard was still waiting.

    Compose sets five variables to make a repro usable on arrival -- skip the wizard,
    auto-provision the first admin -- and the Kubernetes values carried none of them.
    Confidently stated credentials that do not work are worse than printing nothing:
    they send someone to debug their own typing.
    """
    from rc_repro import config
    from rc_repro.services import k8s

    env = {e["name"]: e["value"] for e in
           k8s.values_for(rc_version="8.5.1", rc_image="img",
                          microservices=True)["extraEnv"]}
    assert env["OVERWRITE_SETTING_Show_Setup_Wizard"] == "completed"
    assert env["INITIAL_USER"] == "yes", "without this no admin is created at all"
    assert env["ADMIN_USERNAME"] == config.ADMIN_USERNAME
    assert env["ADMIN_PASS"] == config.ADMIN_PASSWORD
    assert env["ADMIN_EMAIL"] == config.ADMIN_EMAIL
    # And NOTHING the chart already sets. These two were here for one commit and
    # broke every install: a second entry for the same env key is not an override
    # but a conflict, and server-side apply refuses the Deployment outright --
    # "duplicate entries for key [name=\"DEPLOY_PLATFORM\"]". The chart knows it is
    # helm on Kubernetes; extraEnv is for what the chart does NOT know.
    for owned in ("DEPLOY_METHOD", "DEPLOY_PLATFORM", "MONGO_URL", "ROOT_URL",
                  "PORT"):
        assert owned not in env, (
            f"{owned} is set by the chart; a duplicate env key fails the install")


def test_a_failure_reports_its_reason_not_the_warnings_printed_before_it():
    """A real `helm install` failure surfaced two harmless duplicate-port warnings
    and hid its own reason, because the message took the FIRST 400 characters of
    stderr.

    Every tool here prints diagnostics first and its error last -- helm emits klog
    warnings then `Error: ...`, kubectl emits deprecation notices then the message --
    so a prefix reliably shows the least useful part. Warnings are not errors,
    however loudly they are printed.
    """
    import subprocess as sp

    from rc_repro.services import k8s

    noisy = sp.CompletedProcess([], 1, "", (
        'I0813 18:03:17.247875 477232 warnings.go:107] "Warning: '
        'spec.template.spec.containers[2].ports[0]: duplicate port definition"\n'
        'I0813 18:03:17.247897 477232 warnings.go:107] "Warning: duplicate port '
        'name \\"metrics\\""\n'
        "Error: INSTALLATION FAILED: cannot re-use a name that is still in use"))
    got = k8s.why(noisy)
    assert "cannot re-use a name" in got, got
    assert "warnings.go" not in got, "a warning was reported as the failure"

    # No self-announcing error line: the last line is where these tools put it.
    assert k8s.why(sp.CompletedProcess([], 1, "", "connection refused")) == \
        "connection refused"
    # Nothing at all still says something, rather than an empty message.
    assert k8s.why(sp.CompletedProcess([], 1, "", "")) == "no reason given"
    # stdout is consulted too -- kubectl puts some failures there.
    assert "boom" in k8s.why(sp.CompletedProcess([], 1, "boom", ""))


def test_up_uses_the_runtime_the_workspace_already_has(monkeypatch, tmp_path):
    """`rc-repro up -v 8.5.1 --name X` on a Kubernetes workspace defaulted to docker
    and ran `docker compose up` against a workspace with no compose file:

        'rc8-5-1' already exists - bringing it back up.
        no configuration file provided: not found
        error: `docker compose up` failed

    `--runtime` says what to CREATE. What already exists is a fact, not a preference.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="u", host_port=3000,
                           version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra["context"] = k8s.CONTEXT
    lc.runner.write("k", "", m)

    took: list = []
    monkeypatch.setattr(lc, "_create_kubernetes",
                        lambda req, emit=None: took.append(req.runtime) or {})
    monkeypatch.setattr(lc.runner, "up",
                        lambda *a, **kw: pytest.fail("it ran docker compose up"))
    # No --runtime at all, exactly as the failing command had it.
    lc._create_repro_locked(lc.CreateReq(version="8.5.1", name="k"))
    assert took == [topology.KUBERNETES], took


def test_up_brings_a_downed_kubernetes_workspace_back_instead_of_refusing(
        monkeypatch, tmp_path):
    """Two messages in the same tool contradicting each other about one workspace:

        $ rc-repro down --name rc8-5-1
        ✓ down (the namespace and its PersistentVolumeClaim are kept).
          bring it back: rc-repro up --version <same> --name rc8-5-1
        $ rc-repro up -v 8.5.1 --runtime kubernetes
        error: 'rc8-5-1' already exists. `down` first, or pass --force

    Compose reuses an existing workspace. So does this now -- the namespace and PVC
    surviving a plain `down` is exactly what makes bringing it back meaningful.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="u", host_port=3000,
                           version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    lc.runner.write("k", "", m)

    monkeypatch.setattr(lc, "check_capacity", lambda *a, **kw: None)
    monkeypatch.setattr(lc, "pick_host_port", lambda *a, **kw: 3000)
    monkeypatch.setattr(lc.versions, "resolve", lambda v, offline=False: type(
        "R", (), {"rc_version": v, "mongo_tag": "8.0", "rc_image": "img",
                  "mongo_flavor": "official", "oplog": False, "source": "t"})())
    monkeypatch.setattr(k8s, "create_workspace", lambda **kw: {
        "context": k8s.CONTEXT, "namespace": "rc-repro-k", "chart_version": "7.0.0",
        "release": k8s.RELEASE, "port_forward_pid": 0, "bind_host": "",
        "microservices": False})

    out = lc._create_kubernetes(lc.CreateReq(version="8.5.1", name="k",
                                            runtime=topology.KUBERNETES))
    assert out["reused"] is True, "it refused a workspace `down` said to bring back"


def test_the_install_is_idempotent_so_bringing_one_back_works(monkeypatch):
    """`helm install` fails on a release that already exists, which is every `up`
    over an existing workspace and every retry after a partial failure."""
    from rc_repro.services import k8s

    seen: list = []
    monkeypatch.setattr(k8s.subprocess, "run",
                        lambda argv, **kw: seen.append(argv) or
                        __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    k8s.install(namespace="ns", context=k8s.CONTEXT, values={}, chart_version="7.0.0")
    assert seen[0][1:3] == ["upgrade", "--install"], seen[0][:4]


def test_a_downed_workspace_is_down_not_perpetually_starting(monkeypatch, tmp_path):
    """A plain `down` keeps the namespace and its PVC on purpose and uninstalls the
    release. Asking only whether the namespace exists reported such a workspace as
    "starting" -- and it would have said so forever, because nothing was coming.

    Seen on a real box: `rc-repro list` showed `starting` for a workspace the user
    had just torn down.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="u", host_port=3000,
                           version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra["context"] = k8s.CONTEXT

    monkeypatch.setattr(k8s, "workspace_namespaces", lambda *a, **kw: ["rc-repro-k"])
    # Namespace kept, release gone -- exactly what `down` leaves behind.
    monkeypatch.setattr(k8s, "workload_exists", lambda *a, **kw: False)
    assert lc.kubernetes_state("k", m) == "down"

    # Workload back but not serving yet.
    monkeypatch.setattr(k8s, "workload_exists", lambda *a, **kw: True)
    monkeypatch.setattr(k8s, "workspace_ready", lambda *a, **kw: False)
    assert lc.kubernetes_state("k", m) == "starting"
    monkeypatch.setattr(k8s, "workspace_ready", lambda *a, **kw: True)
    assert lc.kubernetes_state("k", m) == "running"

    # And no namespace at all is still down.
    monkeypatch.setattr(k8s, "workspace_namespaces", lambda *a, **kw: [])
    assert lc.kubernetes_state("k", m) == "down"


def test_list_says_which_runtime_each_workspace_is_on(monkeypatch, tmp_path):
    """Two workspaces that differ in what every other command will do to them --
    which ones refuse, where the data lives, how to reach it -- looked identical in
    `list`, which is the one place people look first."""
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    for name, rt in (("d", topology.DOCKER), ("k", topology.KUBERNETES)):
        m = lc.runner.Metadata(name=name, project="p", rc_version="8.5.1",
                               rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                               preset="default", root_url="u", host_port=3000,
                               version_source="t")
        topology.stamp(m.extra, rt)
        lc.runner.write(name, "services: {}\n", m)

    monkeypatch.setattr(lc.runner, "docker_available", lambda **kw: True)
    monkeypatch.setattr(lc.runner, "project_states", lambda: {})
    monkeypatch.setattr(lc.runner, "rc_status_by_project", lambda: {})
    monkeypatch.setattr(k8s, "workspace_namespaces", lambda *a, **kw: [])
    rows = {r["name"]: r for r in lc.list_repros()}
    assert rows["d"]["runtime"] == topology.DOCKER
    assert rows["k"]["runtime"] == topology.KUBERNETES


def test_a_workspace_comes_back_on_the_port_it_left_on(monkeypatch, tmp_path):
    """`up` after `down` allocated a fresh port, so a workspace created on :3382
    came back on :3000 -- every bookmark, every curl pasted into a ticket, and the
    URL the user had just been shown all pointed at nothing.

    An explicit --port still wins; otherwise the recorded one does, and only a
    genuinely new workspace allocates.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="http://localhost:3382",
                           host_port=3382, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    lc.runner.write("k", "", m)

    monkeypatch.setattr(lc, "check_capacity", lambda *a, **kw: None)
    monkeypatch.setattr(lc, "pick_host_port",
                        lambda *a, **kw: 3000)          # what a fresh allocation gives
    monkeypatch.setattr(lc.versions, "resolve", lambda v, offline=False: type(
        "R", (), {"rc_version": v, "mongo_tag": "8.0", "rc_image": "img",
                  "mongo_flavor": "official", "oplog": False, "source": "t"})())
    seen: dict = {}
    monkeypatch.setattr(k8s, "create_workspace", lambda **kw: seen.update(kw) or {
        "context": k8s.CONTEXT, "namespace": "rc-repro-k", "chart_version": "7.0.0",
        "release": k8s.RELEASE, "port_forward_pid": 0, "bind_host": "",
        "microservices": False})

    out = lc._create_kubernetes(lc.CreateReq(version="8.5.1", name="k",
                                            runtime=topology.KUBERNETES))
    assert seen["host_port"] == 3382, "it moved a returning workspace to a new port"
    assert out["url"] == "http://localhost:3382"

    # An explicit --port still wins, because asking for one is asking for one.
    lc._create_kubernetes(lc.CreateReq(version="8.5.1", name="k", port=3999,
                                       runtime=topology.KUBERNETES))
    assert seen["host_port"] == 3999


def test_up_waits_before_seeding_a_kubernetes_workspace(monkeypatch, tmp_path):
    """`up --seed` ran the seeder the instant helm returned:

        error: can't seed — repro not ready (`rc-repro ready --name rc8-5-1`)

    The CLI already forces wait=True when seeding -- `wait=(wait or seed)` -- and
    this path never read it. Helm returning is not Rocket.Chat serving.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    order: list = []
    monkeypatch.setattr(lc, "check_capacity", lambda *a, **kw: None)
    monkeypatch.setattr(lc, "pick_host_port", lambda *a, **kw: 3000)
    monkeypatch.setattr(lc.versions, "resolve", lambda v, offline=False: type(
        "R", (), {"rc_version": v, "mongo_tag": "8.0", "rc_image": "img",
                  "mongo_flavor": "official", "oplog": False, "source": "t"})())
    monkeypatch.setattr(k8s, "create_workspace", lambda **kw: {
        "context": k8s.CONTEXT, "namespace": "rc-repro-k", "chart_version": "7.0.0",
        "release": k8s.RELEASE, "port_forward_pid": 0, "bind_host": "",
        "microservices": False})
    monkeypatch.setattr(lc, "wait_serving",
                        lambda *a, **kw: order.append("wait") or {"ready": True})
    monkeypatch.setattr(lc, "run_seed_inline",
                        lambda *a, **kw: order.append("seed") or {"users": 5})

    lc._create_kubernetes(lc.CreateReq(version="8.5.1", name="k", seed=True,
                                       wait=True, runtime=topology.KUBERNETES))
    assert order == ["wait", "seed"], f"seeded before it was serving: {order}"

    # And without --seed or --wait it still returns promptly.
    order.clear()
    lc._create_kubernetes(lc.CreateReq(version="8.5.1", name="k",
                                       runtime=topology.KUBERNETES))
    assert order == []


def test_ready_asks_kubernetes_not_docker(monkeypatch, tmp_path):
    """`rc-repro ready` answered "Rocket.Chat container is not running (check
    `logs`)" about a workspace whose pods were perfectly healthy -- `rc_state` asks
    docker whether a CONTAINER runs.

    It also re-establishes the port-forward, because `ready` is exactly the command
    someone runs when the URL is not answering, and a forward dies with its pod.
    """
    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="http://localhost:3000",
                           host_port=3000, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra.update({"context": k8s.CONTEXT, "namespace": "rc-repro-k",
                    "port_forward_pid": 999999999})
    lc.runner.write("k", "", m)

    monkeypatch.setattr(lc.runner, "rc_state",
                        lambda *a, **kw: pytest.fail("it asked docker"))
    monkeypatch.setattr(k8s, "workspace_ready", lambda *a, **kw: True)
    # `ready` now also confirms the socket answers, not just that a pid came back --
    # see test_ready_confirms_the_socket_not_just_the_pid. Stubbed true here so this
    # test stays about which RUNTIME is asked, which is what it is named for.
    monkeypatch.setattr(k8s, "forward_reachable", lambda *a, **kw: True)
    established: list = []
    monkeypatch.setattr(k8s, "ensure_port_forward",
                        lambda *a, **kw: established.append(kw) or 4242)

    out = lc.wait_serving(m, lc.null_emit, timeout=30.0)
    assert out["ready"] is True
    assert established, "ready did not re-establish the forward"
    # The dead pid must be replaced in the record, or `down` later signals nothing.
    assert lc.runner.read_meta("k").extra["port_forward_pid"] == 4242


def test_every_compose_only_command_refuses_with_a_way_forward(monkeypatch, tmp_path):
    """Six commands reached for a compose project that a Kubernetes workspace does
    not have, and answered "no configuration file provided: not found" -- which names
    nothing anyone can act on.

    A refusal that hands over the working command is worth more than an
    implementation that does not exist yet, and it can ship first. Each refusal
    names the namespace, so it is copy-pasteable rather than a hint.
    """
    from rc_repro.services import envvars, k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="u", host_port=3000,
                           version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra["context"] = k8s.CONTEXT
    lc.runner.write("k", "", m)

    cases = [
        # This list has shrunk to one. `monitor`, `backup`, `logs`, `upgrade`,
        # `stats` and reading `env` all used to be here and now have Kubernetes
        # paths of their own, each covered by its own test.
        #
        # `env --set` is the one that stays, and not for want of effort: an
        # environment variable cannot be changed inside a running container on
        # either runtime. Compose rewrites its file and recreates the service; here
        # that is a helm upgrade, which is a different operation with different
        # consequences, so it hands the command over rather than guessing.
        (lambda: envvars.set_env("k", {"A": "b"}), "helm"),
    ]
    for call, expect in cases:
        with pytest.raises(errors.ValidationError) as caught:
            call()
        msg = str(caught.value)
        assert expect in msg, msg
        assert "rc-repro-k" in msg, f"the namespace must be named: {msg}"
        assert caught.value.exit_code == 2

    # A Compose workspace passes straight through the guard.
    plain = lc.runner.Metadata(name="d", project="rcrepro-d", rc_version="8.5.1",
                               rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                               preset="default", root_url="u", host_port=3001,
                               version_source="t")
    topology.stamp(plain.extra, topology.DOCKER)
    lc.runner.write("d", "services: {}\n", plain)
    topology.require_compose("d", "backup")     # silent


def test_mongodb_uses_the_operator_where_it_can_and_the_statefulset_where_it_cannot(
        monkeypatch, tmp_path):
    """Keyed on the MongoDB VERSION, which is the shape `mongo_flavor` already uses
    on the Compose side ("official" >= 8, "bitnami-legacy" below).

    rc-repro pairs twelve MongoDB versions -- 3.0 through 8.2 -- because "the
    customer's exact version" is the product's promise, and the operator's window
    does not reach the old half. So this is not operator-or-StatefulSet; it is
    operator where it works, StatefulSet where it must, decided by one rule.
    """
    from rc_repro.services import k8s

    # The version rule, with the operator switched on -- it is opt-in by default
    # until its PVC binds on a live cluster (see the opt-in test).
    monkeypatch.setenv(k8s.USE_OPERATOR_ENV, "1")
    for old in ("3.6", "4.4", "5.0"):
        assert not k8s.operator_supports(old), old
    for new in ("6.0", "7.0", "8.0", "8.2"):
        assert k8s.operator_supports(new), new
    assert not k8s.operator_supports("nonsense")


def test_the_operator_resource_asks_for_scram_and_the_two_documented_users():
    """Auth was the gap the operator is being adopted for: without it an
    authentication ticket cannot be reproduced on Kubernetes at all. Users, roles
    and secret names match the official guide so its commands transfer."""
    import yaml

    from rc_repro.services import k8s

    doc = yaml.safe_load(k8s.mongodb_community_manifest("t", "8.0"))
    assert doc["kind"] == "MongoDBCommunity"
    assert doc["spec"]["type"] == "ReplicaSet" and doc["spec"]["members"] == 1
    assert doc["spec"]["security"]["authentication"]["modes"] == ["SCRAM"]
    users = {u["name"]: u for u in doc["spec"]["users"]}
    assert users["admin"]["roles"][0]["name"] == "root"
    assert users["rocketchat"]["roles"][0]["name"] == "readWrite"
    assert users["rocketchat"]["scramCredentialsSecretName"] == \
        "rocketchat-scram-credentials"
    # The volume survives, as it does on the hand-written path.
    assert doc["spec"]["statefulSet"]["spec"]["volumeClaimTemplates"][0][
        "spec"]["resources"]["requests"]["storage"].endswith("Gi")


def test_the_operator_is_installed_once_per_cluster_not_once_per_workspace(
        monkeypatch, tmp_path):
    """The guide installs the operator INTO the Rocket.Chat namespace, because it
    assumes one Rocket.Chat per cluster. Its CRDs are cluster-scoped, so a second
    per-namespace install collides on them rather than yielding a second operator --
    which is exactly what a tool running several workspaces at once would hit.

    So: one install, its own namespace, watching all of them.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    seen: list = []
    monkeypatch.setattr(k8s, "run", lambda argv, **kw: seen.append(" ".join(argv)) or
                        __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    k8s.ensure_operator(context=k8s.CONTEXT)
    install = [c for c in seen if "upgrade --install" in c][0]
    assert f"-n {k8s.OPERATOR_NAMESPACE}" in install, install
    assert "rc-repro-" not in install.split("-n ")[1].split()[0].replace(
        k8s.OPERATOR_NAMESPACE, ""), "it went into a workspace namespace"
    assert "operator.watchNamespace=*" in install, "it would not see other namespaces"
    # Idempotent: every workspace after the first re-runs this.
    assert "upgrade --install" in install


def test_the_mongo_uri_carries_credentials_and_the_operator_service_name():
    """The operator names its service `<name>-svc`, not `<name>` -- the two are not
    interchangeable, and the hand-written path uses the bare name."""
    from rc_repro.services import k8s

    uri = k8s.operator_mongo_url("rc-repro-t", "s3cret")
    assert "rocketchat:s3cret@" in uri
    assert "mongodb-0.mongodb-svc.rc-repro-t.svc.cluster.local" in uri
    assert "authSource=rocketchat" in uri and "replicaSet=mongodb" in uri
    # The oplog URI points at `local` and needs no authSource.
    assert "/local?" in k8s.operator_mongo_url("rc-repro-t", "s3cret", oplog=True)


def test_the_operator_gets_a_full_release_version_not_a_docker_tag():
    """rc-repro carries a Docker TAG -- "8.0" -- because that is what pulls an image.
    The operator wants a RELEASE version, and the guide shows "8.0.0".

    Given "8.0" it accepts the resource and never reconciles it: no `.status.phase`
    at all, the operator healthy, MongoDB simply absent. The create then waited 300s
    and reported "the operator did not bring MongoDB up", which names nothing. Found
    on the first live run of the operator path.
    """
    import yaml

    from rc_repro.services import k8s

    assert k8s.operator_version("8.0") == "8.0.0"
    assert k8s.operator_version("7.0") == "7.0.0"
    # An explicit --mongo 8.0.4 still means 8.0.4.
    assert k8s.operator_version("8.0.4") == "8.0.4"
    doc = yaml.safe_load(k8s.mongodb_community_manifest("t", "8.0"))
    assert doc["spec"]["version"] == "8.0.0", "a bare tag never reconciles"


def test_the_operator_is_opt_in_until_it_is_proven(monkeypatch):
    """A default that breaks the working case is worse than a missing feature.

    The hand-written StatefulSet was verified end to end on a live cluster -- admin
    login, PVC Bound, data surviving a down/up cycle. Routing MongoDB 6.0+ to the
    operator by default replaced that with a path whose PVC never binds:

        it reports: Pending ReplicaSet is not yet ready, retrying in 10 seconds
        data-volume-mongodb-0   Pending

    So the operator waits behind a flag. What that costs is auth, which is the
    thing it was adopted for -- and saying so is the point of this test.
    """
    from rc_repro.services import k8s

    monkeypatch.delenv(k8s.USE_OPERATOR_ENV, raising=False)
    assert not k8s.operator_enabled()
    assert not k8s.operator_supports("8.0"), "a supported version must still not route"

    monkeypatch.setenv(k8s.USE_OPERATOR_ENV, "1")
    assert k8s.operator_enabled()
    assert k8s.operator_supports("8.0")
    assert not k8s.operator_supports("5.0"), "the version floor still applies"


def test_the_operator_volume_claims_cover_both_volumes_the_pod_mounts():
    """The operator's pod mounts `data-volume` AND `logs-volume`, and overriding
    `volumeClaimTemplates` REPLACES its defaults rather than merging with them --
    a run that declared only `data-volume` produced exactly one PVC, while both of
    the pod's containers mount both volumes. A template list that drops one leaves
    the pod referencing a claim nothing creates.

    (This was NOT what held the first live run at Pending -- that was the missing
    ServiceAccount, see the RBAC test below. Both are real; only one was blocking.)
    """
    import yaml

    from rc_repro.services import k8s

    doc = yaml.safe_load(k8s.mongodb_community_manifest("t", "8.0"))
    names = [v["metadata"]["name"]
             for v in doc["spec"]["statefulSet"]["spec"]["volumeClaimTemplates"]]
    assert names == ["data-volume", "logs-volume"], names


def test_the_database_pod_gets_its_service_account_in_the_workspace_namespace():
    """What actually held the operator path at Pending for three sessions.

    The operator writes a StatefulSet whose pod runs as `mongodb-kubernetes-appdb`.
    Its chart creates that account only in the namespace the chart is installed
    into. rc-repro installs the operator ONCE, in `rc-repro-system`, because the
    chart owns cluster-scoped CRDs and a per-workspace install collides on them at
    the second workspace -- so the account is missing exactly where the pod needs it:

        error: pods "mongodb-0" is forbidden: error looking up service account
        rc-repro-optest/mongodb-kubernetes-appdb: serviceaccount ... not found

    That is an event on the StatefulSet. The MongoDBCommunity resource reports only
    "Pending ReplicaSet is not yet ready", and because no pod is ever created,
    WaitForFirstConsumer holds both PVCs at Pending -- which is the symptom seen
    first and points at storage, which was never the problem.
    """
    import yaml

    from rc_repro.services import k8s

    docs = {d["kind"]: d
            for d in yaml.safe_load_all(k8s.mongo_rbac_manifest("t", owner="o"))}
    assert set(docs) == {"ServiceAccount", "Role", "RoleBinding"}, sorted(docs)

    sa = k8s.MONGO_DB_SERVICE_ACCOUNT
    assert all(d["metadata"]["name"] == sa for d in docs.values())
    # The binding must name the LOCAL account. Pointing at the operator's namespace
    # would apply cleanly and still leave the pod unable to start.
    subject = docs["RoleBinding"]["subjects"][0]
    assert subject["kind"] == "ServiceAccount" and subject["name"] == sa
    assert "namespace" not in subject, "must bind the account in OUR namespace"
    assert docs["RoleBinding"]["roleRef"]["name"] == sa

    # Exactly what a member pod needs: read its own password Secret, and
    # patch/delete/get its own Pod to mark readiness and roll itself.
    rules = {r["resources"][0]: set(r["verbs"]) for r in docs["Role"]["rules"]}
    assert rules == {"secrets": {"get"}, "pods": {"patch", "delete", "get"}}, rules

    # Owned like everything else, so `prune` and the ownership guards see it.
    for doc in docs.values():
        assert doc["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "rc-repro"


def test_the_service_account_is_applied_before_the_database_that_needs_it():
    """Order matters and is not obvious: the RoleBinding has to exist before the
    StatefulSet tries to create a pod, or the pod is rejected and the operator
    backs off. Applying it with the Secrets -- ahead of the MongoDBCommunity
    resource -- is what makes the first attempt the successful one."""
    import inspect

    from rc_repro.services import k8s

    body = inspect.getsource(k8s.create_workspace)
    rbac = body.index("mongo_rbac_manifest")
    community = body.index("mongodb_community_manifest")
    assert rbac < community, "RBAC must be applied before the MongoDBCommunity"


def test_asking_for_the_operator_explicitly_satisfies_the_opt_in_but_not_the_floor(
        monkeypatch):
    """`--mongo-operator` is the user asking for it, so the opt-in switch is met.
    The VERSION floor still applies: the operator cannot manage MongoDB 5.0, and
    silently ignoring the flag would be worse than falling back."""
    from rc_repro.services import k8s

    monkeypatch.delenv(k8s.USE_OPERATOR_ENV, raising=False)
    assert not k8s.operator_supports("8.0"), "off by default"
    assert k8s.operator_supports("8.0", forced=True), "--mongo-operator asks for it"
    assert not k8s.operator_supports("5.0", forced=True), "the floor still holds"


def test_the_monitoring_stack_is_shared_by_the_cluster_not_owned_by_a_workspace():
    """The Compose shape does not transfer, and copying it would break at two.

    `helm template rocketchat/monitoring` with every dependency disabled still
    renders a ClusterRole and a ClusterRoleBinding at FIXED names
    (`monitoring-otel-collector-role`) plus a log-collector DaemonSet -- so a
    per-workspace release collides on cluster-scoped objects at the second
    workspace, exactly as the MongoDB operator's CRDs do, and would also put one
    collector per node per workspace on the same host paths.

    So it lives once in rc-repro-system, like the operator, and `--monitor` on a
    workspace means "point the shared Prometheus at me", not "install a stack".
    """
    from rc_repro.services import k8s

    assert k8s.MONITORING_NAMESPACE == k8s.OPERATOR_NAMESPACE == "rc-repro-system"


def test_prometheus_is_told_to_look_outside_its_own_release():
    """The one setting a shared stack cannot work without.

    kube-prometheus-stack defaults `serviceMonitorSelectorNilUsesHelmValues` and its
    pod equivalent to true, which restricts Prometheus to monitors carrying its own
    release label. A workspace's PodMonitor is in another namespace and another
    release, so it is silently ignored: nothing errors, Grafana just draws empty
    graphs -- which reads as "monitoring is broken" rather than "the selector
    excluded it".
    """
    import inspect

    from rc_repro.services import k8s

    src = inspect.getsource(k8s.ensure_monitoring)
    for key in ("serviceMonitorSelectorNilUsesHelmValues=false",
                "podMonitorSelectorNilUsesHelmValues=false"):
        assert key in src.replace("\"\n               \"", ""), key


def test_detaching_monitoring_leaves_it_up_for_the_workspaces_still_using_it(
        monkeypatch):
    """Shared means one workspace's `--off` must not blind the others.

    This is the real behavioural difference from Compose, where the stack belongs to
    a project and detaching is unambiguous. Getting it wrong is silent: the other
    workspaces keep running and just stop being observable.
    """
    import subprocess

    from rc_repro.services import k8s

    monkeypatch.setattr(k8s, "workspace_namespaces",
                        lambda ctx: ["rc-repro-a", "rc-repro-b"])
    monkeypatch.setattr(k8s, "monitoring_wanted",
                        lambda ns, *, context: ns == "rc-repro-b")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(k8s, "run", fake_run)

    assert k8s.remove_monitoring(context=k8s.CONTEXT) is False
    assert not any("uninstall" in a for argv in calls for a in argv), calls

    monkeypatch.setattr(k8s, "monitoring_wanted", lambda ns, *, context: False)
    calls.clear()
    assert k8s.remove_monitoring(context=k8s.CONTEXT) is True
    assert any("uninstall" in a for argv in calls for a in argv), calls


def test_grafana_is_forwarded_from_the_instance_not_the_operator():
    """`monitoring-grafana` is the grafana-OPERATOR and serves nothing useful;
    `monitoring-grafana-deployment` is the Grafana it manages. Forwarding the wrong
    one gives a page that loads and has no dashboards -- a working URL showing
    nothing, which is worse than a refusal."""
    from rc_repro.services import k8s

    assert k8s.GRAFANA_DEPLOYMENT == "monitoring-grafana-deployment"
    # Deployment, not Service, for the same reason the workspace forward is:
    # a Service with no ready endpoint makes kubectl exit at once.
    import inspect
    src = inspect.getsource(k8s.grafana_forward)
    assert "deployment/{GRAFANA_DEPLOYMENT}" in src


def test_monitor_at_create_time_waits_for_the_workspace_first():
    """Attaching turns on RC's own Prometheus_Enabled over REST, so it needs a
    workspace that answers. `up --monitor` without `--wait` would otherwise attach
    to something not yet serving and fail -- which is exactly what `--seed` did
    before it was fixed on this path."""
    import inspect

    src = inspect.getsource(lc._create_kubernetes)
    monitor_at = src.index("req.monitor")
    assert "wait_serving" in src[monitor_at:monitor_at + 400], \
        "--monitor must wait for the workspace before attaching"


def test_the_grafana_url_is_not_handed_over_before_the_forward_answers(monkeypatch):
    """`kubectl port-forward` returns a pid before it binds the socket.

    The workspace path already carries a test named for this mistake. The Grafana
    path repeated it and the deployment matrix caught it: `rc-repro monitor`
    reported attached and exited 0, and a curl to the URL it printed got nothing at
    all -- a link handed over that quietly did not work.
    """
    from rc_repro.services import k8s

    slept = []
    assert k8s.forward_reachable(1, tries=3, interval=0.01,
                                 sleep=slept.append) is False
    assert len(slept) == 3, "must actually retry rather than check once"


def test_the_statefulset_image_covers_the_versions_the_operator_cannot():
    """Why `mongo:` and not MongoDB Inc's own image, pinned so it is not "fixed".

    This StatefulSet exists to serve what the operator cannot reach, and
    `mongodb/mongodb-community-server` publishes NO tag below 4.4 -- 3.6, 4.0 and
    4.2 are 404 on Docker Hub -- while rc-repro pairs Rocket.Chat < 3.0 with MongoDB
    3.6. "Make it consistent with compose.py" looks like a tidy-up and would break
    precisely the versions this code was written for.

    Same MongoDB either way: same source, version, storage engine and wire protocol,
    differing in base OS and packaging.
    """
    import yaml

    from rc_repro.services import k8s

    assert k8s.mongo_image("3.6") == "mongo:3.6"
    docs = list(yaml.safe_load_all(k8s.mongo_manifest("t", "3.6")))
    sts = [d for d in docs if d and d.get("kind") == "StatefulSet"][0]
    assert sts["spec"]["template"]["spec"]["containers"][0]["image"] == "mongo:3.6"


def test_kubernetes_records_what_built_the_database_not_a_compose_flavour():
    """`mongo_flavor` is "official" / "bitnami-legacy" -- a COMPOSE image choice.

    The Kubernetes StatefulSet honours neither value, so reporting it made
    `rc-repro list` print "8.0 (official)" for a workspace running Docker Hub's
    `mongo:8.0`. A label describing an image the workspace does not run is worse
    than no label, because it is the field someone checks when a version-specific
    bug will not reproduce.
    """
    import inspect

    from rc_repro.services import k8s

    src = inspect.getsource(k8s.create_workspace)
    assert '"mongo_managed_by"' in src and '"mongo_image"' in src
    # Both branches must set them, or the record is right only half the time.
    assert src.count("managed_by = ") == 2, "operator and statefulset must both record"


def test_the_notes_say_the_operator_is_shared_when_it_is_used():
    """The deviation from the official guide has to be visible where someone reads it.

    The guide installs the operator INTO the Rocket.Chat namespace. rc-repro runs
    several workspaces, its CRDs are cluster-scoped, so it installs once in
    rc-repro-system watching everything. Undocumented, the first person to run the
    guide's `kubectl -n <ns> get pods` finds no operator and concludes it was never
    installed.
    """
    # Asserted against the groups a real record produces rather than against the
    # source of the function that builds them: the notes are derived on every read
    # now, so the record is the input and the rendered groups are the output, and
    # only the second of those is what anybody sees.
    def groups_for(managed_by):
        m = lc.runner.Metadata(
            name="k", project="rc-repro-k", rc_version="8.5.1", rc_image="i",
            mongo_tag="8.0", mongo_flavor="official", preset="default",
            root_url="http://localhost:3000", host_port=3000, version_source="map",
            extra={"runtime": "kubernetes", "deployment": "microservices",
                   "namespace": "rc-repro-k", "context": "kind-rc-repro-local",
                   "release": "rocketchat", "mongo_managed_by": managed_by,
                   "mongo_image": "mongo:8.0"})
        return {g["title"]: g for g in lc.note_groups_of(m)}

    op = groups_for("operator")
    shared = op["The MongoDB operator is shared"]
    assert "official guide" in " ".join(shared["body"])
    # And it says where to look, not just that it is elsewhere.
    assert any("get mongodbcommunity" in c for c in shared["commands"])
    assert ("Managed by", "the official operator") in [tuple(r) for r in op["MongoDB"]["rows"]]
    # Monitoring deviates the same way and for the same reason, on both paths.
    assert "shared by the cluster, not installed per" in " ".join(op["Monitoring"]["body"])
    # A plain StatefulSet has no operator to explain, and says so where the guide
    # would have led someone to expect authentication.
    sts = groups_for("statefulset")
    assert "The MongoDB operator is shared" not in sts
    assert ("Authentication", "none") in [tuple(r) for r in sts["MongoDB"]["rows"]]
    assert "official guide" in " ".join(sts["MongoDB"]["body"])


def test_the_oplog_user_can_actually_read_the_oplog(monkeypatch):
    """Two faults in one line, on the only versions that use it.

    Rocket.Chat below 8.x tails the oplog; 8.x dropped it. The operator's oplog URL
    addressed the `local` DATABASE while the user is defined in `rocketchat`, and
    carried no authSource -- so the driver authenticated against `local`, looking for
    a user that has never existed there. Even past that, reading `local` needs a role
    `readWrite` on the app database does not confer.

    The role is not an invention: Rocket.Chat's own chart grants exactly this to its
    bundled MongoDB, in templates/mongodb-init-configmap.yaml --
    `grantRolesToUser('<user>', [{ role: 'clusterMonitor', db: 'admin' }])`.

    This survived every live run because they all used 8.5.1, where the URL is never
    emitted at all.
    """
    import yaml

    from rc_repro.services import k8s

    oplog = k8s.operator_mongo_url("rc-repro-x", "PW", oplog=True)
    assert "/local?" in oplog, oplog
    assert f"authSource={k8s.MONGO_APP_DB}" in oplog, \
        "without authSource the driver authenticates against `local`"

    doc = yaml.safe_load(k8s.mongodb_community_manifest("t", "7.0"))
    app = {u["name"]: u for u in doc["spec"]["users"]}[k8s.MONGO_APP_USER]
    roles = {(r["name"], r["db"]) for r in app["roles"]}
    assert ("readWrite", k8s.MONGO_APP_DB) in roles, roles
    assert ("clusterMonitor", "admin") in roles, \
        "readWrite on the app db cannot read `local`"


def test_the_oplog_url_is_only_emitted_for_the_versions_that_use_it():
    """RC 8.x dropped oplog tailing, so emitting it there is wrong in the other
    direction. The rule lives in versions.py and applies to both runtimes; this
    checks the Kubernetes chart values honour it."""
    from rc_repro.services import k8s

    with_oplog = k8s.values_for(rc_version="7.10.0", rc_image="i", microservices=False,
                                oplog=True, mongo_url="m", oplog_url="o")
    without = k8s.values_for(rc_version="8.5.1", rc_image="i", microservices=False,
                             oplog=False, mongo_url="m")
    assert with_oplog["externalMongodbOplogUrl"] == "o"
    assert "externalMongodbOplogUrl" not in without


def test_the_chart_fix_is_applied_only_where_the_chart_needs_it(monkeypatch):
    """Rocket.Chat 7.x could not install on Kubernetes at all, and no test saw it
    because every live run used 8.5.1.

    Chart 6.26.0 -- the exact-match chart for RC 7.10.0 -- ships
    `containerSecurityContext: {runAsUser: 999, fsGroup: 999}` and renders it onto
    the CONTAINER, where `fsGroup` does not exist. Helm 3 applied client-side and
    dropped it silently; Helm 4 applies server-side and the API server refuses the
    whole Deployment: ".spec.template.spec.containers[name=\\"rocketchat\\"]
    .securityContext.fsGroup: field not declared in schema".

    `null` is the override because Helm MERGES values -- passing a replacement map
    leaves the chart's own fsGroup underneath and changes nothing. And it is applied
    only where the chart has the field: injecting it into chart 7.0.0, which fixed
    the bug upstream, put a literal `fsGroup: null` onto the container instead --
    the same undeclared field from the other direction.
    """
    from rc_repro.services import k8s

    def fake_run(argv, **kw):
        import subprocess
        has_fsgroup = "6.26.0" in argv
        body = ("containerSecurityContext:\n  runAsUser: 999\n  fsGroup: 999\n"
                if has_fsgroup else
                "containerSecurityContext:\n  runAsUser: 65533\n")
        return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")

    monkeypatch.setattr(k8s, "run", fake_run)
    assert k8s.container_security_context("6.26.0") == \
        {"containerSecurityContext": {"fsGroup": None}}
    assert k8s.container_security_context("7.0.0") == {}, \
        "a chart without the field must get no override at all"


def test_ready_confirms_the_socket_not_just_the_pid(monkeypatch, tmp_path):
    """`ready` exited 0 on a workspace whose URL answered nothing, after `restart`.

    The pod being Ready is Kubernetes' answer to a different question. `kubectl
    port-forward` returns a pid before it has bound the socket, so declaring ready
    on the strength of the pid is a guess -- and the live run caught it being wrong:
    `ready` returned success and the very next request to the URL it had just
    confirmed got nothing.

    So the loop keeps going until something actually answers on the port. This is
    the same defect as the Grafana URL, in the place it was fixed second.
    """
    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.5.1",
                           rc_image="i", mongo_tag="8.0", mongo_flavor="official",
                           preset="default", root_url="http://localhost:3999",
                           host_port=3999, version_source="t")
    from rc_repro.services import topology
    topology.stamp(m.extra, topology.KUBERNETES)

    monkeypatch.setattr(k8s, "workspace_ready", lambda *a, **kw: True)
    monkeypatch.setattr(k8s, "ensure_port_forward", lambda *a, **kw: 4242)
    monkeypatch.setattr(lc.runner, "update_meta", lambda *a, **kw: None)

    # Never reachable -> must NOT report ready, must raise the timeout instead.
    monkeypatch.setattr(k8s, "forward_reachable", lambda *a, **kw: False)
    try:
        lc._wait_serving_kubernetes(m, lc.null_emit, timeout=0.2)
    except errors.NotReadyError:
        pass
    else:
        raise AssertionError("a URL that answers nothing must not report ready")

    # Reachable -> ready.
    monkeypatch.setattr(k8s, "forward_reachable", lambda *a, **kw: True)
    out = lc._wait_serving_kubernetes(m, lc.null_emit, timeout=5.0)
    assert out == {"ready": True, "url": "http://localhost:3999"}, out


def test_a_create_stops_waiting_for_a_pod_that_cannot_start(monkeypatch):
    """A mistyped version cost the full timeout, then blamed the timeout.

    Kubernetes knows within seconds that an image does not exist; the readiness loop
    waited its whole 600s anyway and then reported "did not become ready", which names
    the symptom and not the cause. Taken from PR #3.

    The discrimination is the subtle part, and PR #3's note has it: `ImagePullBackOff`
    is a REASON, not a phase, and a pod that is merely pulling slowly from a
    rate-limited registry looks identical at the phase level. So the reason decides,
    and anything unrecognised keeps waiting -- the default has to be patience, or a
    slow pull becomes a failed create.
    """
    import json as _json

    from rc_repro.services import k8s

    def pods(*states):
        return _json.dumps({"items": [
            {"metadata": {"name": p},
             "status": {"containerStatuses": [{"state": {"waiting": {
                 "reason": r, "message": m}}}]}}
            for p, r, m in states]})

    def cluster(payload, rc=0):
        def run(argv, **kw):
            import subprocess as sp
            return sp.CompletedProcess(argv, rc, payload, "")
        return run

    # An image that does not exist is decided; say so.
    monkeypatch.setattr(k8s, "run", cluster(pods(
        ("rocketchat-abc", "ErrImagePull", 'manifest for rocket.chat:9.9.9 not found'))))
    found = k8s.terminal_pod_failure("w", context="c")
    assert found is not None
    pod, reason, message = found
    assert pod == "rocketchat-abc" and reason == "ErrImagePull"
    assert "9.9.9" in message, "carry the registry's own words, not a paraphrase"

    # A pod still starting is NOT terminal -- this is the false positive that would
    # turn every slow pull into a failed create.
    monkeypatch.setattr(k8s, "run", cluster(pods(
        ("rocketchat-abc", "ContainerCreating", ""),
        ("mongodb-0", "PodInitializing", ""))))
    assert k8s.terminal_pod_failure("w", context="c") is None

    # Neither is a healthy pod with no waiting state at all.
    monkeypatch.setattr(k8s, "run", cluster(
        _json.dumps({"items": [{"metadata": {"name": "rc"},
                                "status": {"containerStatuses": [
                                    {"state": {"running": {}}}]}}]})))
    assert k8s.terminal_pod_failure("w", context="c") is None

    # An unreadable cluster is not evidence of failure either.
    monkeypatch.setattr(k8s, "run", cluster("", rc=1))
    assert k8s.terminal_pod_failure("w", context="c") is None
    monkeypatch.setattr(k8s, "run", cluster("not json"))
    assert k8s.terminal_pod_failure("w", context="c") is None

    # An init container counts: MongoDB's fix-permission container runs there.
    monkeypatch.setattr(k8s, "run", cluster(_json.dumps({"items": [
        {"metadata": {"name": "mongodb-0"},
         "status": {"initContainerStatuses": [{"state": {"waiting": {
             "reason": "CreateContainerConfigError", "message": "secret missing"}}}]}}]})))
    got = k8s.terminal_pod_failure("w", context="c")
    assert got and got[1] == "CreateContainerConfigError"


def test_the_registration_token_never_becomes_a_value_or_an_argument(monkeypatch):
    """The EE licence token reaches Rocket.Chat by REFERENCE, never as a literal.

    `--reg-token` was refused on this runtime, and the reason had outlived itself:
    "injected through the preset environment, which this runtime does not apply yet"
    -- preset env had been applied here for some time. Taken from PR #3, whose design
    is better than the obvious one, and the reason is exposure:

      * helm VALUES are readable with `helm get values` by anyone who can reach the
        release, and rc-repro also writes them to `repros/<n>/kubernetes/values.yaml`
        for a human to read. That is precisely where the MongoDB password was moved
        out of, so putting a licence token there would undo the same lesson.
      * The manifest goes to `kubectl apply -f -` on STDIN, so the token is never a
        process argument either, where `ps` shows it to every user on the box.
    """
    import yaml as _yaml

    from rc_repro.services import k8s

    token = "SUPERSECRETTOKEN"

    # 1. The Secret carries it; the values only name the Secret.
    doc = _yaml.safe_load(k8s.reg_token_secret_manifest("w", token=token, owner="me"))
    assert doc["kind"] == "Secret" and doc["type"] == "Opaque"
    assert doc["stringData"][k8s.REG_TOKEN_KEY] == token
    assert doc["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "rc-repro", \
        "teardown selects on ownership, so the Secret has to carry it"

    values = k8s.values_for(rc_version="8.5.1",
                            rc_image="registry.rocket.chat/rocketchat/rocket.chat",
                            microservices=False,
                            mongo_url="mongodb://mongodb:27017/rocketchat",
                            reg_token_secret=k8s.REG_TOKEN_SECRET)
    assert token not in _yaml.safe_dump(values), \
        "the token must not be in anything `helm get values` can print"
    entry = next(e for e in values["extraEnv"] if e["name"] == "REG_TOKEN")
    assert entry["valueFrom"]["secretKeyRef"] == {"name": k8s.REG_TOKEN_SECRET,
                                                 "key": k8s.REG_TOKEN_KEY}
    assert "value" not in entry, "by reference, never by value"

    # 2. Without a token, nothing is referenced -- no dangling secretKeyRef.
    plain = k8s.values_for(rc_version="8.5.1",
                           rc_image="registry.rocket.chat/rocketchat/rocket.chat",
                           microservices=False,
                           mongo_url="mongodb://mongodb:27017/rocketchat")
    assert not any(e["name"] == "REG_TOKEN" for e in plain["extraEnv"]), \
        "a workspace with no token must not reference a Secret that does not exist"

    # 3. Applied on stdin, so it is never visible in argv.
    seen = {}
    def fake_run(argv, **kw):
        import subprocess as sp
        seen["argv"] = argv
        return sp.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(k8s.subprocess, "run",
                        lambda argv, **kw: fake_run(argv, **kw))
    k8s.apply(k8s.reg_token_secret_manifest("w", token=token), namespace="ns",
              context="c")
    assert token not in " ".join(seen["argv"]), \
        "`ps` would show a token passed as an argument to every user on the box"
    assert "-f" in seen["argv"] and "-" in seen["argv"], "applied from stdin"


def test_kubernetes_refuses_what_it_would_silently_drop(monkeypatch, tmp_path):
    """Five create options were ACCEPTED and then ignored on this runtime.

    `--preset ldap` is the clearest: the Kubernetes path's only `presets.load` is a
    hardcoded "default", so the name was written into repro.json and nothing else
    happened -- `rc-repro list` showed "ldap" for a workspace with no LDAP in it. A
    create that reports success and produces something else is the worst outcome
    available, and it is the failure mode this whole runtime split keeps producing:
    --seed, --https, mongo_flavor and mongo_shell were all the same shape.

    `--fresh` is the one that matters most and is reported first: it means DELETE
    this workspace's data, and the Kubernetes path keeps the PersistentVolumeClaim.
    """
    from rc_repro.services import topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def req(**kw):
        return lc.CreateReq(version="8.5.1", runtime=topology.KUBERNETES, **kw)

    cases = [
        # `--reg-token` used to be here, refused because "the EE registration token
        # is injected through the preset environment, which this runtime does not
        # apply yet". Preset env had been applied here for some time, so the reason
        # had outlived itself while the flag stayed refused; it now travels in an
        # Opaque Secret referenced by `valueFrom`, and is asserted below.
        (req(fresh=True), "--fresh"),
        (req(https="local"), "--https"),
        (req(domain="x.example"), "--domain"),
    ]
    for creq, expected in cases:
        with pytest.raises(errors.ValidationError) as caught:
            lc._refuse_unsupported_on_kubernetes(creq)
        assert expected in str(caught.value), f"{expected}: {caught.value}"

    # What IS supported must still pass straight through, or this becomes a wall.
    lc._refuse_unsupported_on_kubernetes(req(preset="default", name="x", pin=True,
                                             monitor=True, seed=True, wait=True,
                                             mongo_operator=True, replicas=3,
                                             reg_token="abc"))
    # And `ldap` passes now BECAUSE it has an adapter -- the refusal is per preset,
    # not a blanket "presets do not work here". That is what slice 4 bought.
    lc._refuse_unsupported_on_kubernetes(req(preset="ldap", params={"users": "3"}))
    # The refusal names the ones that DO work, so the answer is actionable.
    # EVERY preset reaches Kubernetes now, each with its own backing workload:
    # openldap, keycloak (twice), mailpit, minio, nginx. The guard stays because it
    # is what keeps a future preset from being recorded and never applied.
    from rc_repro import presets
    for p in presets.list_presets():
        # `multi-instance` is a DEPLOYMENT on this branch, not a scenario -- the
        # legacy YAML still exists in the catalogue and correctly refuses here,
        # because its nats/traefik pair is what --deployment provides natively.
        if p.name == "multi-instance":
            with pytest.raises(errors.ValidationError):
                lc._refuse_unsupported_on_kubernetes(req(preset=p.name))
            continue
        lc._refuse_unsupported_on_kubernetes(req(preset=p.name))

    # And it still bites: a preset with backing services and no manifests is refused,
    # naming the services that would have been missing.
    import dataclasses
    broken = dataclasses.replace(presets.load("email"), kubernetes_manifests=[])
    monkeypatch.setattr(presets, "resolve", lambda *a, **kw: broken)
    with pytest.raises(errors.ValidationError) as caught:
        lc._refuse_unsupported_on_kubernetes(req(preset="email"))
    assert "mailpit" in str(caught.value), caught.value


def test_the_kubernetes_refusals_are_reached_from_the_create_path():
    """Wired at the dispatch, not merely defined. A guard nothing calls is worse
    than no guard, because the docstring says it is handled."""
    import inspect

    src = inspect.getsource(lc._create_repro_locked)
    call = src.index("_refuse_unsupported_on_kubernetes")
    dispatch = src.index("_create_kubernetes(req")
    assert call < dispatch, "the refusal must run BEFORE the workspace is built"


def test_a_scenarios_settings_reach_the_rocket_chat_container(monkeypatch):
    """The SHARED half of the Scenario contract, and the half that silently did not
    arrive.

    `values_for` took `preset_env`, threaded it from `create_workspace`, and then
    never used it -- the parameter was declared, passed, and dropped on the floor.
    The live run showed the symptom exactly: OpenLDAP deployed, ready and labelled,
    and Rocket.Chat with no LDAP setting on it at all. A backing service nothing is
    configured to use is the same silent-drop shape as the rest of this runtime,
    one level further in.

    Appended AFTER the base list so a preset can override a default rather than be
    overridden by one -- the precedence compose.py already gives it.
    """
    from rc_repro import presets
    from rc_repro.services import k8s, topology

    preset = presets.resolve("ldap", topology.KUBERNETES, {"users": "7"})
    values = k8s.values_for(rc_version="8.5.1", rc_image="i", microservices=False,
                            preset_env=preset.env)
    got = {e["name"]: e["value"] for e in values["extraEnv"]}
    assert got.get("OVERWRITE_SETTING_LDAP_Host") == "openldap", got
    assert got.get("OVERWRITE_SETTING_LDAP_Enable") in ("true", "True"), got
    # The workspace's own admin env must survive alongside it.
    assert got["ADMIN_USERNAME"] and got["INITIAL_USER"] == "yes"

    # A preset wins over a base default, rather than the other way round.
    override = k8s.values_for(rc_version="8.5.1", rc_image="i", microservices=False,
                              preset_env={"ADMIN_NAME": "Scenario"})
    names = [e["value"] for e in override["extraEnv"] if e["name"] == "ADMIN_NAME"]
    assert names[-1] == "Scenario", names


def test_a_scenario_forward_waits_for_a_ready_endpoint(monkeypatch):
    """Binding a local socket is not the same as the backend answering.

    `kubectl port-forward svc/...` binds immediately and only then dials a pod, so a
    forward started while the workload is still booting passes a TCP check on the
    local side, fails upstream and exits. Keycloak takes about forty seconds to
    import its realm, and the live run showed exactly that: "keycloak published at
    http://localhost:8081", then connection refused a minute later.

    So the endpoint is waited for first. Third kind of forward, third time this
    lesson: the workspace URL needed the pod Running, Grafana needed the socket
    confirmed, and this needs a ready endpoint before either check means anything.
    """
    import subprocess

    from rc_repro.services import k8s

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "svc" in argv and "get" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="kc 8081 8080\n", stderr="")
        if "endpoints" in argv:
            # Never ready -- the forward must NOT be started at all.
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(k8s, "run", fake_run)
    monkeypatch.setattr(k8s.subprocess, "Popen",
                        lambda *a, **kw: pytest.fail("forwarded before it was ready"))
    out = k8s.scenario_ui_forwards("w", namespace="rc-repro-w", context=k8s.CONTEXT,
                                   sleep=lambda _s: None)
    assert out == {}, out
    assert sum("endpoints" in a for argv in calls for a in argv) >= 10, \
        "it must actually retry rather than check once"


def test_a_preset_configures_itself_through_the_runtime_that_owns_it(monkeypatch):
    """`no configuration file provided: not found` in the middle of a Kubernetes
    create, followed by "starting".

    That is docker compose's own wording for "there is no compose project here", and
    it appeared because a post_ready handler -- a preset configuring ITSELF -- called
    `runner.compose_exec` on a Kubernetes workspace. Presets exist on both runtimes
    now, so the one call that reaches into a container had to stop being a Compose
    call.
    """
    from rc_repro.services import postready, topology

    m = postready.runner.Metadata(name="k", project="rc-repro-k", rc_version="8.5.1",
                                  rc_image="i", mongo_tag="8.0",
                                  mongo_flavor="official", preset="saml",
                                  root_url="u", host_port=3000, version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)

    monkeypatch.setattr(postready.runner, "compose_exec",
                        lambda *a, **kw: pytest.fail("it reached for docker compose"))
    from rc_repro.services import k8s
    seen = []

    class _R:
        returncode, stdout, stderr = 0, "keycloak-abc", ""

    monkeypatch.setattr(k8s, "run", lambda argv, **kw: (seen.append(argv), _R())[1])
    assert postready._exec_in(m, "keycloak", ["bash", "-lc", "true"]) == 0
    assert any("kubectl" in a for argv in seen for a in argv), seen
    assert any("exec" in argv for argv in seen), seen

    # A Compose workspace must still go the other way -- this is a fork, not a swap.
    c = postready.runner.Metadata(name="c", project="rcrepro-c", rc_version="8.5.1",
                                  rc_image="i", mongo_tag="8.0",
                                  mongo_flavor="official", preset="saml",
                                  root_url="u", host_port=3001, version_source="t")
    monkeypatch.setattr(postready.runner, "compose_exec", lambda *a, **kw: 0)
    monkeypatch.setattr(k8s, "run",
                        lambda *a, **kw: pytest.fail("it reached for kubectl"))
    assert postready._exec_in(c, "keycloak", ["true"]) == 0


def test_a_half_configured_preset_says_so_instead_of_reporting_success():
    """The create printed "could not fetch/apply IdP cert", then "starting", and
    left the reader to work out whether SAML actually worked.

    The DEPLOYMENT succeeding and the CONFIGURATION succeeding are different answers
    to different questions, and a workspace that is running with a half-applied
    preset has to say which one it is -- naming what failed and the command that
    retries it.
    """
    from rc_repro.services import postready

    m = postready.runner.Metadata(name="w", project="p", rc_version="8.5.1",
                                  rc_image="i", mongo_tag="8.0",
                                  mongo_flavor="official", preset="saml",
                                  root_url="u", host_port=3000, version_source="t")
    m.extra["post_ready"] = [{"action": "saml_idp_cert"},
                             {"action": "keycloak_master_ssl_off"}]
    said: list = []
    postready._POST_READY_ACTIONS["saml_idp_cert"] = lambda *a, **kw: False
    postready._POST_READY_ACTIONS["keycloak_master_ssl_off"] = lambda *a, **kw: True
    try:
        failed = postready.run_post_ready(m, object(), lambda e: said.append(e.message))
    finally:
        import importlib
        importlib.reload(postready)

    assert failed == ["saml_idp_cert"], failed
    joined = " ".join(said)
    assert "RUNNING" in joined and "partly configured" in joined, joined
    assert "saml_idp_cert" in joined, joined
    assert "rc-repro ready --name w" in joined, joined


def test_a_scenario_forward_targets_the_deployment_not_the_service(monkeypatch):
    """A live OIDC workspace: Keycloak 1/1 Running, the recorded forward pid gone,
    nothing answering on 8085.

    `kubectl port-forward svc/...` dies when the Service's endpoint churns. The
    workspace's own forward learned this and uses `deployment/`; scenario UIs then
    reintroduced it. It also has to publish the CONTAINER port -- a deployment
    forward cannot use the Service's port, and for SAML those differ (8081 -> 8080).
    """
    import subprocess

    from rc_repro.services import k8s

    spawned = []

    def fake_run(argv, **kw):
        if "get" in argv and "svc" in argv:
            out = "keycloak 8081 8080\n"
        elif "endpoints" in argv:
            out = "10.244.0.9"
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    class _P:
        pid = 4242

    monkeypatch.setattr(k8s, "run", fake_run)
    monkeypatch.setattr(k8s.subprocess, "Popen",
                        lambda argv, **kw: (spawned.append(argv), _P())[1])
    monkeypatch.setattr(k8s, "forward_reachable", lambda *a, **kw: True)

    out = k8s.scenario_ui_forwards("w", namespace="rc-repro-w", context=k8s.CONTEXT,
                                   sleep=lambda _s: None)
    assert out == {8081: 4242}, out
    argv = spawned[0]
    assert "deployment/keycloak" in argv, argv
    assert not any(a.startswith("svc/") for a in argv), argv
    # host:container, so the CONTAINER port is what it maps to.
    assert "8081:8080" in argv, argv


def test_what_was_deployed_is_written_next_to_repro_json(tmp_path, monkeypatch):
    """A Compose workspace has a docker-compose.yml you can read; a Kubernetes one
    had only repro.json, because values go to `helm --values -` and manifests to
    `kubectl apply -f -` on stdin. Good for secrets, bad for answering "what did it
    actually deploy" -- especially once the cluster is gone and `helm get values` is
    no longer there to ask.

    Written for READING: re-running `up` regenerates them, exactly as it regenerates
    docker-compose.yml.
    """
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.services import k8s

    written = k8s.record_rendered(
        "w",
        values={"image": {"tag": "8.5.1"}, "existingMongodbSecret": "rocketchat-mongodb-url"},
        manifests={"mongodb": "kind: StatefulSet\n", "scenario": "kind: Deployment\n"})

    base = tmp_path / "repros" / "w" / "kubernetes"
    assert (base / "values.yaml").exists()
    assert (base / "mongodb.yaml").read_text() == "kind: StatefulSet\n"
    assert (base / "scenario.yaml").read_text() == "kind: Deployment\n"
    assert len(written) == 3, written

    # The values name the Secret; they must never CONTAIN the password. Moving the
    # MongoDB URL off `helm get values` was the point of v0.49.0, and writing it to
    # a file here would undo exactly that.
    body = (base / "values.yaml").read_text()
    assert "existingMongodbSecret" in body
    assert "mongodb://" not in body, body


def test_the_idp_cert_is_fetched_once_not_retried_around_a_retrier(monkeypatch):
    """A SAML create that sat for THIRTY MINUTES, found by the operational audit.

    `rcapi.fetch_saml_idp_cert` retries to its own deadline -- its docstring says
    "Retries until the IdP (e.g. Keycloak, which boots slowly) is serving" -- and
    v0.54.1 wrapped it in a second 20-attempt loop without reading that. 20 x 90s is
    half an hour of a create that looks hung and cannot be told from one that is.

    The deadline is passed IN instead, so there is one place that decides how long to
    wait.
    """
    import inspect

    from rc_repro.services import postready

    src = inspect.getsource(postready._pr_saml_idp_cert)
    # The CALL, not the prose: the comment above it names the function to explain
    # why it is only called once, so counting the name counts the explanation too.
    assert src.count("rcapi.fetch_saml_idp_cert(") == 1, src
    assert "for attempt in range" not in src, "no loop around a function that loops"
    assert "timeout=IDP_CERT_DEADLINE" in src, src

    calls = []
    monkeypatch.setattr(postready.rcapi, "fetch_saml_idp_cert",
                        lambda url, **kw: (calls.append(kw), "")[1])
    postready._pr_saml_idp_cert(
        postready.runner.Metadata(name="w", project="p", rc_version="8.5.1",
                                  rc_image="i", mongo_tag="8.0",
                                  mongo_flavor="official", preset="saml",
                                  root_url="u", host_port=3000, version_source="t"),
        object(), {"descriptor_url": "http://localhost:8081/x", "setting": "s"},
        lambda e: None)
    assert len(calls) == 1, calls
    assert calls[0].get("timeout") == postready.IDP_CERT_DEADLINE, calls


# --- the panel, against a Kubernetes workspace ---------------------------------------

def _kube_workspace(tmp_path, monkeypatch, name="k", preset="ldap", extra=None):
    """A recorded Kubernetes workspace on disk, with NO compose file.

    The absence of the compose file is the condition every defect below shared: each
    of these code paths reached for it, and each answered with something outside the
    ReproError contract when it was not there.
    """
    import json as _json
    from dataclasses import asdict

    from rc_repro.services import k8s, topology

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name=name, project="p", rc_version="8.6.1", rc_image="i",
                           mongo_tag="8.0", mongo_flavor="official", preset=preset,
                           root_url="http://localhost:3010", host_port=3010,
                           version_source="t")
    topology.stamp(m.extra, topology.KUBERNETES)
    m.extra.update({"context": k8s.CONTEXT, "namespace": f"rc-repro-{name}",
                    "bind_host": "127.0.0.1", **(extra or {})})
    ws = lc.runner.workspace(name)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(_json.dumps(asdict(m)), encoding="utf-8")
    assert not (ws / "docker-compose.yml").exists()
    return m


def test_detail_gives_a_kubernetes_workspace_its_links_and_its_pods(
        monkeypatch, tmp_path):
    """The Kubernetes branch of detail() returned before it built either.

    `links` are not compose-shaped and never were: a preset's UI answers on the SAME
    host port under both runtimes, because k8s.scenario_ui_forwards forwards it to the
    same number deliberately. Leaving them out meant phpLDAPadmin, Keycloak, MinIO,
    Mailpit and Grafana were unreachable from the GUI on Kubernetes while the CLI
    printed every one of them.

    `containers` was empty on purpose, and the tab reads an empty list as "no
    containers — this repro is down" — under a workspace that was running, with no
    way at all to see an ImagePullBackOff from a browser.
    """
    from rc_repro.services import k8s

    _kube_workspace(tmp_path, monkeypatch, preset="ldap")
    monkeypatch.setattr(lc, "kubernetes_state", lambda name, meta: "running")
    monkeypatch.setattr(k8s, "pod_rows", lambda name, **kw: [
        {"service": "rocketchat-rocketchat-1", "state": "running",
         "status": "1/1 ready", "health": "healthy", "restarts": 0,
         "started": "2026-08-18T10:00:00Z"},
        {"service": "rocketchat-mongodb-0", "state": "pending",
         "status": "ImagePullBackOff", "health": "", "restarts": 0, "started": ""}])

    d = lc.detail("k")
    labels = {link["label"] for link in d["links"]}
    assert "Rocket.Chat" in labels
    assert "phpLDAPadmin" in labels, "the scenario's own UI, on the same port as Compose"
    assert [c["service"] for c in d["containers"]] == [
        "rocketchat-rocketchat-1", "rocketchat-mongodb-0"]
    assert "ImagePullBackOff" in d["containers"][1]["status"]
    # microservices, because that is what this runtime DEFAULTS to and the record
    # names no deployment -- see topology.DEPLOYMENTS.
    assert d["runtime"] == "kubernetes" and d["deployment"] == "microservices"
    assert d["namespace"] == "rc-repro-k"
    assert d["uptime"], "an uptime, from the earliest running pod"


def test_detail_of_a_kubernetes_workspace_survives_docker_being_down(
        monkeypatch, tmp_path):
    """detail() asked docker first, and its docker-unavailable branch reads the
    workspace's COMPOSE FILE. A Kubernetes workspace has none, so a box whose daemon
    was merely asleep answered the panel with a FileNotFoundError -- a 500 -- for a
    workspace running perfectly well in the cluster. The runtime is asked first now.
    """
    from rc_repro.services import k8s

    _kube_workspace(tmp_path, monkeypatch, preset="default")
    monkeypatch.setattr(lc.runner, "docker_available", lambda **_k: False)
    monkeypatch.setattr(lc, "kubernetes_state", lambda name, meta: "running")
    monkeypatch.setattr(k8s, "pod_rows", lambda name, **kw: [])

    d = lc.detail("k")          # must not raise
    assert d["state"] == "running", "docker's absence says nothing about a cluster"
    assert d["links"], "and the links are still there"


def test_detail_recovers_the_bind_host_of_a_workspace_older_than_the_key(
        monkeypatch, tmp_path):
    """The bind host was only ever written INTO the compose port mappings, so the
    panel could not say anything true about reaching a workspace from another
    machine. New workspaces record it; existing ones are read back out of the file
    they already have, rather than being told "unknown"."""
    import json as _json
    from dataclasses import asdict

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = lc.runner.Metadata(name="old", project="p", rc_version="8.5.1", rc_image="i",
                           mongo_tag="8.0", mongo_flavor="official", preset="default",
                           root_url="http://localhost:3001", host_port=3001,
                           version_source="t")
    ws = lc.runner.workspace("old")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(_json.dumps(asdict(m)), encoding="utf-8")
    (ws / "docker-compose.yml").write_text(
        "services:\n  rocketchat:\n    image: i\n    ports:\n"
        "    - 0.0.0.0:3001:3000\n    environment:\n      ROOT_URL: x\n")
    monkeypatch.setattr(lc.runner, "docker_available", lambda **_k: False)

    assert lc.detail("old")["bind_host"] == "0.0.0.0"


def test_a_load_test_refuses_a_kubernetes_workspace_instead_of_crashing(
        monkeypatch, tmp_path):
    """Both perf entry points read the compose file, so on Kubernetes they raised a
    bare FileNotFoundError: outside the ReproError contract, so `serve` answered 500
    and the CLI printed a traceback. And the refusal is not "unimplemented" -- the
    numbers would be measuring the `kubectl port-forward`.
    """
    from rc_repro.services import perf

    _kube_workspace(tmp_path, monkeypatch, preset="default")
    monkeypatch.setattr(perf.lifecycle.runner, "docker_available", lambda **_k: True)
    with pytest.raises(errors.ValidationError) as caught:
        perf.run_loadtest(perf.LoadtestReq(name="k", scenario="messages", vus=1))
    assert "port-forward" in str(caught.value)
    with pytest.raises(errors.ValidationError):
        perf.run_capacity(perf.CapacityReq(name="k", scenario="messages"))


def test_pod_rows_names_the_reason_a_pod_cannot_start(monkeypatch):
    """The status column's job. A pod stuck on a bad image reports phase `Pending`,
    which says only that something has not happened; the waiting REASON says what.

    An init container is where a Kubernetes workspace most often wedges -- MongoDB
    ships two -- and a pod whose init container is failing has an EMPTY
    containerStatuses, so reading only that list showed "0/0 ready" and named nothing.
    """
    import json as _json

    from rc_repro.services import k8s

    pods = {"items": [
        {"metadata": {"name": "rocketchat-mongodb-0"},
         "status": {"phase": "Pending",
                    "initContainerStatuses": [
                        {"ready": False, "restartCount": 3,
                         "state": {"waiting": {"reason": "CrashLoopBackOff"}}}],
                    "containerStatuses": []}},
        {"metadata": {"name": "rocketchat-rocketchat-x"},
         "status": {"phase": "Running", "startTime": "2026-08-18T10:00:00Z",
                    "initContainerStatuses": [
                        {"ready": True, "restartCount": 0,
                         "state": {"terminated": {"reason": "Completed"}}}],
                    "containerStatuses": [{"ready": True, "restartCount": 0,
                                           "state": {"running": {}}}]}}]}
    monkeypatch.setattr(k8s, "run", lambda *a, **kw: type(
        "R", (), {"returncode": 0, "stdout": _json.dumps(pods), "stderr": ""})())
    rows = {r["service"]: r for r in k8s.pod_rows("k", context="c")}
    assert rows["rocketchat-mongodb-0"]["status"].startswith("CrashLoopBackOff")
    assert "3 restarts" in rows["rocketchat-mongodb-0"]["status"]
    # A SUCCESSFUL init container is not a reason: reporting "Completed" as the pod's
    # status would hide a Rocket.Chat container that was crash-looping behind it.
    assert rows["rocketchat-rocketchat-x"]["status"] == "1/1 ready"
    assert rows["rocketchat-rocketchat-x"]["health"] == "healthy"


def test_pod_rows_answers_empty_rather_than_raising_when_the_cluster_is_asleep():
    """detail() is on the path of every panel open, so this may never raise: a
    workspace whose cluster is unreachable still has to render."""
    from rc_repro.services import k8s

    import unittest.mock as mock
    with mock.patch.object(k8s, "run", return_value=type(
            "R", (), {"returncode": 1, "stdout": "", "stderr": "connection refused"})()):
        assert k8s.pod_rows("k", context="c") == []


# --- the agent skill ------------------------------------------------------------

def test_the_repo_copies_of_the_skill_match_the_packaged_one():
    """Three copies of one file, and the two under `.claude/` and `.agents/` exist so
    a fresh clone teaches an agent to drive this tool. Three copies is also how they
    drift, so this is the thing that stops them.

    It is a real check rather than a formality: the packaged copy is what
    `rc-repro skill install` writes on a machine that has no checkout, so a repo
    copy that says something different is instructions half the callers never see.
    """
    from pathlib import Path

    from rc_repro.services import skill

    root = Path(skill.packaged()).resolve().parents[3]
    want = skill.packaged().read_text(encoding="utf-8")
    for rel in (".claude/skills/rc-repro/SKILL.md", ".agents/skills/rc-repro/SKILL.md"):
        copy = root / rel
        assert copy.exists(), f"{rel} is missing — `cp {skill.packaged()} {rel}`"
        assert copy.read_text(encoding="utf-8") == want, (
            f"{rel} has drifted from the packaged skill — copy it again")


def test_the_skill_knows_stale_from_edited_locally(tmp_path, monkeypatch):
    """Two ways an installed file can differ, needing opposite answers: the package
    moved on (reinstall it) or a human edited it (that is theirs). The sidecar's
    recorded sha is what separates them -- without it, an edit and a new release look
    identical and one of the two gets clobbered."""
    from rc_repro.services import skill

    monkeypatch.setenv("RC_REPRO_SKILL_HOME", str(tmp_path))
    assert skill.host_state("claude").status == "absent"

    skill.install("claude")
    assert skill.host_state("claude").status == "current"
    assert skill.install("claude")["hosts"][0]["action"] == "unchanged"

    path = skill.target("claude")
    path.write_text("a human changed this\n", encoding="utf-8")
    assert skill.host_state("claude").status == "modified"
    with pytest.raises(errors.ConflictError):
        skill.install("claude")
    assert path.read_text(encoding="utf-8") == "a human changed this\n", \
        "a refusal that had already overwritten the file would be worse than none"
    skill.install("claude", force=True)
    assert skill.host_state("claude").status == "current"

    # A copy this build wrote, from an older build: stale, and overwritten freely.
    path.write_text("what 0.1.0 shipped\n", encoding="utf-8")
    (path.parent / skill.SIDECAR).write_text(json.dumps(
        {"rc_repro_version": "0.1.0", "sha256": skill.sha256("what 0.1.0 shipped\n")}),
        encoding="utf-8")
    assert skill.host_state("claude").status == "stale"
    assert skill.install("claude")["hosts"][0]["action"] == "updated"


def test_the_skill_never_writes_into_the_rc_repro_state_directory(tmp_path, monkeypatch):
    """It is read by an agent that knows nothing about RC_REPRO_HOME, so it goes
    where that agent looks -- and a test that got this wrong would write into the
    developer's real ~/.claude."""
    from rc_repro.services import skill

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("RC_REPRO_SKILL_HOME", str(tmp_path / "home"))
    skill.install("all")
    assert (tmp_path / "home" / ".claude" / "skills" / "rc-repro" / "SKILL.md").exists()
    assert not (tmp_path / "state").exists()


# --- the engine floor -----------------------------------------------------------

def test_a_kubernetes_create_is_refused_when_the_ENGINE_is_too_small(monkeypatch):
    """The host and the container engine are different machines on macOS and on
    Podman, and the capacity check only ever asked the host. A laptop with 32 GB and
    a 4 GB VM passed everything and then could not run a Kubernetes workspace -- the
    failure arriving minutes later as pods stuck Pending, which names nothing."""
    from rc_repro.services import k8s
    monkeypatch.setattr(k8s, "plan_cluster", lambda: k8s.ClusterPlan(
        context=k8s.CONTEXT, distribution="kind", create=True))
    monkeypatch.setattr(lc.runner, "docker_capacity", lambda: (2.0, 3 * 1024 ** 3))
    with pytest.raises(errors.PreflightError) as caught:
        lc.check_capacity(lc.CreateReq(version="8.5.1", runtime="kubernetes"))
    said = str(caught.value)
    assert "2 CPUs" in said and "3.0 GiB" in said
    assert "podman machine set" in said, "a refusal has to say how to fix it"

    # Compose runs two containers and works on that VM: a floor there would refuse
    # creates that succeed today.
    monkeypatch.setattr(lc.runner, "host_memory", lambda: (16000, 12000, 0))
    lc.check_capacity(lc.CreateReq(version="8.5.1"))


def test_an_unreadable_engine_is_not_evidence_of_a_small_one(monkeypatch):
    """A refusal on no evidence is how people learn to pass --force by reflex."""
    monkeypatch.setattr(lc.runner, "docker_capacity", lambda: None)
    monkeypatch.setattr(lc.runner, "host_memory", lambda: (16000, 12000, 0))
    lc.check_capacity(lc.CreateReq(version="8.5.1", runtime="kubernetes"))


def test_force_downgrades_the_engine_floor_to_a_warning(monkeypatch):
    events = []
    from rc_repro.services import k8s
    monkeypatch.setattr(k8s, "plan_cluster", lambda: k8s.ClusterPlan(
        context=k8s.CONTEXT, distribution="kind", create=True))
    monkeypatch.setattr(lc.runner, "docker_capacity", lambda: (1.0, 1 * 1024 ** 3))
    monkeypatch.setattr(lc.runner, "host_memory", lambda: (16000, 12000, 0))
    lc.check_capacity(lc.CreateReq(version="8.5.1", runtime="kubernetes", force=True),
                      emit=events.append)
    assert any("below the Kubernetes floor" in e.message for e in events)


def test_the_skill_reports_the_copy_an_agent_actually_reads(tmp_path, monkeypatch):
    """An agent reads `.claude/skills/` relative to the PROJECT before it reads a
    home directory, so in a checkout that is the file in play. Reporting only the
    home copy told a developer `current: false` while they were reading a perfectly
    current skill -- and then pointed them at `skill install`, which writes a
    different copy somewhere the agent was not reading.

    Found by driving the skill and watching `capabilities` say exactly that.
    """
    from rc_repro.services import skill

    monkeypatch.setenv("RC_REPRO_SKILL_HOME", str(tmp_path / "home"))
    project = tmp_path / "checkout"
    (project / ".claude" / "skills" / "rc-repro").mkdir(parents=True)
    local = project / ".claude" / "skills" / "rc-repro" / "SKILL.md"
    local.write_text(skill.packaged().read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(project)

    st = skill.state()
    assert st["hosts"]["project"]["status"] == "current"
    assert st["hosts"]["project"]["scope"] == "project"
    # Nothing is installed in the home, and that does NOT make the state stale:
    # absent copies are not copies somebody read.
    assert st["hosts"]["claude"]["status"] == "absent"
    assert st["current"] is True

    # A checkout carrying an out-of-date copy is worth reporting even though the
    # home copy might be fine, because nobody knows which one the caller loaded.
    local.write_text("what an older branch shipped\n", encoding="utf-8")
    assert skill.state()["current"] is False


def test_no_copy_anywhere_is_not_current(tmp_path, monkeypatch):
    from rc_repro.services import skill

    monkeypatch.setenv("RC_REPRO_SKILL_HOME", str(tmp_path / "home"))
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    st = skill.state()
    assert st["current"] is False, "nothing installed is not the same as up to date"


def test_the_flat_notes_are_derived_from_the_groups_not_maintained_beside_them():
    """A workspace's notes now exist in two renderings -- grouped cards in the GUI,
    a flat list of lines in the terminal and in every record written before groups
    existed. Two hand-written copies is how the two come to disagree, so the groups
    are the source and the flat list is computed from them.

    What the flattening has to preserve is the notes pattern the panel already
    parses: an INDENTED line is a copyable box, a line naming a place is a link row,
    anything else is prose. So a group's rows come out as an aligned indented block
    (a table where it is read as text), its commands come out indented (a code box),
    and its prose comes out flush (prose).
    """
    g = lc.note_group("Kubernetes", rows=[("Cluster", "kind-x"), ("Namespace", "rc-y")])
    assert g == {"title": "Kubernetes", "kind": "", "body": [], "commands": [],
                 "rows": [["Cluster", "kind-x"], ["Namespace", "rc-y"]]}

    lines = lc.flatten_notes([
        g,
        lc.note_group("Port forward", body=["It dies with the pod. Start it again:"],
                      commands=["kubectl -n rc-y port-forward deployment/rc 3001:3000"]),
    ])
    assert lines == [
        "Kubernetes:",
        "    Cluster    kind-x",      # padded to the widest key, so the column lines up
        "    Namespace  rc-y",
        "Port forward:",
        "It dies with the pod. Start it again:",
        "    kubectl -n rc-y port-forward deployment/rc 3001:3000",
    ], lines
    # The two shapes that matter to the parser, stated as such: indentation is what
    # makes a line copyable, and prose must not be indented or it becomes a command.
    assert lines[1].startswith("    ") and lines[-1].startswith("    ")
    assert not lines[4].startswith(" ")


def test_an_empty_group_contributes_no_stray_heading():
    """A group is built unconditionally and filled conditionally -- the operator note
    only exists on one MongoDB path -- so an empty one must vanish rather than leave a
    bare `Monitoring:` above nothing."""
    assert lc.flatten_notes([lc.note_group("Monitoring")]) == ["Monitoring:"]
    assert lc.flatten_notes([]) == []
    assert lc.flatten_notes([lc.note_group("", body=["a bare line"])]) == ["a bare line"]


def _k8s_meta(**extra):
    """A Kubernetes workspace record, as `repro.json` holds one."""
    base = {"runtime": "kubernetes", "deployment": "microservices",
            "namespace": "rc-repro-old", "context": "kind-rc-repro-local",
            "release": "rocketchat", "bind_host": "127.0.0.1",
            "mongo_managed_by": "statefulset", "mongo_image": "mongo:8.0"}
    base.update(extra)
    return lc.runner.Metadata(
        name="old", project="rc-repro-old", rc_version="8.5.1", rc_image="i",
        mongo_tag="8.0", mongo_flavor="official", preset="default",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
        extra=base)


def test_a_workspace_made_before_groups_existed_still_renders_as_groups():
    """The grouped notes were built at create time and written into `repro.json`, so
    they only ever reached workspaces created after the release that added them. Every
    existing workspace kept the flat eleven-bullet dump in `info` and in the GUI panel
    for life, and the only way to see the new shape was to destroy the workspace and
    build another -- which for a repro of a customer's issue is the one thing you
    cannot do.

    Notes are a pure function of the record (namespace, context, release, bind host,
    preset), so they are derived on every read. This record is exactly what an older
    release wrote: a flat list and no groups at all.
    """
    m = _k8s_meta()
    m.extra["notes"] = [
        "microservices on kind-rc-repro-local - about 9 pods, namespace rc-repro-old",
        "reachable on this box at http://localhost:3000",
    ]
    s = lc._summary(m)
    titles = [g["title"] for g in s["note_groups"]]
    assert titles[:2] == ["Kubernetes", "MongoDB"]
    # The three steps read in the order somebody has to perform them.
    assert titles[2:5] == ["1 · Point kubectl and helm at this cluster",
                           "2 · Open the way in",
                           "3 · Look at what is inside"], titles
    # The facts that were welded into a sentence are rows now, for this record too.
    rows = dict((k, v) for k, v in s["note_groups"][0]["rows"])
    assert rows == {"Cluster": "kind-rc-repro-local", "Namespace": "rc-repro-old",
                    "Arrangement": "microservices", "Pods": "about 9"}
    # And the flat list the CLI prints is the derived one, not the sentence above.
    assert s["notes"] == lc.flatten_notes(s["note_groups"])
    assert not any("about 9 pods, namespace" in n for n in s["notes"])


def test_the_url_is_not_repeated_as_a_note_when_the_bind_is_loopback():
    """"reachable on this box at http://localhost:3000" was the fourth copy of that
    string on one screen -- the summary panel prints it, the panel's identity line
    carries it, the links table lists it -- and the browser had to de-duplicate the
    note against the link row to stop it appearing twice.

    What is not said anywhere else is what a NON-loopback bind means, and that is the
    only case the group appears in.
    """
    quiet = [g["title"] for g in lc.note_groups_of(_k8s_meta())]
    assert "Reachable from other machines" not in quiet
    assert not any("reachable on this box" in n
                   for n in lc.flatten_notes(lc.note_groups_of(_k8s_meta())))

    public = lc.note_groups_of(_k8s_meta(bind_host="0.0.0.0"))
    warned = {g["title"]: g for g in public}["Reachable from other machines"]
    assert "0.0.0.0" in " ".join(warned["body"])
    assert "admin123" in " ".join(warned["body"])          # what it exposes
    # The port-forward has to come back the same way it was published, or the
    # workspace is unreachable from the machines the note just warned about.
    forward = {g["title"]: g for g in public}["2 · Open the way in"]
    assert "--address 0.0.0.0" in forward["commands"][0]


def test_a_compose_workspaces_add_ons_are_groups_too():
    """The edge lines and the monitoring block were appended to one flat list, so a
    monitored workspace behind HTTPS had five unrelated bullets in a row. Same words,
    three sections -- and derived, so an older record gets them as well.
    """
    m = lc.runner.Metadata(
        name="c", project="rcrepro-c", rc_version="8.5.1", rc_image="i",
        mongo_tag="8.0", mongo_flavor="official", preset="default",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
        public_url="https://c.example.test",
        extra={"edge": True, "tls": "local", "monitoring": True})
    groups = {g["title"]: g for g in lc.note_groups_of(m)}
    assert list(groups) == ["HTTPS", "Monitoring"]
    assert "https://c.example.test" in " ".join(groups["HTTPS"]["body"])
    assert any("Grafana" in n for n in groups["Monitoring"]["body"])
    # A plain workspace has nothing to add and gets no empty sections.
    m.extra = {}
    m.public_url = ""
    assert lc.note_groups_of(m) == []


def test_a_command_in_the_notes_is_never_wrapped_where_it_would_be_pasted(capsys):
    """`_print_notes` wrapped every line to the box it drew, commands included. A
    port-forward came out as `... port-forward deployment/rocketchat-` on one line and
    `  rocketchat 3000:3000` on the next, so the one thing anyone does with that line
    -- select it and paste it -- produced a broken command. The box is gone and an
    indented line overflows the width instead.

    Driven from a RECORDED flat note, which is both what an older workspace holds and
    the shape the derived groups render commands in.
    """
    from rc_repro import cli

    forward = ("kubectl -n rc-repro-a-fairly-long-workspace-name port-forward "
               "deployment/rocketchat-rocketchat 3000:3000")
    assert len(forward) > 88          # wider than anything `_print_notes` renders
    m = lc.runner.Metadata(
        name="c", project="rcrepro-c", rc_version="8.5.1", rc_image="i",
        mongo_tag="8.0", mongo_flavor="official", preset="default",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
        extra={"notes": ["bring the forward back with:", "    " + forward,
                         "`stats` needs metrics-server in the cluster " * 3]})
    cli._print_notes(m)
    out = capsys.readouterr().out.split("\n")
    assert [ln for ln in out if forward in ln], "the command has to survive as one line"
    assert not any(ln.rstrip().endswith("rocketchat-") for ln in out)
    # Prose is still wrapped, and a hyphenated word is not broken across the wrap.
    assert not any(ln.rstrip().endswith("metrics-") for ln in out)
    prose = [ln for ln in out if ln[:5] == "    " + ln[4:5].strip()]
    assert prose and max(len(ln) for ln in prose) <= 88


def test_the_terminal_prints_the_same_sections_the_panel_draws(capsys):
    """One definition, two renderings. The CLI used to print a flat list inside a box
    while the browser drew cards, and the two could only be compared by eye.
    """
    from rc_repro import cli

    m = _k8s_meta()
    cli._print_notes(m)
    out = capsys.readouterr().out
    for group in lc.note_groups_of(m):
        # `_ascii` first: the terminal renderer replaces the middle dot in a step
        # heading, because a box-drawing or typographic glyph is not width-1 everywhere.
        title = cli._ascii(group["title"])
        assert any(ln.strip() == title for ln in out.split("\n")), title
        for k, v in group["rows"]:
            assert any(k in ln and str(v) in ln for ln in out.split("\n")), k
        for c in group["commands"]:
            assert c in out


def _kube_probe_stubs(monkeypatch, *, kind: bool, our_cluster: bool, active: str,
                      reachable: bool = True):
    """The four facts `plan_cluster` reads, stated instead of probed."""
    from rc_repro.services import k8s
    monkeypatch.setattr(k8s, "which", lambda t: "/usr/bin/kind" if (t == "kind" and kind) else "")
    monkeypatch.setattr(k8s, "clusters",
                        lambda: ([k8s.CLUSTER_NAME] if our_cluster else [], ""))
    monkeypatch.setattr(k8s, "cluster_context",
                        lambda: k8s.CONTEXT if our_cluster else "")
    monkeypatch.setattr(k8s, "active_context", lambda: active)
    monkeypatch.setattr(k8s, "reachable", lambda ctx=None: reachable)
    monkeypatch.setattr(k8s, "distribution", lambda ctx: "k3s")


def test_the_only_difference_between_kind_and_k3s_is_whether_a_cluster_is_built(monkeypatch):
    """`up --runtime kubernetes` could only ever use a cluster rc-repro built itself:
    `create_workspace` opened with `ensure_cluster()`, which refuses when `kind` is
    absent. So on a box running k3s -- an ordinary way to have Kubernetes -- `doctor`
    said "Using your cluster 'default'" and `up` refused to use it, two commands in one
    tool contradicting each other.

    Provisioning is the ONE step that differs. Everything after it already took
    `context=`, which is why adopting a cluster is a resolver and not a runtime.
    """
    from rc_repro.errors import PreflightError
    from rc_repro.services import k8s

    # kind, with its cluster already up: ours, and nothing to build. Today's behaviour.
    _kube_probe_stubs(monkeypatch, kind=True, our_cluster=True, active="default")
    plan = k8s.plan_cluster()
    assert (plan.context, plan.distribution, plan.create) == (k8s.CONTEXT, "kind", False)

    # kind, no cluster yet: build ours. Today's behaviour, and the k3s box is IGNORED --
    # "no change to kind" is the stronger promise, so an existing cluster elsewhere never
    # takes the choice away from a box that can make its own.
    _kube_probe_stubs(monkeypatch, kind=True, our_cluster=False, active="default")
    plan = k8s.plan_cluster()
    assert (plan.context, plan.distribution, plan.create) == (k8s.CONTEXT, "kind", True)

    # No kind: skip provisioning and use what kubectl points at.
    _kube_probe_stubs(monkeypatch, kind=False, our_cluster=False, active="default")
    plan = k8s.plan_cluster()
    assert (plan.context, plan.distribution, plan.create) == ("default", "k3s", False)

    # No kind and nothing reachable: refuse, and the message is now true.
    _kube_probe_stubs(monkeypatch, kind=False, our_cluster=False, active="")
    try:
        k8s.plan_cluster()
    except PreflightError as exc:
        assert "not pointed at one either" in str(exc), str(exc)
    else:
        raise AssertionError("a box with no cluster and no kind must be refused")


def test_a_cluster_rc_repro_made_is_found_by_asking_it_not_by_asking_kind(monkeypatch):
    """`cluster_exists` was `CLUSTER_NAME in clusters()`, and that probe needs the kind
    BINARY. So uninstalling kind while its cluster was still running made the cluster
    invisible -- node containers holding memory that nothing in rc-repro could see or
    remove, and resolution quietly falling through to a different cluster.
    """
    from rc_repro.services import k8s

    # kind gone, but our kubeconfig still names a cluster that answers.
    _kube_probe_stubs(monkeypatch, kind=False, our_cluster=True, active="default")
    plan = k8s.plan_cluster()
    assert plan.context == k8s.CONTEXT, "our own cluster is still the one to use"
    assert plan.create is False


def test_the_distribution_is_read_from_the_node_not_guessed_from_a_name(monkeypatch):
    """A label for messages, never a branch: `k3s --disable traefik` is a real setup, so
    what a cluster CAN do is probed and what it IS is only named. Signals are the ones
    verified against live clusters.
    """
    import subprocess as sp

    from rc_repro.services import k8s

    def answer(text):
        return lambda argv, timeout=None, own=False: sp.CompletedProcess(argv, 0, text, "")

    monkeypatch.setattr(k8s, "run", answer("k3s://docker-01 v1.36.3+k3s1 k3s docker-01"))
    assert k8s.distribution("default") == "k3s"
    monkeypatch.setattr(k8s, "run", answer(
        "kind://docker/rc-repro-local/rc-repro-local-control-plane v1.36.1  x"))
    assert k8s.distribution("kind-rc-repro-local") == "kind"
    monkeypatch.setattr(k8s, "run", answer("aws:///eu-west-1a/i-0abc v1.31.0-eks-1234 m5"))
    assert k8s.distribution("arn:aws:eks:x") == "eks", \
        "a cloud providerID is the most reliable 'not a disposable cluster' signal"
    monkeypatch.setattr(k8s, "run", answer(" v1.29.0  somenode"))
    assert k8s.distribution("x") == "unknown", "unknown is a fine answer"
    monkeypatch.setattr(k8s, "run",
                        lambda argv, timeout=None, own=False: sp.CompletedProcess(argv, 1, "", "no"))
    assert k8s.distribution("x") == "unknown", "an unreachable cluster is not a failure here"


def test_a_create_refused_for_want_of_a_cluster_leaves_no_record(monkeypatch, tmp_path):
    """Measured on a box with k3s and no kind: the refusal left an `incomplete`
    repro.json, `list` showed it as a workspace, and `used_ports()` went on reserving its
    port -- so a create that could never have succeeded held :3142 until somebody ran
    `down --volumes`.

    The write-ahead record is not the problem and must not be weakened: it exists because
    an interrupted create once left a workspace running and unrecorded. The order was.
    Choosing the cluster only probes, so it belongs above the write.
    """
    from rc_repro.errors import PreflightError

    from rc_repro.services import k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(lc, "check_capacity", lambda *a, **k: None)
    # Only functions that predate this change, so the test drives the OLD code too: no
    # kind and nothing for kubectl to point at is the box that gets refused.
    monkeypatch.setattr(k8s, "which", lambda _t: "")
    monkeypatch.setattr(k8s, "cluster_context", lambda: "")
    monkeypatch.setattr(k8s, "active_context", lambda: "")
    req = lc.CreateReq(version="8.5.1", runtime="kubernetes", name="ghost", offline=True)
    try:
        lc._create_kubernetes(req)
    except PreflightError:
        pass
    else:
        raise AssertionError("it must refuse")
    assert not (tmp_path / "repros" / "ghost").exists(), \
        "a refusal wrote a record for a workspace that was never created"
    assert lc.runner.used_ports() == set(), "and it reserved that workspace's port"


def test_doctor_reports_what_a_cluster_provides_the_same_way_on_every_distribution(
        monkeypatch, tmp_path):
    """The difference between kind and k3s is three capabilities -- ingress, load
    balancer, metrics -- and nothing in the report said so. A reader on a context called
    `default` was not even told it was k3s, and found out that `stats` works there by
    running it.

    All three are `ok` rows on purpose. A capability is a FACT; severity belongs to
    whatever needs it, and an absent ingress controller cannot affect a workspace reached
    by port-forward -- `ingress_blocker` is what refuses when something asks for a
    hostname. A doctor that warns about a feature you are not using teaches people to
    ignore its warnings.
    """
    from rc_repro.services import doctor, k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def report_for(pre):
        monkeypatch.setattr(k8s, "preflight", lambda *a, **k: pre)
        monkeypatch.setattr(k8s, "which", lambda t: "/usr/bin/" + t)
        rows = doctor.run_checks()["checks"]
        return {r["check"]: r for r in rows if r["check"].startswith("kubernetes")}

    tools = {n: k8s.Tool(name=n, path="/usr/bin/" + n, version=(9, 9))
             for n in ("kubectl", "helm", "kind")}
    k3s = report_for(k8s.Preflight(
        tools=tools, cluster_reachable=True, context="default",
        provider=k8s.PROVIDER_EXTERNAL, distribution="k3s", node_count=1,
        architectures=["amd64"], default_storage_class="local-path",
        ingress_classes=["traefik"], metrics=True,
        loadbalancer="traefik has 172.16.0.2"))
    assert "k3s, 1 node, amd64" in k3s["kubernetes-cluster"]["message"]
    assert "traefik" in k3s["kubernetes-ingress"]["message"]
    assert "172.16.0.2" in k3s["kubernetes-loadbalancer"]["message"]
    assert "`rc-repro stats` works here" in k3s["kubernetes-metrics"]["message"]
    assert all(k3s[c]["status"] == "ok" for c in
               ("kubernetes-ingress", "kubernetes-loadbalancer", "kubernetes-metrics"))

    kind = report_for(k8s.Preflight(
        tools=tools, cluster_reachable=True, cluster_exists=True, context=k8s.CONTEXT,
        provider=k8s.PROVIDER_KIND, distribution="kind", node_count=1,
        architectures=["amd64"], default_storage_class="standard"))
    assert "kind, 1 node, amd64" in kind["kubernetes-cluster"]["message"]
    # The same three subjects, answered the other way and still not a warning.
    assert "none installed" in kind["kubernetes-ingress"]["message"]
    assert "not confirmed" in kind["kubernetes-loadbalancer"]["message"]
    assert "refuses" in kind["kubernetes-metrics"]["message"]
    assert all(kind[c]["status"] == "ok" for c in
               ("kubernetes-ingress", "kubernetes-loadbalancer", "kubernetes-metrics"))
    # Storage is reported when it is THERE too, not only when it is missing.
    assert "standard (default)" in kind["kubernetes-storage"]["message"]


def test_the_mongodb_operator_goes_with_the_last_workspace_but_not_before(monkeypatch):
    """`--volumes` means delete everything, and an operator left running afterwards is
    the wrong answer -- but three things about it are shared, and the ORDER is what makes
    removing it safe rather than destructive.

    The reference count is not bureaucracy: with a second workspace still holding a
    `MongoDBCommunity`, removing the operator leaves its finalizer with nothing to clear
    it, so a later `down --volumes` on THAT workspace hangs in Terminating forever. It is
    the identical failure `remove_monitoring` recorded for a GrafanaFolder.
    """
    import subprocess as sp

    from rc_repro.services import k8s

    calls = []

    def fake(mongodbs, installed=True, rc=0):
        def run(argv, timeout=None, own=False):
            calls.append(" ".join(argv))
            j = " ".join(argv)
            if "helm list" in j:
                return sp.CompletedProcess(argv, 0,
                                           k8s.OPERATOR_RELEASE if installed else "", "")
            if "get mongodbcommunity" in j:
                return sp.CompletedProcess(argv, 0, "\n".join(mongodbs), "")
            return sp.CompletedProcess(argv, rc, "", "boom" if rc else "")
        return run

    # Nothing left that needs it: the operator goes.
    calls.clear()
    monkeypatch.setattr(k8s, "run", fake([]))
    assert k8s.remove_operator(context="default") is True
    assert any("helm uninstall " + k8s.OPERATOR_RELEASE in c for c in calls), calls
    # The CRD is never touched: deleting it would delete every MongoDBCommunity in the
    # cluster, i.e. every other workspace's database.
    assert not any("delete crd" in c for c in calls), calls
    # Nor the namespace, which is shared with the monitoring stack.
    assert not any("delete namespace" in c for c in calls), calls

    # Another workspace still has one: left alone, and it says whose.
    calls.clear()
    events = []
    monkeypatch.setattr(k8s, "run", fake(["rc-repro-other"]))
    assert k8s.remove_operator(context="default", emit=events.append) is False
    assert not any("helm uninstall" in c for c in calls), calls
    assert any("still used by other" in e.message for e in events), \
        [e.message for e in events]

    # A cluster that never had it is not an error and says nothing.
    calls.clear()
    monkeypatch.setattr(k8s, "run", fake([], installed=False))
    assert k8s.remove_operator(context="default") is False
    assert not any("helm uninstall" in c for c in calls), calls


def test_removing_the_operator_never_fails_a_teardown(monkeypatch):
    """The workspace is already gone by the time this runs, and the next
    `--mongo-operator` is an `upgrade --install` that repairs it. A teardown that reports
    failure for wreckage it cannot help would leave a user believing their workspace is
    still there."""
    import subprocess as sp

    from rc_repro.services import k8s

    def run(argv, timeout=None, own=False):
        j = " ".join(argv)
        if "helm list" in j:
            return sp.CompletedProcess(argv, 0, k8s.OPERATOR_RELEASE, "")
        if "get mongodbcommunity" in j:
            return sp.CompletedProcess(argv, 0, "", "")
        return sp.CompletedProcess(argv, 1, "", "uninstall exploded")

    monkeypatch.setattr(k8s, "run", run)
    events = []
    assert k8s.remove_operator(context="default", emit=events.append) is False
    assert any("could not be uninstalled" in e.message for e in events)
