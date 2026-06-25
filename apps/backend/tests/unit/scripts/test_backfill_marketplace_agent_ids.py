"""Tests for the marketplace agent_id slug→UUID backfill script."""

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import scripts.backfill_marketplace_agent_ids as backfill  # noqa: E402
from core.services import agent_export, marketplace_service  # noqa: E402


def _run(monkeypatch, rows, *, live_dirs, resolve, apply):
    """Drive backfill.main() with mocked listings, workspace, resolver, and DDB
    table. ``resolve`` stands in for ``agent_export._resolve_workspace_id``."""
    ws = MagicMock()
    ws.list_workspace_agent_dirs.return_value = list(live_dirs)
    ws.read_openclaw_config.return_value = {"agents": {"list": []}}
    monkeypatch.setattr(backfill, "get_workspace", lambda: ws)
    monkeypatch.setattr(backfill, "_all_listings", lambda: rows)

    table = MagicMock()
    monkeypatch.setattr(marketplace_service, "_listings_table", lambda: table)
    monkeypatch.setattr(agent_export, "_resolve_workspace_id", resolve)
    monkeypatch.setattr(sys, "argv", ["prog"] + (["--apply"] if apply else []))
    backfill.main()
    return table


def test_dry_run_makes_no_writes(monkeypatch):
    rows = [{"listing_id": "L1", "version": 1, "seller_id": "s", "agent_id": "vera"}]
    table = _run(monkeypatch, rows, live_dirs=["uuid-v"], resolve=lambda s, a, c: "uuid-v", apply=False)
    table.update_item.assert_not_called()


def test_apply_rewrites_slug_to_uuid(monkeypatch):
    rows = [{"listing_id": "L1", "version": 2, "seller_id": "s", "agent_id": "vera"}]
    table = _run(monkeypatch, rows, live_dirs=["uuid-v"], resolve=lambda s, a, c: "uuid-v", apply=True)
    table.update_item.assert_called_once()
    kw = table.update_item.call_args.kwargs
    assert kw["Key"] == {"listing_id": "L1", "version": 2}
    assert "SET agent_id = :u" in kw["UpdateExpression"]
    assert kw["ConditionExpression"] == "agent_id = :old"
    assert kw["ExpressionAttributeValues"] == {":u": "uuid-v", ":old": "vera"}


def test_apply_skips_rows_already_keyed_by_uuid(monkeypatch):
    rows = [{"listing_id": "L1", "version": 1, "seller_id": "s", "agent_id": "uuid-v"}]
    called: list[str] = []

    def resolve(s, a, c):
        called.append(a)
        return a

    table = _run(monkeypatch, rows, live_dirs=["uuid-v"], resolve=resolve, apply=True)
    table.update_item.assert_not_called()
    assert called == []  # short-circuited before the resolver ran


def test_apply_skips_unresolvable_slug(monkeypatch):
    rows = [{"listing_id": "L1", "version": 1, "seller_id": "s", "agent_id": "ghost"}]
    table = _run(monkeypatch, rows, live_dirs=["uuid-v"], resolve=lambda s, a, c: a, apply=True)
    table.update_item.assert_not_called()  # resolver returned unchanged → no write


def test_apply_skips_ambiguous_slug(monkeypatch):
    rows = [{"listing_id": "L1", "version": 1, "seller_id": "s", "agent_id": "vera"}]

    def resolve(s, a, c):
        raise agent_export.AgentNotFoundError("ambiguous")

    table = _run(monkeypatch, rows, live_dirs=["a", "b"], resolve=resolve, apply=True)
    table.update_item.assert_not_called()  # ambiguity is caught, not fatal


def test_skips_sentinel_and_agentless_rows(monkeypatch):
    rows = [
        {"listing_id": "L0", "version": 0, "seller_id": "s", "agent_id": "vera"},  # v0 slug sentinel
        {"listing_id": "L2", "version": 1, "seller_id": "s"},  # no agent_id
    ]
    called: list[str] = []

    def resolve(s, a, c):
        called.append(a)
        return "uuid-v"

    table = _run(monkeypatch, rows, live_dirs=["uuid-v"], resolve=resolve, apply=True)
    table.update_item.assert_not_called()
    assert called == []  # neither row reached the resolver
