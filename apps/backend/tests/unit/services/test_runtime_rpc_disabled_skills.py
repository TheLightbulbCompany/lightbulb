"""M6-R57: the Skills panel's ``disabledNames`` is served by the backend, not the bridge.

The panel is workspace-level and its patch replaces ``skills.disabled`` on EVERY profile
wholesale, so whatever ``skills.status`` returns IS the next write for all of them -- a name
missing from it is deleted, not left alone. ``agents.create`` writes a fresh ``skills`` block
with no ``disabled`` key, so reading one profile can silently re-enable everything the owner
turned off. The union has to come from the roster, and the backend is the only writer of a
profile's ``config.yaml`` (the bridge has it in ``sync.EXCLUDE_GLOBS``).
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock

import pytest

from core.services import runtime_rpc as rpc

_ROWS = [{"name": "pdf", "disabled": False}, {"name": "shopify", "disabled": True}]


def _wire(monkeypatch, *, configs=None, snapshot=None):
    monkeypatch.setattr(rpc, "load_agent_roster", AsyncMock(return_value=[{"id": agent} for agent in (configs or {})]))
    monkeypatch.setattr(
        rpc.container_rpc,
        "call",
        AsyncMock(return_value={"skills": list(_ROWS), "disabledNames": ["only-what-the-bridge-saw"]}),
    )
    monkeypatch.setattr(
        rpc.hermes_profiles,
        "config_snapshot",
        snapshot or (lambda _owner, agent_id: ((configs or {})[agent_id], "h")),
    )


async def _status(owner_id="org_a"):
    return await rpc.execute_sync(owner_id=owner_id, user_id="u1", request_id="r1", method="skills.status", params={})


@pytest.mark.asyncio
async def test_disabled_names_is_the_union_across_the_whole_roster(monkeypatch):
    _wire(
        monkeypatch,
        configs={
            "ceo": {"skills": {"disabled": ["shopify", "pdf"]}},
            "engineering": {"skills": {"disabled": ["stripe"]}},
            # An agent created after the owner's last toggle: no `disabled` key at all. Reading
            # only this one (it sorts first) would re-enable everything, everywhere.
            "analytics": {"skills": {}},
        },
    )

    result = await _status()

    assert result["disabledNames"] == ["pdf", "shopify", "stripe"]
    # The rows still come from the bridge -- only the written-back half is replaced.
    assert result["skills"] == _ROWS


@pytest.mark.asyncio
async def test_a_hermes_config_set_string_literal_is_parsed_not_read_as_one_skill(monkeypatch):
    """`hermes config set` stores a list as a quoted literal. Never our writer, but reading it
    as a single skill named `["pdf","shopify"]` would show everything enabled and let the next
    toggle wipe the real list."""
    _wire(monkeypatch, configs={"ceo": {"skills": {"disabled": '["pdf", "shopify"]'}}})

    assert (await _status())["disabledNames"] == ["pdf", "shopify"]


@pytest.mark.asyncio
async def test_a_transient_s3_failure_raises_instead_of_serving_a_short_union(monkeypatch):
    """The hazard the bridge needed a two-valued "is this authoritative" flag for. `_s3_get`
    re-raises everything that is not a genuine 404, so a throttled read fails the RPC loudly
    rather than yielding a plausibly short list the panel would write over every profile."""

    def boom(_owner, agent_id):
        if agent_id == "engineering":
            raise RuntimeError("SlowDown")
        return {"skills": {"disabled": ["pdf"]}}, "h"

    _wire(monkeypatch, configs={"ceo": {}, "engineering": {}}, snapshot=boom)

    with pytest.raises(Exception, match="SlowDown|runtime"):
        await _status()
