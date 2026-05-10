"""Tests for HookEventRepo (audit trail writes)."""

import logging
from unittest.mock import MagicMock

from core.repositories import hook_event_repo


def test_write_event_persists_required_fields(monkeypatch):
    """write_event constructs a complete row and PutItems it."""
    fake_table = MagicMock()
    monkeypatch.setattr(hook_event_repo, "_get_table", lambda: fake_table)

    hook_event_repo.write_event(
        owner_id="u_1",
        hook_path="wake",
        status_code=200,
        source_ip="203.0.113.42",
        user_agent="Stripe/1.0",
        request_size=512,
        response_size=64,
    )

    fake_table.put_item.assert_called_once()
    item = fake_table.put_item.call_args.kwargs["Item"]
    assert item["owner_id"] == "u_1"
    assert item["hook_path"] == "wake"
    assert item["status_code"] == 200
    assert item["source_ip"] == "203.0.113.42"
    assert item["user_agent"] == "Stripe/1.0"
    assert item["request_size"] == 512
    assert item["response_size"] == 64
    assert isinstance(item["event_id"], str) and len(item["event_id"]) >= 16  # ULID
    assert isinstance(item["timestamp"], int)
    assert isinstance(item["expires_at"], int)
    assert item["expires_at"] > item["timestamp"] // 1000  # TTL in seconds, +30 days


def test_write_event_truncates_long_user_agent(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(hook_event_repo, "_get_table", lambda: fake_table)

    hook_event_repo.write_event(
        owner_id="u_1",
        hook_path="wake",
        status_code=200,
        source_ip="203.0.113.42",
        user_agent="x" * 1000,
        request_size=0,
        response_size=0,
    )

    item = fake_table.put_item.call_args.kwargs["Item"]
    assert len(item["user_agent"]) == 512


def test_write_event_swallows_ddb_errors(monkeypatch, caplog):
    """A DDB write failure must NOT propagate; audit is best-effort."""
    fake_table = MagicMock()
    fake_table.put_item.side_effect = Exception("DynamoDB unreachable")
    monkeypatch.setattr(hook_event_repo, "_get_table", lambda: fake_table)

    caplog.set_level(logging.WARNING, logger="core.repositories.hook_event_repo")

    # Must not raise.
    hook_event_repo.write_event(
        owner_id="u_1",
        hook_path="wake",
        status_code=200,
        source_ip="203.0.113.42",
        user_agent="x",
        request_size=0,
        response_size=0,
    )

    assert any("hook-events write failed" in r.message for r in caplog.records)
