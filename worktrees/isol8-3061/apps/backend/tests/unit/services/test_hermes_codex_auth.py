import json

import pytest
from botocore.exceptions import ClientError

from core.services import hermes_profiles


def test_stage_writes_the_root_auth_json_not_a_per_profile_one(monkeypatch):
    puts = _wire(monkeypatch, None)
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt", "id_token": "it"})
    assert set(puts) == {"org_a/hermes/auth.json", "org_a/hermes/auth.revision"}
    blob = json.loads(puts["org_a/hermes/auth.json"])
    assert blob["active_provider"] == "openai-codex"
    state = blob["providers"]["openai-codex"]
    assert state["tokens"] == {"access_token": "at", "refresh_token": "rt", "id_token": "it"}
    assert state["auth_mode"] == "chatgpt"
    assert state["last_refresh"].endswith("Z")


def test_stage_drops_an_empty_token_rather_than_writing_a_null(monkeypatch):
    puts = _wire(monkeypatch, None)
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt", "id_token": None})
    state = json.loads(puts["org_a/hermes/auth.json"])["providers"]["openai-codex"]
    assert "id_token" not in state["tokens"]


def test_stage_fails_when_the_authoritative_bucket_is_not_configured(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    with pytest.raises(RuntimeError, match="OWNER_STATE_BUCKET"):
        hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})


def test_stage_preserves_other_providers_state(monkeypatch):
    """Minor 6 (fix round 1 continuation): read-modify-write -- an existing auth.json holding
    another provider's state must survive a ChatGPT-OAuth stage untouched."""
    existing_blob = {"active_provider": "anthropic", "providers": {"anthropic": {"tokens": {"api_key": "sk-x"}}}}
    puts = _wire(monkeypatch, json.dumps(existing_blob))
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})
    blob = json.loads(puts["org_a/hermes/auth.json"])
    assert blob["providers"]["anthropic"] == {"tokens": {"api_key": "sk-x"}}
    assert blob["providers"]["openai-codex"]["tokens"]["access_token"] == "at"
    assert blob["active_provider"] == "openai-codex"


def test_a_reconcile_stage_never_replaces_an_existing_grant(monkeypatch):
    """only_if_absent: the stored grant may carry a refresh token the runtime has rotated since;
    overwriting it with the connect-time one would revoke the whole token family."""
    existing = {"providers": {"openai-codex": {"tokens": {"refresh_token": "rotated"}}}}
    puts = _wire(monkeypatch, json.dumps(existing))
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "old"}, only_if_absent=True)
    assert puts == {}

    puts = _wire(monkeypatch, None)
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"}, only_if_absent=True)
    assert json.loads(puts["org_a/hermes/auth.json"])["providers"]["openai-codex"]["tokens"]["refresh_token"] == "rt"


def test_stage_writes_a_uuid4_revision_last(monkeypatch):
    """C3/M4-R44: the same .revision-written-LAST contract a profile's own writers use, so the
    bridge's restore_root_auth (per-turn revision compare) notices a staged grant."""
    import re

    order = []
    monkeypatch.setattr(hermes_profiles, "_s3_get_versioned", lambda bucket, key: (None, None))
    monkeypatch.setattr(
        hermes_profiles,
        "_s3_put_conditionally",
        lambda bucket, key, body, etag: order.append((key, body)),
    )
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: order.append((key, body)))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})
    assert order[-1][0] == "org_a/hermes/auth.revision"
    assert re.fullmatch(r"[0-9a-f]{32}", order[-1][1])


def test_root_home_prefix_is_owner_first_no_profile_segment():
    assert hermes_profiles.root_home_prefix("org_a") == "org_a/hermes/"


# ---- unstage (M6 task-14 row 3 / task-15 item 2) -------------------------------------------


def _wire(monkeypatch, existing):
    puts = {}
    etag = '"etag"' if existing is not None else None
    monkeypatch.setattr(hermes_profiles, "_s3_get_versioned", lambda bucket, key: (existing, etag))
    monkeypatch.setattr(
        hermes_profiles,
        "_s3_put_conditionally",
        lambda bucket, key, body, _etag: puts.update({key: body}),
    )
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.update({key: body}))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    return puts


