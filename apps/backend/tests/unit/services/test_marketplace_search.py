"""Tests for the in-process marketplace search service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import marketplace_search  # noqa: E402


# Canned listings used across the suite. published_at is ISO-Z so newest-first
# string-sort matches chronological-newest-first.
LISTINGS = [
    {
        "listing_id": "1",
        "version": 1,
        "slug": "postgres-helper",
        "name": "Postgres Helper",
        "description_md": "Run SQL queries against your Postgres database.",
        "format": "openclaw",
        "tags": ["postgres", "sql", "database"],
        "status": "published",
        "published_at": "2026-04-01T00:00:00Z",
    },
    {
        "listing_id": "2",
        "version": 1,
        "slug": "redis-tool",
        "name": "Redis Tool",
        "description_md": "Manage Redis caches.",
        "format": "openclaw",
        "tags": ["redis", "cache"],
        "status": "published",
        "published_at": "2026-04-15T00:00:00Z",
    },
    {
        "listing_id": "3",
        "version": 1,
        "slug": "sql-formatter",
        "name": "SQL Formatter",
        "description_md": "Pretty-print SQL.",
        "format": "skillmd",
        "tags": ["sql"],
        "status": "published",
        "published_at": "2026-04-10T00:00:00Z",
    },
    {
        "listing_id": "4",
        "version": 1,
        "slug": "draft-thing",
        "name": "Draft Thing",
        "description_md": "Not yet published.",
        "format": "openclaw",
        "tags": ["postgres"],
        "status": "draft",
        "published_at": None,
    },
]

# The DDB scan is now unfiltered — it returns all rows regardless of status —
# so the mock_table fixture seeds the full LISTINGS set including the draft.
# The in-Python `status == "published"` gate in search() is what excludes the
# draft from default reads.
PUBLISHED_LISTINGS = [li for li in LISTINGS if li["status"] == "published"]


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test gets a fresh cache."""
    marketplace_search._reset_cache()
    yield
    marketplace_search._reset_cache()


@pytest.fixture
def mock_table():
    table = MagicMock()
    table.scan.return_value = {"Items": list(LISTINGS)}
    with patch("core.services.marketplace_search._listings_table", return_value=table):
        yield table


@pytest.mark.asyncio
async def test_empty_query_returns_most_recent_published_first(mock_table):
    results = await marketplace_search.search(query=None)
    # No drafts.
    assert all(li["status"] == "published" for li in results)
    # Newest published first.
    assert [li["slug"] for li in results] == ["redis-tool", "sql-formatter", "postgres-helper"]


@pytest.mark.asyncio
async def test_query_exact_slug_match_scores_highest(mock_table):
    results = await marketplace_search.search(query="postgres-helper")
    assert results[0]["slug"] == "postgres-helper"


@pytest.mark.asyncio
async def test_query_tag_match(mock_table):
    results = await marketplace_search.search(query="postgres")
    slugs = [li["slug"] for li in results]
    # postgres-helper has tag "postgres" (+30) and substring in name (+20)
    # and substring in description (+5). Should rank first.
    assert slugs[0] == "postgres-helper"
    # No other published listings tag/name/description-match "postgres".
    assert "redis-tool" not in slugs
    assert "sql-formatter" not in slugs


@pytest.mark.asyncio
async def test_query_substring_in_description(mock_table):
    # "queries" appears only in description of postgres-helper.
    results = await marketplace_search.search(query="queries")
    slugs = [li["slug"] for li in results]
    assert "postgres-helper" in slugs


@pytest.mark.asyncio
async def test_query_sql_matches_tag_and_description(mock_table):
    results = await marketplace_search.search(query="sql")
    slugs = [li["slug"] for li in results]
    # sql-formatter has tag "sql" (+30) AND substring in name (+20) AND in
    # description (+5). postgres-helper has tag "sql" (+30) AND substring
    # in description (+5). sql-formatter > postgres-helper.
    assert slugs[0] == "sql-formatter"
    assert "postgres-helper" in slugs


