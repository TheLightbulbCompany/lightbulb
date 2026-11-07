"""Task 13: the one rename seam (refresh_agent_soul) -- name line, member labels, C3 `renamed`."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("WS_MANAGEMENT_API_URL", "https://test.execute-api.us-east-1.amazonaws.com/test")

from core.repositories import container_repo, messaging_repo  # noqa: E402
from core.services import hermes_profiles, livekit_huddles, messaging  # noqa: E402
from core.services.hermes_provisioning import refresh_agent_soul  # noqa: E402

OWNER = "org_1"
AGENT = "11111111-1111-4111-8111-111111111111"
R_LIVE = "22222222-2222-4222-8222-222222222222"
R_QUIET = "33333333-3333-4333-8333-333333333333"


def _key(name: str) -> str:
    return f"{hermes_profiles.profile_prefix(OWNER, AGENT)}{name}"


@pytest.fixture
def world(monkeypatch):
    files: dict[str, str] = {}
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: files.get(key))
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: files.__setitem__(key, body))
    roster = [{"id": AGENT, "name": "Nova", "role": "ceo"}]
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(side_effect=lambda owner: [dict(r) for r in roster]))
    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value="front-agent"))  # not the renamed one
    labels = {R_LIVE: "Ada", R_QUIET: "Ada"}

    async def rooms(owner, kind, agent_id):
        return [
            {"member": {"displayName": labels[r]}, "meta": {"roomId": r, "ownerId": OWNER}} for r in (R_LIVE, R_QUIET)
        ]

    async def set_names(room_id, agent_id, name, handle):
        labels[room_id] = name
        return True

    monkeypatch.setattr(messaging_repo, "list_rooms_for_member", rooms)
    set_agent_names = AsyncMock(side_effect=set_names)
    monkeypatch.setattr(messaging_repo, "set_agent_names", set_agent_names)
    monkeypatch.setattr(messaging_repo, "list_members", AsyncMock(return_value=[{"kind": "human", "id": "user_1"}]))
    publisher = SimpleNamespace(publish_user=AsyncMock(return_value=True))
    monkeypatch.setattr(messaging, "get_live_event_publisher", lambda: publisher)
    monkeypatch.setattr(
        container_repo,
        "get_by_status",
        AsyncMock(return_value=[{"ownerId": OWNER, "roomId": R_LIVE, "kind": "huddle", "deadline": 2**40}]),
    )
    send_frames = AsyncMock()
    monkeypatch.setattr(livekit_huddles, "send_frames", send_frames)
    people = AsyncMock(return_value=[{"user_id": "user_1", "first_name": "Aden", "last_name": "Lee"}])
    monkeypatch.setattr("core.services.clerk_admin.list_organization_members", people)
    return SimpleNamespace(
        files=files,
        roster=roster,
        labels=labels,
        set_agent_names=set_agent_names,
        publisher=publisher,
        send_frames=send_frames,
        people=people,
    )


def _notices(send_frames) -> list[tuple[str, dict]]:
    return [(c.args[0], ev["payload"]) for c in send_frames.await_args_list for ev in c.args[1]]


async def test_rename_rewrites_the_name_line_relabels_members_and_tells_the_open_window(world):
    world.files[_key("SOUL.md")] = "I am the owner's own soul."  # owner-edited: no generated marker
    world.files[_key("VOICE_NOTES.md")] = "Your name is Ada.\n\nAden asked: be blunt"

    await refresh_agent_soul(OWNER, AGENT, by="user_1")

    assert world.files[_key("SOUL.md")] == "I am the owner's own soul."
    assert world.files[_key("VOICE_NOTES.md")] == "Your name is Nova.\n\nAden asked: be blunt"
    assert world.labels == {R_LIVE: "Nova", R_QUIET: "Nova"}
    assert world.set_agent_names.await_args.args[3] == "nova"  # the @handle follows the name
    assert world.publisher.publish_user.await_count == 2  # room.member.updated per room
    assert _notices(world.send_frames) == [
        (
            R_LIVE,
            {"kind": "renamed", "agentId": AGENT, "from": "Ada", "to": "Nova", "by": "user_1", "collides": False},
        )
    ]


async def test_a_new_name_a_person_answers_to_collides(world):
    world.people.return_value = [{"user_id": "user_2", "first_name": "Nova", "last_name": "Smith"}]
    await refresh_agent_soul(OWNER, AGENT, by="user_1")
    assert _notices(world.send_frames)[0][1]["collides"] is True


async def test_an_unchanged_name_sends_no_notice_but_still_writes_the_name_line(world):
    world.labels.update({R_LIVE: "Nova", R_QUIET: "Nova"})
    await refresh_agent_soul(OWNER, AGENT)
    assert world.files[_key("VOICE_NOTES.md")] == "Your name is Nova.\n\n"
    world.send_frames.assert_not_awaited()


async def test_teams_patch_rename_and_its_undo_reach_the_open_window(world, monkeypatch):
    from routers.teams import agents as agents_mod
    from routers.teams.schemas import PatchAgentBody

    admin = MagicMock()

    async def patch(*, agent_id, body, session_cookie):
        world.roster[0]["name"] = body["name"]
        return {"id": agent_id, "name": body["name"]}

    admin.patch_agent = AsyncMock(side_effect=patch)
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    ctx = SimpleNamespace(user_id="user_1", owner_id=OWNER, org_id=OWNER, company_id="co", session_cookie="c")
    monkeypatch.setattr(agents_mod, "_resolve_owner_id", lambda _ctx: OWNER)

    await agents_mod.patch_agent(AGENT, PatchAgentBody(name="Nova"), ctx)
    assert world.files[_key("VOICE_NOTES.md")].startswith("Your name is Nova.")
    await agents_mod.patch_agent(AGENT, PatchAgentBody(name="Ada"), ctx)  # Undo
    assert world.files[_key("VOICE_NOTES.md")].startswith("Your name is Ada.")

    assert [(p["from"], p["to"], p["by"]) for _, p in _notices(world.send_frames)] == [
        ("Ada", "Nova", "user_1"),
        ("Nova", "Ada", "user_1"),
    ]


async def test_rename_seam_refreshes_the_facts_manifest(world, monkeypatch):
    """Teams PATCH, agents.update and the voice profile edit all rename through here, so the
    `teammates` list in every agent's facts file must be refreshed from this one place."""
    facts = AsyncMock()
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", facts)
    await refresh_agent_soul(OWNER, AGENT)
    facts.assert_awaited_once_with(OWNER)


@pytest.fixture(autouse=True)
def _no_text_line(monkeypatch):
    """A regenerated SOUL.md reads the workspace's texting line (the front-desk section); none here."""
    from unittest.mock import AsyncMock as _AsyncMock

    monkeypatch.setattr("core.repositories.agent_email_repo.text_line_for_owner", _AsyncMock(return_value=None))
