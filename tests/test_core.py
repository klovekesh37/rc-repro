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


def test_multi_instance_clamps_instance_count():
    assert presets.load("multi-instance", {"instances": "1"}).instances == 2   # min 2
    assert presets.load("multi-instance", {"instances": "99"}).instances == 5  # max 5


# --- compose building ---------------------------------------------------------


def _spec(version: str, preset_name: str = "default", params: dict | None = None):
    r = versions.resolve(version, offline=True)
    pre = presets.load(preset_name, params or {})
    return compose.Spec.from_resolved(
        r, project_name="rcrepro-t", root_url="http://localhost:3000",
        host_port=3000, reg_token=None, preset=pre,
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


def test_used_ports_includes_sidecars(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path))
    meta = runner.Metadata(
        name="x", project="rcrepro-x", rc_version="8.5.1", rc_image="i",
        mongo_tag="8.0", mongo_flavor="official", preset="saml",
        root_url="http://localhost:3000", host_port=3000, version_source="map",
        extra={"sidecar_ports": [8081]},
    )
    runner.write("x", "services: {}\n", meta)
    assert {3000, 8081} <= runner.used_ports()


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