def test_unstage_removes_the_grant_and_bumps_the_revision_last(monkeypatch):
    """There was no unstage at all, so "Disconnect ChatGPT" left the staged grant in S3 and the
    agents kept running on the account the owner had just revoked."""
    puts = _wire(monkeypatch, json.dumps({"active_provider": "openai-codex", "providers": {"openai-codex": {"t": 1}}}))

    assert hermes_profiles.unstage_codex_auth("org_a") is True

    blob = json.loads(puts["org_a/hermes/auth.json"])
    assert blob["providers"] == {}
    assert "active_provider" not in blob
    assert list(puts)[-1] == "org_a/hermes/auth.revision"


def test_unstage_preserves_other_providers_state(monkeypatch):
    """Mirror of the stage side: only ``openai-codex`` leaves."""
    puts = _wire(
        monkeypatch,
        json.dumps({"active_provider": "anthropic", "providers": {"anthropic": {"k": 1}, "openai-codex": {"t": 1}}}),
    )

    assert hermes_profiles.unstage_codex_auth("org_a") is True

    blob = json.loads(puts["org_a/hermes/auth.json"])
    assert blob["providers"] == {"anthropic": {"k": 1}}
    assert blob["active_provider"] == "anthropic"


def test_unstage_writes_nothing_when_there_is_no_grant(monkeypatch):
    for existing in (None, "", json.dumps({"providers": {"anthropic": {}}})):
        puts = _wire(monkeypatch, existing)
        assert hermes_profiles.unstage_codex_auth("org_a") is False
        assert puts == {}


def test_unstage_fails_when_the_authoritative_bucket_is_not_configured(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    with pytest.raises(RuntimeError, match="OWNER_STATE_BUCKET"):
        hermes_profiles.unstage_codex_auth("org_a")


def test_unstage_rejects_a_corrupt_auth_document_instead_of_calling_it_absent(monkeypatch):
    _wire(monkeypatch, "not json")
    with pytest.raises(ValueError):
        hermes_profiles.unstage_codex_auth("org_a")


class _Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self):
        return self.value


class _ConditionalAuthS3:
    """Tiny in-memory S3 model that injects one competing write before the first auth PUT."""

    def __init__(self, initial: dict, competing: dict):
        self.auth = json.dumps(initial).encode()
        self.etag = '"v1"'
        self.competing = json.dumps(competing).encode()
        self.raced = False
        self.revision_writes = 0

    def get_object(self, *, Bucket, Key):
        assert Key.endswith("auth.json")
        return {"Body": _Body(self.auth), "ETag": self.etag}

    def put_object(self, *, Bucket, Key, Body, **conditions):
        if Key.endswith("auth.revision"):
            self.revision_writes += 1
            return {"ETag": '"revision"'}
        assert Key.endswith("auth.json")
        if not self.raced:
            self.auth = self.competing
            self.etag = '"v2"'
            self.raced = True
        expected = conditions.get("IfMatch")
        if expected is not None and expected != self.etag:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if conditions.get("IfNoneMatch") == "*" and self.auth is not None:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.auth = Body if isinstance(Body, bytes) else Body.encode()
        self.etag = '"v3"'
        return {"ETag": self.etag}


def test_stage_retries_a_conditional_conflict_and_keeps_the_competing_provider_update(monkeypatch):
    s3 = _ConditionalAuthS3(
        {"providers": {"anthropic": {"tokens": {"api_key": "old"}}}},
        {"providers": {"anthropic": {"tokens": {"api_key": "rotated"}}}},
    )
    monkeypatch.setattr(hermes_profiles, "_SHARED_S3_CLIENT", s3)
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")

    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})

    blob = json.loads(s3.auth)
    assert blob["providers"]["anthropic"]["tokens"]["api_key"] == "rotated"
    assert blob["providers"]["openai-codex"]["tokens"]["access_token"] == "at"
    assert s3.revision_writes == 1


def test_unstage_retries_a_conditional_conflict_and_keeps_other_providers(monkeypatch):
    s3 = _ConditionalAuthS3(
        {"active_provider": "openai-codex", "providers": {"openai-codex": {"t": 1}, "anthropic": {"k": 1}}},
        {"active_provider": "openai-codex", "providers": {"openai-codex": {"t": 2}, "anthropic": {"k": 2}}},
    )
    monkeypatch.setattr(hermes_profiles, "_SHARED_S3_CLIENT", s3)
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")

    assert hermes_profiles.unstage_codex_auth("org_a") is True

    blob = json.loads(s3.auth)
    assert blob["providers"] == {"anthropic": {"k": 2}}
    assert "active_provider" not in blob
    assert s3.revision_writes == 1
