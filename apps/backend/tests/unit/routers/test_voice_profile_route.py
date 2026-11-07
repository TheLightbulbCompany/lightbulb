"""C8: POST /internal/agents/voice-profile/{ownerId} -- rename and voice notes, asked by voice."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("WS_MANAGEMENT_API_URL", "https://test.execute-api.us-east-1.amazonaws.com/test")

from core.repositories import container_repo, messaging_repo  # noqa: E402
from core.services import hermes_profiles, livekit_huddles, messaging  # noqa: E402
from core.services.paperclip_admin_client import PaperclipApiError  # noqa: E402
from routers import messaging as messaging_router  # noqa: E402

OWNER = "org_1"
AGENT = "11111111-1111-4111-8111-111111111111"
OTHER = "44444444-4444-4444-8444-444444444444"
R_LIVE = "22222222-2222-4222-8222-222222222222"
R_QUIET = "33333333-3333-4333-8333-333333333333"
RUN = "run-1"
NOTES_KEY = f"{hermes_profiles.profile_prefix(OWNER, AGENT)}{hermes_profiles.VOICE_NOTES_FILENAME}"
URL = f"/internal/agents/voice-profile/{OWNER}"
AUTH = {"Authorization": "Bearer tok"}


@pytest.fixture
def world(monkeypatch):
    files: dict[str, str] = {}
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: files.get(key))
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: files.__setitem__(key, body))
    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "tok"}))
    # A rename rewrites the profile, whose inputs include the workspace's texting line (none here).
    monkeypatch.setattr("core.repositories.agent_email_repo.text_line_for_owner", AsyncMock(return_value=None))
    runs = {
        RUN: {"roomId": R_LIVE, "agentId": AGENT, "speakerUserId": "user_1", "speakerName": "Aden"},
        "run-guest": {"roomId": R_LIVE, "agentId": AGENT, "speakerUserId": "user_guest", "speakerName": "Gus"},
        "run-other": {"roomId": R_LIVE, "agentId": OTHER, "speakerUserId": "user_1", "speakerName": "Aden"},
    }
    monkeypatch.setattr(messaging_repo, "get_voice_run", AsyncMock(side_effect=lambda owner, run_id: runs.get(run_id)))
    members = {
        "user_1": {"kind": "human", "id": "user_1", "homeOwnerId": OWNER},
        "user_guest": {"kind": "human", "id": "user_guest", "homeOwnerId": "org_vendor"},
    }
    monkeypatch.setattr(messaging_repo, "get_member", AsyncMock(side_effect=lambda room, kind, uid: members.get(uid)))
    counts: dict[tuple, int] = {}

    async def count(owner, agent_id, kind, limit):
        counts[(agent_id, kind)] = counts.get((agent_id, kind), 0) + 1
        return counts[(agent_id, kind)] <= limit

    monkeypatch.setattr(messaging_repo, "count_voice_edit", count)
    roster = [{"id": AGENT, "name": "Ada", "role": "ceo"}, {"id": OTHER, "name": "Forge", "role": "engineer"}]
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
    monkeypatch.setattr(messaging_repo, "set_agent_names", set_names)
    monkeypatch.setattr(messaging_repo, "list_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging, "get_live_event_publisher", lambda: SimpleNamespace(publish_user=AsyncMock()))
    monkeypatch.setattr(
        container_repo,
        "get_by_status",
        AsyncMock(return_value=[{"ownerId": OWNER, "roomId": R_LIVE, "kind": "huddle", "deadline": 2**40}]),
    )
    send_frames = AsyncMock()
    monkeypatch.setattr(livekit_huddles, "send_frames", send_frames)
    monkeypatch.setattr("core.services.clerk_admin.list_organization_members", AsyncMock(return_value=[]))
    admin = MagicMock()

    async def patch_agent(*, session_cookie, agent_id, body):
        next(r for r in roster if r["id"] == agent_id)["name"] = body["name"]
        return {}

    admin.patch_agent = AsyncMock(side_effect=patch_agent)
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: admin)
    monkeypatch.setattr("core.services.paperclip_admin_session.get_admin_session_cookie", AsyncMock(return_value="c"))
    app = FastAPI()
    app.include_router(messaging_router.router)
    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        files=files,
        labels=labels,
        admin=admin,
        send_frames=send_frames,
        runs=runs,
    )


def _post(world, headers=AUTH, **body):
    return world.client.post(URL, headers=headers, json={"agentId": AGENT, "runId": RUN, **body})


def _notices(send_frames) -> list[tuple[str, dict]]:
    return [(c.args[0], ev["payload"]) for c in send_frames.await_args_list for ev in c.args[1]]


def test_a_member_renames_by_voice(world):
    world.files[NOTES_KEY] = "Your name is Ada.\n\nAden asked: be blunt"
    r = _post(world, action="rename", name="Nova")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "agentId": AGENT, "name": "Nova", "notes": "Aden asked: be blunt"}
    assert world.admin.patch_agent.await_args.kwargs["body"] == {"name": "Nova"}
    assert world.files[NOTES_KEY].startswith("Your name is Nova.")
    assert world.labels == {R_LIVE: "Nova", R_QUIET: "Nova"}
    assert _notices(world.send_frames) == [
        (R_LIVE, {"kind": "renamed", "agentId": AGENT, "from": "Ada", "to": "Nova", "by": "user_1", "collides": False})
    ]


def test_a_guests_steer_makes_them_the_requester_c8_checks(world, monkeypatch):
    from core.services import voice_calls as vc

    # A steer SETs the speaker on the run's row (an atomic update, never a whole-row put).
    def steer(owner, run_id, fields):
        world.runs[run_id] = {**world.runs.get(run_id, {}), **fields}
        return True

    monkeypatch.setattr(messaging_repo, "update_voice_run", AsyncMock(side_effect=steer))
    monkeypatch.setattr(vc, "_run_control", AsyncMock(return_value=202))
    monkeypatch.setattr(vc, "_speaker_name", AsyncMock(return_value="Gus"))
    monkeypatch.setattr(vc.local_session_grants, "mint", lambda *a: "lowered-grant")
    import asyncio

    call = {"ownerId": OWNER, "roomId": R_LIVE}
    steered = asyncio.run(
        vc.steer_voice_run(
            call=call, run_id=RUN, agent_id=AGENT, speaker_id="user_guest", utterance="rename yourself to X"
        )
    )
    assert steered["steered"] is True
    r = _post(world, action="rename", name="X")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_member"
    world.admin.patch_agent.assert_not_awaited()


def test_a_relabel_failure_after_the_rename_still_answers_ok(world, monkeypatch):
    monkeypatch.setattr(messaging_repo, "set_agent_names", AsyncMock(side_effect=RuntimeError("ddb blip")))
    r = _post(world, action="rename", name="Nova")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_a_paperclip_failure_errors_with_no_notice_and_no_file_write(world):
    world.files[NOTES_KEY] = "Your name is Ada.\n\n"
    world.admin.patch_agent.side_effect = PaperclipApiError("PATCH /api/agents -> 500", 500, {})
    r = _post(world, action="rename", name="Nova")
    assert r.status_code == 500
    assert world.files[NOTES_KEY] == "Your name is Ada.\n\n"
    world.send_frames.assert_not_awaited()


@pytest.mark.parametrize(
    ("run_id", "body", "status", "code"),
    [
        ("run-guest", {"action": "rename", "name": "Nova"}, 403, "not_a_member"),
        ("run-other", {"action": "rename", "name": "Nova"}, 404, "voice_run_not_found"),
        ("missing", {"action": "add_note", "text": "hi"}, 404, "voice_run_not_found"),
        (RUN, {"action": "rename", "name": "forge"}, 409, "name_taken"),
        (RUN, {"action": "rename", "name": "two\nlines"}, 422, "invalid_name"),
    ],
)
def test_refusals(world, run_id, body, status, code):
    r = world.client.post(URL, headers=AUTH, json={"agentId": AGENT, "runId": run_id, **body})
    assert r.status_code == status
    assert r.json()["detail"]["code"] == code
    world.admin.patch_agent.assert_not_awaited()
    assert NOTES_KEY not in world.files


def test_the_fourth_rename_in_a_day_is_429(world):
    for name in ("Nova", "Vega", "Lyra"):
        assert _post(world, action="rename", name=name).status_code == 200
    r = _post(world, action="rename", name="Orion")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"


def test_add_note_attributes_the_speaker_and_drops_the_oldest_past_1000(world):
    r = _post(world, action="add_note", text="be blunt")
    assert r.json()["notes"] == "Aden asked: be blunt"
    assert world.files[NOTES_KEY] == "Your name is Ada.\n\nAden asked: be blunt"
    assert _notices(world.send_frames) == [
        (R_LIVE, {"kind": "voice_note_saved", "agentId": AGENT, "name": "Ada", "text": "be blunt", "by": "user_1"})
    ]
    old = "\n".join(f"Aden asked: {i:03d} " + "x" * 280 for i in range(3))  # ~885 chars
    world.files[NOTES_KEY] = f"Your name is Ada.\n\n{old}"
    notes = _post(world, action="add_note", text="y" * 200).json()["notes"]
    assert len(notes) <= 1000
    assert "000" not in notes and notes.endswith("Aden asked: " + "y" * 200)


def test_replace_writes_one_line_and_clear_keeps_the_name_line(world):
    world.files[NOTES_KEY] = "Your name is Ada.\n\nAden asked: a\nAden asked: b"
    assert _post(world, action="replace_notes", text="slower").json()["notes"] == "Aden asked: slower"
    assert _post(world, action="clear_notes").json()["notes"] == ""
    assert world.files[NOTES_KEY] == "Your name is Ada.\n\n"


def test_a_wrong_hook_token_is_401(world):
    r = _post(world, headers={"Authorization": "Bearer nope"}, action="clear_notes")
    assert r.status_code == 401


async def test_count_voice_edit_is_a_conditional_add_per_agent_kind_and_day():
    with (
        patch.object(messaging_repo.settings, "MESSAGING_TABLE", "test-messaging"),
        patch("core.repositories.messaging_repo.run_in_thread", new_callable=AsyncMock) as run,
    ):
        assert await messaging_repo.count_voice_edit(OWNER, AGENT, "rename", 3) is True
        kwargs = run.await_args.kwargs
        assert kwargs["Key"]["pk"] == f"OWNER#{OWNER}"
        assert kwargs["Key"]["sk"].startswith(f"VOICEEDIT#{AGENT}#rename#")
        assert kwargs["ConditionExpression"] == "attribute_not_exists(edits) OR edits < :limit"
        assert "expiresAt" in kwargs["UpdateExpression"]
        run.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        assert await messaging_repo.count_voice_edit(OWNER, AGENT, "rename", 3) is False
