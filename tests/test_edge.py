"""The shared edge: one Traefik holding 443 for the whole box.

Pure-logic only -- no Docker, like the rest of the suite. The container-level
claims (two workspaces behind one edge, hot registration, no published
ports, reaching a host process over the bridge) were verified live against
traefik:v3.4 before this was written; what those runs proved is recorded in
docs/design/team-server.md §11.
"""

from __future__ import annotations

import pytest
import yaml

from rc_repro import config
from rc_repro.errors import ValidationError
from rc_repro.services import edge as fd


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    return tmp_path


def _doc(**kw):
    return fd.compose_doc(fd.Edge(domain="support.xyz.com",
                                       acme_email="ops@xyz.com", **kw))


# --- staying invisible ----------------------------------------------------------

def test_the_edge_lives_outside_the_repro_directory(_home):
    """§3.1: `list`, `prune`, `down` and the GUI grid all enumerate repros_dir().
    Living outside it is what makes the edge untouchable by them -- no
    guard code, just the directory choice."""
    assert config.repros_dir() not in fd.edge_dir().parents
    assert fd.edge_dir().parent == config.home()


def test_the_project_name_cannot_collide_with_a_repro():
    """`down` on a repro named "edge" must not tear down the box's ingress.

    sanitize() maps everything outside [a-z0-9-] to "-", so no repro name can
    ever produce this project name -- but PROJECT_PREFIX + "edge" would.
    """
    from rc_repro.services.lifecycle import sanitize

    assert "_" in fd.PROJECT
    assert fd.PROJECT != config.PROJECT_PREFIX + "edge"
    assert sanitize("edge_x") != "edge_x", "sanitize strips underscores"


# --- the compose document -------------------------------------------------------

def test_it_watches_a_directory_not_a_file():
    """Registration is "write one file"; a single --providers.file.filename would
    make it "rewrite the shared file", which cannot be done concurrently."""
    cmd = _doc()["services"]["edge"]["command"]
    assert f"--providers.file.directory=/etc/traefik/{fd.DYNAMIC_DIR}" in cmd
    assert "--providers.file.watch=true" in cmd
    assert not any(a.startswith("--providers.file.filename") for a in cmd)


def test_it_does_not_mount_the_docker_socket():
    """The file provider exists precisely so this is not needed."""
    vols = _doc()["services"]["edge"]["volumes"]
    assert not any("docker.sock" in v for v in vols)


def test_acme_storage_is_shared_with_workspaces():
    """§6: existing certificates are reused, so only NEW names cost against the
    50-per-domain-per-week limit. A private store would re-request every name."""
    from rc_repro import tls

    vols = _doc()["services"]["edge"]["volumes"]
    assert any(v.startswith(f"{tls.acme_dir()}:") for v in vols)


def test_the_challenge_flags_come_from_the_workspace_code_path():
    """One implementation, so the two cannot drift on the thing that decides
    whether issuance works at all."""
    from rc_repro import tls

    door = fd.Edge(domain="support.xyz.com", acme_email="ops@xyz.com")
    cmd = fd.compose_doc(door)["services"]["edge"]["command"]
    assert all(a in cmd for a in tls.acme_args(door.as_tls_spec()))
    assert "--certificatesresolvers.le.acme.email=ops@xyz.com" in cmd


def test_dns_credentials_are_mounted_only_for_the_dns_challenge():
    assert "env_file" not in _doc()["services"]["edge"]
    assert "env_file" in _doc(acme_challenge="dns")["services"]["edge"]


def test_the_edge_declares_no_network_of_its_own():
    """It joins each workspace's OWN network at runtime. Declaring a shared one
    here is precisely what would put every workspace on the same network and let
    them reach each other.

    Checked on the SERVICE as well as the document: removing only the top-level
    block left the service still naming it, and compose then refused the whole
    project -- "service edge refers to undefined network edge". Caught by running
    it, not by the version of this test that looked only at the top level.
    """
    doc = _doc()
    assert "networks" not in doc
    assert "networks" not in doc["services"]["edge"]


def test_the_edge_project_is_valid_compose(tmp_path, monkeypatch):
    """Parse what compose parses. A document that is structurally fine and still
    an invalid PROJECT is exactly the failure the networks key produced."""
    import subprocess

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    fd.write(fd.Edge(domain="support.xyz.com", acme_email="ops@xyz.com"))
    proc = subprocess.run(
        ["docker", "compose", "-f", str(fd.compose_path()), "config", "-q"],
        capture_output=True, text=True, timeout=60)
    if "docker" in (proc.stderr or "") and proc.returncode == 127:
        pytest.skip("docker not available")
    assert proc.returncode == 0, proc.stderr