@pytest.mark.asyncio
async def test_format_filter(mock_table):
    results = await marketplace_search.search(query=None, format="skillmd")
    assert all(li["format"] == "skillmd" for li in results)
    assert [li["slug"] for li in results] == ["sql-formatter"]


@pytest.mark.asyncio
async def test_format_filter_with_query(mock_table):
    # "sql" matches sql-formatter (skillmd) and postgres-helper (openclaw).
    # With format=openclaw, only postgres-helper survives.
    results = await marketplace_search.search(query="sql", format="openclaw")
    slugs = [li["slug"] for li in results]
    assert slugs == ["postgres-helper"]


@pytest.mark.asyncio
async def test_limit_respected(mock_table):
    results = await marketplace_search.search(query=None, limit=2)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_drops_zero_score_results(mock_table):
    # "nonexistent-token" hits nothing — empty list, not the full snapshot.
    results = await marketplace_search.search(query="nonexistent-token")
    assert results == []


@pytest.mark.asyncio
async def test_drafts_excluded_via_in_python_filter(mock_table):
    """The in-Python status filter is the load-bearing public-vs-seller gate.

    The scan deliberately does NOT carry a FilterExpression on status — that
    used to silently drop draft rows before include_unpublished=True could
    ever surface them. mock_table is now seeded with the full LISTINGS set
    (including draft-thing), and the default search() must still exclude it.
    """
    results = await marketplace_search.search(query=None)
    assert all(li["status"] == "published" for li in results)
    assert "draft-thing" not in [li["slug"] for li in results]

    # Defense against regression: the snapshot scan must be unfiltered.
    # The original bug carried FilterExpression="#s = :pub" with the literal
    # word "status" only in ExpressionAttributeNames — a substring check on
    # the expression string would not have caught it. The strict contract is
    # "no FilterExpression at all" per the module docstring.
    kwargs = mock_table.scan.call_args.kwargs
    assert "FilterExpression" not in kwargs, (
        "scan must not carry a FilterExpression — the snapshot is unfiltered "
        "(status filter happens in-Python in search() so include_unpublished=True works)"
    )


