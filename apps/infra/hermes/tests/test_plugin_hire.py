"""Task 12: Tests for the hire_suggestion tool."""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402


def test_the_payload_uses_the_exact_kind_the_hire_card_matches():
    out = json.loads(lightbulb._hire_suggestion({
        "reason": "You need a bookkeeper.",
        "listings": [{"listing_id": "l1", "slug": "ledger", "name": "Ledger", "price_cents": 900}]}))
    assert out["kind"] == "isol8.hire_suggestion"
    assert out["listings"][0] == {"listing_id": "l1", "slug": "ledger", "name": "Ledger", "price_cents": 900}


def test_a_listing_without_a_slug_or_listing_id_is_dropped_not_shipped():
    out = json.loads(lightbulb._hire_suggestion({
        "reason": "r", "listings": [{"listing_id": "", "slug": "x"}, {"listing_id": "l2", "slug": "ok"}]}))
    assert [entry["slug"] for entry in out["listings"]] == ["ok"]


def test_an_empty_listings_array_returns_an_error_not_a_card_that_renders_nothing():
    assert "at least one listing" in lightbulb._hire_suggestion({"reason": "r", "listings": []})
