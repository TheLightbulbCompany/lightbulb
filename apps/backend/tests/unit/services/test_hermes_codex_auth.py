import json

from core.services import hermes_profiles


def test_stage_writes_the_root_auth_json_not_a_per_profile_one(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.update({key: body}))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt", "id_token": "it"})
    assert set(puts) == {"org_a/hermes/auth.json"}
    blob = json.loads(puts["org_a/hermes/auth.json"])
    assert blob["active_provider"] == "openai-codex"
    state = blob["providers"]["openai-codex"]
    assert state["tokens"] == {"access_token": "at", "refresh_token": "rt", "id_token": "it"}
    assert state["auth_mode"] == "chatgpt"
    assert state["last_refresh"].endswith("Z")


def test_stage_drops_an_empty_token_rather_than_writing_a_null(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.update({key: body}))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt", "id_token": None})
    state = json.loads(puts["org_a/hermes/auth.json"])["providers"]["openai-codex"]
    assert "id_token" not in state["tokens"]


def test_stage_is_a_no_op_without_a_bucket(monkeypatch):
    puts = {}
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda *a: puts.setdefault("x", 1))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    hermes_profiles.stage_codex_auth("org_a", {"access_token": "at", "refresh_token": "rt"})
    assert puts == {}


def test_root_home_prefix_is_owner_first_no_profile_segment():
    assert hermes_profiles.root_home_prefix("org_a") == "org_a/hermes/"
