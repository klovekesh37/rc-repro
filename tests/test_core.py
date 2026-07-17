"""Pure-logic tests (no Docker required).

Run: pip install pytest && pytest    (or: python -m pytest)
These cover version resolution (offline), preset generation, and compose
building — the parts that don't touch Docker or the network.
"""

from __future__ import annotations

import json

from rc_repro import compose, config, presets, rcapi, runner, seed, versions


# --- version resolution (offline / fallback map) ------------------------------


def test_resolve_rc8_official():
    r = versions.resolve("8.4.1", offline=True)
    assert r.mongo_flavor == "official"
    assert r.mongo_shell == "mongosh"
    assert r.oplog is False  # deprecated in 8.x
    assert r.rc_image.endswith("rocketchat/rocket.chat")


def test_resolve_rc7_bitnami_with_oplog():
    r = versions.resolve("7.10.13", offline=True)
    assert r.mongo_flavor == "bitnami-legacy"
    assert r.oplog is True


def test_resolve_rc4_legacy_shell():
    r = versions.resolve("4.8.0", offline=True)
    assert r.mongo_tag == "4.4"
    assert r.mongo_shell == "mongo"  # mongosh only ships in Mongo 5+


def test_resolve_bad_version_raises():
    try:
        versions.resolve("not-a-version", offline=True)
    except ValueError:
        return
    raise AssertionError("expected ValueError for a bad version")


# --- presets ------------------------------------------------------------------


def test_default_preset_loads():
    p = presets.load("default")
    assert p.name == "default"
    assert p.services == {}


def test_ldap_preset_generates_users():
    p = presets.load("ldap", {"users": "3"})
    ldif = dict(p.files)["ldap/50-rc-users.ldif"]
    assert ldif.count("dn: uid=user") == 3
    assert "openldap" in p.services
    assert p.env["OVERWRITE_SETTING_LDAP_Server_Type"] == ""  # generic, not AD


def test_saml_preset_realm_scales_with_users():
    p = presets.load("saml", {"users": "4"})
    realm = json.loads(dict(p.files)["saml/keycloak-realm.json"])
    assert [u["username"] for u in realm["users"]] == ["user1", "user2", "user3", "user4"]
    assert realm["sslRequired"] == "none"
    assert p.post_ready  # fetches the IdP cert at runtime


def test_param_helpers():
    from rc_repro.presets import _common
    assert _common.truthy_param({"x": "YES"}, "x") is True
    assert _common.truthy_param({"x": "0"}, "x") is False
    assert _common.truthy_param({}, "x", default=True) is True
    assert _common.int_param({"n": "7"}, "n", 5) == 7
    assert _common.int_param({"n": ""}, "n", 5) == 5      # empty -> default
    assert _common.int_param({}, "n", 5) == 5
    assert _common.str_param({"b": ""}, "b", "d") == "d"
    assert _common.str_param({"b": "x"}, "b", "d") == "x"


def test_keycloak_shared_scaffolding():
    from rc_repro.presets import _keycloak
    us = _keycloak.users(2)
    assert [u["username"] for u in us] == ["user1", "user2"]
    # saml shape: publish host port -> Keycloak's default 8080
    svc = _keycloak.service("./x/realm.json", 8081)
    assert svc["ports"] == ["8081:8080"]
    assert "KC_HTTP_PORT" not in svc["environment"]
    # oidc shape: same port inside and out (single keycloak:<port> URL)
    svc2 = _keycloak.service("./x/realm.json", 8085, http_port=8085)
    assert svc2["ports"] == ["8085:8085"]
    assert svc2["environment"]["KC_HTTP_PORT"] == "8085"
    realm = json.loads(_keycloak.realm_json([{"clientId": "c"}], 2))
    assert realm["realm"] == "rcrepro" and len(realm["users"]) == 2


def test_unknown_preset_raises():
    try:
        presets.load("does-not-exist")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown preset")


def test_multi_instance_preset_shape():
    p = presets.load("multi-instance", {"instances": "3"})
    assert p.instances == 3
    assert p.entry_service == "traefik"
    assert "nats" in p.services and "traefik" in p.services
    assert p.depends_on == ["nats"]
    # Traefik uses a generated file-provider config listing the 3 backends
    # (matches official rocketchat-compose; no Docker-socket label discovery).
    dynamic = dict(p.files)["traefik/dynamic.yml"]
    assert dynamic.count("- url:") == 3
    assert "http://rocketchat-3:3000" in dynamic
    assert all("docker.sock" not in v for v in p.services["traefik"].get("volumes", []))