def test_it_can_reach_the_gui_which_is_a_host_process():
    """The GUI is uvicorn on the host, not a container, so it is not on the edge
    network and has no name there."""
    svc = _doc()["services"]["edge"]
    assert "host.docker.internal:host-gateway" in svc["extra_hosts"]
    assert "host.docker.internal" in fd.gui_route_yml(
        fd.Edge(domain="d", gui_port=7070))


def test_host_ports_are_overridable_but_container_ports_are_not():
    svc = _doc(http_port=8080, https_port=8443)["services"]["edge"]
    assert svc["ports"] == ["8080:80", "8443:443"]
    assert "--entryPoints.websecure.address=:443" in svc["command"]


def test_the_document_is_valid_yaml():
    from rc_repro import compose

    assert yaml.safe_load(compose.to_yaml(_doc()))["services"]["edge"]


# --- routing --------------------------------------------------------------------

def test_a_workspace_backend_is_not_addressed_by_its_service_name():
    """The bug this exists to prevent: every workspace names its service
    `rocketchat`, Compose registers that as a network alias, and on a SHARED
    network Docker round-robins between them -- measured 3/3 across two
    workspaces. Alice's hostname would intermittently serve Bob's Rocket.Chat.
    """
    route = fd.workspace_route_yml("alice-rc8-5-1", "t1.support.xyz.com")
    assert "http://rocketchat:3000" not in route
    assert "http://rcrepro-alice-rc8-5-1-rocketchat-1:3000" in route


def test_two_workspaces_get_distinct_backends():
    a = fd.workspace_route_yml("alice-rc8-5-1", "a.example.com")
    b = fd.workspace_route_yml("bob-rc8-5-1", "b.example.com")
    assert (fd.backend_container("alice-rc8-5-1", "rocketchat")
            != fd.backend_container("bob-rc8-5-1", "rocketchat"))
    assert a != b


def test_the_router_rule_names_the_host():
    """Traefik derives WHAT certificate to request from the Host() rule. A bare
    PathPrefix made it log "no domain found" and silently serve its default
    certificate -- indistinguishable from a failed issuance."""
    route = fd.workspace_route_yml("w", "t1.support.xyz.com")
    assert 'rule: "Host(`t1.support.xyz.com`)"' in route
    assert "certResolver: le" in route


def test_multi_instance_workspaces_list_every_backend():
    route = fd.workspace_route_yml("w", "h", instances=3)
    for i in (1, 2, 3):
        assert f"http://rcrepro-w-rocketchat-{i}-1:3000" in route
    assert "sticky" in route, "DDP websockets must not be bounced mid-session"


def test_routes_are_valid_yaml():
    doc = yaml.safe_load(fd.workspace_route_yml("w", "h.example.com"))
    assert doc["http"]["routers"]["w"]["service"] == "w"


# --- registration on disk -------------------------------------------------------

def test_register_and_deregister_only_touch_their_own_route():
    fd.register("alice", "a.example.com")
    fd.register("bob", "b.example.com")
    assert fd.registered() == ["alice", "bob"]

    fd.deregister("bob")
    assert fd.registered() == ["alice"]
    assert fd.route_path("alice").is_file(), "a neighbour must survive"


def test_deregistering_an_unknown_workspace_is_not_an_error():
    """Teardown must be idempotent: `down` may run after a failed create that
    never registered anything."""
    fd.deregister("never-existed")


def test_registering_twice_replaces_rather_than_duplicates():
    fd.register("alice", "old.example.com")
    fd.register("alice", "new.example.com")
    assert fd.registered() == ["alice"]
    assert "new.example.com" in fd.route_path("alice").read_text()


def test_the_guis_own_route_is_not_listed_as_a_workspace():
    fd.write(fd.Edge(domain="support.xyz.com", acme_email="ops@xyz.com"))
    fd.register("alice", "a.example.com")
    assert fd.registered() == ["alice"]


def test_write_materialises_the_project(_home):
    fd.write(fd.Edge(domain="support.xyz.com", acme_email="ops@xyz.com"))
    assert fd.installed()
    assert fd.dynamic_dir().is_dir()
    assert yaml.safe_load(fd.compose_path().read_text())["services"]["edge"]


def test_the_served_domain_is_readable_by_another_process():
    """`up` runs in a different process from the `serve` that set this up, and the
    port-443 error is far more useful when it can name what holds it."""
    assert fd.served_domain() == ""
    fd.write(fd.Edge(domain="support.xyz.com", acme_email="ops@xyz.com"))
    assert fd.served_domain() == "support.xyz.com"


