from core.services.marketplace.requirements import Disposition, Requirement


def test_requirement_to_manifest_roundtrips_fields():
    r = Requirement(
        key="stripe",
        display_name="Stripe",
        kind="external_api",
        disposition=Disposition.BYO,
        env=["STRIPE_API_KEY"],
        detected_via=["pipeline:daily-feed:stripe-feed", "env:STRIPE_API_KEY"],
        bundled=False,
        required=True,
        note=None,
        reason="wiring mismatch: tab wires Stripe MCP/STRIPE_SECRET_KEY",
    )
    d = r.to_manifest()
    assert d["key"] == "stripe"
    assert d["disposition"] == "byo"
    assert d["env"] == ["STRIPE_API_KEY"]
    assert d["required"] is True
    assert "wiring mismatch" in d["reason"]


def test_disposition_values_are_stable_strings():
    assert Disposition.INCLUDED == "included"
    assert Disposition.CONNECT == "connect"
    assert Disposition.BYO == "byo"
    assert Disposition.UNAVAILABLE == "unavailable"