def test_email_preset_shape():
    p = presets.load("email")
    assert "mailpit" in p.services
    assert p.depends_on == ["mailpit"]
    assert p.env["OVERWRITE_SETTING_SMTP_Host"] == "mailpit"
    assert p.env["OVERWRITE_SETTING_SMTP_Port"] == "1025"
    # Email-2FA is enabled globally (codes land in Mailpit). No forced opt-in:
    # it only gates users with verified emails (seeded users are; admin isn't
    # until verified manually), so plain admin login keeps working.
    assert p.env["OVERWRITE_SETTING_Accounts_TwoFactorAuthentication_By_Email_Enabled"] == "true"
    assert p.post_ready == []
    # rcapi.login needs Mailpit's URL to fetch codes for rc-repro's own calls
    # whenever a login is 2FA-gated.
    assert p.extra["mailpit_url"] == "http://localhost:8025"
    # verification is opt-in
    assert "OVERWRITE_SETTING_Accounts_EmailVerification" not in p.env
    assert (
        presets.load("email", {"verification": "true"})
        .env["OVERWRITE_SETTING_Accounts_EmailVerification"] == "true"
    )


def test_email_otp_extraction():
    assert rcapi._extract_otp("Your login code is 428913, valid 5 min.") == "428913"
    assert rcapi._extract_otp("<b>042891</b>") == "042891"
    assert rcapi._extract_otp("order #12345678 shipped") is None   # not 6 digits
    assert rcapi._extract_otp("") is None


def test_email_otp_recipient_filter():
    # Mailpit is a catch-all inbox — the fetcher must only match the right user.
    msg = {"To": [{"Address": "Alice@Example.com"}]}
    assert rcapi._addressed_to(msg, "alice@example.com")       # case-insensitive
    assert not rcapi._addressed_to(msg, "admin@example.com")   # other user's mail
    assert rcapi._addressed_to(msg, None)                      # no filter -> any


def test_s3_minio_preset_shape():
    p = presets.load("s3_minio")
    assert "minio" in p.services and "minio-init" in p.services
    assert p.env["OVERWRITE_SETTING_FileUpload_Storage_Type"] == "AmazonS3"
    assert p.env["OVERWRITE_SETTING_FileUpload_S3_ForcePathStyle"] == "true"
    # Default mode proxies downloads through RC — zero-setup, no hosts entry.
    assert p.env["OVERWRITE_SETTING_FileUpload_S3_Proxy_Uploads"] == "true"
    assert p.env["OVERWRITE_SETTING_FileUpload_S3_Proxy_Avatars"] == "true"
    # The object store persists via a named volume (Preset.volumes).
    assert p.volumes == {"minio_data": {"driver": "local"}}
    assert p.depends_on == ["minio"]


def test_s3_minio_presigned_mode_and_bucket():
    p = presets.load("s3_minio", {"presigned": "true", "bucket": "tickets"})
    assert p.env["OVERWRITE_SETTING_FileUpload_S3_Proxy_Uploads"] == "false"
    assert p.env["OVERWRITE_SETTING_FileUpload_S3_Bucket"] == "tickets"
    assert p.env["OVERWRITE_SETTING_FileUpload_S3_BucketURL"].endswith("/tickets")
    assert any("/etc/hosts" in n for n in p.notes)   # browser needs the hosts line
    # bucket-init creates the custom bucket
    assert "local/tickets" in p.services["minio-init"]["entrypoint"][-1]


def test_compose_merges_preset_volumes():
    # The Preset.volumes framework change: preset volumes land in the top-level
    # volumes block (else compose rejects the file), base volume untouched.
    doc = compose.build(_spec("8.4.1", "s3_minio"))
    assert "minio_data" in doc["volumes"]
    assert "mongodb_data" in doc["volumes"]
    assert "minio_data:/data" in doc["services"]["minio"]["volumes"]


