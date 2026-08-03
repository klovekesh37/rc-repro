"""Pure-logic tests (no Docker required).

Run: pip install pytest && pytest    (or: python -m pytest)
These cover version resolution (offline), preset generation, and compose
building — the parts that don't touch Docker or the network.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from rc_repro import compose, config, configimport, presets, rcapi, runner, scaleseed, seed, versions


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


def test_ldap_scenario_resolves_one_intent_for_both_deployments():
    compose = presets.resolve("ldap", "compose", {"users": "3", "domain": "example.org"})
    kubernetes = presets.resolve("ldap", "kubernetes", {"users": "3", "domain": "example.org"})

    assert compose.scenario == kubernetes.scenario == "ldap"
    assert compose.scenario_params == kubernetes.scenario_params == {
        "users": 3,
        "domain": "example.org",
        "base_dn": "dc=example,dc=org",
    }
    assert compose.topology == "compose"
    assert kubernetes.topology == "kubernetes"
    assert compose.env["OVERWRITE_SETTING_LDAP_Host"] == "openldap"
    env = kubernetes.env
    assert env["OVERWRITE_SETTING_LDAP_Host"] == "openldap"
    assert any("kind: Service" in manifest and "name: openldap" in manifest
               for manifest in kubernetes.kubernetes_manifests)


def test_ldap_scenario_reuses_the_legacy_compose_builder():
    legacy = presets.load("ldap", {"users": "3", "domain": "example.org"})
    resolved = presets.resolve("ldap", "compose", {"users": "3", "domain": "example.org"})

    assert resolved.scenario == "ldap"
    assert resolved.services == legacy.services
    assert resolved.files == legacy.files
    assert resolved.env == legacy.env
    assert resolved.params_help == legacy.params_help


def test_scenario_refuses_an_unsupported_deployment_type():
    with pytest.raises(ValueError, match="does not support deployment type 'nomad'"):
        presets.resolve("ldap", "nomad")


def test_user_yaml_still_shadows_a_built_in_scenario():
    config.preset_dir().mkdir(parents=True)
    (config.preset_dir() / "ldap.yaml").write_text(
        "name: ldap\ndescription: local override\nenv: {CUSTOM: value}\n",
        encoding="utf-8",
    )

    resolved = presets.resolve("ldap", params={"users": "3"})

    assert resolved.description == "local override"
    assert resolved.env == {"CUSTOM": "value"}
    assert resolved.scenario == ""


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
    from rc_repro.services import lifecycle as lc
    p = presets.load("livechat")
    assert lc._unknown_params({"agent": "5"}, p) == ["agent"]      # typo caught
    assert lc._unknown_params({"agents": "5"}, p) == []            # correct key accepted
    assert lc._unknown_params({"x": "1"}, presets.load("default")) == ["x"]  # no-param preset


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


# --- config import (support-dump settings.json) -------------------------------


def _dump(tmp_path, items):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(items))
    return p


def test_config_import_keeps_only_customized(tmp_path):
    plan = configimport.build_plan(_dump(tmp_path, [
        {"_id": "A", "value": True, "packageValue": True},     # unchanged -> skip
        {"_id": "B", "value": "x", "packageValue": "y"},       # changed -> apply
    ]))
    assert plan.apply == [("B", "x")]


def test_config_import_skips_redacted(tmp_path):
    plan = configimport.build_plan(_dump(tmp_path, [
        {"_id": "SMTP_Password", "value": "XXXXXXXX", "packageValue": ""},
        {"_id": "Some_Secret", "value": "********", "packageValue": ""},
        {"_id": "Real", "value": "keep", "packageValue": ""},
    ]))
    assert [s for s, _ in plan.apply] == ["Real"]
    assert set(plan.redacted) == {"SMTP_Password", "Some_Secret"}


def test_config_import_denies_identity_settings(tmp_path):
    plan = configimport.build_plan(_dump(tmp_path, [
        {"_id": "Site_Url", "value": "https://cust", "packageValue": ""},
        {"_id": "Enterprise_License", "value": "RCV3_x", "packageValue": ""},
        {"_id": "Assets_logo", "value": {"url": "a"}, "packageValue": {}},
        {"_id": "Message_MaxAllowedSize", "value": 9000, "packageValue": 5000},
    ]))
    assert [s for s, _ in plan.apply] == ["Message_MaxAllowedSize"]
    assert set(plan.denied) == {"Site_Url", "Enterprise_License", "Assets_logo"}


def test_config_import_detects_oauth_providers(tmp_path):
    plan = configimport.build_plan(_dump(tmp_path, [
        {"_id": "Accounts_OAuth_Custom-Ms_entra_id", "value": True, "packageValue": False},
        {"_id": "Accounts_OAuth_Custom-Ms_entra_id-id", "value": "abc", "packageValue": ""},
    ]))
    assert plan.oauth_services == ["Ms_entra_id"]


def test_config_import_only_filter(tmp_path):
    items = [
        {"_id": "Livechat_title", "value": "T", "packageValue": ""},
        {"_id": "LDAP_Enable", "value": True, "packageValue": False},
    ]
    plan = configimport.build_plan(_dump(tmp_path, items), only={"Livechat"})
    assert [s for s, _ in plan.apply] == ["Livechat_title"]


def test_config_import_tolerates_malformed_entries(tmp_path):
    # a hand-edited dump: a bare string, a dict with a numeric _id, a dict with
    # no value — none should crash build_plan; the one good entry still applies.
    plan = configimport.build_plan(_dump(tmp_path, [
        "just a string",
        {"_id": 123, "value": "x", "packageValue": "y"},
        {"_id": "NoValue", "packageValue": "y"},
        {"_id": "Good", "value": "new", "packageValue": "old"},
    ]))
    assert [s for s, _ in plan.apply] == ["Good"]


def test_config_import_redaction_variants(tmp_path):
    plan = configimport.build_plan(_dump(tmp_path, [
        {"_id": "A", "value": "xxxxxxxx", "packageValue": ""},   # lowercase mask
        {"_id": "B", "value": "●●●●●●", "packageValue": ""},      # bullet mask
        {"_id": "C", "value": "########", "packageValue": ""},   # hash mask
        {"_id": "D", "value": "real-value", "packageValue": ""},  # legit
    ]))
    assert [s for s, _ in plan.apply] == ["D"]
    assert set(plan.redacted) == {"A", "B", "C"}


def test_config_import_rejects_non_list(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"not": "a list"}')
    try:
        configimport.build_plan(p)
    except ValueError as exc:
        assert "list of settings" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# --- data-scale prefill (--scale) ---------------------------------------------


def test_parse_scale_users_and_messages():
    assert scaleseed.parse_scale("users=50000,messages=800000@team-chat") == {
        "users": 50000, "messages": (800000, "team-chat")}


def test_parse_scale_users_only():
    assert scaleseed.parse_scale("users=100") == {"users": 100}


def test_parse_scale_messages_without_room_raises():
    for bad in ("messages=100", "users=abc", "foo=1"):
        try:
            scaleseed.parse_scale(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_parse_scale_room_id_embedded_safely():
    # room ref is repr()'d into the JS, so a quote can't break out of the string
    js_room = repr("evil'; db.dropDatabase(); //")
    assert js_room.startswith(("'", '"')) and "dropDatabase" in js_room


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
    # create_repro guards this: an all-symbols name would otherwise write into
    # the repros root itself.
    from rc_repro.services.lifecycle import sanitize
    assert sanitize("!!!") == ""


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_config_file_is_owner_only(tmp_path, monkeypatch):
    # config.yaml can hold reg_token (a Cloud registration token applying an EE
    # license), so the default umask's 0644 would expose it to every local user.
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    config.save_config({"reg_token": "SECRET"})
    assert stat.S_IMODE(config.config_file().stat().st_mode) == 0o600
    assert stat.S_IMODE(config.home().stat().st_mode) == 0o700
    # no temp file left behind
    assert not config.config_file().with_name("config.yaml.tmp").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_config_file_permissions_tightened_on_rewrite(tmp_path, monkeypatch):
    # A file written by an older rc-repro is already 0644; saving must fix it
    # rather than preserve the loose mode. Unknown keys must survive too.
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    config.home().mkdir(parents=True)
    config.config_file().write_text(
        "default_repro: old\ncustom_team_key: keep-me\n",
        encoding="utf-8",
    )
    os.chmod(config.config_file(), 0o644)
    raw = config.load_config(with_env=False)
    raw["default_repro"] = "new"
    config.save_config(raw)
    assert stat.S_IMODE(config.config_file().stat().st_mode) == 0o600
    reloaded = config.load_config(with_env=False)
    assert reloaded["default_repro"] == "new"
    assert reloaded["custom_team_key"] == "keep-me"


def test_config_save_survives_chmod_failure(tmp_path, monkeypatch):
    # Explicit boundary for Windows / filesystems without POSIX mode bits:
    # chmod may fail, but the write is still the user's intent.
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    def boom(_path, _mode):
        raise OSError("permission bits unsupported")

    monkeypatch.setattr(config.os, "chmod", boom)
    config.save_config({"default_repro": "ok"})
    assert config.config_file().read_text(encoding="utf-8")
    assert config.load_config(with_env=False)["default_repro"] == "ok"
    assert not config.config_file().with_name("config.yaml.tmp").exists()


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


def test_monitoring_ships_k6_loadtest_dashboard():
    # loadtest --live streams k6_* metrics via remote-write; a dashboard must be
    # provisioned to actually SEE them (Explore alone is easy to miss).
    from rc_repro import monitoring
    files = dict(monitoring.files(["rocketchat"]))
    raw = files.get("monitoring/grafana/dashboards/k6-loadtest.json")
    assert raw, "k6 load-test dashboard not shipped"
    dash = json.loads(raw)
    assert dash["title"] == "k6 Load Test" and dash["panels"]
    # its panels must actually query the k6_ metrics the --live push produces,
    # against the provisioned Prometheus datasource.
    body = json.dumps(dash)
    assert "k6_http_reqs_total" in body and "k6_http_req_duration_p95" in body
    assert "DS_PROMETHEUS" in body


def test_no_monitoring_by_default():
    doc = compose.build(_spec("8.4.1"))
    assert "prometheus" not in doc["services"] and "grafana" not in doc["services"]


def test_monitoring_ships_logs_stack():
    # Loki + OTel collector, mirroring RocketChat/rocketchat-compose; the
    # collector must be SCOPED to this repro's project so it never tails others.
    from rc_repro import monitoring
    svcs = monitoring.services()
    assert "loki" in svcs and "opentelemetry-logs-collector" in svcs
    assert "loki_data" in monitoring.VOLUMES
    fs = dict(monitoring.files(["rocketchat"], project="rcrepro-demo"))
    assert "monitoring/loki/config.yaml" in fs
    assert "monitoring/grafana/provisioning/datasources/loki.yml" in fs
    otel = fs["monitoring/otel/config.yaml"]
    assert "http://loki:3100/otlp" in otel              # exports to Loki
    assert 'com.docker.compose.project"] == "rcrepro-demo"' in otel  # scoped to this repro


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


def test_baseline_label_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro.perf import baseline
    assert baseline.sanitize_label("Before Fix!") == "before-fix"
    for bad in ("", "///", "!!!"):
        try:
            baseline.sanitize_label(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for label {bad!r}")
    payload = {"label": "base", "ctx": {"scenario": "journey"},
               "summary": {"rps": 50.0, "p95": 120.0}}
    baseline.save("base", payload)
    assert baseline.load("base")["summary"]["p95"] == 120.0
    try:
        baseline.load("missing")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_baseline_compare_flags_regressions():
    from rc_repro.perf import baseline
    base = {"summary": {"rps": 100.0, "p50": 40.0, "p95": 100.0, "p99": 150.0,
                        "error_rate": 0.001,
                        "steps": {"login": {"p95": 100.0}, "post": {"p95": 50.0}}}}
    cur = {"summary": {"rps": 90.0, "p50": 41.0, "p95": 300.0, "p99": 160.0,
                       "error_rate": 0.001,
                       "steps": {"login": {"p95": 310.0}, "post": {"p95": 49.0}}}}
    rows = {r["metric"]: r for r in baseline.compare(cur, base)}
    assert rows["p95"]["flag"] and rows["p95"]["pct"] > 100        # +200% -> regression
    assert not rows["p50"]["flag"]                                 # +2.5% -> noise
    assert not rows["throughput (rps)"]["flag"]                    # -10% worse but under threshold
    assert rows["step login p95"]["flag"]                          # per-step regression caught
    assert not rows["step post p95"]["worse"]                      # improvement


def test_baseline_step_order_canonical():
    from rc_repro.perf import baseline
    steps = {"post": {}, "zeta": {}, "login": {}, "open": {}}
    assert baseline.step_order(steps) == ["login", "open", "post", "zeta"]


def test_loadtest_journey_scenario_registered():
    from importlib import resources
    from rc_repro.perf import k6
    assert "journey" in k6.SCENARIOS
    d = resources.files("rc_repro").joinpath("data", "loadtest")
    assert d.joinpath("journey.js").is_file()
    # journey + mixed emit per-step trends; common.js collects them into `steps`
    assert "step_" in d.joinpath("journey.js").read_text(encoding="utf-8")
    assert "step_" in d.joinpath("mixed.js").read_text(encoding="utf-8")
    assert "steps" in d.joinpath("common.js").read_text(encoding="utf-8")


def test_loadtest_report_renders_steps_snapshot_compare():
    from rc_repro.perf import report
    ctx = {"name": "acme", "version": "8.5.1", "scenario": "journey", "vus": 20,
           "duration": "30s", "ramp": "", "target": "http://rocketchat:3000",
           "users": 5, "label": "journey"}
    summary = {"count": 900, "rps": 30.0, "p50": 40, "p90": 90, "p95": 120, "p99": 200,
               "avg": 55, "min": 10, "max": 400, "error_rate": 0.0, "checks_rate": 1.0,
               "steps": {"login": {"count": 180, "p50": 100, "p95": 180, "p99": 250},
                         "post": {"count": 180, "p50": 30, "p95": 60, "p99": 90}}}
    snapshot = {"rc_version": "8.5.1", "preset": "default", "instances": 1,
                "users": 6, "rooms": 5, "messages": 40}
    compare = {"label": "base", "saved_at": "2026-07-18T10:00:00",
               "rows": [{"metric": "p95", "before": 100.0, "after": 120.0,
                         "pct": 20.0, "worse": True, "flag": False}]}
    host = {"os": "test", "cpu": 8, "docker": "27.0", "compose": "2.30"}
    md = report.loadtest_markdown(ctx, summary, [], None, host,
                                  snapshot=snapshot, compare=compare)
    assert "5 seeded users" in md and "## Workspace" in md
    assert "## Per-step latency" in md and "| login |" in md
    assert "## vs baseline `base`" in md and "+20%" in md


def test_constrain_parse():
    from rc_repro.perf import constrain
    c = constrain.parse("rc=2cpu/2g,mongo=0.5cpu/512m")
    assert c["rc"] == {"cpus": 2.0, "mem": "2g"}
    assert c["mongo"] == {"cpus": 0.5, "mem": "512m"}
    assert constrain.parse("rc=1cpu")["rc"] == {"cpus": 1.0, "mem": None}
    assert constrain.parse("mongo=1gb")["mongo"] == {"cpus": None, "mem": "1g"}
    for bad in ("rc", "rc=", "rc=fast", "rc=0cpu", "rc=2cpu/2parsecs"):
        try:
            constrain.parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_constrain_resolve_aliases():
    from rc_repro.perf import constrain
    lim = {"cpus": 1.0, "mem": "1g"}
    # single-instance: rc -> rocketchat; mongo -> mongodb
    services = ["rocketchat", "mongodb", "mongo-init"]
    r = constrain.resolve_services({"rc": lim, "mongo": lim}, services)
    assert set(r) == {"rocketchat", "mongodb"}
    # multi-instance: rc expands to every instance
    multi = ["rocketchat-1", "rocketchat-2", "traefik", "nats", "mongodb"]
    r2 = constrain.resolve_services({"rc": lim}, multi)
    assert set(r2) == {"rocketchat-1", "rocketchat-2"}
    # exact service names pass through; unknown ones are actionable errors
    assert set(constrain.resolve_services({"traefik": lim}, multi)) == {"traefik"}
    try:
        constrain.resolve_services({"keycloak": lim}, services)
    except ValueError as exc:
        assert "keycloak" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown service")


def test_constrain_human():
    from rc_repro.perf import constrain
    assert constrain.human({"rc": {"cpus": 2.0, "mem": "2g"},
                            "mongo": {"cpus": 0.5, "mem": None}}) == "rc=2cpu/2g, mongo=0.5cpu"


def test_rcmetrics_prom_parser():
    from rc_repro.perf import rcmetrics
    text = (
        "# HELP nodejs_eventloop_lag_seconds Lag of event loop in seconds.\n"
        "# TYPE nodejs_eventloop_lag_seconds gauge\n"
        "nodejs_eventloop_lag_seconds 0.0123\n"
        "nodejs_heap_size_used_bytes 123456789\n"
        'rocketchat_rest_api_count{method="GET",path="/x"} 5\n'
        'rocketchat_rest_api_count{method="POST",path="/y"} 7\n'
        "not a metric line\n"
    )
    parsed = rcmetrics.parse_prom(text)
    assert parsed["nodejs_eventloop_lag_seconds"] == 0.0123
    assert parsed["nodejs_heap_size_used_bytes"] == 123456789
    assert parsed["rocketchat_rest_api_count"] == 12          # labeled series summed
    # series accumulate and summarize
    s = rcmetrics.ServiceSeries()
    s.add(parsed)
    s.add({"nodejs_eventloop_lag_seconds": 0.5})
    lag = s.summary()["eventloop_lag_s"]
    assert lag["max"] == 0.5 and lag["n"] == 2


def test_timeline_bucketing(tmp_path):
    import json as _json
    from rc_repro.perf import timeline
    # Synthetic k6 point stream: 60s of requests, latency degrading, errors late.
    # k6 emits compact JSON (no spaces) — the parser's prefilter relies on that.
    compact = {"separators": (",", ":")}
    lines = []
    for sec in range(60):
        t = f"2026-07-19T10:00:{sec:02d}.123456789+00:00"
        lines.append(_json.dumps({"type": "Point", "metric": "http_req_duration",
                                  "data": {"time": t, "value": 50.0 + sec * 10}}, **compact))
        if sec >= 45:
            lines.append(_json.dumps({"type": "Point", "metric": "http_req_failed",
                                      "data": {"time": t, "value": 1}}, **compact))
    p = tmp_path / "points.json"
    p.write_text("\n".join(lines), encoding="utf-8")
    tl = timeline.parse(p)
    assert tl and len(tl["buckets"]) <= timeline.TARGET_BUCKETS + 1
    assert tl["buckets"][-1]["p95"] > tl["buckets"][0]["p95"]      # degradation visible
    assert tl["first_error_s"] is not None and tl["first_error_s"] >= 40
    art = timeline.render_ascii(tl)
    assert "p95 over time" in art[0] and "errors" in art[1]
    assert timeline.parse(tmp_path / "missing.json") is None


def test_verdict_rules():
    from rc_repro.perf import verdict
    base = {"p95": 120.0, "rps": 50.0, "error_rate": 0.0}
    # saturated event loop -> named finding
    v = verdict.analyze(base, rcmetrics={"rocketchat": {
        "eventloop_lag_s": {"mean": 0.3, "max": 1.2, "last": 0.9, "n": 5}}})
    assert any("event loop saturated" in f for f in v)
    # COLLSCAN -> missing index finding
    v2 = verdict.analyze(base, mongo={"total": 8, "collscan": 3, "slow": [
        {"ns": "rocketchat.rocketchat_message", "op": "query", "millis": 900,
         "plan": "COLLSCAN", "docs": 50000, "keys": 0, "ret": 20, "cmd": ""}]})
    assert any("COLLSCAN" in f and "missing index" in f for f in v2)
    # errors with 429s + timeline start time
    v3 = verdict.analyze({"p95": 100.0, "rps": 10.0, "error_rate": 0.2,
                          "status": {"429": 50}}, timeline={"first_error_s": 42})
    assert any("429" in f and "~42s" in f for f in v3)
    # histogram lag key preferred; degraded tier
    v5 = verdict.analyze(base, rcmetrics={"rocketchat": {
        "eventloop_lag_max_s": {"mean": 0.15, "max": 0.23, "last": 0.2, "n": 6}}})
    assert any("degraded" in f for f in v5)
    # high client latency with no server signal -> queueing finding, not "headroom"
    v6 = verdict.analyze({"p95": 1400.0, "rps": 15.0, "error_rate": 0.0})
    assert any("queueing outside" in f for f in v6)
    # clean run -> headroom statement
    v4 = verdict.analyze(base)
    assert len(v4) == 1 and "headroom" in v4[0]


def test_spike_recovery_math():
    from rc_repro.perf import timeline
    # 90s run, 1s buckets: calm first third, spiked middle, recovering tail.
    def bucket(t, p95):
        return {"t0": t, "reqs": 10, "p50": p95 * 0.6, "p95": p95, "max": p95 * 2, "errors": 0}
    buckets = ([bucket(t, 100.0) for t in range(0, 30)]
               + [bucket(t, 900.0) for t in range(30, 60)]
               + [bucket(t, 800.0) for t in range(60, 68)]      # still elevated
               + [bucket(t, 120.0) for t in range(68, 90)])     # back under 1.5x baseline
    tl = {"width_s": 1, "span_s": 89, "buckets": buckets, "first_error_s": None}
    rec = timeline.spike_recovery(tl)
    assert rec["baseline_p95"] == 100.0 and rec["spike_p95"] == 900.0
    assert rec["recovered_after_s"] == 8
    # never recovers -> None
    stuck = {"width_s": 1, "span_s": 89, "first_error_s": None,
             "buckets": buckets[:60] + [bucket(t, 900.0) for t in range(60, 90)]}
    assert timeline.spike_recovery(stuck)["recovered_after_s"] is None


def test_mem_slopes_requires_span():
    from rc_repro.perf.resources import ResourceMonitor
    mon = ResourceMonitor("x")
    # 20 minutes of samples, RAM growing 1MB/min -> 60MB/h
    mon._series["rcrepro-x-rocketchat-1"] = [
        (t * 60.0, 50.0, 1e9 + t * 1e6, 8e9) for t in range(21)]
    # only 2 minutes -> too short to say anything
    mon._series["rcrepro-x-mongodb-1"] = [(0.0, 10.0, 2e8, 8e9), (120.0, 10.0, 3e8, 8e9)]
    slopes = mon.mem_slopes()
    assert round(slopes["rcrepro-x-rocketchat-1"] / 1e6) == 60
    assert "rcrepro-x-mongodb-1" not in slopes


def test_verdict_spike_and_soak_rules():
    from rc_repro.perf import verdict
    base = {"p95": 120.0, "rps": 50.0, "error_rate": 0.0}
    v = verdict.analyze(base, spike={"baseline_p95": 100.0, "spike_p95": 900.0,
                                     "recovered_after_s": None})
    assert any("Did not recover" in f for f in v)
    v2 = verdict.analyze(base, soak={"rocketchat": 75e6})
    assert any("75MB/h" in f and "leak" in f for f in v2)
    v3 = verdict.analyze(base, spike={"baseline_p95": 100.0, "spike_p95": 900.0,
                                      "recovered_after_s": 4})
    assert len(v3) == 1 and "headroom" in v3[0]   # quick recovery isn't a finding


def test_new_scenarios_registered():
    from importlib import resources
    from rc_repro.perf import k6
    d = resources.files("rc_repro").joinpath("data", "loadtest")
    for s in ("webhook", "badbot"):
        assert s in k6.SCENARIOS and d.joinpath(f"{s}.js").is_file()
    assert "SPIKE" in d.joinpath("common.js").read_text(encoding="utf-8")


def test_capacity_report_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from pathlib import Path
    from rc_repro.perf import report
    ctx = {"name": "acme", "version": "8.5.1", "scenario": "journey", "slo": "p95=500ms",
           "users": 5, "step_duration": "20s", "target": "http://rocketchat:3000",
           "constrained": "rocketchat=2cpu"}
    steps = [
        {"vus": 10, "rps": 140.0, "p95": 90.0, "error_rate": 0.0, "ok": True,
         "lag_max_s": 0.02, "breached": []},
        {"vus": 20, "rps": 150.0, "p95": 620.0, "error_rate": 0.01, "ok": False,
         "lag_max_s": 1.2, "breached": ["p95 <= 500ms (actual 620ms)"]},
    ]
    host = {"os": "t", "cpu": 8, "docker": "27", "compose": "2.30"}
    out = report.write_capacity(ctx, steps, "~10 concurrent VUs",
                                "at 20 VUs the RC event loop saturated", host, "TESTSTAMP")
    md_text = Path(out).read_text(encoding="utf-8")
    assert "capacity report" in md_text and "~10 concurrent VUs" in md_text
    assert "**FAIL** — p95 <= 500ms" in md_text and "Resource caps" in md_text
    assert "event loop saturated" in md_text


def test_mongoprof_last_json():
    from rc_repro.perf import mongoprof
    out = "some banner\nWarning: x\n{\"was\": 0, \"slowms\": 100}\n"
    assert mongoprof._last_json(out) == {"was": 0, "slowms": 100}
    assert mongoprof._last_json("no json here") is None


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


# --- error taxonomy / exit codes ----------------------------------------------


def test_error_taxonomy_codes_and_exit_codes_are_distinct():
    from rc_repro import errors
    subs = [errors.ValidationError, errors.ConflictError, errors.NotFoundError,
            errors.NotReadyError, errors.DockerError, errors.CreateFailedError,
            errors.AuthorityGateError]
    codes = [c.code for c in subs]
    exits = [c.exit_code for c in subs]
    assert len(set(codes)) == len(codes), "error codes must be unique"
    assert len(set(exits)) == len(exits), "exit codes must be unique per cause"
    # every subclass stays a ReproError so existing handlers keep catching it
    assert all(issubclass(c, errors.ReproError) for c in subs)
    # exit 0 is success and must never be an error's code
    assert 0 not in exits
    # every exit code is documented in the published map
    assert all(e in errors.EXIT_CODES for e in exits)


def test_not_ready_and_create_failed_stay_distinct():
    # 5 means "still unknown, clock ran out"; 7 means "known dead, stop now".
    # Collapsing them is what makes callers wait out an already-failed run.
    from rc_repro import errors
    assert errors.NotReadyError.exit_code == 5
    assert errors.CreateFailedError.exit_code == 7


def test_authority_gate_carries_approve_with():
    from rc_repro import errors
    exc = errors.AuthorityGateError(
        "cluster 'prod-eu' is not approved", kind="cluster", subject="prod-eu",
        approve_with="rc-repro use --cluster prod-eu",
        code="GATE_UNAPPROVED_CLUSTER")
    assert exc.exit_code == 6
    assert exc.code == "GATE_UNAPPROVED_CLUSTER"   # per-gate code overrides
    assert exc.as_gate() == {"kind": "cluster", "subject": "prod-eu",
                             "approve_with": "rc-repro use --cluster prod-eu"}
    assert errors.AuthorityGateError.code == "GATE"  # class default untouched


def test_http_status_still_intact():
    # The web API maps on http_status; adding exit codes must not disturb it.
    from rc_repro import errors
    assert errors.ValidationError.http_status == 400
    assert errors.NotFoundError.http_status == 404
    assert errors.ConflictError.http_status == 409
    assert errors.DockerError.http_status == 502


def test_ui_die_defaults_to_one_and_honours_override():
    import typer
    from rc_repro import ui
    for expected, kwargs in ((1, {}), (6, {"exit_code": 6})):
        try:
            ui.die("boom", **kwargs)
        except typer.Exit as exc:
            assert exc.exit_code == expected
        else:
            raise AssertionError("ui.die must raise typer.Exit")


# --- machine-readable output contract -----------------------------------------


def test_envelope_shape_and_versions():
    from rc_repro import jsonout
    env = jsonout.envelope("info", {"name": "x"})
    assert set(env) == {"schema", "contract", "rc_repro_version", "generated_at",
                        "ok", "data", "warnings", "error"}
    assert env["schema"] == "rc-repro.info.v1"
    assert env["contract"] == jsonout.CONTRACT == 1
    assert env["ok"] is True and env["error"] is None
    # warnings is always a list so callers can iterate without a None check
    assert env["warnings"] == []


def test_error_envelope_uses_the_taxonomy_code():
    from rc_repro import errors, jsonout
    env = jsonout.error_envelope(errors.NotFoundError("no repro named 'x'"))
    assert env["ok"] is False and env["data"] is None
    assert env["schema"] == "rc-repro.error.v1"
    assert env["error"]["code"] == "NOT_FOUND"      # stable; message is not
    assert "gate" not in env["error"]               # only gates carry one


def test_error_envelope_carries_gate_for_authority_errors():
    from rc_repro import errors, jsonout
    exc = errors.AuthorityGateError("not approved", kind="cluster",
                                    subject="prod-eu",
                                    approve_with="rc-repro use --cluster prod-eu",
                                    code="GATE_UNAPPROVED_CLUSTER")
    env = jsonout.error_envelope(exc)
    assert env["error"]["code"] == "GATE_UNAPPROVED_CLUSTER"
    assert env["error"]["gate"]["approve_with"] == "rc-repro use --cluster prod-eu"


def test_list_json_is_one_line_and_empty_is_not_an_error(tmp_path, monkeypatch):
    import json as _json
    from typer.testing import CliRunner
    from rc_repro.cli import app
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    res = CliRunner().invoke(app, ["list", "--json"])
    assert res.exit_code == 0
    assert len(res.stdout.strip().splitlines()) == 1   # one object per line
    payload = _json.loads(res.stdout)
    assert payload["schema"] == "rc-repro.list.v1"
    assert payload["data"]["repros"] == []             # [] not a prose line


def test_info_json_missing_repro_emits_error_envelope(tmp_path, monkeypatch):
    import json as _json
    from typer.testing import CliRunner
    from rc_repro.cli import app
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    res = CliRunner().invoke(app, ["info", "--name", "no-such-repro", "--json"])
    assert res.exit_code == 4                          # NOT_FOUND, not a flat 1
    payload = _json.loads(res.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"


# --- NDJSON progress stream ----------------------------------------------------


def _ev(**kw):
    from rc_repro.services.events import Event
    kw.setdefault("message", "m")
    return Event(**kw)


def test_event_writer_normalises_unpublished_phases():
    from rc_repro import jsonout
    w = jsonout.EventWriter()
    # a published phase passes through
    assert w.event(_ev(phase="pull"))["phase"] == "pull"
    # an unpublished one becomes "info" so the published set really is closed,
    # and the original is preserved rather than discarded
    out = w.event(_ev(phase="k6"))
    assert out["phase"] == "info"
    assert out["detail"]["phase_raw"] == "k6"


def test_event_writer_pct_is_monotonic():
    from rc_repro import jsonout
    w = jsonout.EventWriter()
    assert w.event(_ev(pct=10))["pct"] == 10
    assert w.event(_ev(pct=40))["pct"] == 40
    # a service reporting out of order must not make progress go backwards
    assert w.event(_ev(pct=25))["pct"] == 40
    assert w.event(_ev(pct=None))["pct"] is None   # unknown stays unknown


def test_event_writer_drops_terminal_events(capsys):
    from rc_repro import jsonout
    w = jsonout.EventWriter()
    w.emit(_ev(phase="pull"))
    w.emit(_ev(phase="done", terminal=True))   # wrapper emits the envelope itself
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1


def test_event_schema_and_published_phases():
    from rc_repro import jsonout
    out = jsonout.EventWriter().event(_ev(phase="wait", level="warn", pct=5))
    assert out["schema"] == "rc-repro.event.v1"
    assert out["contract"] == jsonout.CONTRACT
    assert out["level"] == "warn"
    # the phases the existing Event model already used must all stay published,
    # or the GUI's current stream would start normalising to "info"
    for legacy in ("pull", "boot", "wait", "post_ready", "seed", "restore", "done"):
        assert legacy in jsonout.PHASES


def test_json_mode_writes_only_objects_to_stdout(tmp_path, monkeypatch):
    # Contract: under --json, stdout carries envelope/event objects only. Prose
    # belongs on stderr. Engine is unavailable here, so this exercises the error
    # path, which is the one most likely to leak a human-readable line.
    import json as _json
    from typer.testing import CliRunner
    from rc_repro.cli import app
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    # This is an error-wire-format test, not an integration test. Make the
    # intended preflight failure deterministic on CI hosts that have Docker.
    monkeypatch.setattr("rc_repro.runner.docker_available", lambda: False)
    res = CliRunner().invoke(app, ["up", "--version", "8.6.1", "--json"])
    lines = res.stdout.strip().splitlines()
    assert lines, "expected at least one object"
    for ln in lines:
        _json.loads(ln)          # every stdout line must parse as JSON
    last = _json.loads(lines[-1])
    assert last["ok"] is False and last["error"]["code"]
    assert res.exit_code == last_exit_for(last["error"]["code"])


def last_exit_for(code: str) -> int:
    from rc_repro import errors
    for cls in (errors.ValidationError, errors.ConflictError, errors.NotFoundError,
                errors.NotReadyError, errors.DockerError, errors.CreateFailedError):
        if cls.code == code:
            return cls.exit_code
    return 1


def test_up_json_streams_events_then_exactly_one_envelope(tmp_path, monkeypatch, capsys):
    # The happy path, without needing an engine: stand in for the service call and
    # assert the wire contract holds — events first, exactly one envelope, and it
    # is last. A real `up` cannot verify this reliably on every host.
    import json as _json
    from typer.testing import CliRunner
    from rc_repro import cli
    from rc_repro.cli import app
    from rc_repro.services.events import Event

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))

    def fake_create(req, emit=None, stream_output=False):
        for ph, pct in (("preflight", 0), ("resolve", 5), ("pull", 40),
                        ("boot", 60), ("wait", 80)):
            emit(Event(f"{ph} step", phase=ph, pct=pct))
        emit(Event("warn about something", phase="wait", level="warn"))
        emit(Event("done", phase="done", terminal=True, data={"name": "rc-x"}))
        return {"name": "rc-x", "waited": True, "booted_s": 12}

    monkeypatch.setattr(cli.lcsvc, "create_repro", fake_create)
    res = CliRunner().invoke(app, ["up", "--version", "8.6.1", "--json"])
    assert res.exit_code == 0

    lines = [_json.loads(l) for l in res.stdout.strip().splitlines()]
    envelopes = [d for d in lines if d["schema"] != "rc-repro.event.v1"]
    events_ = [d for d in lines if d["schema"] == "rc-repro.event.v1"]

    assert len(envelopes) == 1, "exactly one envelope"
    assert lines[-1] is envelopes[0], "the envelope must be the last line"
    assert lines[-1]["schema"] == "rc-repro.up.v1"
    assert lines[-1]["data"]["name"] == "rc-x"
    # the terminal event is not published as an event; the envelope replaces it
    assert len(events_) == 6
    assert [e["phase"] for e in events_][:5] == ["preflight", "resolve", "pull", "boot", "wait"]
    # pct never decreases across the stream
    pcts = [e["pct"] for e in events_ if e["pct"] is not None]
    assert pcts == sorted(pcts)
    assert any(e["level"] == "warn" for e in events_)


# --- capabilities discovery ----------------------------------------------------


def test_capabilities_is_derived_not_hardcoded():
    from rc_repro import errors, jsonout
    from rc_repro.cli import app
    cap = jsonout.capabilities(app)
    assert cap["contract_versions"] == [jsonout.CONTRACT]
    # phases and exit codes come straight from their definitions, so they cannot
    # drift from what the code actually emits
    assert cap["phases"] == list(jsonout.PHASES)
    assert cap["exit_codes"] == {str(k): v for k, v in sorted(errors.EXIT_CODES.items())}
    # every error code in the taxonomy is discoverable
    assert "NOT_FOUND" in cap["error_codes"]
    assert "CREATE_FAILED" in cap["error_codes"]
    # presets are read from the catalog, not a literal list
    assert "default" in cap["presets"]


def test_capabilities_reports_which_verbs_speak_json():
    from rc_repro import jsonout
    from rc_repro.cli import app
    cap = jsonout.capabilities(app)
    by_name = {c["name"]: c for c in cap["commands"]}
    for verb in ("up", "ready", "down", "list", "info"):
        assert by_name[verb]["json"] is True, verb
        assert by_name[verb]["schema"] == f"rc-repro.{verb}.v1"
    # only the long-running verbs stream events
    assert by_name["up"]["streams"] is True
    assert by_name["list"]["streams"] is False
    # a verb without --json is reported honestly rather than omitted
    assert by_name["logs"]["json"] is False
    assert "--name" in by_name["up"]["flags"]


def test_capabilities_needs_no_engine(tmp_path, monkeypatch):
    # A skill calls this before it knows the environment works, so it must answer
    # with no engine present. Engine checks belong to `doctor`.
    import json as _json
    from typer.testing import CliRunner
    from rc_repro import cli
    from rc_repro.cli import app
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.runner, "docker_available", lambda: False)
    res = CliRunner().invoke(app, ["capabilities"])
    assert res.exit_code == 0
    payload = _json.loads(res.stdout)
    assert payload["schema"] == "rc-repro.capabilities.v1"
    assert payload["ok"] is True


def test_capabilities_discovers_the_kubernetes_topology():
    # Derived from the catalog, so a preset declaring a new topology becomes
    # discoverable without touching the capability record.
    from rc_repro import jsonout
    from rc_repro.cli import app
    cap = jsonout.capabilities(app)
    assert cap["topologies"] == ["compose", "kubernetes"]
    assert cap["presets_by_topology"]["kubernetes"] == ["microservices"]
    assert "default" in cap["presets_by_topology"]["compose"]


def test_microservices_preset_declares_its_topology_and_licence():
    from rc_repro import presets
    p = presets.load("microservices")
    assert p.topology == "kubernetes"
    # Microservices are an enterprise feature; the flag is advisory, and evidence
    # records the actual licence state rather than blocking the run.
    assert p.requires_license is True
    # Compose-shaped fields stay empty: this preset builds Helm values.
    assert p.services == {} and p.env == {}


def test_every_other_preset_stays_on_compose():
    from rc_repro import presets
    for p in presets.list_presets():
        if p.name != "microservices":
            assert p.topology == "compose", p.name


def test_lifecycle_dispatches_on_topology(monkeypatch):
    from rc_repro.services import lifecycle as lc
    assert lc._topology_of("microservices") == "kubernetes"
    assert lc._topology_of("default") == "compose"
    # an unknown preset must not be guessed into the Kubernetes path; the Compose
    # body raises a proper ValidationError for it moments later
    assert lc._topology_of("does-not-exist") == "compose"


# --- onboarding ----------------------------------------------------------------


def test_onboarding_absent_is_an_authority_gate(tmp_path, monkeypatch):
    # An agent on a fresh machine must not invent its own baseline. It stops with
    # exit 6 and the exact command to ask a human to run.
    from rc_repro import errors
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    assert onboarding.state()["completed"] is False
    with pytest.raises(errors.AuthorityGateError) as ei:
        onboarding.require_onboarded()
    exc = ei.value
    assert exc.exit_code == 6 and exc.code == "GATE_NOT_ONBOARDED"
    assert exc.as_gate()["approve_with"] == onboarding.ONBOARD_COMMAND


def test_onboarding_persists_and_stops_asking(tmp_path, monkeypatch):
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    st = onboarding.complete(grants=["engine-resize"], preferences={"retain_runs": True})
    assert st["completed"] and st["grants"]["engine_resize"] is True
    assert st["preferences"]["retain_runs"] is True
    onboarding.require_onboarded()                 # no raise, ever again
    onboarding.require_grant("engine-resize")      # granted


def test_missing_grant_is_a_gate_but_onboarding_is_not_reasked(tmp_path, monkeypatch):
    # A missing grant is an unanswered question, which is different from a settled
    # one. Onboarding stops re-asking what was answered; it does not make rc-repro
    # silent about authority it was never given.
    from rc_repro import errors
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(grants=[])                 # onboarded, nothing granted
    onboarding.require_onboarded()                 # settled: silent
    with pytest.raises(errors.AuthorityGateError) as ei:
        onboarding.require_grant("engine-resize")
    assert ei.value.code == "GATE_ENGINE_RESIZE"
    assert ei.value.as_gate()["approve_with"] == "rc-repro onboard"


def test_settled_grant_denial_points_to_interactive_reconfiguration(tmp_path, monkeypatch):
    from rc_repro import errors
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    onboarding.complete(denied_grants=["owned-cluster"])

    with pytest.raises(errors.AuthorityGateError) as ei:
        onboarding.require_grant("owned-cluster")

    assert ei.value.code == "GATE_OWNED_CLUSTER"
    assert ei.value.as_gate()["approve_with"] == "rc-repro onboard --reconfigure"


def test_onboarding_updates_only_answers_supplied_by_this_run(tmp_path, monkeypatch):
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    first = onboarding.complete(
        grants=["owned-cluster"], denied_grants=["engine-resize"],
        preferences={"retain_runs": True})
    assert first["grants"] == {"owned_cluster": True, "engine_resize": False}
    assert all(first["answered_grants"].values())
    second = onboarding.complete(grants=["engine-resize"])
    assert second["grants"] == {"owned_cluster": True, "engine_resize": True}
    assert second["preferences"]["retain_runs"] is True
    assert second["clusters"] == ["rc-repro-local"]


def test_interactive_onboarding_shows_facts_persists_authority_and_does_not_reask(
        tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from rc_repro.cli import app
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: {
        "os": "Ubuntu 24.04.4 LTS", "os_version": "24.04", "architecture": "x86_64",
        "cpus": 4, "memory_gib": 15.6, "disk_free_gib": 75.0,
        "tools": {"docker": "Docker 29.6.1", "compose": "Docker Compose v5.3.1",
                  "kind": "kind v0.32.0", "kubectl": "Client Version: v1.36.1",
                  "helm": "v3.21.3"},
        "docker_ready": True, "engine_memory_gib": 15.6, "engine_cpus": 4,
        "missing_kubernetes_tools": [], "microservices_ready": True,
        "engine_resize_relevant": False,
    })

    first = CliRunner().invoke(app, ["onboard"], input="y\nn\n")
    assert first.exit_code == 0, first.output
    assert "Detected environment" in first.output
    assert "create and later delete those owned resources" in first.output
    assert "Enterprise topology" in first.output
    assert onboarding.FIRST_RUN_COMMAND in first.output
    saved = onboarding.state()
    assert saved["grants"]["owned_cluster"] is True
    assert saved["preferences"]["retain_runs"] is False
    assert saved["answered_grants"]["engine_resize"] is False

    second = CliRunner().invoke(app, ["onboard"], input="")
    assert second.exit_code == 0, second.output
    assert "Settled choices are unchanged" in second.output
    assert "May rc-repro" not in second.output
    assert onboarding.state()["grants"] == saved["grants"]


def test_interactive_onboarding_prints_a_first_command_the_detected_machine_can_run(
        tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from rc_repro.cli import app
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: {
        "os": "Ubuntu 24.04.4 LTS", "os_version": "24.04", "architecture": "x86_64",
        "cpus": 2, "memory_gib": 3.8, "disk_free_gib": 75.0,
        "tools": {"docker": "Docker 29.6.1", "compose": "Docker Compose v5.3.1",
                  "kind": "missing", "kubectl": "missing", "helm": "missing"},
        "docker_ready": True, "engine_memory_gib": 3.8, "engine_cpus": 2,
        "missing_kubernetes_tools": ["kind", "kubectl", "helm"],
        "microservices_ready": False, "engine_resize_relevant": False,
    })

    result = CliRunner().invoke(app, ["onboard"], input="y\nn\n")

    assert result.exit_code == 0, result.output
    assert onboarding.FIRST_RUN_COMMAND not in result.output
    assert "rc-repro up --version 8.6.1 --name first-repro --wait" in result.output


def test_environment_detection_routes_docker_commands_through_runner(tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(onboarding, "_version_line", lambda tool, *args: f"{tool} ok")
    monkeypatch.setattr(runner, "docker_cli_version", lambda: "Docker version 29.6.1")
    monkeypatch.setattr(runner, "compose_version_line", lambda: "Docker Compose version v5.3.1")
    monkeypatch.setattr(runner, "docker_available", lambda: True)
    monkeypatch.setattr(k8s, "engine_capacity", lambda: (15.6, 4))
    monkeypatch.setattr(k8s, "engine_resize_supported", lambda: False)

    detected = onboarding.detect_environment()

    assert detected["tools"]["docker"] == "Docker version 29.6.1"
    assert detected["tools"]["compose"] == "Docker Compose version v5.3.1"


@pytest.mark.parametrize(
    ("capacity", "resize_supported", "expected"),
    [((8.0, 2), True, False), ((2.0, 4), False, False), ((2.0, 4), True, True)],
)
def test_environment_only_offers_resize_for_a_supported_memory_shortfall(
        tmp_path, monkeypatch, capacity, resize_supported, expected):
    from rc_repro import runner
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(onboarding, "_version_line", lambda tool, *args: f"{tool} ok")
    monkeypatch.setattr(runner, "docker_cli_version", lambda: "Docker version 29.6.1")
    monkeypatch.setattr(runner, "compose_version_line", lambda: "Docker Compose version v5.3.1")
    monkeypatch.setattr(runner, "docker_available", lambda: True)
    monkeypatch.setattr(k8s, "engine_capacity", lambda: capacity)
    monkeypatch.setattr(k8s, "engine_resize_supported", lambda: resize_supported)

    assert onboarding.detect_environment()["engine_resize_relevant"] is expected


def test_noninteractive_onboarding_needs_accept_defaults_and_explicit_cluster_grant(
        tmp_path, monkeypatch):
    import json as _json
    from typer.testing import CliRunner
    from rc_repro.cli import app
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding, "detect_environment", lambda: {})

    refused = CliRunner().invoke(app, ["onboard", "--json"])
    assert refused.exit_code == 2
    assert "--accept-defaults" in refused.output

    baseline = CliRunner().invoke(app, ["onboard", "--accept-defaults", "--json"])
    assert baseline.exit_code == 0, baseline.output
    assert _json.loads(baseline.stdout)["data"]["grants"]["owned_cluster"] is False

    granted = CliRunner().invoke(
        app, ["onboard", "--accept-defaults", "--grant", "owned-cluster", "--json"])
    assert granted.exit_code == 0, granted.output
    assert _json.loads(granted.stdout)["data"]["grants"]["owned_cluster"] is True


def test_onboarding_is_additive_and_keeps_existing_keys(tmp_path, monkeypatch):
    # Never rename or retype an existing key: a config written before onboarding
    # existed must survive untouched.
    from rc_repro import config
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    config.save_config({"default_repro": "rc8", "bind_host": "127.0.0.1"})
    onboarding.complete(grants=["engine-resize"])
    cfg = config.load_config()
    assert cfg["default_repro"] == "rc8" and cfg["bind_host"] == "127.0.0.1"
    assert "config_version" not in cfg          # additive-only, no migration
    # an unknown preference in the file is ignored rather than honoured
    cfg["preferences"]["not_a_real_pref"] = True
    config.save_config(cfg)
    assert "not_a_real_pref" not in onboarding.state()["preferences"]


def test_onboarding_rejects_unknown_grants(tmp_path, monkeypatch):
    from rc_repro import errors
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    with pytest.raises(errors.ValidationError) as ei:
        onboarding.complete(grants=["delete-everything"])
    assert "engine-resize" in str(ei.value)      # names what is available


def test_onboarding_never_persists_a_secret(tmp_path, monkeypatch):
    # A registration token keeps its ephemeral route; onboarding must not bake it in.
    from rc_repro import config
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RC_REPRO_REG_TOKEN", "SUPERSECRET")
    onboarding.complete(grants=[])
    assert "SUPERSECRET" not in config.config_file().read_text()


def test_capabilities_reports_onboarding_state(tmp_path, monkeypatch):
    from rc_repro import jsonout
    from rc_repro.cli import app
    from rc_repro.services import onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    cap = jsonout.capabilities(app)
    assert cap["onboarding"]["completed"] is False
    assert cap["onboarding"]["onboard_with"] == onboarding.ONBOARD_COMMAND
    onboarding.complete(grants=["engine-resize"])
    assert jsonout.capabilities(app)["onboarding"]["completed"] is True


def test_capabilities_bad_config_still_routes_a_human_to_interactive_onboarding(monkeypatch):
    from rc_repro import jsonout
    from rc_repro.services import onboarding
    monkeypatch.setattr(onboarding, "state", lambda: (_ for _ in ()).throw(ValueError("bad")))

    assert jsonout._onboarding_state()["onboard_with"] == "rc-repro onboard"


# --- the agent skill bundle ----------------------------------------------------


def test_committed_host_copies_match_the_packaged_bundle():
    """The whole point of one canonical bundle is that copies cannot diverge.

    Without this test the repo's committed .claude/ and .agents/ copies drift from
    the packaged one, which is exactly the divergent-copy problem the skill design
    exists to prevent.
    """
    from pathlib import Path
    from rc_repro.services import skill
    canonical = skill.bundle_text()
    for rel in (".claude/skills/rc-repro/SKILL.md", ".agents/skills/rc-repro/SKILL.md"):
        p = Path(rel)
        assert p.exists(), f"{rel} is missing; copy rc_repro/data/skill/SKILL.md there"
        assert p.read_text(encoding="utf-8") == canonical, (
            f"{rel} has drifted from rc_repro/data/skill/SKILL.md")


def test_skill_frontmatter_is_restricted_to_the_spec_fields():
    # Host-only fields stay out of the canonical body: the superset host is the fork
    # risk here, not a gap.
    from rc_repro.services import skill
    text = skill.bundle_text()
    assert text.startswith("---\n")
    front = text.split("---", 2)[1]
    keys = {ln.split(":", 1)[0].strip() for ln in front.strip().splitlines()
            if ":" in ln and not ln.startswith(" ")}
    assert keys == {"name", "description"}, keys
    # description drives activation, so it must name situations, not the tool
    assert "reproduce" in front.lower()


def test_skill_body_delegates_rather_than_duplicating_the_contract():
    # It must not restate flags or error codes: a skill that disagrees with the
    # binary is worse than no skill.
    from rc_repro.services import skill
    text = skill.bundle_text()
    assert "rc-repro capabilities" in text          # points at the authority
    assert "exit 6" in text                         # teaches the gate rule
    for leaked in ("VALIDATION_FAILED", "ENGINE_UNAVAILABLE", "exit_codes"):
        assert leaked not in text, f"{leaked} duplicates the contract"


def test_skill_install_is_idempotent_and_detects_drift(tmp_path, monkeypatch):
    from rc_repro import errors
    from rc_repro.services import skill
    monkeypatch.setenv("HOME", str(tmp_path))          # never touch the real ~/.claude
    assert skill.status("claude").state == "absent"
    st = skill.install("claude")
    assert st.state == "current"
    assert skill.install("claude").state == "current"  # idempotent

    # a human edit is detected and never silently overwritten
    (st.path / "SKILL.md").write_text("locally edited", encoding="utf-8")
    assert skill.status("claude").state == "modified"
    with pytest.raises(errors.ConflictError):
        skill.install("claude")
    assert skill.install("claude", force=True).state == "current"


def test_skill_stale_when_the_recorded_version_differs(tmp_path, monkeypatch):
    import json as _j
    from rc_repro.services import skill
    monkeypatch.setenv("HOME", str(tmp_path))
    st = skill.install("claude")
    side = st.path / ".rc-repro-skill.json"
    data = _j.loads(side.read_text())
    data["rc_repro_version"] = "0.0.1-old"
    side.write_text(_j.dumps(data))
    assert skill.status("claude").state == "stale"


def test_cursor_and_copilot_need_no_separate_install():
    # They read the Claude Code and Codex directories, so a separate copy would be
    # a divergent copy for no benefit.
    from rc_repro import errors
    from rc_repro.services import skill
    for host, covered_by in (("cursor", "claude"), ("copilot", "codex")):
        with pytest.raises(errors.ValidationError) as ei:
            skill.target_dir(host)
        assert covered_by in str(ei.value)