@pytest.mark.asyncio
async def test_cache_ttl_avoids_rescan(mock_table, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(marketplace_search.time, "time", lambda: fake_now[0])

    await marketplace_search.search(query=None)
    assert mock_table.scan.call_count == 1

    # 30s later: cache hit, no rescan.
    fake_now[0] += 30
    await marketplace_search.search(query="postgres")
    assert mock_table.scan.call_count == 1

    # 60s after first call (boundary): still cached.
    fake_now[0] = 1059.9
    await marketplace_search.search(query=None)
    assert mock_table.scan.call_count == 1

    # >60s: re-scan.
    fake_now[0] = 1061.0
    await marketplace_search.search(query=None)
    assert mock_table.scan.call_count == 2


@pytest.mark.asyncio
async def test_cache_refresh_paginates_through_last_evaluated_key():
    """Refresh loop must follow LastEvaluatedKey across multiple scan pages.

    Pagination correctness was an explicit requirement; without this test the
    `scan_kwargs["ExclusiveStartKey"] = last` line is uncovered and a
    refactor could silently drop page 2+ of large listing tables.
    """
    page_one = {
        "listing_id": "p1",
        "version": 1,
        "slug": "page-one",
        "name": "Page One",
        "description_md": "first page",
        "format": "openclaw",
        "tags": [],
        "status": "published",
        "published_at": "2026-04-01T00:00:00Z",
    }
    page_two = {
        "listing_id": "p2",
        "version": 1,
        "slug": "page-two",
        "name": "Page Two",
        "description_md": "second page",
        "format": "openclaw",
        "tags": [],
        "status": "published",
        "published_at": "2026-04-02T00:00:00Z",
    }

    table = MagicMock()
    last_key = {"listing_id": "x", "version": 1}
    table.scan.side_effect = [
        {"Items": [page_one], "LastEvaluatedKey": last_key},
        {"Items": [page_two]},  # no LastEvaluatedKey -> pagination ends
    ]

    with patch("core.services.marketplace_search._listings_table", return_value=table):
        results = await marketplace_search.search(query=None)

    # Two scan calls — one for each page.
    assert table.scan.call_count == 2

    # Second call must thread ExclusiveStartKey from the first page's
    # LastEvaluatedKey.
    second_call_kwargs = table.scan.call_args_list[1].kwargs
    assert second_call_kwargs.get("ExclusiveStartKey") == last_key

    # Both pages' items make it into the snapshot / results.
    slugs = {li["slug"] for li in results}
    assert {"page-one", "page-two"} <= slugs


@pytest.mark.asyncio
async def test_search_includes_unpublished_when_flag_set():
    """When include_unpublished=True, draft + review + published rows all surface."""
    snapshot = [
        {
            "listing_id": "l1",
            "slug": "a",
            "name": "A",
            "status": "published",
            "seller_id": "u_seller",
            "format": "openclaw",
            "tags": [],
            "version": 1,
            "description_md": "",
            "created_at": "2026-05-01",
            "published_at": "2026-05-01",
            "price_cents": 0,
            "install_count": 0,
            "manifest_json": {},
            "updated_at": "2026-05-01",
        },
        {
            "listing_id": "l2",
            "slug": "b",
            "name": "B",
            "status": "draft",
            "seller_id": "u_seller",
            "format": "openclaw",
            "tags": [],
            "version": 1,
            "description_md": "",
            "created_at": "2026-05-02",
            "published_at": None,
            "price_cents": 0,
            "install_count": 0,
            "manifest_json": {},
            "updated_at": "2026-05-02",
        },
        {
            "listing_id": "l3",
            "slug": "c",
            "name": "C",
            "status": "review",
            "seller_id": "u_seller",
            "format": "openclaw",
            "tags": [],
            "version": 1,
            "description_md": "",
            "created_at": "2026-05-03",
            "published_at": None,
            "price_cents": 0,
            "install_count": 0,
            "manifest_json": {},
            "updated_at": "2026-05-03",
        },
    ]
    table = MagicMock()
    # The DDB-side FilterExpression would normally drop non-published rows;
    # we deliberately return them all here so the in-Python branch under test
    # is exercised end-to-end.
    table.scan.return_value = {"Items": list(snapshot)}
    with patch("core.services.marketplace_search._listings_table", return_value=table):
        items = await marketplace_search.search(query=None, seller_id="u_seller", include_unpublished=True, limit=10)

    statuses = {item["status"] for item in items}
    assert statuses == {"published", "draft", "review"}


@pytest.mark.asyncio
async def test_search_default_filters_to_published():
    """Default behavior unchanged: only published rows surface."""
    snapshot = [
        {
            "listing_id": "l1",
            "slug": "a",
            "name": "A",
            "status": "published",
            "seller_id": "u_seller",
            "format": "openclaw",
            "tags": [],
            "version": 1,
            "description_md": "",
            "created_at": "2026-05-01",
            "published_at": "2026-05-01",
            "price_cents": 0,
            "install_count": 0,
            "manifest_json": {},
            "updated_at": "2026-05-01",
        },
        {
            "listing_id": "l2",
            "slug": "b",
            "name": "B",
            "status": "draft",
            "seller_id": "u_seller",
            "format": "openclaw",
            "tags": [],
            "version": 1,
            "description_md": "",
            "created_at": "2026-05-02",
            "published_at": None,
            "price_cents": 0,
            "install_count": 0,
            "manifest_json": {},
            "updated_at": "2026-05-02",
        },
    ]
    table = MagicMock()
    table.scan.return_value = {"Items": list(snapshot)}
    with patch("core.services.marketplace_search._listings_table", return_value=table):
        items = await marketplace_search.search(query=None, seller_id="u_seller", limit=10)

    statuses = {item["status"] for item in items}
    assert statuses == {"published"}


@pytest.mark.asyncio
async def test_default_limit_is_24(mock_table):
    # Stuff the table with 50 published listings.
    many = []
    for i in range(50):
        many.append(
            {
                "listing_id": str(i),
                "version": 1,
                "slug": f"item-{i}",
                "name": f"Item {i}",
                "description_md": "x",
                "format": "openclaw",
                "tags": [],
                "status": "published",
                "published_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
            }
        )
    mock_table.scan.return_value = {"Items": many}

    results = await marketplace_search.search(query=None)
    assert len(results) == 24


@pytest.mark.asyncio
async def test_draft_row_surfaces_to_seller_via_include_unpublished(monkeypatch):
    """End-to-end: a draft row in the underlying scan response surfaces to a
    seller-scoped include_unpublished=True call. Catches the bug where the
    FilterExpression at scan time would silently drop drafts before the cache
    fills.
    """
    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {
                "listing_id": "l_pub",
                "slug": "pub",
                "name": "Pub",
                "status": "published",
                "seller_id": "u_seller",
                "format": "openclaw",
                "tags": [],
                "version": 1,
                "description_md": "",
                "created_at": "2026-05-01",
                "published_at": "2026-05-01",
                "price_cents": 0,
                "install_count": 0,
                "manifest_json": {},
                "updated_at": "2026-05-01",
            },
            {
                "listing_id": "l_draft",
                "slug": "draft",
                "name": "Draft",
                "status": "draft",
                "seller_id": "u_seller",
                "format": "openclaw",
                "tags": [],
                "version": 1,
                "description_md": "",
                "created_at": "2026-05-02",
                "published_at": None,
                "price_cents": 0,
                "install_count": 0,
                "manifest_json": {},
                "updated_at": "2026-05-02",
            },
        ],
    }

    monkeypatch.setattr(marketplace_search, "_listings_table", lambda: table)

    items = await marketplace_search.search(query=None, seller_id="u_seller", include_unpublished=True)
    slugs = {item["slug"] for item in items}
    assert "draft" in slugs, "expected draft listing to surface for seller-scoped include_unpublished read"

    # Verify the snapshot scan was unfiltered. The original bug carried
    # FilterExpression="#s = :pub" with "status" only in ExpressionAttributeNames,
    # so a substring check on the expression string would have passed against
    # the buggy code. The strict contract is "no FilterExpression at all".
    scan_calls = table.scan.call_args_list
    assert len(scan_calls) >= 1
    for call in scan_calls:
        kwargs = call.kwargs
        assert "FilterExpression" not in kwargs, (
            "scan must not carry a FilterExpression — the snapshot is unfiltered "
            "(status filter happens in-Python in search() so include_unpublished=True works)"
        )