def test_livechat_preset_shape():
    p = presets.load("livechat")
    assert "widget-site" in p.services
    assert p.env["OVERWRITE_SETTING_Livechat_enabled"] == "true"
    assert p.env["OVERWRITE_SETTING_API_Enable_CORS"] == "true"   # cross-origin widget
    # the widget iframes RC; X-Frame-Options: sameorigin would block it cross-origin
    assert p.env["OVERWRITE_SETTING_Iframe_Restrict_Access"] == "false"
    assert p.ports == [8090]
    # widget page uses the {{ROOT_URL}} placeholder (substituted at write time)
    assert "{{ROOT_URL}}/livechat" in dict(p.files)["livechat/index.html"]
    # agent + department are set up once RC is serving
    assert p.post_ready[0]["action"] == "livechat_setup"
    # department is created by default (assign agents to it), opt-out via --set
    assert p.post_ready[0]["department"] == "support"
    assert presets.load("livechat", {"department": "false"}).post_ready[0]["department"] == ""


def test_unknown_set_param_rejected():
    # `--set agent=5` (typo for `agents`) was silently ignored before.
    from rc_repro import cli
    p = presets.load("livechat")
    assert cli._unknown_params({"agent": "5"}, p) == ["agent"]      # typo caught
    assert cli._unknown_params({"agents": "5"}, p) == []            # correct key accepted
    assert cli._unknown_params({"x": "1"}, presets.load("default")) == ["x"]  # no-param preset


def test_root_url_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro import runner
    meta = runner.Metadata(
        name="lc", project="rcrepro-lc", rc_version="8.5.1", rc_image="i",
        mongo_tag="8.0", mongo_flavor="official", preset="livechat",
        root_url="http://localhost:4321", host_port=4321, version_source="map",
    )
    runner.write("lc", "services: {}\n", meta,
                 files=[("livechat/index.html", "src={{ROOT_URL}}/livechat")])
    written = (runner.workspace("lc") / "livechat/index.html").read_text()
    assert written == "src=http://localhost:4321/livechat"   # placeholder resolved


def test_multi_instance_clamps_instance_count():
    assert presets.load("multi-instance", {"instances": "1"}).instances == 2   # min 2
    assert presets.load("multi-instance", {"instances": "99"}).instances == 5  # max 5


# --- compose building ---------------------------------------------------------


def _spec(version: str, preset_name: str = "default", params: dict | None = None,
          monitoring_flag: bool = False):
    r = versions.resolve(version, offline=True)
    pre = presets.load(preset_name, params or {})
    return compose.Spec.from_resolved(
        r, project_name="rcrepro-t", root_url="http://localhost:3000",
        host_port=3000, reg_token=None, preset=pre, monitoring=monitoring_flag,
    )


def test_compose_official_has_community_server_and_init():
    doc = compose.build(_spec("8.4.1"))           # RC 8 -> Mongo 8 -> official flavor
    assert "mongo-init" in doc["services"]
    assert "mongodb-fix-permission" in doc["services"]   # community-server runs as uid 1001
    assert "mongodb-community-server" in doc["services"]["mongodb"]["image"]
    assert doc["services"]["mongodb"]["user"] == "1001"
    assert "MONGO_OPLOG_URL" not in doc["services"]["rocketchat"]["environment"]


def test_compose_bitnami_no_init_and_oplog():
    doc = compose.build(_spec("7.10.13"))
    assert "mongo-init" not in doc["services"]        # bitnami auto-inits
    assert "bitnamilegacy/mongodb" in doc["services"]["mongodb"]["image"]
    assert doc["services"]["mongodb"]["platform"] == "linux/amd64"
    assert "MONGO_OPLOG_URL" in doc["services"]["rocketchat"]["environment"]


def test_compose_yaml_is_valid():
    import yaml
    doc = compose.build(_spec("8.4.1"))
    text = compose.to_yaml(doc)
    parsed = yaml.safe_load(text)
    assert parsed["name"] == "rcrepro-t"


def test_compose_multi_instance_clones_and_meshes():
    doc = compose.build(_spec("8.4.1", "multi-instance", {"instances": "3"}))
    svcs = doc["services"]
    # three cloned RC instances, no single "rocketchat"
    assert {"rocketchat-1", "rocketchat-2", "rocketchat-3"} <= set(svcs)
    assert "rocketchat" not in svcs
    inst = svcs["rocketchat-2"]
    assert inst["environment"]["TRANSPORTER"] == "monolith+nats://nats:4222"
    assert "INSTANCE_IP" not in inst["environment"]                # NATS transporter, not DDP mesh
    assert inst["ports"] == ["127.0.0.1:3002:3000"]                # direct access on host_port+2, loopback-bound
    assert "nats" in inst["depends_on"]                            # preset depends_on applied
    # cold-start serialisation: 2..N wait for instance-1 to be healthy first
    assert inst["depends_on"]["rocketchat-1"]["condition"] == "service_healthy"
    assert "healthcheck" in svcs["rocketchat-1"]
    # NATS + Traefik present; Traefik got the published host port
    assert "nats" in svcs
    assert svcs["traefik"]["ports"] == ["127.0.0.1:3000:80"]


