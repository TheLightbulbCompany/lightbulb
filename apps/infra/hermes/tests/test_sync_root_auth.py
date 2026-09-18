import json

from bridge import sync
from tests import factories as f


def _reset(monkeypatch):
    monkeypatch.setattr(sync, "_ROOT_AUTH_REVISION", None)
    monkeypatch.setattr(sync, "_ROOT_AUTH_ETAG", None, raising=False)
    monkeypatch.setattr(sync, "_ROOT_AUTH_HASH", None)


def test_a_grant_staged_between_two_turns_is_restored_on_the_second_turn(monkeypatch, home):
    """C3/M4-R44: restore_root_auth drops the restore-ONCE-per-process flag -- a
    stage_codex_auth landing after this microVM's first turn must still be picked up."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    revisions = iter(["r1", "r1", "r2"])  # unchanged, then a fresh grant staged before turn 3
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: next(revisions))
    downloads = []
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: downloads.append(key))
    (home / "auth.json").write_text("{}")

    sync.restore_root_auth()  # cold: pulls
    sync.restore_root_auth()  # same revision: no pull
    sync.restore_root_auth()  # backend staged a newer grant: pulls again

    assert downloads == ["auth.json", "auth.json"]


def test_restore_root_auth_short_circuits_after_the_first_check_for_an_owner_with_no_revision(monkeypatch, home):
    """NEW minor (fix round 2 continuation): almost every owner never connected ChatGPT, so
    auth.revision never exists and _ROOT_AUTH_REVISION started life staying None -- the guard
    (`_ROOT_AUTH_REVISION is not None`) never tripped, so every turn attempted a doomed
    download forever. The first check may still try once (there's no way to know in advance);
    every one after that must short-circuit."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: None)
    downloads = []
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: downloads.append(key))

    sync.restore_root_auth()
    sync.restore_root_auth()
    sync.restore_root_auth()

    assert downloads == ["auth.json"]  # only the cold-start attempt


def test_restore_root_auth_still_catches_a_revision_that_first_appears(monkeypatch, home):
    """An owner who just connected ChatGPT for the FIRST time -- the sentinel from prior absent
    turns must not permanently suppress the eventual real revision."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    revisions = iter([None, None, "r1"])
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: next(revisions))
    downloads = []
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: downloads.append(key))

    sync.restore_root_auth()  # cold, absent
    sync.restore_root_auth()  # still absent -- short-circuits
    sync.restore_root_auth()  # first staging -- must download

    assert downloads == ["auth.json", "auth.json"]


def test_a_stale_local_copy_never_overwrites_a_newer_staged_grant(monkeypatch, home):
    """C3: Hermes rotated the token locally, but the backend staged an even newer grant mid-
    turn -- uploading over it would silently discard the fresher one. The next turn's restore
    picks the staged grant up instead."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: "r1")
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: None)
    (home / "auth.json").write_text('{"active_provider": "openai-codex"}')
    sync.restore_root_auth()  # baseline hash recorded at revision r1

    # Hermes rewrote the local file in place during the turn (a refresh).
    (home / "auth.json").write_text('{"active_provider": "openai-codex", "rotated": true}')
    # ...but the backend staged a DIFFERENT grant meanwhile -- the revision moved.
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: "r2")
    uploads = []
    monkeypatch.setattr(sync, "_upload_object", lambda src, key: uploads.append(key))

    sync.save_root_auth()

    assert uploads == []  # the stale local rotation must not clobber the staged r2 grant


class _PreconditionFailed(RuntimeError):
    response = {"Error": {"Code": "PreconditionFailed"}}


class _Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self):
        return self.value


class _InterleavingAuthS3:
    """Actual conditional-PUT semantics plus an unstage after the writer's version check."""

    def __init__(self):
        self.auth = json.dumps({"providers": {"openai-codex": {"tokens": {"refresh_token": "old"}}}}).encode()
        self.auth_etag = '"v1"'
        self.revision = b"r1"
        self.raced = False

    def get_object(self, *, Bucket, Key):
        if Key.endswith("auth.revision"):
            return {"Body": _Body(self.revision), "ETag": '"revision-v1"'}
        return {"Body": _Body(self.auth), "ETag": self.auth_etag}

    def head_object(self, *, Bucket, Key):
        return {"ETag": self.auth_etag}

    def download_file(self, bucket, key, dest):
        with open(dest, "wb") as target:
            target.write(self.auth)

    def upload_file(self, path, bucket, key):
        self._unstage_between_check_and_write()
        with open(path, "rb") as source:
            self.auth = source.read()  # old implementation resurrects the stale grant
        self.auth_etag = '"v3"'

    def put_object(self, *, Bucket, Key, Body, **conditions):
        if Key.endswith("auth.revision"):
            self.revision = Body if isinstance(Body, bytes) else Body.encode()
            return {"ETag": '"revision-v2"'}
        self._unstage_between_check_and_write()
        if conditions.get("IfMatch") != self.auth_etag:
            raise _PreconditionFailed("auth.json changed")
        self.auth = Body if isinstance(Body, bytes) else Body.encode()
        self.auth_etag = '"v3"'
        return {"ETag": self.auth_etag}

    def _unstage_between_check_and_write(self):
        if self.raced:
            return
        self.auth = json.dumps({"providers": {}}).encode()
        self.auth_etag = '"v2"'
        self.revision = b"r2"
        self.raced = True


