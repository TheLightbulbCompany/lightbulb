from bridge import sync
from tests import factories as f


def test_the_root_auth_json_is_restored_once_and_saved_back_after_a_turn(monkeypatch):
    ops = []
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: ops.append(("get", key)))
    monkeypatch.setattr(sync, "_upload_object", lambda src, key: ops.append(("put", key)))
    sync._ROOT_AUTH_RESTORED.clear()
    sync.restore_root_auth()
    sync.restore_root_auth()
    sync.save_root_auth()
    assert ops == [("get", "auth.json"), ("put", "auth.json")]


def test_root_auth_is_a_no_op_without_a_bucket(monkeypatch):
    """Same convention as restore_profile/save_profile: no S3_BUCKET means dev/test."""
    monkeypatch.setattr(sync, "S3_BUCKET", "")

    def _fail(*a):
        raise AssertionError("must not touch S3 without a bucket")

    monkeypatch.setattr(sync, "_download_object", _fail)
    monkeypatch.setattr(sync, "_upload_object", _fail)
    sync._ROOT_AUTH_RESTORED.clear()
    sync.restore_root_auth()  # must not raise
    sync.save_root_auth()  # must not raise


def test_download_object_and_upload_object_use_the_s3_prefix(home, monkeypatch):
    """The real (non-mocked) helpers key under S3_PREFIX -- <owner>/hermes/auth.json, matching
    hermes_profiles.root_home_prefix on the backend side exactly."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "S3_PREFIX", "org_a/hermes")
    calls = []

    class FakeS3:
        def download_file(self, bucket, key, dest):
            calls.append(("get", bucket, key))

        def upload_file(self, path, bucket, key):
            calls.append(("put", bucket, key))

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())
    sync._download_object("auth.json", sync.HOME / "auth.json")
    sync._upload_object(sync.HOME / "auth.json", "auth.json")
    assert calls == [
        ("get", "bucket-under-test", "org_a/hermes/auth.json"),
        ("put", "bucket-under-test", "org_a/hermes/auth.json"),
    ]


def test_run_turn_restores_and_saves_the_root_auth_json(home, posted, monkeypatch):
    """Task 15b: the root auth.json needs the same restore-before / save-after treatment as a
    profile dir (turn.py's existing restore_profile/save_profile calls), since a profile with no
    providers.openai-codex block of its own reads and rotates ROOT's grant."""
    from bridge import turn

    calls = []
    monkeypatch.setattr(sync, "restore_root_auth", lambda: calls.append("restore"))
    monkeypatch.setattr(sync, "save_root_auth", lambda: calls.append("save"))
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "ack", turn.ZERO_USAGE))
    turn.run_turn("ceo", f.ingress())
    assert calls == ["restore", "save"]