def test_compose_single_instance_unchanged_by_new_fields():
    # default preset (instances=1) must still produce exactly one rocketchat.
    doc = compose.build(_spec("8.4.1"))
    assert "rocketchat" in doc["services"]
    assert "rocketchat-1" not in doc["services"]
    assert doc["services"]["rocketchat"]["ports"] == ["127.0.0.1:3000:3000"]


def test_compose_binds_loopback_everywhere():
    # Hardening: every published port (RC + all sidecars) binds to 127.0.0.1
    # by default (official rocketchat-compose BIND_IP pattern).
    doc = compose.build(_spec("8.4.1", "s3_minio"))
    published = [p for svc in doc["services"].values() for p in svc.get("ports", [])]
    assert published, "expected published ports"
    assert all(p.startswith("127.0.0.1:") for p in published), published


def test_compose_bind_override():
    spec = _spec("8.4.1")
    spec.bind_host = "0.0.0.0"   # up --bind 0.0.0.0 (deliberate LAN sharing)
    doc = compose.build(spec)
    assert doc["services"]["rocketchat"]["ports"] == ["0.0.0.0:3000:3000"]


def test_s3_bucket_name_validated():
    try:
        presets.load("s3_minio", {"bucket": "Bad Name!"})
    except ValueError as exc:
        assert "bucket" in str(exc)
        return
    raise AssertionError("expected ValueError for an invalid bucket name")


def test_int_param_bad_value_is_actionable():
    from rc_repro.presets import _common
    try:
        _common.int_param({"users": "many"}, "users", 5)
    except ValueError as exc:
        assert "--set users=" in str(exc)
        return
    raise AssertionError("expected ValueError for a non-numeric --set value")


def test_port_free_detects_loopback_listener():
    # Regression: repros bind 127.0.0.1:<port>, and a wildcard-bind probe with
    # SO_REUSEADDR can miss a loopback listener on macOS -> auto-pick collides.
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert runner.port_free(port) is False   # something IS listening on loopback
    finally:
        srv.close()


def test_pick_port_bounded(monkeypatch):
    # Hosts where nothing can bind (sandboxes) must get a clean error, not an
    # OverflowError from scanning past 65535.
    monkeypatch.setattr(runner, "port_free", lambda p: False)
    monkeypatch.setattr(runner, "used_ports", set)
    try:
        runner.pick_port()
    except RuntimeError as exc:
        assert "no free host port" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    try:
        runner.pick_port_range(3)
    except RuntimeError as exc:
        assert "consecutive" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_seed_profile_strict():
    try:
        seed.plan_from("larg")   # typo must not silently seed `small`
    except ValueError as exc:
        assert "unknown seed profile" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_multi_instance_bad_count_is_actionable():
    try:
        presets.load("multi-instance", {"instances": "many"})
    except ValueError as exc:
        assert "--set instances=" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_yaml_preset_notes_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    pdir = tmp_path / "presets"
    pdir.mkdir(parents=True)
    (pdir / "team.yaml").write_text(
        "name: team\nnotes: [tip one, tip two]\nparams_help: {x: does x}\n"
    )
    p = presets.load("team")
    assert p.notes == ["tip one", "tip two"]
    assert p.params_help == {"x": "does x"}


def test_sanitize_can_produce_empty_name():
    # cli.up guards this: an all-symbols --name would otherwise write into the
    # repros root itself.
    from rc_repro.cli import _sanitize
    assert _sanitize("!!!") == ""


# --- config / runner (12-factor items) -----------------------------------------


def test_preset_ports_match_registry():
    # Every preset with side services declares exactly its registry ports, so
    # allocation/preflight can see them.
    for name, expected in config.PRESET_PORTS.items():
        p = presets.load(name)
        assert p.ports == list(expected), f"{name} declares {p.ports}, registry says {expected}"
    assert presets.load("default").ports == []


