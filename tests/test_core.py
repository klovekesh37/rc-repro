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


def test_parse_scale_room_ref_cannot_reach_the_js_unquoted(monkeypatch):
    # This used to assert on the stdlib's repr() and never touch scaleseed at all,
    # so the guard it was named for had no coverage. Exercise both layers: the
    # _SPEC_RE whitelist (the real guard) and the quoted interpolation.
    from rc_repro import scaleseed
    captured = {}
    monkeypatch.setattr(scaleseed, "_eval",
                        lambda name, js: captured.setdefault("js", js) or (0, '{"inserted": 1}'))
    scaleseed.bulk_messages("x", 1, "team-chat")
    assert "'team-chat'" in captured["js"]          # only ever inside a JS literal
    for bad in ("ev'il", 'ev"il', "ev\\il", "ev\nil", "a b", "a;b", "$where"):
        try:
            scaleseed.parse_scale(f"messages=1@{bad}")
        except ValueError:
            continue
        raise AssertionError(f"_SPEC_RE should reject room {bad!r}")


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


def test_config_save_closes_raw_descriptor_when_fdopen_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    real_open = config.os.open
    opened = []

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail_fdopen(*_args, **_kwargs):
        raise OSError("could not construct file object")

    monkeypatch.setattr(config.os, "open", tracking_open)
    monkeypatch.setattr(config.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="could not construct file object"):
        config.save_config({"default_repro": "not-written"})

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert not list(config.home().glob("config.yaml.*.tmp"))


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


def _fake_run(calls, returncode=0, stdout=""):
    """subprocess.run stub that records the argv it was handed."""
    import types

    def run(cmd, **_kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


def test_constrain_restore_only_touches_capped_dimensions(monkeypatch):
    # A CPU-only --constrain must not make restore() impose a memory limit on a
    # container that had none (which, with --memory-swap == --memory, would also
    # disable swap) — restore only puts back what apply() actually changed.
    from rc_repro.perf import constrain
    calls = []
    monkeypatch.setattr(constrain.subprocess, "run", _fake_run(calls))
    monkeypatch.setattr(constrain.runner, "docker_capacity", lambda: (8.0, 8_000_000_000))
    a = constrain.Applied("cid1", "rocketchat", 0, 0, 0, set_cpus=True, set_mem=False)
    assert constrain.restore([a]) == []
    assert calls == [["docker", "update", "--cpus", "8", "cid1"]]


def test_constrain_restore_reports_partial_failure(monkeypatch):
    # docker info unavailable, a real prior CPU limit but no prior memory limit:
    # the memory dimension can't be resolved. That must be REPORTED — it used to
    # be silent whenever the built command was non-empty, so the test's memory
    # cap stayed applied and the caller printed no warning.
    from rc_repro.perf import constrain
    calls = []
    monkeypatch.setattr(constrain.subprocess, "run", _fake_run(calls))
    monkeypatch.setattr(constrain.runner, "docker_capacity", lambda: None)
    a = constrain.Applied("cid1", "rocketchat", 4_000_000_000, 0, 0,
                          set_cpus=True, set_mem=True)
    problems = constrain.restore([a])
    assert len(problems) == 1 and "memory" in problems[0]
    assert calls == [["docker", "update", "--cpus", "4", "cid1"]]   # CPU still restored


def test_constrain_restore_reproduces_implicit_swap_default(monkeypatch):
    # MemorySwap == 0 next to a real Memory limit is docker's "twice memory"
    # default, not "unknown". Restoring swap == memory would hand back a
    # STRICTER config (no swap) than the container started with.
    from rc_repro.perf import constrain
    calls = []
    monkeypatch.setattr(constrain.subprocess, "run", _fake_run(calls))
    monkeypatch.setattr(constrain.runner, "docker_capacity", lambda: (8.0, 8_000_000_000))
    a = constrain.Applied("cid1", "mongodb", 0, 2_147_483_648, 0, set_cpus=False, set_mem=True)
    assert constrain.restore([a]) == []
    assert calls == [["docker", "update", "--memory", "2147483648",
                      "--memory-swap", "4294967296", "cid1"]]
    # an explicit prior swap limit round-trips verbatim; -1 stays unlimited
    for prior_swap, expected in ((3_000_000_000, "3000000000"), (-1, "-1")):
        calls.clear()
        constrain.restore([constrain.Applied("c", "mongodb", 0, 2_147_483_648, prior_swap,
                                             set_cpus=False, set_mem=True)])
        assert calls[0][-2] == expected


def test_constrain_restore_never_raises(monkeypatch):
    # restore() runs inside the callers' finally; an OSError escaping it would
    # skip their remaining cleanup (deleting the seeded-user token file) and mask
    # whatever error triggered the finally. Report it, don't raise.
    from rc_repro.perf import constrain

    def boom(*_a, **_k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(constrain.subprocess, "run", boom)
    monkeypatch.setattr(constrain.runner, "docker_capacity", lambda: (8.0, 8_000_000_000))
    problems = constrain.restore(
        [constrain.Applied("cid1", "rocketchat", 0, 0, 0, set_cpus=True)])
    assert len(problems) == 1 and "rocketchat" in problems[0]


def test_constrain_inspect_limits_bad_output_is_runtime_error(monkeypatch):
    # Every call site catches RuntimeError; a ValueError from int() would escape
    # as a raw traceback (CLI) or an InternalError job (GUI).
    from rc_repro.perf import constrain
    monkeypatch.setattr(constrain.subprocess, "run", _fake_run([], stdout="<no value>\n"))
    try:
        constrain._inspect_limits("cid1")
    except RuntimeError as exc:
        assert "cid1" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for unparseable docker inspect output")


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


def test_verdict_refuses_to_judge_an_unmeasured_run():
    from rc_repro.perf import verdict
    # A run that issued no requests emits no latency keys at all (handleSummary
    # drops undefined values). It used to render as a measured 0ms and end in
    # "the workspace has headroom at this load" -- a health claim from no data.
    zero = {"rps": 0, "count": 0, "error_rate": 0, "checks_rate": None,
            "status": {"2xx": 0, "429": 0, "4xx": 0, "5xx": 0, "other": 0}}
    for summary in (zero, {}):
        v = verdict.analyze(summary)
        assert len(v) == 1
        assert "No requests were recorded" in v[0]
        assert "headroom" not in v[0]


def test_loadtest_markdown_marks_unmeasured_metrics():
    from rc_repro.perf import report
    zero = {"rps": 0, "count": 0, "error_rate": 0, "checks_rate": None}
    ctx = {"name": "r", "version": "8.5.1", "scenario": "messages", "vus": 10,
           "duration": "30s", "ramp": "", "target": "http://rocketchat:3000", "users": 0}
    md = report.loadtest_markdown(ctx, zero, [], None, {})
    assert "| latency p95 | - |" in md          # not "**0ms**"
    assert "| checks passed | - |" in md        # not "100.0%"
    # a measured run is formatted exactly as before
    real = {"rps": 50.0, "count": 100, "p50": 10.0, "p90": 20.0, "p95": 66.0,
            "p99": 88.0, "avg": 12.0, "min": 3.0, "max": 99.0,
            "error_rate": 0.0123, "checks_rate": 1.0}
    md2 = report.loadtest_markdown(ctx, real, [], None, {})
    assert "| latency p95 | **66ms** |" in md2
    assert "| throughput | **50.0 req/s** |" in md2
    assert "| error rate | 1.23% |" in md2 and "| checks passed | 100.0% |" in md2


def test_loadtest_markdown_labels_a_spike_run_as_a_spike():
    from rc_repro.perf import report
    # ctx["vus"] is the value the CLI told the user it was IGNORING under --spike,
    # so reporting it described a load shape that never ran.
    ctx = {"name": "r", "version": "8.5.1", "scenario": "messages", "vus": 10,
           "duration": "60s", "ramp": "", "spike": "10:100",
           "target": "http://rocketchat:3000", "users": 0}
    md = report.loadtest_markdown(ctx, {"p95": 1.0}, [], None, {})
    assert "spike 10:100 VUs for 60s" in md and "10 VUs for 60s" not in md


def test_loadtest_script_budgets_spike_ramps_and_parses_compound_durations():
    # No JS runtime here, so assert on the shipped script -- as the other
    # loadtest-script tests do. The invariants: the two 1s transition ramps are
    # budgeted out of --duration (3*third+2 overshot it, which also skewed
    # timeline.spike_recovery's exact-thirds windows), seconds() understands k6's
    # compound durations (1m30s), and an unrun check set reports null.
    from importlib import resources
    js = resources.files("rc_repro").joinpath("data", "loadtest", "common.js").read_text(
        encoding="utf-8")
    assert "ms|s|m|h" in js
    assert "const body = total - 2;" in js
    assert "rate: null" in js


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
            errors.NotReadyError, errors.DockerError, errors.PreflightError,
            errors.CreateFailedError, errors.AuthorityGateError]
    codes = [c.code for c in subs]
    assert len(set(codes)) == len(codes), "error codes must be unique"
    # every subclass stays a ReproError so existing handlers keep catching it
    assert all(issubclass(c, errors.ReproError) for c in subs)
    exits = [c.exit_code for c in subs]
    # exit 0 is success and must never be an error's code
    assert 0 not in exits
    # every exit code is documented in the published map
    assert all(e in errors.EXIT_CODES for e in exits)
    # PreflightError and DockerError share exit 3 (preflight bucket) by design.
    assert errors.DockerError.exit_code == errors.PreflightError.exit_code == 3
    # Other base classes keep distinct process exits.
    other = [c.exit_code for c in subs
             if c not in (errors.DockerError, errors.PreflightError)]
    assert len(set(other)) == len(other)


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
    for verb in ("up", "ready", "down", "list", "info", "inspect"):
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
        "docker_ready": True, "engine_provider": "podman",
        "engine_memory_gib": 15.6, "engine_cpus": 4,
        "missing_kubernetes_tools": [], "microservices_ready": True,
        "engine_resize_supported": False, "engine_resize_relevant": False,
    })

    # Goal-first: microservices deployment, no scenario, small seed, no retain,
    # grant owned-cluster, review apply.
    first = CliRunner().invoke(app, ["onboard"], input=(
        "microservices\n"   # deployment
        "\n"                # scenario none
        "small\n"           # seed
        "n\n"               # retain_runs
        "y\n"               # owned-cluster
        "y\n"               # apply review
    ))
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
        "docker_ready": True, "engine_provider": "docker",
        "engine_memory_gib": 3.8, "engine_cpus": 2,
        "missing_kubernetes_tools": ["kind", "kubectl", "helm"],
        "microservices_ready": False, "engine_resize_supported": False,
        "engine_resize_relevant": False,
    })

    # Compose path: no Kubernetes authority questions.
    result = CliRunner().invoke(app, ["onboard"], input=(
        "default\n"   # deployment
        "\n"          # scenario
        "none\n"      # seed
        "n\n"         # retain
        "y\n"         # apply
    ))

    assert result.exit_code == 0, result.output
    assert "Kubernetes authority" in result.output or "skipped" in result.output
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
    monkeypatch.setattr(runner, "docker_server_platform", lambda: "Docker Engine - Community")
    monkeypatch.setattr(runner, "docker_server_components", lambda: ("Engine",))
    monkeypatch.setattr(runner, "docker_endpoint", lambda: "unix:///var/run/docker.sock")
    monkeypatch.setattr(runner, "docker_kernel_version", lambda: "6.18.0")
    monkeypatch.setattr(k8s, "engine_capacity", lambda: (15.6, 4))
    monkeypatch.setattr(k8s, "engine_resize_supported", lambda: False)

    detected = onboarding.detect_environment()

    assert detected["tools"]["docker"] == "Docker version 29.6.1"
    assert detected["tools"]["compose"] == "Docker Compose version v5.3.1"
    assert detected["engine_provider"] == "docker"