@pytest.mark.asyncio
async def test_default_search_still_excludes_drafts_after_cache_change(monkeypatch):
    """Defense-in-depth regression test: even though the cache now holds all
    statuses, the default (no flag) search still returns published-only.
    """
    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {
                "listing_id": "l_pub",
                "slug": "pub",
                "name": "Pub",
                "status": "published",
                "seller_id": "u_seller",
                "format": "openclaw",
                "tags": [],
                "version": 1,
                "description_md": "",
                "created_at": "2026-05-01",
                "published_at": "2026-05-01",
                "price_cents": 0,
                "install_count": 0,
                "manifest_json": {},
                "updated_at": "2026-05-01",
            },
            {
                "listing_id": "l_draft",
                "slug": "draft",
                "name": "Draft",
                "status": "draft",
                "seller_id": "u_seller",
                "format": "openclaw",
                "tags": [],
                "version": 1,
                "description_md": "",
                "created_at": "2026-05-02",
                "published_at": None,
                "price_cents": 0,
                "install_count": 0,
                "manifest_json": {},
                "updated_at": "2026-05-02",
            },
        ],
    }

    monkeypatch.setattr(marketplace_search, "_listings_table", lambda: table)

    items = await marketplace_search.search(query=None)  # default — no flag
    statuses = {item["status"] for item in items}
    assert statuses == {"published"}, "default search must filter to published-only"


