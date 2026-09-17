from bridge import sync


def test_a_changed_revision_triggers_a_re_pull(monkeypatch):
    pulls = []
    revisions = iter(["1", "1", "2"])
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: next(revisions))
    monkeypatch.setattr(sync, "_download_profile", lambda agent_id: pulls.append(agent_id))
    sync._REVISIONS.clear()
    sync.ensure_profile("a1")  # cold: pull
    sync.ensure_profile("a1")  # same revision: no pull
    sync.ensure_profile("a1")  # bumped: pull
    assert pulls == ["a1", "a1"]


def test_an_unreadable_revision_does_not_re_pull_a_warm_profile(monkeypatch):
    pulls = []
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: None)
    monkeypatch.setattr(sync, "_download_profile", lambda agent_id: pulls.append(agent_id))
    sync._REVISIONS.clear()
    sync._REVISIONS["a1"] = "7"
    sync.ensure_profile("a1")
    assert pulls == []  # a transient S3 blip must not re-download a working profile mid-turn


def test_ensure_profile_is_a_no_op_without_a_bucket(monkeypatch):
    """Same convention as restore_profile/save_profile: no S3_BUCKET means dev/test, not a
    broken deploy -- never attempt a network call."""
    monkeypatch.setattr(sync, "S3_BUCKET", "")

    def _fail(agent_id):
        raise AssertionError("must not read a revision without a bucket")

    monkeypatch.setattr(sync, "_read_revision", _fail)
    sync._REVISIONS.clear()
    sync.ensure_profile("a1")  # must not raise


def test_download_profile_pulls_exactly_the_four_backend_owned_files(home, monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    calls = []

    class FakeS3:
        def download_file(self, bucket, key, dest):
            calls.append(key)

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())
    sync._download_profile("a1")
    assert calls == [
        f"{sync.S3_PREFIX}/a1/config.yaml",
        f"{sync.S3_PREFIX}/a1/.env",
        f"{sync.S3_PREFIX}/a1/SOUL.md",
        f"{sync.S3_PREFIX}/a1/.revision",
    ]


def test_download_profile_survives_one_missing_object(home, monkeypatch):
    """A profile that predates Task 7 (or a fresh one still mid-provision) may not have all
    four objects yet -- one missing GetObject must not abort the other three."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    calls = []

    class FlakyS3:
        def download_file(self, bucket, key, dest):
            if key.endswith("SOUL.md"):
                raise RuntimeError("NoSuchKey")
            calls.append(key)

    monkeypatch.setattr(sync, "_s3", lambda: FlakyS3())
    sync._download_profile("a1")  # must not raise
    assert calls == [
        f"{sync.S3_PREFIX}/a1/config.yaml",
        f"{sync.S3_PREFIX}/a1/.env",
        f"{sync.S3_PREFIX}/a1/.revision",
    ]
