from pathlib import Path

from bridge import sync


def test_a_changed_revision_triggers_a_re_pull(monkeypatch):
    pulls = []
    revisions = iter(["1", "1", "2"])
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: next(revisions))
    # returns True: ensure_profile only records a revision whose re-pull COMPLETED (fix round 1, I2)
    monkeypatch.setattr(sync, "_download_profile", lambda agent_id: (pulls.append(agent_id), True)[1])
    sync._REVISIONS.clear()
    sync.ensure_profile("a1")  # cold: pull
    sync.ensure_profile("a1")  # same revision: no pull
    sync.ensure_profile("a1")  # bumped: pull
    assert pulls == ["a1", "a1"]


def test_an_unreadable_revision_does_not_re_pull_a_warm_profile(monkeypatch):
    pulls = []
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: None)
    # returns True: ensure_profile only records a revision whose re-pull COMPLETED (fix round 1, I2)
    monkeypatch.setattr(sync, "_download_profile", lambda agent_id: (pulls.append(agent_id), True)[1])
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


def test_download_profile_pulls_exactly_the_backend_owned_files(home, monkeypatch):
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
        f"{sync.S3_PREFIX}/a1/lightbulb-facts.json",
        f"{sync.S3_PREFIX}/a1/VOICE_NOTES.md",
    ]


def test_restore_profile_seeds_revisions_from_the_freshly_downloaded_revision_file(home, monkeypatch):
    """Minor 12 (fix round 1 continuation): the full tree walk restore_profile just did already
    downloaded .revision (if the profile has one) along with everything else -- seed _REVISIONS
    from that local copy so ensure_profile's very next call doesn't immediately think a hot
    patch landed and re-pull the same four files it just restored."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")

    class FakeS3:
        def get_paginator(self, name):
            class Paginator:
                def paginate(self, Bucket, Prefix):
                    return [{"Contents": [{"Key": f"{Prefix}config.yaml"}, {"Key": f"{Prefix}.revision"}]}]

            return Paginator()

        def download_file(self, bucket, key, dest):
            (sync._profile_dir("a1") / Path(key).name).write_text("r5" if key.endswith(".revision") else "x")

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())
    sync._restored.discard("a1")
    sync._REVISIONS.pop("a1", None)
    sync.restore_profile("a1")
    assert sync._REVISIONS["a1"] == "r5"

    # And ensure_profile's very next call, still seeing revision "r5", must not re-pull.
    pulls = []
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: "r5")
    # returns True: ensure_profile only records a revision whose re-pull COMPLETED (fix round 1, I2)
    monkeypatch.setattr(sync, "_download_profile", lambda agent_id: (pulls.append(agent_id), True)[1])
    sync.ensure_profile("a1")
    assert pulls == []


def test_pull_new_mcp_token_files_seeds_a_new_token_and_leaves_an_existing_one_untouched(home, monkeypatch):
    """F2 (predeploy review, M6-R35): a warm re-pull seeds a token file for a server the
    microVM has never seen, but never overwrites one it already has -- Hermes's own
    OAuthClientProvider is the sole rotator of an already-seeded file."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    dest = sync._profile_dir("a1") / "mcp-tokens"
    dest.mkdir(parents=True)
    (dest / "stripe.json").write_text('{"access_token": "already-rotated"}')

    downloaded = []

    class FakeS3:
        def get_paginator(self, name):
            assert name == "list_objects_v2"

            class Paginator:
                def paginate(self, Bucket, Prefix):
                    assert Prefix == f"{sync.S3_PREFIX}/a1/mcp-tokens/"
                    return [
                        {
                            "Contents": [
                                {"Key": f"{Prefix}stripe.json"},  # already seeded -- must be skipped
                                {"Key": f"{Prefix}qbo.json"},  # never seen -- must be pulled
                            ]
                        }
                    ]

            return Paginator()

        def download_file(self, bucket, key, target):
            downloaded.append(key)
            Path(target).write_text('{"access_token": "fresh"}')

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())
    sync._pull_new_mcp_token_files("a1")

    assert downloaded == [f"{sync.S3_PREFIX}/a1/mcp-tokens/qbo.json"]
    assert (dest / "stripe.json").read_text() == '{"access_token": "already-rotated"}'
    assert (dest / "qbo.json").read_text() == '{"access_token": "fresh"}'


def test_pull_new_mcp_token_files_is_best_effort_on_a_listing_failure(home, monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")

    class ExplodingS3:
        def get_paginator(self, name):
            raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(sync, "_s3", lambda: ExplodingS3())
    sync._pull_new_mcp_token_files("a1")  # must not raise


def test_download_profile_also_pulls_new_mcp_tokens(home, monkeypatch):
    """_download_profile (ensure_profile's revision-changed path) triggers the mcp-tokens
    re-pull, not just the five named backend-owned files."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    calls = []

    class FakeS3:
        def download_file(self, bucket, key, dest):
            calls.append(key)

        def get_paginator(self, name):
            class Paginator:
                def paginate(self, Bucket, Prefix):
                    return [{"Contents": [{"Key": f"{Prefix}qbo.json"}]}]

            return Paginator()

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())
    sync._download_profile("a1")
    assert f"{sync.S3_PREFIX}/a1/mcp-tokens/qbo.json" in calls


def test_download_profile_survives_one_missing_object(home, monkeypatch):
    """A profile that predates Task 7 (or a fresh one still mid-provision) may not have all
    backend-owned objects yet -- one missing GetObject must not abort the rest."""
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
        f"{sync.S3_PREFIX}/a1/lightbulb-facts.json",
        f"{sync.S3_PREFIX}/a1/VOICE_NOTES.md",
    ]
