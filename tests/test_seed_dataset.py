"""Public Seed Dataset planning, readback, and evidence contracts."""

from types import SimpleNamespace


def test_seed_plan_resolves_identities_and_expected_totals():
    from rc_repro import seed

    plan = seed.plan_from("small")
    record = plan.as_dict()

    assert record["profile"] == "small"
    assert record["users"] == 5
    assert record["channels"] == 3
    assert record["messages_per_channel"] == 5
    assert record["identities"]["users"] == ["alice", "bob", "carol", "dave", "erin"]
    assert record["identities"]["channels"] == ["team-chat", "dev", "support"]
    assert record["identities"]["dm_pairs"] == [["alice", "bob"], ["bob", "carol"]]
    assert record["expected"] == {
        "users": 5,
        "channels": 3,
        "groups": 0,
        "messages": 20,
        "dm_messages": 2,
        "dms": 2,
        "thread_replies": 0,
    }


def test_seed_body_reports_actual_message_successes_not_approximate_attempts(monkeypatch):
    from rc_repro import rcapi, seed

    monkeypatch.setattr(rcapi, "login", lambda *a, **k: (_ for _ in ()).throw(Exception("offline")))
    plan = seed.plan_from("small", users=1, channels=1, messages=2)

    def post(path, headers, payload):
        ok = not path.endswith("chat.postMessage")
        return SimpleNamespace(ok=ok, json=lambda: {"message": {"_id": "m1"}})

    result = seed._seed_body("http://x", {"h": "1"}, plan, post, lambda _m: None)

    assert result["attempted"]["messages"] == 4
    assert result["attempted"]["channels"] == 1
    assert result["attempted"]["groups"] == 0
    assert result["messages"] == 0
    assert result["actual"]["messages"] == 0
    assert "~" not in " ".join(result.get("logs", []))


def test_seed_plan_resolves_dm_count_when_user_override_cannot_form_pairs():
    from rc_repro import seed

    plan = seed.plan_from("small", users=1)

    assert plan.as_dict()["requested_dms"] == 2
    assert plan.as_dict()["dms"] == 0
    assert plan.expected_counts()["dms"] == 0


def test_seed_plan_never_repeats_a_dm_room_when_users_are_overridden():
    from rc_repro import seed

    plan = seed.plan_from("small", users=2)

    assert plan.dm_pairs == [("alice", "bob")]
    assert plan.expected_counts()["dms"] == 1


def test_seed_attempts_keep_public_channels_and_private_groups_separate(monkeypatch):
    from rc_repro import rcapi, seed

    monkeypatch.setattr(rcapi, "login", lambda *a, **k: (_ for _ in ()).throw(Exception("offline")))
    plan = seed.plan_from("standard", users=1, channels=1, messages=0)

    result = seed._seed_body(
        "http://x", {"h": "1"}, plan,
        lambda *_args, **_kwargs: SimpleNamespace(ok=False),
        lambda _message: None,
    )

    assert result["attempted"]["channels"] == 1
    assert result["attempted"]["groups"] == 2
    assert set(result["attempted"]) == set(result["actual"])


def test_seed_verification_failure_is_non_retryable_create_failure():
    from rc_repro import seed

    failure = seed.SeedVerificationError("readback did not match", {})

    assert failure.code == "CREATE_FAILED"
    assert failure.exit_code == 7
    assert failure.http_status == 500


def test_seed_verification_exposes_mismatch_diagnostics():
    from rc_repro import seed

    plan = seed.plan_from("small")
    verification = seed.verify_plan(
        plan,
        {"users": 5, "channels": 3, "groups": 0, "messages": 0,
         "dm_messages": 2, "dms": 2},
    )

    assert verification["ok"] is False
    assert verification["mismatches"]["messages"] == {"expected": 20, "actual": 0}


def test_seed_readback_counts_named_rooms_and_messages():
    from rc_repro import rcapi, seed

    plan = seed.plan_from("small")
    users = [{"username": name} for name in plan.user_names]
    channels = ([{"name": name, "msgs": 5} for name in plan.channel_names]
                + [{"name": "general", "msgs": 5}])
    ims = [{"usernames": list(pair), "msgs": 1} for pair in plan.dm_pairs]

    def get(url, **_kwargs):
        if url.endswith("users.list"):
            payload = {"users": users}
        elif url.endswith("channels.list"):
            payload = {"channels": channels}
        elif url.endswith("im.list"):
            payload = {"ims": ims}
        else:
            payload = {}
        return SimpleNamespace(ok=True, json=lambda: payload)

    observed = seed.readback("http://x", rcapi.Auth("t", "u"), plan, get=get)
    verification = seed.verify_plan(plan, observed)

    assert observed["messages"] == 20
    assert observed["dms"] == 2
    assert verification["ok"] is True
    assert verification["exact"] is True


def test_seed_readback_uses_a_participant_when_admin_cannot_see_dms():
    from rc_repro import rcapi, seed

    plan = seed.plan_from("small")
    users = [{"username": name} for name in plan.user_names]
    channels = ([{"name": name, "msgs": 5} for name in plan.channel_names]
                + [{"name": "general", "msgs": 5}])
    ims = [{"usernames": list(pair), "msgs": 1} for pair in plan.dm_pairs]
    participant_headers = {name: rcapi.Auth(f"token-{name}", f"id-{name}")
                           for name in plan.user_names}

    def get(url, headers=None, **_kwargs):
        if url.endswith("users.list"):
            payload = {"users": users}
        elif url.endswith("channels.list"):
            payload = {"channels": channels}
        elif url.endswith("im.list"):
            # The admin sees no user-authored DMs; a participant does.
            payload = {"ims": ims if headers.get("X-User-Id", "").startswith("id-") else []}
        else:
            payload = {}
        return SimpleNamespace(ok=True, json=lambda: payload)

    observed = seed.readback(
        "http://x", rcapi.Auth("admin-token", "admin-id"), plan, get=get,
        participants=participant_headers,
    )
    verification = seed.verify_plan(plan, observed)

    assert observed["dm_pairs"] == [list(pair) for pair in plan.dm_pairs]
    assert verification["ok"] is True


