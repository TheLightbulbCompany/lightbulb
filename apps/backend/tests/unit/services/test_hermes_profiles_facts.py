"""Row #15 / Ruling M6-R30 — owner facts staged into every roster profile."""

from core.services import hermes_profiles


def test_writes_facts_then_revision_last_per_agent(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((key, body)))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-owner-state")

    hermes_profiles.write_facts_to_profiles("org_a", ["ceo", "eng"], '{"plan": "Workspace"}')

    keys = [key for key, _ in puts]
    assert keys == [
        "org_a/hermes/ceo/lightbulb-facts.json",
        "org_a/hermes/ceo/.revision",
        "org_a/hermes/eng/lightbulb-facts.json",
        "org_a/hermes/eng/.revision",
    ]
    assert puts[0][1] == '{"plan": "Workspace"}'
    assert puts[2][1] == '{"plan": "Workspace"}'
    # Two distinct revisions -- not a shared/stale value across agents.
    assert puts[1][1] != puts[3][1]


def test_is_a_no_op_without_a_bucket(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")

    def _fail(*a, **k):
        raise AssertionError("must not touch S3 without a configured bucket")

    monkeypatch.setattr(hermes_profiles, "_s3_put", _fail)
    hermes_profiles.write_facts_to_profiles("org_a", ["ceo"], "{}")  # must not raise


def test_facts_filename_matches_owner_facts_module():
    from core.services import owner_facts

    assert hermes_profiles.FACTS_FILENAME == owner_facts.FACTS_FILENAME
