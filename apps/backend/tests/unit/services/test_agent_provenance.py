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