# --- workspaces stop serving TLS themselves -------------------------------------

def _spec(tls_on: bool):
    """A compose.Spec with and without TLS -- they must produce the same document."""
    from rc_repro import compose, presets, tls

    return compose.Spec(
        project_name="rcrepro-alice-rc8-5-1", rc_image="rc", rc_tag="8.5.1",
        mongo_tag="6.0", mongo_flavor="official", mongo_shell="mongosh",
        oplog=True, root_url="http://localhost:3000", host_port=3000,
        reg_token=None, preset=presets.load("default"),
        tls=(tls.TlsSpec(mode=tls.MODE_ACME, host="t1.support.xyz.com", port=443)
             if tls_on else None))


def test_a_workspaces_compose_is_identical_with_and_without_tls():
    """THE property the whole design rests on. Because this document does not
    change, adding or removing a name is a route file appearing or disappearing --
    no container is ever rebuilt to gain or lose HTTPS, which is what made
    migrating twenty workspaces a recreate-everything operation before."""
    from rc_repro import compose

    assert compose.build(_spec(True)) == compose.build(_spec(False))


def test_a_workspace_runs_no_traefik_and_publishes_no_443():
    from rc_repro import compose, tls

    doc = compose.build(_spec(True))
    assert tls.SERVICE not in doc["services"]
    published = [p for s in doc["services"].values() for p in s.get("ports", [])]
    assert not any(str(p).endswith(":443") for p in published)


def test_a_workspace_keeps_its_own_private_network():
    """No shared network anywhere in the document: that is what stops workspaces
    reaching each other, and the edge joins THEM instead."""
    from rc_repro import compose

    doc = compose.build(_spec(True))
    assert "networks" not in doc
    assert "networks" not in doc["services"]["rocketchat"]


# --- what `up --domain` decides -------------------------------------------------

def _resolve(monkeypatch, *, running: bool = True, https: bool = False,
             emitted: list | None = None):
    from rc_repro.services import lifecycle as lc

    monkeypatch.setattr(fd, "running", lambda: running)
    monkeypatch.setattr(fd, "issue_local_cert", lambda host: None)
    req = (lc.CreateReq(version="8.5.1", https=True) if https else
           lc.CreateReq(version="8.5.1", domain="t1.support.xyz.com",
                        acme_email="ops@xyz.com"))
    emit = (lambda ev: emitted.append(ev)) if emitted is not None else lc.null_emit
    return lc._resolve_tls(req, "alice-rc8-5-1", "127.0.0.1", emit=emit)


def test_a_domain_workspace_claims_no_port_at_all(monkeypatch):
    """The entire point. It terminates no TLS, so there is nothing to conflict
    with -- which is what deletes the port-443 arbitration, the `--adopt` flag and
    the second-workspace refusal along with it."""
    spec = _resolve(monkeypatch)
    assert spec.port == 443, "the URL carries no port; the EDGE holds it"
    assert not hasattr(spec, "http_redirect"), "the edge redirects :80 once, for all"


def test_a_workspace_is_never_published_publicly_for_a_challenge(monkeypatch):
    """TLS-ALPN used to force --domain onto 0.0.0.0 so Let's Encrypt could connect.
    Nothing connects to the workspace now -- the edge answers the challenge -- so
    widening the bind on a repro running fixed weak credentials buys nothing."""
    from rc_repro.services import lifecycle as lc

    req = lc.CreateReq(version="8.5.1", domain="t1.support.xyz.com",
                       acme_email="ops@xyz.com")
    monkeypatch.setattr(fd, "running", lambda: True)
    lc._resolve_tls(req, "w", "127.0.0.1")
    assert req.bind_public is False


def test_local_https_takes_the_same_path_as_a_real_domain(monkeypatch):
    """`--https` used to run its own Traefik on an allocated port, so ten of them
    ate ten ports and each got a different URL. Now every name answers on 443."""
    spec = _resolve(monkeypatch, https=True)
    assert spec.port == 443
    assert spec.host.endswith(".rcrepro.localhost")


def test_an_edge_with_no_domain_still_serves_local_names():
    """`up --https` on a box with no domain and no configuration at all. A
    resolver declared without an email makes Traefik fail at startup, so the
    branch is real rather than tidiness."""
    cmd = fd.compose_doc(fd.Edge())["services"]["edge"]["command"]
    assert not any("certificatesresolvers" in a for a in cmd)
    assert "--entryPoints.websecure.address=:443" in cmd