def test_seed_verification_marks_missing_rooms_unavailable():
    from rc_repro import seed

    plan = seed.plan_from("small")
    observed = {
        "users": 5, "channels": 3, "groups": 0, "messages": 20,
        "dm_messages": 2, "dms": 2, "thread_replies": 0,
        "usernames": plan.user_names,
        "channel_names": plan.channel_names,
        "group_names": [],
        "dm_pairs": [list(pair) for pair in plan.dm_pairs],
        "messages_by_room": {"general": 5},
    }

    verification = seed.verify_plan(plan, observed)

    assert verification["ok"] is False
    assert "messages:team-chat" in verification["unavailable"]


def test_evidence_includes_the_persisted_seed_record(tmp_path, monkeypatch):
    import json

    from rc_repro import runner
    from rc_repro.services import evidence, k8s

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    meta = runner.Metadata(
        name="seed-evidence", project="rc-repro-seed-evidence", rc_version="8.6.1",
        rc_image="rocket.chat", mongo_tag="7.0", mongo_flavor="official",
        preset="microservices", root_url="http://localhost:31410", host_port=31410,
        version_source="test", extra={"topology": "kubernetes", "k8s_namespace": "rc-repro-seed-evidence"},
    )
    meta.extra["seed"] = {
        "profile": "small",
        "plan": {"users": 5, "channels": 3, "messages_per_channel": 5},
        "readback": {"users": 5, "channels": 3, "messages": 20, "dms": 2},
        "verification": {"ok": True, "mismatches": {}},
    }
    runner.write("seed-evidence", "microservices: {}\n", meta, artifact_name="values.yaml")
    monkeypatch.setattr(k8s, "pods", lambda _name: [])
    monkeypatch.setattr(k8s, "aggregate_state", lambda _pods: "running")
    monkeypatch.setattr(k8s, "forward_state", lambda _meta: "up")

    record = evidence.record("seed-evidence")

    assert record["seed"]["plan"]["messages_per_channel"] == 5
    assert record["seed"]["readback"]["messages"] == 20
    assert json.dumps(record, sort_keys=True).find("admin123") == -1


def test_lifecycle_persists_only_the_seed_proof_fields(tmp_path, monkeypatch):
    from rc_repro import runner
    from rc_repro.services import lifecycle

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    meta = runner.Metadata(
        name="persist-seed", project="rc-repro-persist-seed", rc_version="8.6.1",
        rc_image="rocket.chat", mongo_tag="7.0", mongo_flavor="official",
        preset="default", root_url="http://localhost:31411", host_port=31411,
        version_source="test",
    )
    runner.write("persist-seed", "services: {}\n", meta)
    lifecycle.persist_seed_result(meta, {
        "plan": {"profile": "small", "users": 5},
        "actual": {"users": 5},
        "readback": {"users": 5},
        "verification": {"ok": True},
        "durations": {"users": 1.0},
    })

    saved = runner.read_meta("persist-seed")
    assert saved.extra["seed"] == {
        "profile": "small",
        "plan": {"profile": "small", "users": 5},
        "actual": {"users": 5},
        "readback": {"users": 5},
        "verification": {"ok": True},
    }


def test_lifecycle_persists_failed_seed_verification_before_raising(tmp_path, monkeypatch):
    import pytest

    from rc_repro import rcapi, runner, seed
    from rc_repro.services import lifecycle

    monkeypatch.setenv("RC_REPRO_HOME", str(tmp_path / "home"))
    meta = runner.Metadata(
        name="failed-seed", project="rc-repro-failed-seed", rc_version="8.6.1",
        rc_image="rocket.chat", mongo_tag="7.0", mongo_flavor="official",
        preset="default", root_url="http://localhost:31412", host_port=31412,
        version_source="test",
    )
    runner.write("failed-seed", "services: {}\n", meta)
    failure = seed.SeedVerificationError(
        "seed verification failed: messages: expected 20, read back 0",
        {
            "plan": {"profile": "small", "users": 5},
            "attempted": {"messages": 20},
            "actual": {"messages": 0},
            "readback": {"messages": 0},
            "verification": {"ok": False, "mismatches": {
                "messages": {"expected": 20, "actual": 0},
            }},
        },
    )
    monkeypatch.setattr(lifecycle, "ensure_reachable", lambda _name: None)
    monkeypatch.setattr(lifecycle, "login", lambda _meta: rcapi.Auth("t", "u"))
    monkeypatch.setattr(lifecycle.seeder, "plan_from", lambda _profile: object())
    monkeypatch.setattr(lifecycle.seeder, "seed", lambda *a, **k: (_ for _ in ()).throw(failure))

    with pytest.raises(seed.SeedVerificationError):
        lifecycle.run_seed_inline(meta, "small", False, lambda _event: None)

    saved = runner.read_meta("failed-seed")
    assert saved.extra["seed"]["verification"]["ok"] is False
    assert saved.extra["seed"]["readback"]["messages"] == 0
