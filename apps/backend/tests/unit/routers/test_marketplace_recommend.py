"""Slug resolution for hire recommendations.

Resolving server-side is what stops a model rendering a hire card for a listing
that does not exist — the card can never drift from the catalog because it is
built from the catalog.
"""

from routers.marketplace_listings import _resolve_recommended

_PUBLISHED = [
    {
        "slug": "pulse",
        "name": "Pulse",
        "status": "published",
        "price_cents": 0,
        "tags": ["content"],
        "description_md": "Marketing intel.",
    },
    {
        "slug": "ora",
        "name": "Ora",
        "status": "published",
        "price_cents": 0,
        "tags": ["calendar"],
        "description_md": "Scheduling.",
    },
    {
        "slug": "retired-one",
        "name": "Gone",
        "status": "retired",
        "price_cents": 0,
        "tags": [],
        "description_md": "Old.",
    },
]


def test_resolves_known_slugs_in_the_order_requested():
    out = _resolve_recommended(["ora", "pulse"], _PUBLISHED)
    assert [i["slug"] for i in out] == ["ora", "pulse"]


def test_drops_unknown_slugs_rather_than_failing():
    """A hallucinated slug must produce no card, never a failed tool call that
    costs another model turn to recover from."""
    out = _resolve_recommended(["pulse", "not-a-real-agent"], _PUBLISHED)
    assert [i["slug"] for i in out] == ["pulse"]


def test_drops_listings_that_are_not_published():
    """Retired/taken-down rows must never be offered — the deploy route would
    404 on them anyway."""
    assert _resolve_recommended(["retired-one"], _PUBLISHED) == []


def test_returns_the_compact_shape():
    [item] = _resolve_recommended(["pulse"], _PUBLISHED)
    assert set(item) == {"listing_id", "slug", "name", "summary", "tags", "price_cents"}


def test_deduplicates_repeated_slugs():
    """A model naming the same agent twice should not render two cards."""
    assert [i["slug"] for i in _resolve_recommended(["pulse", "pulse"], _PUBLISHED)] == ["pulse"]


def test_empty_request_is_empty_not_everything():
    assert _resolve_recommended([], _PUBLISHED) == []