# --- doctor ---------------------------------------------------------------------

def _doctor(monkeypatch, *, running=True, meta=()):
    """run_checks() with the network probes stubbed out."""
    import requests

    from rc_repro import runner
    from rc_repro.services import doctor, lifecycle as lc

    monkeypatch.setattr(runner, "docker_available", lambda **_k: True)
    monkeypatch.setattr(runner, "docker_server_version", lambda: "29.0")
    monkeypatch.setattr(runner, "compose_version", lambda: "5.3.1")
    monkeypatch.setattr(runner, "docker_kernel_version", lambda: "6.1.0")
    monkeypatch.setattr(runner, "hub_logged_in", lambda: True)
    monkeypatch.setattr(runner, "port_free", lambda p, h="": True)
    monkeypatch.setattr(runner, "pick_port", lambda: 3000)
    monkeypatch.setattr(runner, "list_meta", lambda: list(meta))
    monkeypatch.setattr(lc, "list_repros", lambda: [])
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(
        requests.RequestException()))
    monkeypatch.setattr(fd, "running", lambda: running)
    return doctor.run_checks()["checks"]


def _msgs(checks, status=None):
    return " | ".join(c["message"] for c in checks
                      if status is None or c["status"] == status)


def test_doctor_says_nothing_about_a_edge_that_was_never_set_up(monkeypatch):
    """Most installs have none; rows about it would be noise."""
    assert "ront door" not in _msgs(_doctor(monkeypatch))


def test_doctor_reports_a_running_edge(monkeypatch):
    fd.write(fd.Edge(domain="support.xyz.com", acme_email="ops@xyz.com"))
    fd.register("alice-rc8-5-1", "t1.support.xyz.com")
    checks = _doctor(monkeypatch)
    assert "support.xyz.com" in _msgs(checks, "ok")
    assert "1 workspace route" in _msgs(checks, "ok")


def test_a_stopped_edge_is_a_failure_not_a_warning(monkeypatch):
    """§8, shared fate: while it is down the GUI and every registered workspace
    are unreachable, and nothing else in the report would say so."""
    fd.write(fd.Edge(domain="support.xyz.com", acme_email="ops@xyz.com"))
    fd.register("alice-rc8-5-1", "t1.support.xyz.com")
    failures = _msgs(_doctor(monkeypatch, running=False), "fail")
    assert "NOT running" in failures and "unreachable" in failures


def test_a_route_whose_workspace_is_gone_is_flagged(monkeypatch):
    """It points the edge at nothing, so that hostname 502s instead of
    404ing and the name cannot be reused."""
    fd.write(fd.Edge(domain="d", acme_email="e"))
    fd.register("deleted-by-hand", "t9.support.xyz.com")
    assert "deleted-by-hand" in _msgs(_doctor(monkeypatch), "warn")


def test_the_edge_check_never_breaks_the_report(monkeypatch):
    """doctor's contract: it must run when the environment is broken."""
    fd.write(fd.Edge(domain="d", acme_email="e"))
    monkeypatch.setattr(fd, "registered", lambda: 1 / 0)
    checks = _doctor(monkeypatch)
    assert checks, "the report must still come back"
    assert "could not be determined" in _msgs(checks, "warn")


# --- adopting a pre-front-door workspace ----------------------------------------
# Reported from a real box: one working `--domain` workspace, and the second was
# refused because the first one's own Traefik held 443. Setting up a edge
# then failed on the same port. Nothing could get out of that state.

def _acme_workspace(name: str, host: str, tmp_path):
    import dataclasses
    import json

    from rc_repro import runner

    doc = ("services:\n"
           "  rocketchat:\n    image: rc\n    ports: ['0.0.0.0:3000:3000']\n"
           "  mongodb:\n    image: mongo\n"
           "  traefik:\n    image: traefik\n    ports: ['443:443', '80:80']\n")
    f = {x.name: "" for x in dataclasses.fields(runner.Metadata)}
    f.update(name=name, project=f"rcrepro-{name}", rc_version="8.5.1",
             mongo_tag="8.0", host_port=3000, preset="email",
             root_url="http://localhost:3000", public_url=f"https://{host}",
             extra={"tls": "acme", "tls_ports": [443], "tls_email": "o@e.com"})
    ws = runner.workspace(name)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(json.dumps(dataclasses.asdict(runner.Metadata(**f))))
    (ws / "docker-compose.yml").write_text(doc)


def test_a_pre_edge_workspace_is_found(tmp_path):
    _acme_workspace("lovekesh-rc8-5-1-email", "testrepo.kestron.org", tmp_path)
    assert fd.holders_of_443() == ["lovekesh-rc8-5-1-email"]


