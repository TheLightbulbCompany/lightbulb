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
