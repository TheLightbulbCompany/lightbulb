"""Agent-shaped compact projection for marketplace search.

The default search response is ~50KB for 8 listings, dominated by
``manifest_json`` (file counts, safety flags, requirements). An agent asking
"who could do content work?" cannot spend that many tokens to find out, so the
compact view carries only what is needed to judge fit.
"""

import json

from routers.marketplace_listings import _compact_items, _summary_of

_HEAVY = {
    "slug": "pulse",
    "name": "Pulse",
    "price_cents": 0,
    "tags": ["marketing", "content"],
    "description_md": "# Pulse\n\nMarketing intel and content research.\n\nMore prose here.",
    "manifest_json": {"file_count": 195, "safety_flags": [{"pattern": "eval"}]},
    "s3_prefix": "listings/abc/v9/",
    "manifest_sha256": "deadbeef",
    "seller_id": "user_x",
    "seller_display_name": "Someone",
    "status": "published",
}


def test_compact_drops_heavy_fields():
    [item] = _compact_items([_HEAVY])
    assert set(item) == {"listing_id", "slug", "name", "summary", "tags", "price_cents"}


def test_compact_summary_is_first_prose_line_not_a_heading():
    [item] = _compact_items([_HEAVY])
    assert item["summary"] == "Marketing intel and content research."


def test_compact_response_is_small():
    """The whole point: a catalog an agent can actually afford to read."""
    payload = json.dumps(_compact_items([_HEAVY] * 8))
    assert len(payload) < 2048, f"compact payload grew to {len(payload)} bytes"


def test_compact_tolerates_missing_fields():
    """Listings predate several of these fields; a projection must not KeyError."""
    [item] = _compact_items([{"slug": "x"}])
    assert item == {
        "listing_id": "",
        "slug": "x",
        "name": "",
        "summary": "",
        "tags": [],
        "price_cents": 0,
    }


def test_compact_coerces_null_tags_and_price():
    """DynamoDB rows carry explicit nulls, which are not the same as absent."""
    [item] = _compact_items([{"slug": "x", "tags": None, "price_cents": None}])
    assert item["tags"] == []
    assert item["price_cents"] == 0


def test_summary_skips_blank_lines_and_headings():
    assert _summary_of({"description_md": "\n\n## Heading\n\n   \n\nReal text."}) == "Real text."


def test_summary_is_empty_when_there_is_no_prose():
    assert _summary_of({"description_md": "# Only A Heading"}) == ""
    assert _summary_of({}) == ""


def test_summary_is_bounded():
    long_line = "x" * 500
    assert len(_summary_of({"description_md": long_line})) == 200


def test_compact_carries_listing_id_because_slugs_are_not_unique():
    """listing_id is the canonical identity (marketplace_service.get_published_by_id:
    "slugs are non-unique"). Every downstream action — deploy, checkout — must
    address the listing by UUID, so the projection an agent hands to the hire
    card has to carry it.
    """
    [item] = _compact_items([{**_HEAVY, "listing_id": "81ab74f8-efc2-464c"}])
    assert item["listing_id"] == "81ab74f8-efc2-464c"