def test_adopting_drops_the_workspaces_own_traefik(tmp_path, monkeypatch):
    from rc_repro import runner, tls

    _acme_workspace("w", "t1.kestron.org", tmp_path)
    monkeypatch.setattr(fd, "attach", lambda name: True)
    monkeypatch.setattr(fd, "_docker", lambda *a, **k: type("P", (), {"returncode": 0, "stdout": ""})())

    res = fd.adopt("w")
    assert res == {"name": "w", "host": "t1.kestron.org"}
    doc = runner.read_compose("w")
    assert tls.SERVICE not in doc["services"], "its Traefik must go, or 443 stays held"
    # No network edits: the workspace keeps its own, and the EDGE joined it live.
    assert "networks" not in doc


def test_adopting_registers_the_route_and_drops_the_port_claim(tmp_path, monkeypatch):
    from rc_repro import runner

    _acme_workspace("w", "t1.kestron.org", tmp_path)
    monkeypatch.setattr(fd, "attach", lambda name: True)
    monkeypatch.setattr(fd, "_docker", lambda *a, **k: type("P", (), {"returncode": 0, "stdout": ""})())
    fd.adopt("w")

    assert fd.registered() == ["w"]
    assert "t1.kestron.org" in fd.route_path("w").read_text()
    meta = runner.read_meta("w")
    assert meta.extra["edge"] is True
    # It publishes no 443 now; leaving the claim would make every later workspace
    # allocate around a port this one does not hold.
    assert meta.extra["tls_ports"] == []
    assert fd.holders_of_443() == []


