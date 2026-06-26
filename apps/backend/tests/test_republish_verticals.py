"""Tests for republish_verticals helpers + dry-run safety.

The heavy orchestration in ``_republish_one`` calls already-tested marketplace
service functions; here we cover the pure helpers, the embedded bootstrap
content, and that a dry-run never mutates (returns a PLAN line before any AWS or
EFS call).
"""

from __future__ import annotations

import asyncio

from scripts.republish_verticals import (
    _SEED_BOOTSTRAPS,
    _published_listings,
    _republish_one,
    _stored_agent_id,
)


def test_stored_agent_id_prefers_column_then_manifest():
    assert _stored_agent_id({"agent_id": "uuid-1"}) == "uuid-1"
    assert _stored_agent_id({"manifest_json": {"agent_id": "uuid-2"}}) == "uuid-2"
    assert _stored_agent_id({"agent_id": "uuid-1", "manifest_json": {"agent_id": "uuid-2"}}) == "uuid-1"
    assert _stored_agent_id({}) is None


def test_seed_bootstraps_are_real_not_placeholder():
    assert set(_SEED_BOOTSTRAPS) == {"ora", "thread"}
    for slug, body in _SEED_BOOTSTRAPS.items():
        assert "Hello, World" not in body, f"{slug} still the stock placeholder"
        assert "first-run setup" in body
        assert "## What I need connected" in body
    # Each declares its real integration.
    assert "Google Calendar" in _SEED_BOOTSTRAPS["ora"]
    assert "gog" in _SEED_BOOTSTRAPS["ora"]
    assert "Channels" in _SEED_BOOTSTRAPS["thread"]


class _FakeTable:
    def __init__(self, items):
        self._items = items

    def scan(self, **_kwargs):
        # Single page; filter is applied by the caller's status filter contract,
        # so we return only what a published-status scan would.
        return {"Items": self._items}


def test_published_listings_filters_status_and_seller():
    items = [
        {"listing_id": "a", "seller_id": "s1", "slug": "ora", "status": "published"},
        {"listing_id": "b", "seller_id": "s2", "slug": "echo", "status": "published"},
        {"listing_id": "c", "seller_id": "s1", "slug": "draft", "status": "draft"},  # excluded
    ]
    assert {r["listing_id"] for r in _published_listings(_FakeTable(items), None)} == {"a", "b"}
    assert {r["listing_id"] for r in _published_listings(_FakeTable(items), "s1")} == {"a"}


def test_dry_run_returns_plan_without_mutating():
    row = {"listing_id": "lst_ora", "slug": "ora", "seller_id": "s1", "version": 1, "agent_id": "uuid-ora"}
    out = asyncio.run(_republish_one(row, owner_id="org_x", approved_by="__system__", apply=False, approve=False))
    assert out.startswith("PLAN")
    assert "ora" in out
    assert "v1->v2" in out
    assert "seed-bootstrap" in out  # ora is a seed target


def test_dry_run_non_seed_agent_has_no_seed_marker():
    row = {"listing_id": "lst_echo", "slug": "echo", "seller_id": "s1", "version": 1, "agent_id": "uuid-echo"}
    out = asyncio.run(_republish_one(row, owner_id="org_x", approved_by="__system__", apply=False, approve=True))
    assert out.startswith("PLAN")
    assert "seed-bootstrap" not in out
    assert "approve" in out


def test_dry_run_skips_row_without_agent_id():
    row = {"listing_id": "lst_x", "slug": "x", "seller_id": "s1", "version": 1}
    out = asyncio.run(_republish_one(row, owner_id="org_x", approved_by="__system__", apply=False, approve=False))
    assert out.startswith("SKIP")


def test_dry_run_skips_row_without_seller_id():
    row = {"listing_id": "lst_y", "slug": "y", "version": 1, "agent_id": "uuid-y"}
    out = asyncio.run(_republish_one(row, owner_id="org_x", approved_by="__system__", apply=False, approve=False))
    assert out.startswith("SKIP")
    assert "seller_id" in out
