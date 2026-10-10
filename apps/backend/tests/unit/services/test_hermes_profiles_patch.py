from core.services import hermes_profiles


def test_patch_deep_merges_and_reuploads(monkeypatch):
    stored = {"model": {"default": "arn:son", "provider": "bedrock"}, "memory": {"memory_enabled": True}}
    uploaded = {}
    monkeypatch.setattr(hermes_profiles, "read_profile_config", lambda o, a, **kw: stored)
    monkeypatch.setattr(hermes_profiles, "_upload_config_yaml", lambda o, a, cfg: uploaded.update(cfg=cfg))
    out = hermes_profiles.patch_profile_config("org_a", "a1", {"memory": {"memory_enabled": False}})
    assert uploaded["cfg"]["memory"] == {"memory_enabled": False}
    assert uploaded["cfg"]["model"]["default"] == "arn:son"  # untouched keys survive the merge
    assert out["memory"]["memory_enabled"] is False


def test_patch_raises_when_the_profile_has_no_config_yaml_yet(monkeypatch):
    """C4: a patch onto a never-provisioned profile is a defect upstream, not something to
    merge onto {} and upload as if it were the whole profile."""
    import pytest

    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)

    def _must_not_upload(*a, **k):
        raise AssertionError("must not upload onto a missing config.yaml")

    monkeypatch.setattr(hermes_profiles, "_upload_config_yaml", _must_not_upload)
    with pytest.raises(FileNotFoundError):
        hermes_profiles.patch_profile_config("org_a", "a1", {"memory": {"memory_enabled": False}})


def test_patch_propagates_a_transient_s3_error_instead_of_treating_it_as_absent(monkeypatch):
    """C4: a 503/SlowDown must propagate, not be swallowed into the same 'absent' signal as a
    genuine NoSuchKey."""
    import botocore.exceptions
    import pytest

    def _boom(*, Bucket, Key):
        raise botocore.exceptions.ClientError({"Error": {"Code": "SlowDown"}}, "GetObject")

    monkeypatch.setattr(
        hermes_profiles, "_shared_s3_client", lambda: type("C", (), {"get_object": staticmethod(_boom)})()
    )

    def _must_not_upload(*a, **k):
        raise AssertionError("must not upload after a transient S3 failure")

    monkeypatch.setattr(hermes_profiles, "_upload_config_yaml", _must_not_upload)
    with pytest.raises(botocore.exceptions.ClientError):
        hermes_profiles.patch_profile_config("org_a", "a1", {"memory": {"memory_enabled": False}})


def test_read_profile_config_never_reads_env(monkeypatch):
    """M4-R1: config.get must never surface .env content — it reads config.yaml alone."""
    calls = []
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: calls.append(key) or "model: {}\n")
    hermes_profiles.read_profile_config("org_a", "a1")
    assert calls == ["org_a/hermes/a1/config.yaml"]


def test_read_profile_config_is_empty_dict_when_the_object_is_missing(monkeypatch):
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)
    assert hermes_profiles.read_profile_config("org_a", "a1") == {}


def test_read_profile_config_required_raises_instead_of_returning_empty(monkeypatch):
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)
    import pytest

    with pytest.raises(FileNotFoundError):
        hermes_profiles.read_profile_config("org_a", "a1", required=True)


def test_config_snapshot_hashes_the_stored_text_not_a_re_dump(monkeypatch):
    """M4-R43: the hash is over the STORED config.yaml text, so two calls against the same
    unchanged object always agree, and it changes exactly when the persisted bytes do."""
    import hashlib

    text = "model:\n  default: arn:son\n"
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: text)
    config, config_hash = hermes_profiles.config_snapshot("org_a", "a1")
    assert config == {"model": {"default": "arn:son"}}
    assert config_hash == hashlib.sha256(text.encode()).hexdigest()


def test_config_snapshot_of_a_never_provisioned_profile_is_an_empty_config_with_a_stable_hash(monkeypatch):
    import hashlib

    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)
    config, config_hash = hermes_profiles.config_snapshot("org_a", "a1")
    assert config == {}
    assert config_hash == hashlib.sha256(b"").hexdigest()


class _FakeClientErrorS3:
    """A minimal stand-in for boto3's S3 client that raises a real
    ``botocore.exceptions.ClientError`` shaped exactly like the real SDK does."""

    def __init__(self, code: str):
        self._code = code

    def get_object(self, *, Bucket, Key):
        import botocore.exceptions

        raise botocore.exceptions.ClientError({"Error": {"Code": self._code}}, "GetObject")


def test_s3_get_returns_none_only_for_nosuchkey_or_404(monkeypatch):
    for code in ("NoSuchKey", "404"):
        monkeypatch.setattr(hermes_profiles, "_shared_s3_client", lambda code=code: _FakeClientErrorS3(code))
        assert hermes_profiles._s3_get("b", "k") is None


def test_s3_get_reraises_every_other_client_error(monkeypatch):
    import botocore.exceptions
    import pytest

    monkeypatch.setattr(hermes_profiles, "_shared_s3_client", lambda: _FakeClientErrorS3("SlowDown"))
    with pytest.raises(botocore.exceptions.ClientError):
        hermes_profiles._s3_get("b", "k")


def test_s3_put_and_s3_get_share_one_cached_boto3_client(monkeypatch):
    """Minors 1+2: a fresh boto3.client("s3") per call was the old shape -- refresh_integrations
    alone can call _s3_put/_s3_get a dozen times per reconcile."""
    monkeypatch.setattr(hermes_profiles, "_SHARED_S3_CLIENT", None)
    builds = []

    class _Fake:
        def put_object(self, **kw):
            pass

        def get_object(self, **kw):
            return {"Body": type("B", (), {"read": lambda self: b"x"})()}

    def _client(*a, **k):
        builds.append(1)
        return _Fake()

    monkeypatch.setattr("boto3.client", _client)
    hermes_profiles._s3_put("b", "k1", "v1")
    hermes_profiles._s3_get("b", "k2")
    hermes_profiles._s3_put("b", "k3", "v3")
    assert len(builds) == 1
