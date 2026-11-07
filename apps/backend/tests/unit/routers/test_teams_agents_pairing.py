"""Teams agent PATCH → the agent's SOUL.md (create/delete/profile behaviour: tests/unit/services/test_agent_registry_writes.py)."""

from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext

_AUTH = AuthContext(user_id="user_member_b", org_id="org_xyz")
_AGENT = {"id": "agent-1", "name": "Ada", "role": "engineer", "reportsTo": None}


@pytest.fixture
def update(monkeypatch):
    from core.services import agent_registry

    mock = AsyncMock(side_effect=lambda ws, aid, fields: {**_AGENT, **fields})
    monkeypatch.setattr(agent_registry, "update", mock)
    return mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_kwargs", "expected"),
    [
        ({"name": "Renamed"}, True),
        ({"title": "Head of Ops"}, True),
        ({"capabilities": "Writes"}, True),
        # budgetMonthlyCents never appears in SOUL.md -- no S3 round trip for it.
        ({"budget_monthly_cents": 500}, False),
    ],
)
async def test_teams_patch_agent_refreshes_soul_only_on_identity_edits(monkeypatch, update, body_kwargs, expected):
    """A rename reaches a Hermes agent ONLY through its SOUL.md -- and only there."""
    refresh = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.refresh_agent_soul", refresh)

    from routers.teams.agents import patch_agent
    from routers.teams.schemas import PatchAgentBody

    await patch_agent(_AGENT, PatchAgentBody(**body_kwargs), _AUTH)

    assert refresh.await_count == (1 if expected else 0)
    if expected:
        refresh.assert_awaited_once_with("org_xyz", "agent-1", by="user_member_b")


def test_soul_fields_covers_every_field_build_soul_md_renders():
    """M6 task-15 item 4. ``_SOUL_FIELDS`` must track ``build_soul_md``, not ``PatchAgentBody``:
    a field the renderer uses but this set omits produces no error anywhere. ``id`` is the key."""
    from core.services.hermes_profiles import build_soul_md
    from routers.teams.agents import _SOUL_FIELDS

    base = {"id": "a1", "name": "Ada", "role": "ceo", "title": "Chief", "capabilities": "things"}
    rendered = build_soul_md(base)
    for field in _SOUL_FIELDS:
        assert build_soul_md({**base, field: "changed-value"}) != rendered, f"{field} is not in SOUL.md"
    assert not _SOUL_FIELDS & {"reportsTo", "budgetMonthlyCents", "icon"}


@pytest.mark.asyncio
async def test_teams_patch_agent_survives_a_failed_soul_refresh(monkeypatch, update):
    """The registry row (what the UI reads) already landed, so a SOUL hiccup is logged, not raised."""
    monkeypatch.setattr(
        "core.services.hermes_provisioning.refresh_agent_soul", AsyncMock(side_effect=RuntimeError("s3 down"))
    )
    from routers.teams.agents import patch_agent
    from routers.teams.schemas import PatchAgentBody

    assert (await patch_agent(_AGENT, PatchAgentBody(name="Renamed"), _AUTH))["name"] == "Renamed"
