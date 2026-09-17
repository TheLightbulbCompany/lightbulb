from core.services import hermes_profiles


def test_patch_deep_merges_and_reuploads(monkeypatch):
    stored = {"model": {"default": "arn:son", "provider": "bedrock"}, "memory": {"memory_enabled": True}}
    uploaded = {}
    monkeypatch.setattr(hermes_profiles, "read_profile_config", lambda o, a: stored)
    monkeypatch.setattr(hermes_profiles, "_upload_config_yaml", lambda o, a, cfg: uploaded.update(cfg=cfg))
    out = hermes_profiles.patch_profile_config("org_a", "a1", {"memory": {"memory_enabled": False}})
    assert uploaded["cfg"]["memory"] == {"memory_enabled": False}
    assert uploaded["cfg"]["model"]["default"] == "arn:son"  # untouched keys survive the merge
    assert out["memory"]["memory_enabled"] is False


def test_set_replaces_wholesale_not_merged(monkeypatch):
    """M4-R1: config.set is a REPLACE, unlike config.patch's deep merge — a key present in the
    stored config but absent from the new one must not survive."""
    uploaded = {}
    monkeypatch.setattr(hermes_profiles, "_upload_config_yaml", lambda o, a, cfg: uploaded.update(cfg=cfg))
    out = hermes_profiles.set_profile_config("org_a", "a1", {"model": {"default": "new"}})
    assert uploaded["cfg"] == {"model": {"default": "new"}}
    assert out == {"model": {"default": "new"}}


def test_read_profile_config_never_reads_env(monkeypatch):
    """M4-R1: config.get must never surface .env content — it reads config.yaml alone."""
    calls = []
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: calls.append(key) or "model: {}\n")
    hermes_profiles.read_profile_config("org_a", "a1")
    assert calls == ["org_a/hermes/a1/config.yaml"]


def test_read_profile_config_is_empty_dict_when_the_object_is_missing(monkeypatch):
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)
    assert hermes_profiles.read_profile_config("org_a", "a1") == {}