def test_adopting_keeps_the_data_volume(tmp_path, monkeypatch):
    """The whole point of transforming the compose file rather than recreating
    from scratch: `down -v` is never involved."""
    from rc_repro import runner

    _acme_workspace("w", "t1.kestron.org", tmp_path)
    monkeypatch.setattr(fd, "attach", lambda name: True)
    calls = []
    monkeypatch.setattr(fd, "_docker", lambda *a, **k: calls.append(a) or
                        type("P", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(runner, "up", lambda *a, **k: calls.append(("up",)) or 0)
    monkeypatch.setattr(runner, "down", lambda *a, **k: calls.append(("down",)) or 0)
    fd.adopt("w")
    assert ("up",) not in calls, "adopting must not recreate the workspace"
    assert ("down",) not in calls, "nor tear it down"
    assert any("rm" in c for c in calls), "only its traefik container is removed"


def test_a_local_https_workspace_is_not_adoptable(tmp_path, monkeypatch):
    """`--https` uses a local certificate on its own allocated port; it holds no
    443 and has no public name to register."""
    import dataclasses
    import json

    from rc_repro import runner
    from rc_repro.errors import ValidationError

    f = {x.name: "" for x in dataclasses.fields(runner.Metadata)}
    f.update(name="l", project="rcrepro-l", rc_version="8.5.1", mongo_tag="8.0",
             host_port=3000, preset="default", root_url="http://localhost:3000",
             extra={"tls": "local", "tls_ports": [8443]})
    ws = runner.workspace("l")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "repro.json").write_text(json.dumps(dataclasses.asdict(runner.Metadata(**f))))
    (ws / "docker-compose.yml").write_text("services: {}\n")

    assert fd.holders_of_443() == []
    with pytest.raises(ValidationError):
        fd.adopt("l")


# --- the edge is restartable without losing routes ------------------------------

def test_restarting_the_edge_reattaches_every_route(monkeypatch):
    """Attachments are RUNTIME state on the container, so recreating it -- an
    upgrade, a restart, a reboot that pulls a new image -- loses every one while
    the route files survive untouched. The names would then answer 502 rather
    than erroring, which nothing else in the tool would report."""
    fd.write(fd.Edge())
    monkeypatch.setattr(fd, "_docker", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": ""})())
    for n in ("alice-rc8-5-1", "bob-rc8-5-1"):
        fd.register(n, f"{n}.example.com")

    attached = []
    monkeypatch.setattr(fd, "attach", lambda name: attached.append(name) or True)
    monkeypatch.setattr(fd, "_compose", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": ""})())
    fd.up(pull=False)
    assert sorted(attached) == ["alice-rc8-5-1", "bob-rc8-5-1"]


def test_reattach_reports_what_it_could_not_join():
    fd.write(fd.Edge())
    assert fd.reattach_all() == [], "nothing registered, nothing to fail"


def test_a_failed_start_names_what_holds_the_port(monkeypatch):
    """"The edge did not start" sends you looking at the edge, when the cause is
    almost always something ELSE on :443 -- hit twice while building this."""
    monkeypatch.setattr(fd, "_docker", lambda *a, **k: type(
        "P", (), {"returncode": 0,
                  "stdout": "some-other-traefik\t0.0.0.0:443->443/tcp\n"})())
    assert "some-other-traefik" in fd.port_holder(443)


def test_a_bare_edge_writes_no_gui_route(tmp_path, monkeypatch):
    """`up --https` on a box with no public name starts a bare edge. Writing the
    GUI route anyway produced Host(``), which Traefik rejects at load -- "empty
    args for matcher Host" -- plus a certResolver a bare edge never declares. Two
    errors on every start, for a route that cannot work."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    fd.write(fd.Edge())
    assert not fd.route_path("_gui").exists()
    assert fd.registered() == []


def test_giving_the_edge_a_domain_later_adds_the_gui_route(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    fd.write(fd.Edge())
    fd.write(fd.Edge(domain="gui.example.com", acme_email="o@e.com"))
    assert 'Host(`gui.example.com`)' in fd.route_path("_gui").read_text()


def test_resolving_tls_never_starts_a_container(tmp_path, monkeypatch):
    """A resolve function that starts docker made the TEST SUITE stand up a real
    edge holding :443, which then broke a live run on the same box. Docker work
    belongs in the create path."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.services import lifecycle as lc

    def boom(*a, **k):
        raise AssertionError("_resolve_tls started a container")

    monkeypatch.setattr(fd, "ensure_running", boom)
    monkeypatch.setattr(fd, "up", boom)
    req = lc.CreateReq(version="8.5.1", domain="t1.example.com", acme_email="o@e.com")
    lc._resolve_tls(req, "w", "127.0.0.1")


def test_ensure_running_writes_a_bare_config_when_there_is_none(tmp_path, monkeypatch):
    """The lazy start itself: `up --https` on a box with no configuration at all
    has to produce a working edge. (That it is CALLED from the create path, and
    not from _resolve_tls, is pinned by test_resolving_tls_never_starts_a_container
    above; the container-level start is verified live, not here.)"""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setattr(fd, "running", lambda: False)
    monkeypatch.setattr(fd, "up", lambda pull=True: 0)
    assert fd.ensure_running() is True
    assert fd.installed(), "a bare edge config must have been written"
    assert not fd.route_path("_gui").exists(), "and no broken GUI route with it"


# --- adopting is automatic now ---------------------------------------------------

def test_a_legacy_workspace_is_moved_across_without_being_asked(tmp_path, monkeypatch):
    """`--adopt` existed when moving a workspace meant recreating every container:
    asking permission for something expensive is very different from asking to
    remove one proxy container. It is instant and touches no data now, so it
    happens by itself -- and it MUST, because the legacy Traefik holding :443 is
    the reason the edge cannot start at all."""
    _acme_workspace("legacy", "t1.example.com", tmp_path)
    monkeypatch.setattr(fd, "running", lambda: False)
    monkeypatch.setattr(fd, "_docker", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": ""})())
    started = []
    monkeypatch.setattr(fd, "up", lambda pull=True: started.append(True) or 0)

    assert fd.ensure_running() is True
    assert fd.registered() == ["legacy"], "its route must exist after the move"
    assert started == [True]


def test_one_workspace_that_will_not_move_does_not_block_the_others(tmp_path, monkeypatch):
    """Nor the edge: whatever still holds the port is named by port_holder()."""
    _acme_workspace("good", "t1.example.com", tmp_path)
    _acme_workspace("bad", "t2.example.com", tmp_path)
    monkeypatch.setattr(fd, "running", lambda: False)
    monkeypatch.setattr(fd, "_docker", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(fd, "up", lambda pull=True: 0)

    real = fd.adopt

    def flaky(name):
        if name == "bad":
            raise ValidationError("nope")
        return real(name)

    monkeypatch.setattr(fd, "adopt", flaky)
    assert fd.ensure_running() is True
    assert fd.registered() == ["good"]


# --- a workspace keeps its name across down/up -----------------------------------

def test_reusing_a_workspace_restores_its_https_name(tmp_path, monkeypatch):
    """`down` then `up` is the documented way to bring a workspace back with its
    data. It printed "URL https://…" from the record while the name served
    NOTHING: teardown removed the route and the reuse path never resolved TLS, so
    nothing rewrote it. Found by running the round trip, not by any test."""
    from rc_repro import runner
    from rc_repro.services import lifecycle as lc

    _acme_workspace("w", "t1.example.com", tmp_path)
    fd.write(fd.Edge())
    monkeypatch.setattr(fd, "running", lambda: True)
    monkeypatch.setattr(fd, "_docker", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": ""})())
    assert fd.registered() == [], "the teardown removed it"

    lc._restore_route(runner.read_meta("w"))
    assert fd.registered() == ["w"]
    assert "t1.example.com" in fd.route_path("w").read_text()


def test_restoring_a_name_never_fails_the_workspace(tmp_path, monkeypatch):
    """The workspace is up and usable either way; failing a reuse over the
    ingress would be the wrong trade."""
    from rc_repro import runner
    from rc_repro.services import lifecycle as lc

    _acme_workspace("w", "t1.example.com", tmp_path)
    monkeypatch.setattr(fd, "running", lambda: True)
    monkeypatch.setattr(fd, "register", lambda *a, **k: (_ for _ in ()).throw(
        OSError("disk full")))
    lc._restore_route(runner.read_meta("w"))          # must not raise


def test_a_workspace_with_no_name_restores_nothing(tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import lifecycle as lc

    _acme_workspace("w", "t1.example.com", tmp_path)
    meta = runner.read_meta("w")
    meta.public_url = ""
    called = []
    monkeypatch.setattr(fd, "register", lambda *a, **k: called.append(True))
    lc._restore_route(meta)
    assert called == []


def test_a_local_certificate_can_be_issued_before_any_edge_exists(tmp_path, monkeypatch):
    """The first `up --https` on a fresh box: nothing has created the watched
    directory yet, and atomic_write puts its temp file beside the target -- so a
    missing parent was a FileNotFoundError and `up --https` crashed outright.
    Found by running it, in an audit, not by any test."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    assert not fd.dynamic_dir().exists(), "precondition: no edge yet"

    fd.issue_local_cert("w.rcrepro.localhost")
    assert (fd.certs_dir() / "w.rcrepro.localhost.crt").is_file()
    decl = fd.dynamic_dir() / "_cert-w.rcrepro.localhost.yml"
    assert decl.is_file() and "certFile:" in decl.read_text()


def test_the_edge_attaches_after_the_workspace_is_up_not_before(tmp_path, monkeypatch):
    """Attaching needs the network `docker compose up` has only just created.
    Registering during the build wrote a correct route to a backend the edge
    could not resolve, so the name answered 502 -- and a 502 is not an error
    anyone sees, it just looks like a broken workspace."""
    import inspect

    from rc_repro.services import lifecycle as lc

    src = inspect.getsource(lc._create_repro_locked)
    up_at = src.index("rc = _up(repro_name")
    attach_at = src.index("edgesvc.attach(repro_name)")
    assert attach_at > up_at, "attach must come after the workspace is up"


def test_the_edge_mounts_the_local_certificate_directory():
    """issue_local_cert() writes a declaration pointing at /etc/traefik/certs.
    Without the mount that path does not exist inside the container and Traefik
    quietly serves its OWN default certificate -- which looks exactly like a
    working setup until you read the issuer."""
    vols = _doc()["services"]["edge"]["volumes"]
    assert any(v.startswith("./certs:") for v in vols), vols


def test_writing_the_edge_creates_the_certs_directory(tmp_path, monkeypatch):
    """Before compose mounts it: a bind-mount source that does not exist is
    created by Docker as root, and rc-repro then cannot write certificates in."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    fd.write(fd.Edge())
    assert fd.certs_dir().is_dir()


def test_tls_status_survives_a_workspace_with_no_port_of_its_own(tmp_path, monkeypatch):
    """Adoption clears tls_ports, and `[0]` on the empty list was an IndexError --
    a traceback from `tls-status` on exactly the workspaces the edge serves."""
    from rc_repro import cli, runner
    from typer.testing import CliRunner

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    _acme_workspace("w", "t1.example.com", tmp_path)
    meta = runner.read_meta("w")
    meta.extra["tls_ports"] = []
    runner.write("w", "services: {}\n", meta)
    monkeypatch.setattr(runner, "docker_available", lambda **k: True)
    r = CliRunner().invoke(cli.app, ["tls-status", "--name", "w"])
    assert "IndexError" not in (r.output or ""), r.output
    assert not isinstance(r.exception, IndexError)


def test_a_broken_gui_route_from_an_older_version_is_repaired(tmp_path, monkeypatch):
    """An older rc-repro wrote a GUI route even with no domain, producing
    `Host(``)` -- rejected at every reload with "empty args for matcher Host".
    Not writing it any more does nothing for the edges that already have one, and
    nobody would think to look in a generated directory. Found on a real box."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    fd.write(fd.Edge())
    fd.route_path("_gui").write_text('rule: "Host(``)"\n')   # as the old code left it
    monkeypatch.setattr(fd, "running", lambda: True)

    fd.ensure_running()
    assert not fd.route_path("_gui").exists()


def test_a_real_gui_route_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    fd.write(fd.Edge(domain="gui.example.com", acme_email="o@e.com"))
    monkeypatch.setattr(fd, "running", lambda: True)
    fd.ensure_running()
    assert 'Host(`gui.example.com`)' in fd.route_path("_gui").read_text()


def test_another_homes_edge_is_not_mistaken_for_this_one(tmp_path, monkeypatch):
    """Every RC_REPRO_HOME uses the same compose project name, so a container
    started from a DIFFERENT home matched the label filter and running() said
    True while installed() said False. ensure_running() then wrote no
    configuration at all, routes went into a directory nothing watched, and every
    name 404'd while `edge status` reported "no edge yet" beside a running edge.
    """
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    calls = []

    def fake_docker(*args, **kw):
        calls.append(args)
        out = ""
        if args[:1] == ("ps",):
            out = "deadbeef\n"
        elif args[:1] == ("inspect",):
            out = "/somebody/elses/.rc-repro/edge\n"      # a different home
        return type("P", (), {"returncode": 0, "stdout": out})()

    monkeypatch.setattr(fd, "_docker", fake_docker)
    assert fd.running() is False, "a foreign edge is not this home's edge"


def test_this_homes_edge_is_recognised(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def fake_docker(*args, **kw):
        out = ""
        if args[:1] == ("ps",):
            out = "deadbeef\n"
        elif args[:1] == ("inspect",):
            out = str(fd.edge_dir()) + "\n"
        return type("P", (), {"returncode": 0, "stdout": out})()

    monkeypatch.setattr(fd, "_docker", fake_docker)
    assert fd.running() is True


def test_a_foreign_edge_holding_443_is_named_as_such(tmp_path, monkeypatch):
    """Skipping by container NAME alone hid the most confusing case: an edge from
    a different RC_REPRO_HOME has exactly our name, so it was excluded as "us"
    and reported as "something outside Docker"."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def fake_docker(*args, **kw):
        out = ""
        if args[:2] == ("ps", "--format"):
            out = f"{fd.CONTAINER}\t0.0.0.0:443->443/tcp\n"
        elif args[:1] == ("ps",):
            out = "deadbeef\n"
        elif args[:1] == ("inspect",):
            out = "/somebody/elses/.rc-repro/edge\n"
        return type("P", (), {"returncode": 0, "stdout": out})()

    monkeypatch.setattr(fd, "_docker", fake_docker)
    assert "another RC_REPRO_HOME" in fd.port_holder(443)


def test_starting_an_edge_never_replaces_another_homes(tmp_path, monkeypatch):
    """Compose keys a project by NAME and every home uses the same one, so
    `up -d` REPLACED the other home's container rather than failing -- silently
    taking down every https name it served. Observed doing exactly that. It
    matters because the DEFAULT home is ~/.rc-repro: on a shared box, the second
    person's `up --https` steals the first person's ingress."""
    from rc_repro.errors import ConflictError

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    started = []

    def fake_docker(*args, **kw):
        out = ""
        if args[:1] == ("ps",):
            out = "deadbeef\n"
        elif args[:1] == ("inspect",):
            out = "/home/someone-else/.rc-repro/edge\n"
        return type("P", (), {"returncode": 0, "stdout": out})()

    monkeypatch.setattr(fd, "_docker", fake_docker)
    monkeypatch.setattr(fd, "_compose", lambda *a, **k: started.append(a) or type(
        "P", (), {"returncode": 0, "stdout": ""})())

    with pytest.raises(ConflictError) as exc:
        fd.up(pull=False)
    assert "/home/someone-else/.rc-repro/edge" in str(exc.value)
    assert started == [], "it must not have run compose at all"


def test_our_own_edge_is_not_treated_as_foreign(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    def fake_docker(*args, **kw):
        out = ""
        if args[:1] == ("ps",):
            out = "deadbeef\n"
        elif args[:1] == ("inspect",):
            out = str(fd.edge_dir()) + "\n"
        return type("P", (), {"returncode": 0, "stdout": out})()

    monkeypatch.setattr(fd, "_docker", fake_docker)
    assert fd.foreign_edge() == ""