def test_used_ports_includes_sidecars_and_monitoring(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    meta = runner.Metadata(
        name="x", project="rcrepro-x", rc_version="8.5.1", rc_image="i",
        mongo_tag="8.0", mongo_flavor="official", preset="saml",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
        extra={"sidecar_ports": [8081], "monitoring_ports": [9090, 5050]},
    )
    runner.write("x", "services: {}\n", meta)
    # RC port, sidecar port, AND monitoring ports are all claimed so a new
    # repro's auto-picked port can't collide with any of them.
    assert {3000, 8081, 9090, 5050} <= runner.used_ports()


def test_config_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setenv("RC_REPRO_REG_TOKEN", "tok-from-env")
    cfg = config.load_config()
    assert cfg["reg_token"] == "tok-from-env"
    # env wins over the file
    config.save_config({"reg_token": "tok-from-file"})
    assert config.load_config()["reg_token"] == "tok-from-env"


def test_env_values_never_persisted_to_config_file(tmp_path, monkeypatch):
    # Regression: read-modify-write flows (up --pin / use) must not bake
    # ephemeral env values (secrets!) into config.yaml.
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setenv("RC_REPRO_REG_TOKEN", "SECRET")
    raw = config.load_config(with_env=False)   # what save paths must use
    assert "reg_token" not in raw
    raw["default_repro"] = "x"
    config.save_config(raw)
    assert "SECRET" not in config.config_file().read_text()
    # ...while readers still see the env value
    assert config.load_config()["reg_token"] == "SECRET"


def test_version_single_source():
    import rc_repro
    # resolved from package metadata (pyproject), never a hardcoded literal
    assert rc_repro.__version__ and rc_repro.__version__ != "0.0.0-dev"


# --- monitoring (--monitor add-on) --------------------------------------------


def test_monitoring_added_to_any_preset():
    from rc_repro import monitoring
    # attaches to a plain repro: prometheus + grafana + exporters, RC metrics on,
    # loopback-bound ports, its own volumes.
    doc = compose.build(_spec("8.4.1", monitoring_flag=True))
    svcs = doc["services"]
    assert {"prometheus", "grafana", "node-exporter", "mongodb-exporter"} <= set(svcs)
    assert doc["services"]["rocketchat"]["environment"][monitoring.RC_METRICS_ENV] == "true"
    assert svcs["prometheus"]["ports"] == ["127.0.0.1:9090:9090"]
    assert svcs["grafana"]["ports"] == ["127.0.0.1:5050:3000"]
    # exporters are internal (scraped by Prometheus), not published to the host
    assert "ports" not in svcs["node-exporter"]
    assert "ports" not in svcs["mongodb-exporter"]
    assert {"prometheus_tsdb", "grafana_data"} <= set(doc["volumes"])


def test_monitoring_scrapes_all_multi_instances():
    # the whole point of the flag: Prometheus targets follow the RC topology.
    assert compose.rc_service_names(1) == ["rocketchat"]
    assert compose.rc_service_names(3) == ["rocketchat-1", "rocketchat-2", "rocketchat-3"]
    from rc_repro import monitoring
    sd = dict(monitoring.files(["rocketchat-1", "rocketchat-2"]))["monitoring/file_sd_configs/rocketchat.yml"]
    assert "rocketchat-1:9458" in sd and "rocketchat-2:9458" in sd


def test_monitoring_ships_full_dashboards_and_exporter_targets():
    from rc_repro import monitoring
    files = dict(monitoring.files(["rocketchat"]))
    dash = json.loads(files["monitoring/grafana/dashboards/rocketchat-metrics.json"])
    assert dash["title"] == "Rocket.Chat Metrics" and len(dash["panels"]) > 30
    # exporter dashboards shipped too
    assert "monitoring/grafana/dashboards/node-exporter-full.json" in files
    assert "monitoring/grafana/dashboards/mongodb-exporter.json" in files
    # exporter scrape targets present
    assert "mongodb-exporter:9216" in files["monitoring/file_sd_configs/mongo.yml"]
    assert "node-exporter:9100" in files["monitoring/file_sd_configs/node-exporter.yml"]


def test_no_monitoring_by_default():
    doc = compose.build(_spec("8.4.1"))
    assert "prometheus" not in doc["services"] and "grafana" not in doc["services"]


def test_monitoring_bind_ports_handles_portless_exporters():
    # Regression: the attach path binds ports over ALL monitoring services;
    # node-exporter/mongodb-exporter have no 'ports' key -> must not KeyError.
    from rc_repro import monitoring
    bound = monitoring.bind_ports(monitoring.services(), "127.0.0.1")
    assert bound["prometheus"]["ports"] == ["127.0.0.1:9090:9090"]
    assert bound["grafana"]["ports"] == ["127.0.0.1:5050:3000"]
    assert "ports" not in bound["node-exporter"]
    assert "ports" not in bound["mongodb-exporter"]


# --- perf (Phase 1: timing + resource sampling) -------------------------------


def test_timings_percentiles_and_histogram():
    from rc_repro.perf import Timings
    t = Timings()
    for v in range(1, 101):        # 1..100 ms
        t.add(float(v))
    s = t.summary()
    assert s["count"] == 100
    assert s["p50"] == 50 and s["p95"] == 95 and s["p99"] == 99
    assert s["min"] == 1 and s["max"] == 100
    assert t.rate_per_s(2.0) == 50.0
    h = t.histogram()
    assert h and h.isascii()       # ASCII sparkline (no ambiguous-width glyphs)


def test_timings_empty_is_safe():
    from rc_repro.perf import Timings
    t = Timings()
    assert t.summary() == {"count": 0}
    assert t.pct(95) == 0.0 and t.histogram() == ""


def test_fmt_ms():
    from rc_repro.perf.timings import fmt_ms
    assert fmt_ms(42) == "42ms" and fmt_ms(1500) == "1.50s"


def test_resources_parsers():
    from rc_repro.perf import resources as R
    assert R._parse_cpu("78.34%") == 78.34
    used, limit = R._parse_mem("540MiB / 2GiB")
    assert round(used) == 540 * 1024**2 and round(limit) == 2 * 1024**3


def test_resource_report_windows_and_peaks():
    from rc_repro.perf.resources import ResourceMonitor
    mon = ResourceMonitor("x")
    # inject a synthetic series: (t, cpu, mem_used, mem_limit)
    mon._series = {"rc": [(0.0, 4, 100, 2000), (1.0, 80, 500, 2000), (2.0, 30, 300, 2000)]}
    rep = mon.report()["rc"]
    assert rep.idle_cpu == 4 and rep.peak_cpu == 80 and rep.peak_cpu_t == 1.0
    assert rep.peak_mem == 500
    # windowed to just the last sample
    assert mon.report(window=(1.5, 2.5))["rc"].peak_cpu == 30


def test_seed_returns_durations_and_latency(monkeypatch):
    # drive the seed body with a mock poster; no server needed.
    from unittest.mock import MagicMock
    from rc_repro import seed, rcapi
    resp = MagicMock(ok=True); resp.json.return_value = {"message": {"_id": "m"}}
    post = MagicMock(return_value=resp)
    plan = seed.plan_from("small", users=2, channels=1, messages=3)
    monkeypatch.setattr(rcapi, "login", lambda *a, **k: (_ for _ in ()).throw(Exception("no server")))
    out = seed._seed_body("http://x", {"h": "1"}, plan, post, lambda m: None)
    assert set(out["durations"]) == {"users", "channels", "messages", "dms"}
    assert out["latency"]["count"] >= 1   # message latencies collected


# --- benchmark (version comparison) -------------------------------------------


def _bench_row(version, seed_s, p95, ok=True):
    return {
        "version": version, "ok": ok, "mongo": "8.0 (official)",
        "image": f"registry.rocket.chat/rocketchat/rocket.chat:{version}",
        "boot_s": 10.0, "seed_total_s": seed_s,
        "users": 20, "user_rate": 6.5, "messages": 100, "msg_rate": 100 / seed_s,
        "msg_p95_ms": p95, "msg_p99_ms": p95 * 2, "rc_cpu": 80, "mongo_cpu": 40, "rc_mem_mb": 1400,
        "seed": {"users": 20, "channels": 8, "messages": 100, "dms": 5,
                 "durations": {"users": 3.0, "channels": 0.4, "messages": seed_s, "dms": 0.5},
                 "latency": {"count": 100, "mean": p95 / 2, "min": 10, "max": p95 * 2,
                             "p50": p95 / 2, "p90": p95 * 0.9, "p95": p95, "p99": p95 * 2}},
        "resources": {"rocketchat": {"idle_cpu": 5, "peak_cpu": 80, "idle_mem": 1e9,
                                     "peak_mem": 1.4e9, "limit_mem": 2e9},
                      "mongodb": {"idle_cpu": 2, "peak_cpu": 40, "idle_mem": 3e8,
                                  "peak_mem": 4e8, "limit_mem": 2e9}},
    }


def test_benchmark_flags_regression():
    from rc_repro.perf import report
    a = _bench_row("8.5.1", 5.0, 100)
    b = _bench_row("8.6.0", 9.0, 340)     # +80% seed, +240% p95 vs a
    assert report.regression_flag(b, a, 25.0)          # flagged
    assert report.regression_flag(a, None, 25.0) == "" # first version: no baseline
    steady = _bench_row("8.6.1", 5.2, 105)
    assert report.regression_flag(steady, a, 25.0) == ""  # within threshold


def test_benchmark_table_and_markdown_render():
    from rc_repro.perf import report
    results = [_bench_row("8.5.1", 5.0, 100), _bench_row("8.6.0", 9.0, 340),
               {"version": "9.9.9", "ok": False, "error": "no such version"}]
    headers, rows, flags = report.table_rows(results, 25.0)
    assert len(rows) == 3 and "regression" in flags[1]
    host = {"os": "test", "cpu": 8, "docker": "27.0", "compose": "2.30"}
    md = report.benchmark_markdown(results, "standard", 25.0, host)
    # summary + workload explanation + per-version detail all present
    assert "rc-repro benchmark report" in md and "8.6.0" in md and "FAILED" in md
    assert "What the workload did" in md and "Per-version detail" in md
    assert "Message latency" in md and "Resource peaks during seed" in md


# --- perf (Phase 2: load test + SLO gate) -------------------------------------


def test_slo_parse_units_and_ops():
    from rc_repro.perf import slo
    rules = slo.parse("p95=300ms,error=1%,rps=100,avg=1.5s")
    by = {r[0]: r for r in rules}
    assert by["p95"] == ("p95", "<=", 300.0, "300ms")
    assert by["avg"][2] == 1500.0                 # 1.5s -> ms
    assert by["error"] == ("error", "<=", 1.0, "1%")
    assert by["rps"] == ("rps", ">=", 100.0, "100")


def test_slo_rejects_unknown_metric():
    from rc_repro.perf import slo
    for bad in ("throughput=100", "p95"):         # unknown metric / missing '='
        try:
            slo.parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_slo_evaluate_pass_and_fail():
    from rc_repro.perf import slo
    summary = {"p95": 250.0, "error_rate": 0.005, "rps": 120.0}
    rules = slo.parse("p95=300ms,error=1%,rps=100")
    res = {r["key"]: r for r in slo.evaluate(rules, summary)}
    assert res["p95"]["ok"] and res["error"]["ok"] and res["rps"]["ok"]
    # now breach each bound
    bad = {"p95": 400.0, "error_rate": 0.02, "rps": 50.0}
    res2 = {r["key"]: r for r in slo.evaluate(rules, summary=bad)}
    assert not res2["p95"]["ok"] and not res2["error"]["ok"] and not res2["rps"]["ok"]


def test_slo_absent_metric_fails_not_measured():
    # A metric missing from the summary must FAIL (not silently PASS at 0.0).
    from rc_repro.perf import slo
    res = slo.evaluate(slo.parse("p99=300ms"), summary={"p95": 100.0})[0]
    assert res["measured"] is False and res["ok"] is False


def test_loadtest_target_detection():
    from rc_repro import cli
    assert cli._loadtest_target({"services": {"rocketchat": {}, "mongodb": {}}}) == "http://rocketchat:3000"
    assert cli._loadtest_target({"services": {"traefik": {}, "rocketchat-1": {}}}) == "http://traefik:80"
    assert cli._loadtest_target({"services": {"rocketchat-1": {}, "rocketchat-2": {}}}) == "http://rocketchat-1:3000"


def test_loadtest_markdown_renders():
    from rc_repro.perf import report
    ctx = {"name": "acme", "version": "8.5.1", "scenario": "messages", "vus": 50,
           "duration": "60s", "ramp": "", "target": "http://rocketchat:3000"}
    summary = {"count": 3000, "rps": 50.0, "p50": 40, "p90": 90, "p95": 120,
               "p99": 200, "avg": 55, "min": 10, "max": 400,
               "error_rate": 0.004, "checks_rate": 0.996}
    from rc_repro.perf import slo
    slo_res = slo.evaluate(slo.parse("p95=300ms,error=1%"), summary)
    host = {"os": "test", "cpu": 8, "docker": "27.0", "compose": "2.30"}
    md = report.loadtest_markdown(ctx, summary, slo_res, None, host)
    assert "rc-repro load-test report" in md and "messages" in md
    assert "SLO gate" in md and "throughput" in md and "50.0 req/s" in md


def test_loadtest_scenarios_and_scripts_present():
    from importlib import resources
    from rc_repro.perf import k6
    d = resources.files("rc_repro").joinpath("data", "loadtest")
    assert "custom" in k6.SCENARIOS
    for name in k6.SCENARIOS:
        assert d.joinpath(f"{name}.js").is_file()
    assert d.joinpath("common.js").is_file()


def test_parse_endpoint():
    from rc_repro import cli
    assert cli._parse_endpoint("GET /api/v1/channels.list") == ("GET", "/api/v1/channels.list")
    assert cli._parse_endpoint("post /api/v1/chat.postMessage") == ("POST", "/api/v1/chat.postMessage")
    assert cli._parse_endpoint("/api/v1/me") == ("GET", "/api/v1/me")   # bare path defaults to GET
    assert cli._parse_endpoint("GET /api/v1/x?count=100&a=b")[1] == "/api/v1/x?count=100&a=b"
    for bad in ("", "GET channels.list", "  "):   # empty / non-absolute path
        try:
            cli._parse_endpoint(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_parse_endpoint_unsupported_method():
    from rc_repro import cli
    assert cli._parse_endpoint("PATCH /api/v1/x") == ("PATCH", "/api/v1/x")
    try:
        cli._parse_endpoint("HEAD /api/v1/me")
    except ValueError as exc:
        assert "unsupported method" in str(exc)
    else:
        raise AssertionError("expected ValueError for HEAD")


def test_parse_ramp():
    from rc_repro import cli
    assert cli._parse_ramp("10:200") == (10, 200)
    for bad in ("10", "a:b", "5:0", "1:2:3"):
        try:
            cli._parse_ramp(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for ramp {bad!r}")


def test_short_res_map_keeps_multi_instance_index():
    # Multi-instance rocketchat-1/-2 must not collapse to one key (report data loss).
    from rc_repro import cli
    res = {"rcrepro-x-rocketchat-1": 1, "rcrepro-x-rocketchat-2": 2, "rcrepro-x-mongodb-1": 3}
    assert sorted(cli._short_res_map(res, "x")) == ["mongodb", "rocketchat-1", "rocketchat-2"]
    # Single instance still collapses to the clean base name.
    single = {"rcrepro-y-rocketchat-1": 1, "rcrepro-y-mongodb-1": 2}
    assert sorted(cli._short_res_map(single, "y")) == ["mongodb", "rocketchat"]


def test_bind_ports_no_double_prefix_on_ip_qualified():
    from rc_repro import compose
    doc = {"services": {
        "a": {"ports": ["8025:8025"]},            # bare -> prefixed
        "b": {"ports": ["127.0.0.1:9000:9000"]},  # already IP -> untouched
    }}
    compose._bind_ports(doc, "127.0.0.1")
    assert doc["services"]["a"]["ports"] == ["127.0.0.1:8025:8025"]
    assert doc["services"]["b"]["ports"] == ["127.0.0.1:9000:9000"]


def test_status_breakdown_non_zero_only():
    from rc_repro import cli
    from rc_repro.perf import report
    summary = {"status": {"2xx": 1158, "429": 61, "4xx": 0, "5xx": 41, "other": 0}}
    assert cli._status_breakdown(summary) == "2xx 1158 · 429 61 · 5xx 41"
    assert report._status_breakdown(summary) == "2xx 1158 · 429 61 · 5xx 41"
    assert cli._status_breakdown({}) == ""            # no status -> empty


# --- seed ---------------------------------------------------------------------


def test_seed_profile_and_overrides():
    p = seed.plan_from("standard")
    assert (p.users, p.channels, p.messages, p.rich) == (20, 8, 20, True)
    p2 = seed.plan_from("standard", users=3, messages=1)
    assert p2.users == 3 and p2.messages == 1 and p2.channels == 8  # override + inherit


def test_seed_usernames_avoid_userN_collision():
    # ldap/saml presets use user1..userN; seed users must never collide.
    names = [seed.username(i) for i in range(60)]
    assert all(not n.startswith("user") for n in names)
    assert len(set(names)) == len(names)          # unique
    assert names[0] == "alice"


def test_seed_channel_names_unique():
    names = [seed.channel_name(i) for i in range(30)]
    assert len(set(names)) == len(names)
