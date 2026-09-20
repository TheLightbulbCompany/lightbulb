"""Q9a row #3 (task-9-scan.md): hermes_profiles.delete_profile -- the AgentCore equivalent of
removing an ``agents.list`` entry from ``openclaw.json``. There is no config file to edit, so
this agent's own S3 profile prefix is the durable record to remove."""

from core.services import hermes_profiles


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class _FakeClient:
    def __init__(self, pages):
        self._paginator = _FakePaginator(pages)
        self.deleted: list[tuple[str, list[dict]]] = []
        self.deleted_objects: list[tuple[str, str]] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self._paginator

    def delete_objects(self, *, Bucket, Delete):
        self.deleted.append((Bucket, Delete["Objects"]))

    def delete_object(self, *, Bucket, Key):
        self.deleted_objects.append((Bucket, Key))


def test_delete_profile_deletes_every_object_under_the_prefix_one_call_per_page(monkeypatch):
    pages = [
        {"Contents": [{"Key": "owner_1/hermes/agent_1/config.yaml"}, {"Key": "owner_1/hermes/agent_1/.env"}]},
        {"Contents": [{"Key": "owner_1/hermes/agent_1/SOUL.md"}]},
    ]
    client = _FakeClient(pages)
    monkeypatch.setattr(hermes_profiles, "_shared_s3_client", lambda: client)
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "test-bucket")

    hermes_profiles.delete_profile("owner_1", "agent_1")

    assert client.deleted == [
        ("test-bucket", [{"Key": "owner_1/hermes/agent_1/config.yaml"}, {"Key": "owner_1/hermes/agent_1/.env"}]),
        ("test-bucket", [{"Key": "owner_1/hermes/agent_1/SOUL.md"}]),
    ]
    assert client.deleted_objects == [("test-bucket", "marketplace-installs/owner_1/agent_1.json")]


def test_delete_profile_skips_empty_pages(monkeypatch):
    client = _FakeClient([{"Contents": []}, {}])
    monkeypatch.setattr(hermes_profiles, "_shared_s3_client", lambda: client)
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "test-bucket")

    hermes_profiles.delete_profile("owner_1", "agent_1")

    assert client.deleted == []
    assert client.deleted_objects == [("test-bucket", "marketplace-installs/owner_1/agent_1.json")]


def test_delete_profile_noops_without_a_configured_bucket(monkeypatch):
    def _must_not_be_called():
        raise AssertionError("must not build a client without a configured bucket")

    monkeypatch.setattr(hermes_profiles, "_shared_s3_client", _must_not_be_called)
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")

    hermes_profiles.delete_profile("owner_1", "agent_1")  # must not raise
