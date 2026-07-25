"""Tests for the compose-failure diagnoser (pure matching + evidence gathering)."""

from __future__ import annotations

from rc_repro.services import diagnose


def test_match_hub_rate_limit():
    text = ("Error: unable to copy from source docker://registry.rocket.chat/...: "
            "toomanyrequests: You have reached your unauthenticated pull rate limit.")
    hint = diagnose.match(text)
    assert hint and "docker login" in hint and "rate limit" in hint.lower()


def test_match_mongo_kernel_server_121912():
    text = ('{"s":"F","c":"CONTROL","id":12257600,"ctx":"main","msg":"MongoDB cannot '
            'start: Linux kernel versions 6.19 and newer has a known incompatibility"}')
    hint = diagnose.match(text)
    assert hint and "SERVER-121912" in hint and "NOT a volume" in hint


def test_match_replica_set_never_initialised():
    hint = diagnose.match("MongoServerError: NotYetInitialized: no replset config has been received")
    assert hint and "replica set" in hint.lower()


def test_match_platform_mismatch():
    hint = diagnose.match("no matching manifest for linux/arm64/v8 in the manifest list entries")
    assert hint and "architecture" in hint.lower()


def test_match_missing_tag_modern_containerd_message():
    # the message Docker Desktop / containerd actually emits for a bad tag
    text = ('Error response from daemon: failed to resolve reference '
            '"registry.rocket.chat/rocketchat/rocket.chat:0.0.0": not found')
    hint = diagnose.match(text)
    assert hint and "tag was not found" in hint


def test_match_unauthorized_and_disk_and_ports():
    assert "login" in (diagnose.match("Error: authentication required") or "")
    assert "disk" in (diagnose.match("write /data: no space left on device") or "").lower()
    assert "1024" in (diagnose.match("rootlessport cannot expose privileged port 80, bind: permission denied") or "")


def test_match_is_case_insensitive_and_none_on_clean():
    assert diagnose.match("TOOMANYREQUESTS") is not None
    assert diagnose.match("Container started; all healthy") is None
    assert diagnose.match("") is None


def test_match_precedence_rate_limit_beats_generic_manifest():
    # a rate-limit error mentions the manifest too; the more specific hint must win
    text = ("reading manifest 8.6.1 in registry.rocket.chat/rocketchat/rocket.chat: "
            "toomanyrequests: You have reached your unauthenticated pull rate limit.")
    assert "docker login" in diagnose.match(text)


def test_diagnose_failure_uses_container_logs(monkeypatch):
    monkeypatch.setattr(diagnose.runner, "compose_logs_capture",
                        lambda name, **k: "F CONTROL SERVER-121912 kernel versions 6.19")
    monkeypatch.setattr(diagnose.runner, "compose_pull_capture",
                        lambda name: (_ for _ in ()).throw(AssertionError("must not pull when logs suffice")))
    assert "SERVER-121912" in diagnose.diagnose_failure("x")


def test_diagnose_failure_falls_back_to_pull_when_nothing_started(monkeypatch):
    monkeypatch.setattr(diagnose.runner, "compose_logs_capture", lambda name, **k: "")
    monkeypatch.setattr(diagnose.runner, "compose_pull_capture",
                        lambda name: "toomanyrequests: unauthenticated pull rate limit")
    assert "docker login" in diagnose.diagnose_failure("x")


def test_diagnose_failure_none_when_logs_present_but_unknown(monkeypatch):
    monkeypatch.setattr(diagnose.runner, "compose_logs_capture", lambda name, **k: "normal startup lines")
    monkeypatch.setattr(diagnose.runner, "compose_pull_capture",
                        lambda name: (_ for _ in ()).throw(AssertionError("pull not attempted when logs exist")))
    assert diagnose.diagnose_failure("x") is None


def test_diagnose_failure_never_raises(monkeypatch):
    monkeypatch.setattr(diagnose.runner, "compose_logs_capture",
                        lambda name, **k: (_ for _ in ()).throw(RuntimeError("docker gone")))
    monkeypatch.setattr(diagnose.runner, "compose_pull_capture",
                        lambda name: (_ for _ in ()).throw(RuntimeError("docker gone")))
    # logs raised -> treated as empty -> pull attempted -> also raises -> None
    assert diagnose.diagnose_failure("x") is None
