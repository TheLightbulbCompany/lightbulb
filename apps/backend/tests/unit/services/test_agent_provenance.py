import json
import sys
from types import SimpleNamespace
import pytest

from core.services import agent_provenance
from core.services.agent_provenance import marketplace_provenance, stamp_agent_provenance


def test_seed_origin_wins_without_name_or_role_inference():
    seeded = stamp_agent_provenance(
        {"id": "seed-id", "name": "Renamed"},
        seeded_ids={"seed-id"},
        sidecar={
            "listing_id": "listing-id",
            "template_slug": "impostor",
            "template_version": 1,
            "seller_id": "seller-id",
        },
    )
    custom_named_friday = stamp_agent_provenance(
        {"id": "custom-id", "name": "Friday", "role": "ceo"},
        seeded_ids={"seed-id"},
    )

    assert seeded["origin"] == "Seed"
    assert "marketplace" not in seeded
    assert custom_named_friday["origin"] == "Custom"


def test_legacy_or_partial_sidecar_is_custom():
    legacy = {
        "template_slug": "pulse",
        "template_version": 2,
        "seller_id": "seller-id",
    }

    assert marketplace_provenance(legacy) is None
    assert stamp_agent_provenance({"id": "a"}, seeded_ids=set(), sidecar=legacy)["origin"] == "Custom"


def test_complete_sidecar_returns_canonical_marketplace_fields():
    stamped = stamp_agent_provenance(
        {"id": "a"},
        seeded_ids=set(),
        sidecar={
            "listing_id": "listing-id",
            "template_slug": "forge",
            "template_version": "3",
            "seller_id": "seller-id",
        },
    )

    assert stamped == {
        "id": "a",
        "seeded": False,
        "origin": "Marketplace",
        "marketplace": {
            "listingId": "listing-id",
            "slug": "forge",
            "version": 3,
            "seller": "seller-id",
        },
    }


def test_starter_origin_wins_over_marketplace_without_seed_protection():
    sidecar = {
        "listing_id": "listing-id",
        "template_slug": "forge",
        "template_version": 3,
        "seller_id": "seller-id",
    }

    starter = stamp_agent_provenance(
        {"id": "agent-ceo"},
        seeded_ids=set(),
        starter_ids={"agent-ceo"},
        sidecar=sidecar,
    )
    legacy_seed = stamp_agent_provenance(
        {"id": "agent-ceo"},
        seeded_ids={"agent-ceo"},
        starter_ids={"agent-ceo"},
        sidecar=sidecar,
    )

    assert starter == {"id": "agent-ceo", "seeded": False, "origin": "Starter"}
    assert legacy_seed == {"id": "agent-ceo", "seeded": True, "origin": "Seed"}


@pytest.mark.asyncio
async def test_load_sidecars_reads_only_valid_backend_markers(monkeypatch):
    markers = {
        "good": {"agent_id": "good", "listing_id": "l1", "template_slug": "x", "template_version": 1, "seller_id": "s"},
        "wrong": {
            "agent_id": "other",
            "listing_id": "l2",
            "template_slug": "y",
            "template_version": 1,
            "seller_id": "s",
        },
        "bad": {"agent_id": "bad", "listing_id": "l3"},
    }
    monkeypatch.setattr(agent_provenance, "_read_sidecar", lambda _owner, agent: markers.get(agent))

    result = await agent_provenance._load_agent_sidecars("owner", ["good", "wrong", "bad"])

    assert set(result) == {"good"}
    assert marketplace_provenance(result["good"])["listingId"] == "l1"


@pytest.mark.asyncio
async def test_write_marker_serializes_trusted_identity(monkeypatch):
    monkeypatch.setattr(agent_provenance.settings, "OWNER_STATE_BUCKET", "bucket")
    captured = {}

    class S3:
        def put_object(self, **kwargs):
            captured.update(kwargs)

    async def run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(agent_provenance, "run_in_thread", run)
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda _name: S3()))

    await agent_provenance.write_marketplace_provenance(
        owner_id="owner", agent_id="agent", listing_id="listing", slug="forge", version=3, seller_id="seller"
    )

    assert captured["Bucket"] == "bucket"
    assert captured["Key"] == "marketplace-installs/owner/agent.json"
    marker = json.loads(captured["Body"].decode())
    assert marker["agent_id"] == "agent"
    assert marker["listing_id"] == "listing"
    assert marker["template_version"] == 3


@pytest.mark.asyncio
async def test_write_marker_rejects_missing_bucket(monkeypatch):
    monkeypatch.setattr(agent_provenance.settings, "OWNER_STATE_BUCKET", "")

    with pytest.raises(RuntimeError, match="OWNER_STATE_BUCKET"):
        await agent_provenance.write_marketplace_provenance(
            owner_id="owner", agent_id="agent", listing_id="listing", slug="forge", version=1, seller_id="seller"
        )


def test_runtime_owner_policy_cannot_write_backend_install_marker():
    from fnmatch import fnmatchcase
    from core.billing.bedrock_aip import build_owner_litestream_s3_policy

    bucket = "lightbulb-dev-openclaw-replicas"
    policy = build_owner_litestream_s3_policy("org_test", bucket)
    marker = f"arn:aws:s3:::{bucket}/{agent_provenance.marketplace_provenance_key('org_test', 'agent')}"
    writable = [s["Resource"] for s in policy["Statement"] if "s3:PutObject" in s["Action"]]
    assert writable
    assert not any(fnmatchcase(marker, resource) for resource in writable)
