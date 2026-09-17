import json

from core.services import hermes_profiles


def test_stage_writes_the_root_auth_json_not_a_per_profile_one(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.update({key: body}))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt", "id_token": "it"})
    assert set(puts) == {"org_a/hermes/auth.json", "org_a/hermes/auth.revision"}
    blob = json.loads(puts["org_a/hermes/auth.json"])
    assert blob["active_provider"] == "openai-codex"
    state = blob["providers"]["openai-codex"]
    assert state["tokens"] == {"access_token": "at", "refresh_token": "rt", "id_token": "it"}
    assert state["auth_mode"] == "chatgpt"
    assert state["last_refresh"].endswith("Z")


def test_stage_drops_an_empty_token_rather_than_writing_a_null(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.update({key: body}))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt", "id_token": None})
    state = json.loads(puts["org_a/hermes/auth.json"])["providers"]["openai-codex"]
    assert "id_token" not in state["tokens"]


def test_stage_is_a_no_op_without_a_bucket(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda *a: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda *a: puts.setdefault("x", 1))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})
    assert puts == {}


def test_stage_preserves_other_providers_state(monkeypatch):
    """Minor 6 (fix round 1 continuation): read-modify-write -- an existing auth.json holding
    another provider's state must survive a ChatGPT-OAuth stage untouched."""
    existing_blob = {"active_provider": "anthropic", "providers": {"anthropic": {"tokens": {"api_key": "sk-x"}}}}
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: json.dumps(existing_blob))
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.update({key: body}))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})
    blob = json.loads(puts["org_a/hermes/auth.json"])
    assert blob["providers"]["anthropic"] == {"tokens": {"api_key": "sk-x"}}
    assert blob["providers"]["openai-codex"]["tokens"]["access_token"] == "at"
    assert blob["active_provider"] == "openai-codex"


def test_stage_writes_a_uuid4_revision_last(monkeypatch):
    """C3/M4-R44: the same .revision-written-LAST contract a profile's own writers use, so the
    bridge's restore_root_auth (per-turn revision compare) notices a staged grant."""
    import re

    order = []
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: order.append((key, body)))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})
    assert order[-1][0] == "org_a/hermes/auth.revision"
    assert re.fullmatch(r"[0-9a-f]{32}", order[-1][1])


def test_root_home_prefix_is_owner_first_no_profile_segment():
    assert hermes_profiles.root_home_prefix("org_a") == "org_a/hermes/"