def test_runner_reads_active_server_component_names(monkeypatch):
    from rc_repro import runner
    monkeypatch.setattr(
        runner, "_first_line",
        lambda _cmd: '[{"Name":"Podman Engine"},{"Name":"Conmon"}]')

    assert runner.docker_server_components() == ("Podman Engine", "Conmon")


def test_environment_detection_recognises_active_podman_socket(tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(onboarding, "_version_line", lambda tool, *args: f"{tool} ok")
    monkeypatch.setattr(runner, "docker_cli_version", lambda: "Docker version 29.6.1")
    monkeypatch.setattr(runner, "compose_version_line", lambda: "Docker Compose version v5.3.1")
    monkeypatch.setattr(runner, "docker_available", lambda: True)
    monkeypatch.setattr(
        runner, "docker_server_platform", lambda: "linux/arm64/fedora-43")
    monkeypatch.setattr(runner, "docker_server_components", lambda: ("Engine",))
    monkeypatch.setattr(
        runner, "docker_endpoint",
        lambda: "unix:///Users/test/.local/share/containers/podman/machine/podman.sock")
    monkeypatch.setattr(runner, "docker_kernel_version", lambda: "6.18.0")
    monkeypatch.setattr(k8s, "engine_capacity", lambda: (15.6, 4))
    monkeypatch.setattr(k8s, "engine_resize_supported", lambda: True)

    detected = onboarding.detect_environment()

    assert detected["engine_provider"] == "podman"


def test_environment_detection_recognises_podman_server_component(
        tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import k8s, onboarding
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(onboarding.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(onboarding, "_version_line", lambda tool, *args: f"{tool} ok")
    monkeypatch.setattr(runner, "docker_cli_version", lambda: "Docker version 29.7.1")
    monkeypatch.setattr(runner, "compose_version_line", lambda: "Docker Compose version 5.3.1")
    monkeypatch.setattr(runner, "docker_available", lambda: True)
    monkeypatch.setattr(
        runner, "docker_server_platform", lambda: "linux/arm64/fedora-43")
    monkeypatch.setattr(
        runner, "docker_server_components", lambda: ("Podman Engine", "Conmon"))
    monkeypatch.setattr(
        runner, "docker_endpoint", lambda: "unix:///var/run/docker.sock")
    monkeypatch.setattr(runner, "docker_kernel_version", lambda: "6.19.7-200.fc43")
    monkeypatch.setattr(k8s, "engine_capacity", lambda: (5.8, 5))
    monkeypatch.setattr(k8s, "engine_resize_supported", lambda: True)

    detected = onboarding.detect_environment()

    assert detected["engine_provider"] == "podman"
    assert detected["engine_kernel_version"] == "6.19.7-200.fc43"


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
    monkeypatch.setattr(runner, "docker_server_platform", lambda: "Podman Engine")
    monkeypatch.setattr(runner, "docker_server_components", lambda: ("Podman Engine",))
    monkeypatch.setattr(runner, "docker_endpoint", lambda: "unix:///var/run/docker.sock")
    monkeypatch.setattr(runner, "docker_kernel_version", lambda: "6.18.0")
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


def test_seed_restores_only_the_2fa_value_it_changed(monkeypatch):
    # "Unreadable" is not "was on". Restoring ON for both turned email-2FA on for
    # the ldap/saml/oidc/livechat presets, which switch it OFF on purpose.
    prior = [None]
    calls = []

    def fake_get(_url, _auth, _pw, sid, **_kw):
        return {"Accounts_TwoFactorAuthentication_By_Email_Enabled": prior[0],
                "API_Enable_Rate_Limiter": False}.get(sid)

    def fake_set(_url, _auth, _pw, sid, value, **_kw):
        calls.append((sid, value))
        return True

    monkeypatch.setattr(seed.rcapi, "get_setting", fake_get)
    monkeypatch.setattr(seed.rcapi, "set_setting", fake_set)
    monkeypatch.setattr(seed, "_seed_body", lambda *_a, **_k: {"ok": True})
    twofa = "Accounts_TwoFactorAuthentication_By_Email_Enabled"
    auth = seed.rcapi.Auth(token="t", user_id="u")

    cases = [
        (None, []),                                 # unreadable -> don't touch it
        (False, []),                                # already off -> leave it
        (True, [(twofa, False), (twofa, True)]),     # on -> disable, then restore
    ]
    for prior_value, expected in cases:
        prior[0] = prior_value
        calls.clear()
        seed.seed("http://x", auth, seed.PROFILES["small"])
        assert [c for c in calls if c[0] == twofa] == expected, prior_value


def test_seed_does_not_count_failed_dms():
    class _Resp:
        def __init__(self, ok):
            self.ok, self.status_code = ok, 200 if ok else 400

        def json(self):
            return {"message": {"_id": "m1"}}

    # every im.create is rejected (revoked create-d permission, DM max users)
    def post(path, _headers, _payload):
        return _Resp(not path.endswith("im.create"))

    plan = seed.Plan(users=3, channels=1, messages=1, dms=3, rich=False)
    out = seed._seed_body("http://x", {"h": "1"}, plan, post, lambda _m: None)
    # was 3: post() returns None only on a TRANSPORT error, so a 400 still counted
    # and the number reached the benchmark report as workload never created.
    assert out["dms"] == 0


def test_scaleseed_does_not_rerun_a_partially_applied_script(monkeypatch):
    from rc_repro import scaleseed
    shells = []

    def killed_midway(_name, _service, args, **_kw):
        shells.append(args[0])
        return 137, '{"inserted": 400000}'      # OOM-killed after committing batches

    monkeypatch.setattr(scaleseed.runner, "compose_exec_capture", killed_midway)
    rc, _out = scaleseed._eval("x", "print(1)")
    # Falling through to `mongo` would re-run a non-idempotent insertMany and
    # duplicate every batch already written.
    assert rc == 137 and shells == ["mongosh"]

    shells.clear()

    def binary_missing(_name, _service, args, **_kw):
        shells.append(args[0])
        return 127, ""                          # never started, no output

    monkeypatch.setattr(scaleseed.runner, "compose_exec_capture", binary_missing)
    scaleseed._eval("x", "print(1)")
    assert shells == ["mongosh", "mongo"]       # the legacy fallback still applies


def test_parse_scale_rejects_ambiguous_and_oversized_specs():
    from rc_repro import scaleseed
    for bad in ("users=10@team-chat",             # a room means nothing for users
                "messages=10@a,messages=20@b",    # silently last-wins before
                "users=1,users=2",
                f"users={scaleseed._MAX_DOCS + 1}"):
        try:
            scaleseed.parse_scale(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")
    assert scaleseed.parse_scale("users=10,messages=20@general") == {
        "users": 10, "messages": (20, "general")}


def test_configimport_redaction_requires_a_single_mask_alphabet():
    from rc_repro import configimport
    for masked in ("XXXXXXXX", "xxxx", "****", "######", "●●●●●●", "····"):
        assert configimport._is_redacted(masked), masked
    # A character class matched any MIXTURE, and "." inside [...] is a literal
    # dot -- so real dotted/mixed values were dropped from the import while being
    # reported to the operator as "the dump masked this secret".
    for real in ("....", "*.*.*.*", "x.x.x.x", "#.#.#", "Xx.#*", "1.2.3.4",
                 "a.b.c.d", "XXX"):
        assert not configimport._is_redacted(real), real


def test_login_refuses_an_unfiltered_otp_poll(monkeypatch):
    from rc_repro import rcapi

    class _R:
        status_code, ok = 401, False
        text = '{"error":"totp-required"}'

        def json(self):
            return {}

    monkeypatch.setattr(rcapi.requests, "post", lambda *_a, **_k: _R())
    # Mailpit is a catch-all inbox; with no resolvable recipient the old code
    # polled unfiltered and could hand back a DIFFERENT user's code.
    try:
        rcapi.login("http://x", "alice", "alice", mailpit_url="http://mailpit")
    except RuntimeError as exc:
        assert "another user's code" in str(exc)
    else:
        raise AssertionError("expected a refusal rather than an unfiltered poll")


def test_login_rejects_a_non_200_success_status(monkeypatch):
    from rc_repro import rcapi

    class _R:
        status_code, ok = 302, True             # proxy redirect to HTTPS
        text = "<html>Found</html>"

        def json(self):
            return {}

        def raise_for_status(self):
            return None                          # no-op for 3xx, as requests does

    monkeypatch.setattr(rcapi.requests, "post", lambda *_a, **_k: _R())
    try:
        rcapi.login("http://x")
    except RuntimeError as exc:
        assert "302" in str(exc) and "2FA" in str(exc)
    else:
        raise AssertionError("expected a clear error, not the misleading 2FA path")


def _spec_for(pre):
    """A Spec around a hand-built Preset (the shared _spec() takes a preset NAME)."""
    r = versions.resolve("8.4.1", offline=True)
    return compose.Spec.from_resolved(
        r, project_name="rcrepro-t", root_url="http://localhost:3000",
        host_port=3000, reg_token=None, preset=pre)


def test_compose_entry_service_must_exist():
    from rc_repro.presets import Preset
    # Silently skipping a bad entry_service meant NOTHING published the host port:
    # the repro booted unreachable at its own advertised root_url.
    pre = Preset(name="broken", entry_service="nope", services={"real": {"image": "x"}})
    try:
        compose.build(_spec_for(pre))
    except ValueError as exc:
        assert "entry_service" in str(exc) and "nope" in str(exc)
    else:
        raise AssertionError("expected ValueError for an undefined entry_service")


def test_compose_entry_service_takes_the_host_port_from_rc():
    from rc_repro.presets import Preset
    # A single-instance preset with an entry_service: RC must give up the published
    # host port, or it is bound twice and `up` fails "port is already allocated".
    pre = Preset(name="lb", entry_service="proxy", services={"proxy": {"image": "x"}})
    doc = compose.build(_spec_for(pre))
    assert doc["services"]["proxy"]["ports"] == ["127.0.0.1:3000:80"]
    assert "ports" not in doc["services"]["rocketchat"]

    # multi-instance keeps its direct per-instance ports (host_port + i)
    multi = Preset(name="lb2", entry_service="proxy", instances=2,
                   services={"proxy": {"image": "x"}})
    doc2 = compose.build(_spec_for(multi))
    assert doc2["services"]["proxy"]["ports"] == ["127.0.0.1:3000:80"]
    assert doc2["services"]["rocketchat-1"]["ports"] == ["127.0.0.1:3001:3000"]
    assert doc2["services"]["rocketchat-2"]["ports"] == ["127.0.0.1:3002:3000"]


def test_bind_ports_handles_a_bare_container_port():
    doc = {"services": {
        "a": {"ports": ["8025:8025"]},           # host:container
        "b": {"ports": ["127.0.0.1:9000:9000"]},  # already IP-qualified
        "c": {"ports": ["8025"]},                 # BARE container port
        "d": {"ports": ["1.2.3.4:5:6"]},
    }}
    compose._bind_ports(doc, "127.0.0.1")
    assert doc["services"]["a"]["ports"] == ["127.0.0.1:8025:8025"]
    assert doc["services"]["b"]["ports"] == ["127.0.0.1:9000:9000"]   # untouched
    # "127.0.0.1:8025" would be read as host:container with an invalid host port;
    # IP::CONTAINER keeps the loopback guarantee with an ephemeral host port.
    assert doc["services"]["c"]["ports"] == ["127.0.0.1::8025"]
    assert doc["services"]["d"]["ports"] == ["1.2.3.4:5:6"]


def test_read_meta_tolerates_a_field_from_a_newer_version(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    ws = tmp_path / "repros" / "r"
    ws.mkdir(parents=True)
    (ws / "docker-compose.yml").write_text("name: rcrepro-r\n", encoding="utf-8")
    blob = {
        "name": "r", "project": "rcrepro-r", "rc_version": "8.5.1", "rc_image": "i",
        "mongo_tag": "8.0", "mongo_flavor": "official", "preset": "default",
        "root_url": "http://localhost:3000", "host_port": 3000,
        "version_source": "map (fallback)",
        "a_field_from_the_future": {"nested": True},   # written by a newer rc-repro
    }
    (ws / "repro.json").write_text(json.dumps(blob), encoding="utf-8")
    # Metadata(**blob) used to TypeError, and list_meta swallows that -- silently
    # dropping the repro from `rc-repro list`.
    m = runner.read_meta("r")
    assert m.name == "r" and m.host_port == 3000
    assert [x.name for x in runner.list_meta()] == ["r"]


def test_update_config_is_a_locked_read_modify_write(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    monkeypatch.setenv("RC_REPRO_REG_TOKEN", "ephemeral-secret")
    config.update_config(lambda cfg: cfg.__setitem__("default_repro", "a"))
    config.update_config(lambda cfg: cfg.__setitem__("other", 1))
    raw = config.config_file().read_text(encoding="utf-8")
    # both updates survive (a read/write pair could lose one), and the env-only
    # token is never persisted into the file
    assert config.load_config(with_env=False) == {"default_repro": "a", "other": 1}
    assert "ephemeral-secret" not in raw


def test_resolve_name_rejects_a_name_that_is_not_a_repro(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    from rc_repro import errors
    from rc_repro.services import lifecycle as lcsvc
    # The name becomes a filesystem path and a compose project name; sanitize()
    # only runs at creation, so every other entry point validates the shape.
    for bad in ("../../etc", "..", "Has-Caps", "with space", "semi;colon"):
        try:
            lcsvc.resolve_name(bad)
        except errors.ValidationError:
            continue
        raise AssertionError(f"expected ValidationError for {bad!r}")


def test_detail_redacts_secret_env_values():
    from rc_repro.services import lifecycle as lcsvc
    # The env tab is served to any client holding the session token and used to
    # carry these verbatim.
    for key in ("REG_TOKEN", "ADMIN_PASS",
                "OVERWRITE_SETTING_LDAP_Authentication_Password",
                "OVERWRITE_SETTING_FileUpload_S3_AWSSecretAccessKey",
                "MINIO_ROOT_PASSWORD"):
        assert lcsvc.redact_env(key, "s3cret") == lcsvc.REDACTED, key
    # non-secrets stay visible -- debugging them is the point of the tab
    for key, val in (("ROOT_URL", "http://localhost:3000"),
                     ("MONGO_URL", "mongodb://mongodb:27017/rocketchat"),
                     ("DEPLOY_METHOD", "docker")):
        assert lcsvc.redact_env(key, val) == val, key
    assert lcsvc.redact_env("REG_TOKEN", "") == ""      # empty stays empty


def test_k6_keeps_secrets_out_of_the_argv(tmp_path, monkeypatch):
    import types
    from rc_repro.perf import k6
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    ws = tmp_path / "repros" / "r"
    ws.mkdir(parents=True)
    captured = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        (ws / "loadtest" / "summary.json").write_text(json.dumps({"rps": 1}), encoding="utf-8")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(k6.subprocess, "run", fake_run)
    k6.run("r", "messages", vus=1, duration="1s", ramp=None,
           token="PAT-SECRET-VALUE", uid="u1", target="http://rocketchat:3000")
    argv = " ".join(captured["cmd"])
    # `ps` / /proc/<pid>/cmdline expose the argv to every local user for the whole
    # run, and `docker inspect` afterwards.
    assert "PAT-SECRET-VALUE" not in argv
    assert config.ADMIN_PASSWORD not in argv
    assert "--env-file" in captured["cmd"]
    # non-secret env still rides the argv, and the secret file does not outlive the run
    assert "RC_URL=http://rocketchat:3000" in captured["cmd"]
    assert not (ws / "loadtest" / "k6.env").exists()


# --- web UI static assets -----------------------------------------------------

def test_webui_hidden_toggles_actually_hide():
    """`el.hidden = true` must really hide, and must target an id that exists.

    The UA stylesheet's `[hidden] { display: none }` loses to any author rule
    setting `display` on the same element, and `form label`/`.checks label` both
    do. Without an explicit `!important` override every hidden-toggle on a label
    is a silent no-op -- the seed dialog shipped showing "Profile" and "Scale
    spec" simultaneously in both modes because of exactly this.
    """
    import re
    from importlib import resources
    webui = resources.files("rc_repro").joinpath("data", "webui")
    css = webui.joinpath("app.css").read_text(encoding="utf-8")
    js = webui.joinpath("app.js").read_text(encoding="utf-8")
    html = webui.joinpath("index.html").read_text(encoding="utf-8")

    assert re.search(r"\[hidden\][^{]*\{[^}]*display:\s*none\s*!important", css), \
        "app.css must force `[hidden] { display: none !important }`"

    toggled = set(re.findall(r'\$\("#([^"]+)"\)\.hidden\s*=', js))
    assert toggled, "expected app.js to toggle .hidden on at least one element"
    missing = sorted(toggled - set(re.findall(r'id="([^"]+)"', html)))
    assert not missing, f"app.js toggles .hidden on id(s) not in index.html: {missing}"


def test_webui_busy_state_is_styled_and_labelled():
    """Every action that shows a spinner must have a verb, and the classes app.js
    emits for it must exist in app.css.

    The busy state is assembled from three places -- the label passed to
    runAction, the BUSY_VERB lookup, and the .spin/.working CSS -- so a rename in
    any one of them silently degrades to a frozen, unstyled button.
    """
    import re
    from importlib import resources
    webui = resources.files("rc_repro").joinpath("data", "webui")
    css = webui.joinpath("app.css").read_text(encoding="utf-8")
    js = webui.joinpath("app.js").read_text(encoding="utf-8")

    # Keys may be bare (Stop:) or quoted ("Make default":) -- a label with a space
    # has to be quoted, and reading only bare keys made such an entry invisible
    # here, so the check passed while the button still said "Make default…".
    verbs = {bare or quoted for quoted, bare in
             re.findall(r"(?:\"([^\"]+)\"|(\w+))\s*:\s*\"\w+ing\b",
                        js[js.index("const BUSY_VERB"):].split("}")[0])}
    assert verbs, "expected a BUSY_VERB map in app.js"

    # Labels handed to runAction must resolve to a verb, else the button shows the
    # bare label ("Stop…") instead of "Stopping…".
    literal = set(re.findall(r'runAction\([^,]+,\s*"([^"]+)"', js))
    state = set(re.findall(r'\b\w+:\s*"(\w+)"', js[js.index("const STATE_LABEL"):]
                           .split("}")[0]))
    missing = sorted((literal | state) - verbs)
    assert not missing, f"action label(s) with no BUSY_VERB entry: {missing}"

    for cls, sel in [("spin", r"\.spin\s*\{"), ("btn working", r"\.btn\.working"),
                     ("pill working", r"\.pill\.working")]:
        assert re.search(sel, css), f"app.js emits class {cls!r} with no rule in app.css"
    assert re.search(r"@keyframes\s+rc-spin", css), "the .spin animation is undefined"
    # A spinner that cannot animate must still say what is happening.
    assert "prefers-reduced-motion" in css


def test_webui_handles_every_state_the_backend_can_report():
    """Each state repro_state() can return must be styled and filterable.

    repro_state() stopped flattening docker's transitional states, so they now
    reach the dashboard for real. A state the UI does not know about renders with
    no colour and lands in renderDetail's fallback branch -- which did not exist
    until these became reachable.
    """
    import re
    from importlib import resources
    from rc_repro.services import lifecycle as lc
    webui = resources.files("rc_repro").joinpath("data", "webui")
    css = webui.joinpath("app.css").read_text(encoding="utf-8")
    html = webui.joinpath("index.html").read_text(encoding="utf-8")

    for state in ("running", "stopped", "down", "unknown") + lc.TRANSIENT_STATES:
        assert re.search(rf"\.card\.st-{state}\b", css), f"no card accent for state {state!r}"
        assert re.search(rf"\.pill\.{state}\b", css), f"no pill colour for state {state!r}"
    # …and each real one is reachable from the status filter.
    for state in lc.TRANSIENT_STATES:
        assert f'value="{state}"' in html, f"status filter cannot select {state!r}"


# --- HTTPS add-on (--https) ---------------------------------------------------


def _tls_spec(**kw):
    from rc_repro import tls
    base = dict(mode=tls.MODE_LOCAL, host="x.rcrepro.localhost", port=8443)
    base.update(kw)
    return tls.TlsSpec(**base)


def test_tls_root_url_omits_an_implicit_443():
    """A real domain answers on 443, so the URL must not carry it.

    RC advertises ROOT_URL verbatim; "https://host:443" in an OAuth callback or a
    mobile workspace URL is a mismatch against the same host without the port.
    """
    from rc_repro import tls
    assert _tls_spec(port=8443).root_url == "https://x.rcrepro.localhost:8443"
    assert _tls_spec(mode=tls.MODE_ACME, host="rc1.example.com", port=443).root_url \
        == "https://rc1.example.com"


def test_local_ca_is_created_once_and_leaf_carries_the_right_sans(monkeypatch, tmp_path):
    """The CA is reused, and the leaf gets SANs -- not just a CN.

    Browsers have ignored commonName since Chrome 58, so a cert with only a CN is
    rejected outright (ERR_CERT_COMMON_NAME_INVALID).
    """
    import subprocess
    from rc_repro import tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    key, crt = tls.ensure_ca()
    assert key.exists() and crt.exists()
    assert oct(key.stat().st_mode)[-3:] == "600", "the CA key must not be world-readable"
    before = crt.read_bytes()
    assert tls.ensure_ca() == (key, crt)
    assert crt.read_bytes() == before, "ensure_ca must not regenerate an existing CA"

    cert_pem, key_pem = tls.issue_leaf("x.rcrepro.localhost", ["192.168.1.42"])
    assert "BEGIN CERTIFICATE" in cert_pem and "PRIVATE KEY" in key_pem
    leaf = tmp_path / "leaf.crt"
    leaf.write_text(cert_pem, encoding="utf-8")
    text = subprocess.run(["openssl", "x509", "-in", str(leaf), "-noout", "-text"],
                          capture_output=True, text=True).stdout
    assert "DNS:x.rcrepro.localhost" in text
    assert "DNS:localhost" in text            # keeps the existing http habits working
    assert "IP Address:192.168.1.42" in text  # a LAN IP, which Let's Encrypt cannot issue
    assert "IP Address:127.0.0.1" in text
    # And it must actually chain to the CA, not merely parse.
    v = subprocess.run(["openssl", "verify", "-CAfile", str(crt), str(leaf)],
                       capture_output=True, text=True)
    assert v.returncode == 0, v.stdout + v.stderr


def test_https_adds_traefik_and_leaves_rocketchats_own_port_published():
    """RC keeps its http port so rc-repro's own API calls need no CA.

    login/PAT/seed/loadtest all use meta.root_url in 70+ places; pointing those at
    a locally-signed https URL would fail verification in every one of them.
    """
    pre = presets.load("default")
    res = versions.resolve("8.6.1", offline=True)
    st = _tls_spec()
    spec = compose.Spec.from_resolved(
        res, project_name="rcrepro-x", root_url=st.root_url, host_port=3000,
        reg_token=None, preset=pre, tls=st)
    doc = compose.build(spec)

    assert "traefik" in doc["services"]
    t = doc["services"]["traefik"]
    assert t["ports"] == ["127.0.0.1:8443:443"]
    assert t["depends_on"] == ["rocketchat"], "must not route before RC exists"
    # RC advertises https, but still publishes its own plain port.
    assert doc["services"]["rocketchat"]["environment"]["ROOT_URL"] == st.root_url
    assert doc["services"]["rocketchat"]["ports"] == ["127.0.0.1:3000:3000"]


def test_https_refuses_to_layer_onto_a_preset_that_already_runs_traefik():
    """multi-instance owns its own Traefik; two would fight over the entrypoint.

    Silently merging produced a repro that booted and served nothing, with the
    reason only in `docker compose logs traefik`.
    """
    import pytest
    pre = presets.load("multi-instance", {"instances": "2"})
    res = versions.resolve("8.6.1", offline=True)
    spec = compose.Spec.from_resolved(
        res, project_name="rcrepro-x", root_url="https://x", host_port=3000,
        reg_token=None, preset=pre, tls=_tls_spec())
    with pytest.raises(ValueError, match="already runs"):
        compose.build(spec)


def test_acme_flags_map_to_traefik_resolver_args():
    from rc_repro import tls
    prod = tls.service(_tls_spec(mode=tls.MODE_ACME, host="rc1.example.com", port=443,
                                 acme_email="ops@example.com"))
    cmd = " ".join(prod["command"])
    assert "--certificatesresolvers.le.acme.email=ops@example.com" in cmd
    assert "acme.tlschallenge=true" in cmd, "default challenge needs only :443"
    assert "caserver" not in cmd, "production must not point at the staging directory"
    # acme.json lives outside the workspace, so `down --volumes` cannot force a
    # re-issue (5 certs per identical hostname per 7 days).
    assert any(str(tls.acme_dir()) in v for v in prod["volumes"])

    stg = tls.service(_tls_spec(mode=tls.MODE_ACME, host="rc1.example.com", port=443,
                                acme_email="a@b.c", acme_staging=True))
    assert "acme-staging-v02" in " ".join(stg["command"])

    # :80 and the permanent redirect to https, matching the official
    # rocketchat-compose Traefik files. Decided by the caller (it has to probe the
    # port), so the builder just honours the flag.
    plain = tls.service(_tls_spec(mode=tls.MODE_ACME, host="h", port=443,
                                  acme_email="a@b.c"))
    assert "--entryPoints.web.address=:80" not in plain["command"]
    assert "80:80" not in plain["ports"]

    redir = tls.service(_tls_spec(mode=tls.MODE_ACME, host="h", port=443,
                                  acme_email="a@b.c", http_redirect=True))
    cmd = redir["command"]
    assert "--entryPoints.web.address=:80" in cmd
    assert "--entryPoints.web.http.redirections.entryPoint.to=websecure" in cmd
    assert "--entryPoints.web.http.redirections.entryPoint.scheme=https" in cmd
    assert "--entryPoints.web.http.redirections.entryPoint.permanent=true" in cmd
    assert "80:80" in redir["ports"]

    # Local mode is on an allocated port, so a redirect does not apply at all.
    assert tls.can_redirect_http(tls.MODE_LOCAL, 8443) is False
    assert tls.can_redirect_http(tls.MODE_ACME, 443) is True
    assert tls.can_redirect_http(tls.MODE_OWN, 443) is True


def test_dynamic_config_uses_a_static_pair_locally_and_a_resolver_for_acme():
    from rc_repro import tls
    local = dict(tls.files(_tls_spec(), ["rocketchat"], "CERT", "KEY"))
    assert local["tls/certs/tls.crt"] == "CERT" and local["tls/certs/tls.key"] == "KEY"
    assert "certFile: /etc/traefik/certs/tls.crt" in local["tls/dynamic.yml"]
    assert "certResolver" not in local["tls/dynamic.yml"]

    acme = dict(tls.files(_tls_spec(mode=tls.MODE_ACME, host="h", port=443), ["rocketchat"]))
    assert "tls/certs/tls.crt" not in acme, "ACME certs come from Traefik, not the workspace"
    assert "certResolver: le" in acme["tls/dynamic.yml"]
    # The DDP websocket must not be bounced between instances mid-session.
    assert "sticky" in acme["tls/dynamic.yml"] and "secure: true" in acme["tls/dynamic.yml"]


def test_own_cert_is_validated_before_traefik_would_silently_fail(tmp_path):
    import pytest
    from rc_repro import tls
    from rc_repro.errors import ValidationError
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    with pytest.raises(ValidationError, match="no such file"):
        tls.read_own_cert(str(cert), str(key))
    cert.write_text("not a cert", encoding="utf-8")
    key.write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    with pytest.raises(ValidationError, match="not a PEM certificate"):
        tls.read_own_cert(str(cert), str(key))
    cert.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
    key.write_text("nope", encoding="utf-8")
    with pytest.raises(ValidationError, match="not a PEM private key"):
        tls.read_own_cert(str(cert), str(key))


def test_metadata_external_url_prefers_https_and_survives_an_old_repro_json(tmp_path, monkeypatch):
    """public_url is additive: a repro.json written before it must still load."""
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    m = runner.Metadata(
        name="x", project="rcrepro-x", rc_version="8.6.1", rc_image="i", mongo_tag="8.0",
        mongo_flavor="official", preset="default", root_url="http://localhost:3000",
        host_port=3000, version_source="map")
    assert m.external_url == "http://localhost:3000"       # no TLS -> unchanged
    m.public_url = "https://x.rcrepro.localhost:8443"
    assert m.external_url == "https://x.rcrepro.localhost:8443"

    runner.write("x", "services: {}\n", m)
    (runner.workspace("x") / "repro.json").write_text(
        json.dumps({"name": "x", "project": "rcrepro-x", "rc_version": "8.6.1",
                    "rc_image": "i", "mongo_tag": "8.0", "mongo_flavor": "official",
                    "preset": "default", "root_url": "http://localhost:3000",
                    "host_port": 3000, "version_source": "map"}), encoding="utf-8")
    old = runner.read_meta("x")
    assert old.public_url == "" and old.external_url == "http://localhost:3000"


def test_privileged_port_is_not_reported_busy_just_because_we_cannot_bind_it(monkeypatch):
    """443 is publishable even though an unprivileged process cannot bind it.

    The docker daemon runs as root, so `ports: 443:443` works fine. bind() as the
    calling user raises EACCES, and treating that as "in use" made
    `up --https --domain ...` refuse 443 on every non-root machine with nothing
    listening on it at all.
    """
    import socket
    from rc_repro import runner

    real_socket = socket.socket

    class FakeSock:
        """Nothing is listening; bind() fails the way a privileged port does."""
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, _t): pass
        def setsockopt(self, *a): pass
        def connect_ex(self, _addr): return 111          # ECONNREFUSED: no listener
        def bind(self, addr):
            if addr[1] < 1024:
                raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(socket, "socket", FakeSock)
    assert runner.port_free(443) is True
    assert runner.port_free(8443) is True

    # A real listener must still register as busy — the connect() probe catches it.
    class Listening(FakeSock):
        def connect_ex(self, _addr): return 0
    monkeypatch.setattr(socket, "socket", Listening)
    assert runner.port_free(443) is False
    assert runner.port_free(8443) is False

    # And a genuine EADDRINUSE on an unprivileged port is still busy.
    class InUse(FakeSock):
        def bind(self, addr): raise OSError(98, "Address already in use")
    monkeypatch.setattr(socket, "socket", InUse)
    assert runner.port_free(8443) is False
    socket.socket = real_socket


def test_staging_notes_never_claim_the_certificate_is_trusted():
    """A Let's Encrypt STAGING root is in no trust store — that is the point.

    The notes said "Publicly trusted, so the mobile app accepts it with nothing to
    install" for staging too, which sent people hunting for a broken workspace when
    the browser warning was the correct, expected outcome.
    """
    from rc_repro import tls
    spec = tls.TlsSpec(mode=tls.MODE_ACME, host="rc1.example.com", port=443,
                       acme_email="a@b.c", acme_staging=True)
    text = " ".join(tls.notes(spec, "rc1")).lower()
    assert "staging" in text
    assert "not trusted" in text, "staging must say plainly that it is untrusted"
    assert "publicly trusted" not in text
    assert "success signal" in text, "a warning on staging is expected, not a failure"
    # And it must say how to get a real one.
    assert "without --acme-staging" in text
    # The name is threaded in, so the hint is copy-pasteable.
    assert "tls-status --name rc1" in " ".join(tls.notes(spec, "rc1"))

    prod = tls.TlsSpec(mode=tls.MODE_ACME, host="rc1.example.com", port=443,
                       acme_email="a@b.c")
    assert "publicly trusted" in " ".join(tls.notes(prod, "rc1")).lower()


def test_dns_preflight_rules_out_what_acme_cannot_possibly_reach(monkeypatch):
    """Catch the certain-to-fail cases before an attempt spends quota.

    Let's Encrypt allows 5 failed validations per hostname per hour, so a
    misconfiguration that is knowable up front must not cost one.
    """
    from rc_repro import tls
    monkeypatch.setattr(tls, "resolves_to", lambda h: [])
    ok, msg = tls.dns_preflight("nope.example.com")
    assert not ok and "does not resolve" in msg

    monkeypatch.setattr(tls, "resolves_to", lambda h: ["127.0.0.1"])
    ok, msg = tls.dns_preflight("local.example.com")
    assert not ok and "cannot reach" in msg

    monkeypatch.setattr(tls, "resolves_to", lambda h: ["192.168.1.5", "10.0.0.4"])
    ok, _ = tls.dns_preflight("lan.example.com")
    assert not ok, "private-only must fail: ACME connects from the public internet"

    # RFC 5737 documentation ranges (203.0.113.0/24 etc.) count as private to
    # Python, and rightly so -- nobody serves a workspace from one. Use a real
    # routable address here.
    monkeypatch.setattr(tls, "resolves_to", lambda h: ["93.184.216.34"])
    ok, msg = tls.dns_preflight("rc1.example.com")
    assert ok and "93.184.216.34" in msg
    monkeypatch.setattr(tls, "resolves_to", lambda h: ["192.168.1.5", "93.184.216.34"])
    assert tls.dns_preflight("rc1.example.com")[0]


def test_verify_reads_a_real_endpoint_and_separates_the_two_trust_questions(tmp_path, monkeypatch):
    """verify() must report an untrusted-but-working endpoint as working.

    Staging and local-CA certs are untrusted by design; refusing to look at them
    would report "broken" for a setup behaving exactly as intended. And `trusted`
    (system store) must stay distinct from `trusted_via_ca` (chains to a given CA),
    else local mode always claims trust and hides whether trust-ca has run.
    """
    import socket as _socket
    import ssl
    import threading
    from rc_repro import tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    _, ca_crt = tls.ensure_ca()
    cert_pem, key_pem = tls.issue_leaf("localhost")
    cf, kf = tmp_path / "s.crt", tmp_path / "s.key"
    cf.write_text(cert_pem, encoding="utf-8")
    kf.write_text(key_pem, encoding="utf-8")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cf), str(kf))
    srv = _socket.socket()
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except (TimeoutError, OSError):
                continue
            try:
                with ctx.wrap_socket(c, server_side=True):
                    pass
            except OSError:
                pass
            finally:
                c.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        r = tls.verify("localhost", port, timeout=5, cafile=str(ca_crt))
        assert r["serving"] is True and not r["error"]
        assert "rc-repro local CA" in r["issuer"]
        assert "localhost" in r["subject"]
        assert r["fallback"] is False
        assert r["trusted_via_ca"] is True, "must chain to the CA it was handed"
        assert r["trusted"] is False, "the system store does not know this CA"
    finally:
        stop.set()
        srv.close()
        t.join(timeout=3)

    # Nothing listening -> reported, not raised.
    dead = tls.verify("127.0.0.1", port, timeout=2, cafile=str(ca_crt))
    assert dead["serving"] is False and dead["error"]


def test_dns_preflight_falls_back_to_public_resolvers(monkeypatch):
    """The question is "what will Let's Encrypt see?", not "what does /etc/resolv.conf say?".

    A lab or corporate resolver with a stale negative cache reported a perfectly
    good public record as absent, and the hard failure blocked a valid setup.
    """
    import socket
    from rc_repro import tls

    def no_local_answer(*a, **k):
        raise OSError("resolver has nothing for this name")

    monkeypatch.setattr(socket, "getaddrinfo", no_local_answer)
    monkeypatch.setattr(tls, "_dig",
                        lambda h, r: ["104.21.71.43"] if r == "1.1.1.1" else [])
    assert tls.resolves_to("chatrepo.example.org") == ["104.21.71.43"]
    # public=False is the "only what this machine sees" variant.
    assert tls.resolves_to("chatrepo.example.org", public=False) == []


def test_preflight_refuses_an_inbound_challenge_behind_a_tls_terminating_proxy(monkeypatch):
    """An orange-clouded record cannot pass tlsalpn, and the error says why.

    Cloudflare terminates TLS at its edge, so Let's Encrypt validates against
    Cloudflare's certificate and never reaches this host. dns-01 is unaffected: it
    only reads a TXT record.
    """
    from rc_repro import tls
    monkeypatch.setattr(tls, "resolves_to", lambda h, public=True: ["104.21.71.43", "172.67.143.25"])
    ok, msg = tls.dns_preflight("chatrepo.example.org", "tlsalpn")
    assert not ok
    assert "Cloudflare" in msg and "--acme-challenge dns" in msg
    assert "CF_DNS_API_TOKEN" in msg, "must say where the token goes"
    ok, msg = tls.dns_preflight("chatrepo.example.org", "dns")
    assert ok and "no inbound access" in msg

    # A normal public origin still passes the inbound challenges.
    monkeypatch.setattr(tls, "resolves_to", lambda h, public=True: ["93.184.216.34"])
    assert tls.dns_preflight("rc1.example.org", "tlsalpn")[0]


def test_dns_challenge_ignores_where_the_name_points(monkeypatch):
    """dns-01 needs no inbound reachability, so private/loopback answers are fine."""
    from rc_repro import tls
    monkeypatch.setattr(tls, "resolves_to", lambda h, public=True: ["10.0.0.5"])
    assert tls.dns_preflight("internal.example.org", "dns")[0]
    # ...but an inbound challenge to the same name cannot work.
    assert not tls.dns_preflight("internal.example.org", "tlsalpn")[0]


def test_staging_and_production_use_separate_acme_storage():
    """Traefik keys stored certs by RESOLVER name, not by CA server.

    Pointing one resolver at staging and then at production leaves the staging
    certificate in storage and keeps serving it -- the well-known "delete
    acme.json when you switch" trap. Separate files make the switch real.
    """
    from rc_repro import tls

    def storage(staging):
        spec = tls.TlsSpec(mode=tls.MODE_ACME, host="h", port=443,
                           acme_email="a@b.c", acme_staging=staging)
        return [a for a in tls.service(spec)["command"] if ".acme.storage=" in a][0]

    assert storage(True) != storage(False)
    assert tls.ACME_FILE_STAGING in storage(True)
    assert storage(False).endswith("/" + tls.ACME_FILE)
    # Only the staging run may point at the staging directory.
    stg = tls.TlsSpec(mode=tls.MODE_ACME, host="h", port=443, acme_email="a@b.c",
                      acme_staging=True)
    prod = tls.TlsSpec(mode=tls.MODE_ACME, host="h", port=443, acme_email="a@b.c")
    assert "acme-staging-v02" in " ".join(tls.service(stg)["command"])
    assert "acme-staging-v02" not in " ".join(tls.service(prod)["command"])


def test_dns_credentials_are_required_and_provider_agnostic(tmp_path, monkeypatch):
    """Every lego provider reads its OWN variables, so this checks the file, not keys.

    Mounting the env file only "if it exists" let a missing file through, and
    Traefik then ran with no credentials and failed opaquely minutes later.
    """
    from rc_repro import tls
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))

    for provider in ("cloudflare", "route53", "digitalocean", "gcloud", "azuredns"):
        ok, msg = tls.dns_credentials(provider)
        assert not ok
        assert str(tls.dns_env_path()) in msg
        # Point at THAT provider's documentation, not Cloudflare's.
        assert f"{tls.LEGO_PROVIDER_DOCS}{provider}/" in msg

    env = tls.dns_env_path()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("# a comment\n\n", encoding="utf-8")
    ok, msg = tls.dns_credentials("route53")
    assert not ok and "no KEY=VALUE" in msg

    # Any provider's variables satisfy it, and values are never echoed back.
    env.write_text("AWS_ACCESS_KEY_ID=AKIAsecret\nAWS_SECRET_ACCESS_KEY=hunter2\n",
                   encoding="utf-8")
    ok, msg = tls.dns_credentials("route53")
    assert ok and "AWS_ACCESS_KEY_ID" in msg and "AWS_SECRET_ACCESS_KEY" in msg
    assert "AKIAsecret" not in msg and "hunter2" not in msg

    # And the file is mounted unconditionally, not "if it exists".
    spec = tls.TlsSpec(mode=tls.MODE_ACME, host="h", port=443, acme_email="a@b.c",
                       acme_challenge="dns", acme_dns_provider="route53")
    assert tls.service(spec)["env_file"] == [str(env)]


def test_verify_can_present_a_different_sni_than_the_address_it_dials():
    """tls-status must probe THIS host, not the public name.

    Checking the hostname meant a proxy in front (Cloudflare's orange cloud)
    answered instead, and its valid edge certificate was reported as ours while
    our own Traefik had none.
    """
    import inspect
    from rc_repro import tls
    sig = inspect.signature(tls.verify)
    assert "sni" in sig.parameters
    src = inspect.getsource(tls.verify)
    assert "server_hostname=servername" in src
    assert "server_hostname=host" not in src, "every handshake must honour sni"


def test_acme_router_declares_the_domain_it_needs_a_certificate_for():
    """Traefik must be TOLD the domain, because the rule has no Host() matcher.

    Traefik derives what to request from a router's Host() rule. This rule is
    PathPrefix(`/`) on purpose, so the workspace also answers on localhost — which
    left ACME with nothing to ask for: it logs "no domain found" and silently
    serves its default certificate, indistinguishable from a failed challenge with
    no request ever having been made.
    """
    from rc_repro import tls
    spec = tls.TlsSpec(mode=tls.MODE_ACME, host="testrepo.kestron.org", port=443,
                       acme_email="a@b.c")
    y = dict(tls.files(spec, ["rocketchat"]))["tls/dynamic.yml"]
    assert "certResolver: le" in y
    assert "domains:" in y
    assert '- main: "testrepo.kestron.org"' in y

    # Static-certificate modes must NOT carry a resolver or a domains block.
    local = dict(tls.files(tls.TlsSpec(host="x.rcrepro.localhost", port=8443),
                           ["rocketchat"], "C", "K"))["tls/dynamic.yml"]
    assert "certResolver" not in local and "domains:" not in local


def test_dns01_does_not_require_the_host_to_resolve(monkeypatch):
    """dns-01 reads a TXT at _acme-challenge.<host>; the host needs no A record.

    Requiring one refused setups that would have issued perfectly well — you can
    get a certificate before pointing the name anywhere.
    """
    from rc_repro import tls
    monkeypatch.setattr(tls, "resolves_to", lambda h, public=True: [])
    ok, msg = tls.dns_preflight("testrepo.kestron.org", "dns")
    assert ok and "fine for dns-01" in msg
    # An inbound challenge still needs the name to resolve, and now points at the
    # alternative that does not.
    ok, msg = tls.dns_preflight("testrepo.kestron.org", "tlsalpn")
    assert not ok and "--acme-challenge dns" in msg


def test_reachability_gaps_separate_a_valid_cert_from_a_reachable_workspace(monkeypatch):
    """Issuance succeeding is not the same as the name being reachable.

    dns-01 issues with no DNS record and no public route, after which the summary
    advertises an https URL nothing outside the machine can open. Proven against a
    real run: a production certificate was issued and served correctly while the
    hostname had no record at all and Traefik was bound to 127.0.0.1.
    """
    from rc_repro import tls
    spec = tls.TlsSpec(mode=tls.MODE_ACME, host="testrepo.example.org", port=443,
                       acme_email="a@b.c", acme_challenge="dns",
                       acme_dns_provider="cloudflare")

    # No DNS record and loopback-bound: both gaps named.
    monkeypatch.setattr(tls, "resolves_to", lambda h, public=True: [])
    monkeypatch.setattr(tls, "host_has_public_address", lambda: False)
    gaps = tls.reachability_gaps(spec, "127.0.0.1")
    assert any("no DNS record" in g for g in gaps)
    assert any("bound to 127.0.0.1" in g for g in gaps)

    # Public bind but no public address on the host -> still unreachable.
    gaps = tls.reachability_gaps(spec, "0.0.0.0")
    assert any("no public address" in g for g in gaps)
    assert not any("bound to" in g for g in gaps)

    # Record exists, public bind, routable host -> nothing to warn about.
    monkeypatch.setattr(tls, "resolves_to", lambda h, public=True: ["93.184.216.34"])
    monkeypatch.setattr(tls, "host_has_public_address", lambda: True)
    assert tls.reachability_gaps(spec, "0.0.0.0") == []
    # ...but binding to loopback is still a gap even with good DNS.
    assert tls.reachability_gaps(spec, "127.0.0.1") == [
        "the workspace is bound to 127.0.0.1"]


def test_domain_is_normalized_the_way_the_official_docs_require():
    """The Rocket.Chat compose docs say DOMAIN must have no scheme or trailing slash.

    They say it because people get it wrong, and unguarded it corrupted three
    things at once: ROOT_URL became "https://https://host", the ACME `domains`
    entry became a value Let's Encrypt rejects, and the TLS SNI name never matched.
    """
    import pytest
    from rc_repro import tls
    from rc_repro.errors import ValidationError

    # One obvious meaning -> corrected, and the correction is reported.
    for given in ("rc1.example.com", "https://rc1.example.com", "http://rc1.example.com/",
                  "RC1.Example.COM.", "  rc1.example.com  ", "rc1.example.com."):
        host, note = tls.normalize_domain(given)
        assert host == "rc1.example.com", given
        if given != "rc1.example.com":
            assert note, f"{given!r} was changed silently"

    # Ambiguous or unsupported -> refused, because silently dropping part of what
    # was asked for is worse than saying no.
    for given, expect in [
        ("rc1.example.com:8443", "contains a port"),
        ("rc1.example.com/chat", "contains a path"),
        ("*.example.com", "is a wildcard"),
        ("bad_host.example.com", "not a valid hostname"),
        ("-x.example.com", "not a valid hostname"),
        ("https://", "empty"),
    ]:
        with pytest.raises(ValidationError, match=expect):
            tls.normalize_domain(given)

    # And the normalized host is what reaches ROOT_URL and the ACME domains block.
    host, _ = tls.normalize_domain("HTTPS://RC1.Example.COM/")
    spec = tls.TlsSpec(mode=tls.MODE_ACME, host=host, port=443, acme_email="a@b.c")
    assert spec.root_url == "https://rc1.example.com"
    y = dict(tls.files(spec, ["rocketchat"]))["tls/dynamic.yml"]
    assert '- main: "rc1.example.com"' in y


# --- env var overrides (up --env / rc-repro env) -------------------------------


def test_env_overrides_beat_the_preset_and_can_remove_a_base_key():
    """User overrides are applied last, and None removes a key entirely.

    Blanking a base default to "" is not the same as removing it — Rocket.Chat
    treats an empty value as set — so unset has to delete the key.
    """
    pre = presets.load("default")
    res = versions.resolve("8.6.1", offline=True)
    spec = compose.Spec.from_resolved(
        res, project_name="p", root_url="http://x", host_port=3000, reg_token=None,
        preset=pre, env_overrides={"MY_VAR": "v", "ADMIN_USERNAME": "someone",
                                   "MONGO_URL": None})
    env = compose.build(spec)["services"]["rocketchat"]["environment"]
    assert env["MY_VAR"] == "v"
    assert env["ADMIN_USERNAME"] == "someone", "an override must beat the base default"
    assert "MONGO_URL" not in env, "None must delete the key, not blank it"
    # Untouched keys survive.
    assert env["ROOT_URL"] == "http://x" and env["PORT"] == "3000"

    # No overrides -> byte-identical to before the feature existed.
    plain = compose.Spec.from_resolved(
        res, project_name="p", root_url="http://x", host_port=3000, reg_token=None,
        preset=pre)
    assert "MY_VAR" not in compose.build(plain)["services"]["rocketchat"]["environment"]


def test_env_overrides_apply_to_every_rocketchat_instance():
    """multi-instance clones rocketchat into rocketchat-1..N; all must get them."""
    pre = presets.load("multi-instance", {"instances": "3"})
    res = versions.resolve("8.6.1", offline=True)
    spec = compose.Spec.from_resolved(
        res, project_name="p", root_url="http://x", host_port=3000, reg_token=None,
        preset=pre, env_overrides={"MY_VAR": "v"})
    doc = compose.build(spec)
    rc = [s for s in doc["services"] if s.startswith("rocketchat")]
    assert len(rc) == 3
    for svc in rc:
        assert doc["services"][svc]["environment"]["MY_VAR"] == "v", svc


def test_env_var_names_are_validated_before_reaching_compose():
    """An invalid name produces a compose file docker rejects, so refuse it here."""
    import pytest
    from rc_repro.errors import ValidationError
    from rc_repro.services import envvars

    assert envvars.parse_set(["A=1", "B_2=x=y"]) == {"A": "1", "B_2": "x=y"}
    assert envvars.parse_set([]) == {}
    with pytest.raises(ValidationError, match="not KEY=VALUE"):
        envvars.parse_set(["JUST_A_KEY"])
    for bad in ("2LEADING=1", "has-hyphen=1", "has space=1", "=1"):
        with pytest.raises(ValidationError, match="valid environment variable name"):
            envvars.parse_set([bad])
    with pytest.raises(ValidationError, match="valid environment variable name"):
        envvars.check_names(["has-hyphen"])


def test_env_rows_mark_which_keys_the_user_set():
    """The panel offers "remove" on every row, and it means different things.

    Removing an override restores the preset default; removing an inherited key
    deletes it from the workspace. The panel can only say which if detail() marks
    them.
    """
    from rc_repro.services import lifecycle as lc
    doc = {"services": {"rocketchat": {"environment": {
        "MY_VAR": "v", "ADMIN_PASS": "secret", "PORT": "3000"}}}}
    rows = {r["key"]: r for r in lc._env_rows(doc, {"MY_VAR": "v"})}
    assert rows["MY_VAR"]["override"] is True
    assert rows["PORT"]["override"] is False
    assert rows["ADMIN_PASS"]["value"] == "********", "credentials still masked"
    # No overrides argument -> nothing marked, and the shape is unchanged.
    assert all(r["override"] is False for r in lc._env_rows(doc))
    # compose's list form is handled too.
    listform = {"services": {"rocketchat": {"environment": ["A=1", "B=2"]}}}
    assert [r["key"] for r in lc._env_rows(listform, {"A": "1"})] == ["A", "B"]
    assert lc._env_rows(listform, {"A": "1"})[0]["override"] is True


def test_setting_expansion_and_the_bare_setting_id_trap():
    """A Rocket.Chat SETTING only applies from the environment WITH the prefix.

    Verified against a live 8.6.1 workspace: `Accounts_ShowFormLogin=false` left the
    setting `True`, while `OVERWRITE_SETTING_Accounts_ShowFormLogin=false` made it
    `False`. The bare form is accepted by docker and silently does nothing, which is
    why --setting exists and why a bare setting id is warned about.
    """
    from rc_repro.services import envvars
    assert envvars.SETTING_PREFIX == "OVERWRITE_SETTING_"
    assert envvars.as_setting(["Message_AllowEditing=false"]) == {
        "OVERWRITE_SETTING_Message_AllowEditing": "false"}
    # A value containing '=' survives; only the first separator splits.
    assert envvars.as_setting(["A_B=x=y"]) == {"OVERWRITE_SETTING_A_B": "x=y"}
    assert envvars.as_setting([]) == {}
    # It reuses the same name validation as --set.
    import pytest
    from rc_repro.errors import ValidationError
    with pytest.raises(ValidationError, match="not KEY=VALUE"):
        envvars.as_setting(["Message_AllowEditing"])


def test_bare_setting_warning_asks_the_workspace_and_stays_quiet_when_it_cannot(monkeypatch):
    """Which names are settings is version-specific, so ask the workspace, not a list.

    And it is only a warning path: an unreachable workspace must not block an env
    change, since setting env on a repro that is not serving is legitimate.
    """
    from rc_repro import rcapi
    from rc_repro.services import envvars
    meta = type("M", (), {"name": "e", "root_url": "http://x", "extra": {}})()

    monkeypatch.setattr(envvars.lifecycle, "login", lambda m: object())
    monkeypatch.setattr(rcapi, "setting_ids",
                        lambda *a, **k: {"Message_AllowEditing", "Accounts_ShowFormLogin"})
    seen: list = []
    envvars.warn_bare_settings(meta, ["Message_AllowEditing", "MY_OWN_VAR"], seen.append)
    msgs = " ".join(str(getattr(e, "message", e)) for e in seen)
    assert "Message_AllowEditing is a Rocket.Chat SETTING" in msgs
    assert "OVERWRITE_SETTING_Message_AllowEditing" in msgs, "must give the fix"
    assert "MY_OWN_VAR" not in msgs, "a real env var must not be flagged"

    # Already prefixed -> nothing to say, and no API call needed.
    seen.clear()
    envvars.warn_bare_settings(meta, ["OVERWRITE_SETTING_Message_AllowEditing"], seen.append)
    assert seen == []

    # Workspace unreachable -> silent, never fatal.
    seen.clear()
    monkeypatch.setattr(envvars.lifecycle, "login",
                        lambda m: (_ for _ in ()).throw(RuntimeError("not serving")))
    envvars.warn_bare_settings(meta, ["Message_AllowEditing"], seen.append)
    assert seen == []
    seen.clear()
    monkeypatch.setattr(envvars.lifecycle, "login", lambda m: object())
    monkeypatch.setattr(rcapi, "setting_ids", lambda *a, **k: None)
    envvars.warn_bare_settings(meta, ["Message_AllowEditing"], seen.append)
    assert seen == []