@pytest.mark.asyncio
async def test_search_filters_by_category(monkeypatch):
    """Browse view category chips drive a case-insensitive filter on the
    listing's "category" field. Listings without a matching category fall out.
    """
    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {
                "listing_id": "l1",
                "slug": "a",
                "name": "A",
                "status": "published",
                "seller_id": "u_s",
                "format": "openclaw",
                "tags": [],
                "version": 1,
                "description_md": "",
                "created_at": "2026-05-01",
                "published_at": "2026-05-01",
                "price_cents": 0,
                "install_count": 0,
                "manifest_json": {},
                "updated_at": "2026-05-01",
                "category": "Engineering",
            },
            {
                "listing_id": "l2",
                "slug": "b",
                "name": "B",
                "status": "published",
                "seller_id": "u_s",
                "format": "openclaw",
                "tags": [],
                "version": 1,
                "description_md": "",
                "created_at": "2026-05-01",
                "published_at": "2026-05-01",
                "price_cents": 0,
                "install_count": 0,
                "manifest_json": {},
                "updated_at": "2026-05-01",
                "category": "Sales",
            },
        ],
    }

    monkeypatch.setattr(marketplace_search, "_listings_table", lambda: table)

    items = await marketplace_search.search(category="engineering")
    slugs = {item["slug"] for item in items}
    assert slugs == {"a"}


@pytest.mark.asyncio
async def test_status_in_filter_runs_before_limit(monkeypatch):
    """Seller-scoped reads must apply ``status_in`` BEFORE the limit slice.

    Otherwise a seller with many published rows ahead of a single ``review``
    row loses the match — the limit cuts the published rows first and the
    review row never makes it into the response. Regression for PR #567
    Codex P2.

    Setup: 30 published listings + 1 review listing for the same seller.
    Caller asks for ``status_in=["review"]`` with ``limit=10``.
    Expectation: the review row surfaces.
    """
    items: list[dict] = []
    for i in range(30):
        items.append(
            {
                "listing_id": f"l_pub_{i}",
                "slug": f"pub-{i}",
                "name": f"Pub {i}",
                "status": "published",
                "seller_id": "u_seller",
                "format": "openclaw",
                "tags": [],
                "version": 1,
                "description_md": "",
                # All published_at values are newer than the review row so
                # they would dominate a published_at-desc sort, masking the
                # bug if status_in were applied after the limit slice.
                "published_at": f"2026-05-{(i % 28) + 1:02d}T00:00:00Z",
                "price_cents": 0,
            }
        )
    items.append(
        {
            "listing_id": "l_review",
            "slug": "review-row",
            "name": "Review Row",
            "status": "review",
            "seller_id": "u_seller",
            "format": "openclaw",
            "tags": [],
            "version": 1,
            "description_md": "",
            "published_at": "2026-04-01T00:00:00Z",
            "price_cents": 0,
        }
    )

    table = MagicMock()
    table.scan.return_value = {"Items": items}
    monkeypatch.setattr(marketplace_search, "_listings_table", lambda: table)

    results = await marketplace_search.search(
        query=None,
        seller_id="u_seller",
        include_unpublished=True,
        status_in=["review"],
        limit=10,
    )
    statuses = {li["status"] for li in results}
    assert statuses == {"review"}, (
        "status_in must filter BEFORE the limit slice — otherwise the "
        "review row is lost to the 30 published rows ahead of it."
    )
    slugs = {li["slug"] for li in results}
    assert "review-row" in slugs
