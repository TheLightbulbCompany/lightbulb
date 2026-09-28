import json
import threading
import time

from bridge import sync


def test_request_dumps_logs_and_turn_context_never_leave_the_microvm():
    assert sync.is_excluded("sessions/request_dump_20260916T101500.json") is True
    assert sync.is_excluded("logs/hermes.log") is True
    assert sync.is_excluded(".lightbulb-turn/room:agent.json") is True
    assert sync.is_excluded("state.db-wal") is True
    assert sync.is_excluded("media/messaging/room-1/a1.png") is True
    assert sync.is_excluded("memories/MEMORY.md") is False
    assert sync.is_excluded("sessions/index.db") is False


def test_the_four_backend_owned_files_never_go_back_up(home, monkeypatch):
    """Task 13 (M4-R20): config.yaml/.env/SOUL.md/.revision are written by
    hermes_profiles.upload_profile/patch_profile_config/set_profile_config and re-pulled by
    ensure_profile on a changed revision -- save_profile must never upload the bridge's own local
    (possibly stale the instant a hot patch lands) copy back over a backend write. Row #15 /
    Ruling M6-R30 adds a fifth: lightbulb-facts.json (hermes_profiles.write_facts_to_profiles)."""
    assert sync.is_excluded("config.yaml") is True
    assert sync.is_excluded(".env") is True
    assert sync.is_excluded("SOUL.md") is True
    assert sync.is_excluded(".revision") is True
    assert sync.is_excluded("lightbulb-facts.json") is True

    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", ".env", "SOUL.md", ".revision", "lightbulb-facts.json", "memory.md"):
        (profile / name).write_text("x")

    uploaded = []
    monkeypatch.setattr(
        sync,
        "_s3",
        lambda: type("FakeS3", (), {"upload_file": staticmethod(lambda path, bucket, key: uploaded.append(key))})(),
    )
    sync.save_profile("ceo")
    assert uploaded == [f"{sync.S3_PREFIX}/ceo/memory.md"]


def test_s3_client_is_cached_across_calls(monkeypatch):
    """Minor 2 (fix round 2 continuation): _s3() built a fresh boto3.client("s3") on every call
    -- restore_root_auth/save_root_auth alone now cost 2-3 of these per turn
    (_root_auth_revision on both restore and save, plus the actual get/put).

    ``boto3`` itself isn't installed in this test environment (only ``botocore``, per the bridge
    test runner's deps) -- ``_s3()``'s own ``import boto3`` is stubbed via sys.modules instead of
    patching a real ``boto3.client``."""
    import sys
    import types

    monkeypatch.setattr(sync, "_S3_CLIENT", None)
    builds = []

    class _Fake:
        pass

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *a, **k: (builds.append(1), _Fake())[1]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    first = sync._s3()
    second = sync._s3()
    assert first is second
    assert len(builds) == 1


def test_boot_secrets_come_from_secrets_manager_not_the_runtime_env():
    class FakeSM:
        def get_secret_value(self, SecretId):
            assert SecretId == "lightbulb/dev/hermes/org_test"
            return {"SecretString": json.dumps({"hookToken": "h", "gatewayToken": "g", "bedrockBearer": "b"})}

    assert sync.load_boot_secrets("lightbulb/dev/hermes/org_test", client=FakeSM()) == {
        "hookToken": "h", "gatewayToken": "g", "bedrockBearer": "b"
    }


# ---- fix round (minor 12): two turns for the same agent must not upload concurrently ----------
def test_save_profile_serializes_concurrent_calls_for_the_same_agent(home, monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "memory.md").write_text("hi")

    active = 0
    max_active = 0
    lock = threading.Lock()

    class FakeS3:
        def upload_file(self, path, bucket, key):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())

    threads = [threading.Thread(target=sync.save_profile, args=("ceo",)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active == 1  # never more than one upload in flight for this agent