def test_warm_save_cannot_resurrect_a_grant_unstaged_after_its_version_check(monkeypatch, home):
    """The verified race: a warm turn passes its old revision check, disconnect lands, then the
    turn saves its rotated local copy. The auth object's own ETag must fence that stale writer."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "S3_PREFIX", "org_a/hermes")
    s3 = _InterleavingAuthS3()
    monkeypatch.setattr(sync, "_s3", lambda: s3)

    sync.restore_root_auth()
    (home / "auth.json").write_text(
        json.dumps({"providers": {"openai-codex": {"tokens": {"refresh_token": "rotated-locally"}}}})
    )
    try:
        sync.save_root_auth()
    except _PreconditionFailed as exc:
        assert exc.response["Error"]["Code"] == "PreconditionFailed"

    assert "openai-codex" not in json.loads(s3.auth)["providers"]


def test_an_unchanged_local_file_uploads_nothing(monkeypatch, home):
    """C3/minor 4: this turn never touched auth.json at all -- no upload, and (per minor 4) no
    warning-level log for the ordinary case of an owner with no ChatGPT OAuth connected."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: "r1")
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: None)
    (home / "auth.json").write_text("{}")
    sync.restore_root_auth()

    uploads = []
    monkeypatch.setattr(sync, "_upload_object", lambda src, key: uploads.append(key))
    sync.save_root_auth()  # the file on disk is byte-identical to what restore just saw

    assert uploads == []


def test_save_root_auth_is_a_no_op_when_there_is_no_local_auth_json(monkeypatch, home):
    """minor 4: an owner who has never connected ChatGPT OAuth has no local auth.json at all --
    silently skip, no warning-level log."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")

    def _fail(*a, **k):
        raise AssertionError("must not touch S3 with no local auth.json")

    monkeypatch.setattr(sync, "_upload_object", _fail)
    monkeypatch.setattr(sync, "_root_auth_revision", _fail)
    sync.save_root_auth()  # must not raise


def test_a_rotated_local_grant_uploads_and_bumps_the_revision_when_nothing_else_moved(monkeypatch, home):
    """The normal happy path: Hermes rotated the token this turn, nobody else staged a newer
    grant meanwhile -- upload, then advance the revision so the NEXT process sees it changed."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: "r1")
    monkeypatch.setattr(sync, "_root_auth_etag", lambda: '"v1"')
    monkeypatch.setattr(sync, "_download_object", lambda key, dest: None)
    (home / "auth.json").write_text('{"v": 1}')
    sync.restore_root_auth()

    (home / "auth.json").write_text('{"v": 2}')  # rotated in place during the turn
    uploads = []
    new_revisions = []

    class FakeS3:
        def put_object(self, *, Bucket, Key, Body):
            new_revisions.append(Body.decode() if isinstance(Body, bytes) else Body)

    monkeypatch.setattr(
        sync,
        "_upload_object",
        lambda src, key, *, if_match: uploads.append((key, if_match)) or '"v2"',
    )
    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())

    sync.save_root_auth()

    assert uploads == [("auth.json", '"v1"')]
    assert sync._ROOT_AUTH_ETAG == '"v2"'
    assert len(new_revisions) == 1 and new_revisions[0] != "r1"
    assert sync._ROOT_AUTH_REVISION == new_revisions[0]  # recorded, so the next save compares to it


def test_root_auth_is_a_no_op_without_a_bucket(monkeypatch, home):
    """Same convention as restore_profile/save_profile: no S3_BUCKET means dev/test."""
    _reset(monkeypatch)
    monkeypatch.setattr(sync, "S3_BUCKET", "")

    def _fail(*a, **k):
        raise AssertionError("must not touch S3 without a bucket")

    monkeypatch.setattr(sync, "_download_object", _fail)
    monkeypatch.setattr(sync, "_upload_object", _fail)
    monkeypatch.setattr(sync, "_root_auth_revision", _fail)
    sync.restore_root_auth()  # must not raise
    (home / "auth.json").write_text("{}")
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

        def put_object(self, *, Bucket, Key, Body, IfMatch):
            calls.append(("put", Bucket, Key, IfMatch, Body))
            return {"ETag": '"new"'}

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())
    (home / "auth.json").write_text("{}")
    sync._download_object("auth.json", sync.HOME / "auth.json")
    assert sync._upload_object(sync.HOME / "auth.json", "auth.json", if_match='"old"') == '"new"'
    assert calls == [
        ("get", "bucket-under-test", "org_a/hermes/auth.json"),
        ("put", "bucket-under-test", "org_a/hermes/auth.json", '"old"', b"{}"),
    ]


def test_root_auth_revision_reads_the_marker_object(home, monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "S3_PREFIX", "org_a/hermes")

    class FakeS3:
        def get_object(self, *, Bucket, Key):
            assert (Bucket, Key) == ("bucket-under-test", "org_a/hermes/auth.revision")
            return {"Body": type("B", (), {"read": lambda self: b"abc123\n"})()}

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())
    assert sync._root_auth_revision() == "abc123"


def test_root_auth_revision_is_none_when_unreadable(home, monkeypatch):
    """No auth.revision yet (never staged) is not an error -- same convention as _read_revision
    for a profile's own .revision."""

    class BoomS3:
        def get_object(self, *, Bucket, Key):
            raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(sync, "_s3", lambda: BoomS3())
    assert sync._root_auth_revision() is None


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
