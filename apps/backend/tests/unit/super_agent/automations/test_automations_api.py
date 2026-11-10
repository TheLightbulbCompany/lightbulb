"""Automations API, the confirm card and Run now (lane A, Task 6; contracts-v2 §11, spec 06 §5.1)."""

import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import tasks_schema as ts
from core.auth import AuthContext, get_current_user
from core.repositories import automations_repo as repo
from core.repositories import tasks_repo
from core.services import automation_schedule
from core.temporal.automations import FireInput

WS = "org_1"
SAM = "user_sam"
PRIYA = "user_priya"
ROOM = str(uuid.uuid4())
BASE = "/api/v1/automations"

_IN = {
    "name": "Monday signups",
    "instructions": "Post the week's signups in #workshop-launch.",
    "schedule": {"cron": ["0 9 * * 1"], "tz": "America/New_York"},
    "deliverTo": f"room:{ROOM}",
}


@pytest.fixture
def api(table, monkeypatch):
    """Switch on, moto rows, Temporal faked (the Schedule wrapper records calls), billing entitled."""
    from core.temporal import client as tc
    from main import app

    temporal = MagicMock()
    temporal.start_workflow = AsyncMock()
    monkeypatch.setattr(tc, "_client", temporal)
    calls: list[tuple] = []

    async def upsert(row):
        calls.append(("upsert", row["scheduleId"], row["status"]))
        return "created"

    async def delete(ws, sid):
        calls.append(("delete", sid))

    async def next_runs(ws, sid, n=3):
        return [datetime(2027, 1, 4, 14, 0, tzinfo=timezone.utc)]

    with (
        patch("core.services.super_agent_flags.flags_for", AsyncMock(return_value=True)) as flags,
        patch.object(automation_schedule, "upsert", upsert),
        patch.object(automation_schedule, "delete", delete),
        patch.object(automation_schedule, "next_runs", next_runs),
        patch("core.services.entitlement_guards.billing_repo") as billing,
        patch("core.services.chat_gate.gate_chat", AsyncMock(return_value={"blocked": False})) as gate,
        patch("core.repositories.user_repo.get_timezone", AsyncMock(return_value="Europe/London")),
        patch("core.repositories.messaging_repo.get_member", AsyncMock(return_value={"id": "member"})),
        patch("core.services.messaging.require_room_member", AsyncMock(return_value=({}, {}))) as room_door,
    ):
        billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        yield SimpleNamespace(
            client=TestClient(app),
            app=app,
            temporal=temporal,
            calls=calls,
            flags=flags,
            gate=gate,
            billing=billing,
            room_door=room_door,
        )
    app.dependency_overrides.pop(get_current_user, None)


def _as(api, user=SAM, *, admin=True):
    api.app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id=user, org_id=WS, org_role="org:admin" if admin else "org:member"
    )
    return api.client


async def _draft(api, user=SAM, **over):
    r = _as(api, user).post(BASE, json=_IN | over)
    assert r.status_code == 200, r.text
    return r.json()


async def _active(api, **over):
    view = await _draft(api, **over)
    r = _as(api, SAM, admin=True).post(f"{BASE}/{view['id']}/activate")  # a room automation: an admin turns it on
    assert r.status_code == 200, r.text
    return r.json()


# --- the switch, create, view ----------------------------------------------------------------------------


async def test_deliver_room_must_be_a_room_the_saver_sits_in(api):
    """Gate run 2 (A11): an automation saved with `room:acc-a1-…` made every run fail its Router batch."""
    from core.services.messaging import ApiProblem

    api.room_door.side_effect = ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    r = _as(api).post(BASE, json=_IN | {"deliverTo": "room:acc-a1-66ac88"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "room_not_found"
    api.room_door.side_effect = None
    view = await _draft(api)
    api.room_door.side_effect = ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    r = _as(api).patch(f"{BASE}/{view['id']}", json={"deliverTo": "room:nope", "version": view["version"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "room_not_found"


async def test_create_is_draft_and_schedule_paused(api):
    view = await _draft(api)
    assert view["status"] == "draft" and view["createdBy"] == "user" and view["ownerUserId"] == SAM
    assert view["schedule"] == {"cron": ["0 9 * * 1"], "tz": "America/New_York", "remainingRuns": None}
    assert view["nextRuns"] == ["2027-01-04T14:00:00+00:00"]
    assert view["budgetPerRunUsd"] == 1.0 and view["deliverTo"] == f"room:{ROOM}" and view["hooks"] == []
    assert api.calls == [("upsert", view["id"], "draft")]  # the Temporal Schedule exists, paused
    assert (await repo.get_schedule(WS, view["id"]))["cron"] == ["0 9 * * 1"]


async def test_create_rejects_bad_cron_and_tz(api):
    r = _as(api).post(BASE, json=_IN | {"schedule": {"cron": ["*/5 * * * *"], "tz": "America/New_York"}})
    assert r.status_code == 400 and "10 minutes" in r.json()["detail"]["message"]
    r = _as(api).post(BASE, json=_IN | {"schedule": {"cron": ["0 9 * * 1"], "tz": "Mars/Olympus"}})
    assert r.status_code == 400
    assert await repo.list_schedules(WS) == []


async def test_view_returns_schedule_tz_not_viewer(api):
    view = await _draft(api)
    r = _as(api, PRIYA).get(f"{BASE}/{view['id']}")  # Priya's own zone is Europe/London (patched)
    assert r.status_code == 200 and r.json()["schedule"]["tz"] == "America/New_York"


async def test_event_only_automation_has_no_schedule_or_next_runs(api):
    view = await _draft(api, schedule=None)
    assert view["schedule"] is None and view["nextRuns"] == []
    assert api.calls == []


async def test_member_cannot_add_webhook_trigger(api):
    body = _IN | {"events": [{"provider": "stripe", "eventIds": ["invoice.payment_failed"]}]}
    r = _as(api, PRIYA, admin=False).post(BASE, json=body)
    assert r.status_code == 403
    assert await repo.list_schedules(WS) == []
    assert _as(api, PRIYA, admin=False).post(BASE, json=_IN).status_code == 200  # a schedule alone is fine


async def test_create_with_event_trigger_seeds_a_hook(api):
    seed = AsyncMock(return_value={"trigger_public_id": "pub_1", "guided": True, "instructions": "paste"})
    with patch("core.services.automation_hooks.seed", seed):
        view = await _draft(api, events=[{"provider": "stripe", "eventIds": ["invoice.payment_failed"]}])
    seed.assert_awaited_once()
    kw = seed.await_args.kwargs
    assert seed.await_args.args == (WS, "stripe", ["invoice.payment_failed"])
    assert kw["automation_id"] == view["id"] and kw["actor_user_id"] == SAM and kw["specialist"] is None
    assert view["setup"] == [{"publicId": "pub_1", "guided": True, "instructions": "paste"}]


async def test_hooks_render_from_hook_rows(api):
    view = await _draft(api)
    await repo.put_hook(
        WS,
        {
            "publicId": "pub_1",
            "scheduleId": view["id"],
            "provider": "stripe",
            "events": ["invoice.payment_failed"],
            "signingMode": "hmac_sha256",
            "secretRef": "hook-pub_1",
            "armed": True,
        },
    )
    row = await repo.get_schedule(WS, view["id"])
    await repo.update_schedule(WS, view["id"], {"hookPublicIds": ["pub_1"]}, expected_version=row["version"])
    hooks = _as(api).get(f"{BASE}/{view['id']}").json()["hooks"]
    assert hooks == [
        {
            "publicId": "pub_1",
            "provider": "stripe",
            "events": ["invoice.payment_failed"],
            "fireUrl": hooks[0]["fireUrl"],
            "armed": True,
            "lastSigFailAt": None,
        }
    ]
    assert hooks[0]["fireUrl"].endswith(f"/api/v1/hooks/t/{WS}/pub_1")


# --- activate, pause, resume, patch, delete -----------------------------------------------------------------


async def test_activate_requires_entitlement(api):
    view = await _draft(api)
    api.billing.get_by_owner_id = AsyncMock(return_value=None)
    r = _as(api).post(f"{BASE}/{view['id']}/activate")
    assert r.status_code == 402
    assert (await repo.get_schedule(WS, view["id"]))["status"] == "draft"


async def test_activate_then_pause_resume_matches_temporal(api):
    view = await _draft(api)
    sid = view["id"]
    assert _as(api).post(f"{BASE}/{sid}/activate").json()["status"] == "active"
    assert _as(api).post(f"{BASE}/{sid}/pause").json()["status"] == "paused"
    assert _as(api).post(f"{BASE}/{sid}/resume").json()["status"] == "active"
    assert [c for c in api.calls if c[0] == "upsert"] == [
        ("upsert", sid, "draft"),
        ("upsert", sid, "active"),
        ("upsert", sid, "paused"),
        ("upsert", sid, "active"),
    ]
    assert _as(api).post(f"{BASE}/{sid}/resume").status_code == 409  # already active


async def test_a_one_off_whose_date_passed_will_not_turn_on(api):
    """Turned on after its date, a one-off would wait a year: 409 with words the card can show."""
    view = await _draft(api, schedule={"cron": ["21 6 5 11 *"], "tz": "America/New_York"})
    assert view["schedule"]["remainingRuns"] == 1
    row = await repo.get_schedule(WS, view["id"])
    await repo.update_schedule(
        WS, view["id"], {"scheduleSetAt": "2026-11-04T12:00:00+00:00"}, expected_version=row["version"]
    )
    r = _as(api).post(f"{BASE}/{view['id']}/activate")
    assert r.status_code == 409
    assert r.json()["detail"] == {
        "code": "one_off_passed",
        "message": "This was for 5 Nov; set a new time to turn it on.",
    }
    assert (await repo.get_schedule(WS, view["id"]))["status"] == "draft"
    # a new time (set now, so its date is ahead) turns on
    cur = _as(api).get(f"{BASE}/{view['id']}").json()
    patch = {"schedule": {"cron": ["21 6 5 11 *"], "tz": "America/New_York"}, "version": cur["version"]}
    assert _as(api).patch(f"{BASE}/{view['id']}", json=patch).status_code == 200
    assert _as(api).post(f"{BASE}/{view['id']}/activate").status_code == 200


async def test_remaining_runs_zero_keeps_a_dated_cron_yearly(api):
    view = await _draft(api, schedule={"cron": ["0 9 5 11 *"], "tz": "UTC", "remainingRuns": 0})
    assert view["schedule"]["remainingRuns"] == 0
    assert _as(api).post(f"{BASE}/{view['id']}/activate").status_code == 200


async def test_room_automation_needs_an_org_admin_to_activate(api):
    """A room automation runs for everyone in the room: turning it on is an org-admin act, even for its owner
    (the Router's proposal is deliverTo=room with ownerUserId=requester)."""
    view = await _draft(api, SAM, deliverTo=f"room:{ROOM}")
    assert _as(api, SAM, admin=False).post(f"{BASE}/{view['id']}/activate").status_code == 403  # owner, member
    assert _as(api, PRIYA, admin=False).post(f"{BASE}/{view['id']}/activate").status_code == 403
    assert _as(api, PRIYA, admin=True).post(f"{BASE}/{view['id']}/activate").status_code == 200
    r = _as(api, SAM, admin=False).post(BASE, json=_IN | {"deliverTo": f"room:{ROOM}", "activate": True})
    assert r.status_code == 403 and await repo.list_schedules(WS) and len(await repo.list_schedules(WS)) == 1


async def test_personal_automation_is_activated_by_its_owner_only(api):
    view = await _draft(api, SAM, deliverTo="none")
    assert _as(api, PRIYA, admin=True).post(f"{BASE}/{view['id']}/activate").status_code == 403  # admin, not owner
    assert _as(api, SAM, admin=False).post(f"{BASE}/{view['id']}/activate").status_code == 200
    r = _as(api, SAM, admin=False).post(BASE, json=_IN | {"deliverTo": "none", "activate": True})
    assert r.status_code == 200 and r.json()["status"] == "active"


def _google_hook(sid, pid, *, armed, expires_at):
    return {
        "publicId": pid,
        "scheduleId": sid,
        "provider": "google",
        "events": ["calendar.changed"],
        "signingMode": "bearer",
        "secretRef": f"hook-{pid}",
        "armed": armed,
        "account": "ada@example.com",
        "expiresAt": expires_at,
    }


async def test_activate_rearms_only_the_broken_google_watch(api):
    view = await _active(api)
    sid = view["id"]
    await repo.put_hook(WS, _google_hook(sid, "pub_broken", armed=False, expires_at="2020-01-01T00:00:00+00:00"))
    await repo.put_hook(WS, _google_hook(sid, "pub_fine", armed=True, expires_at="2099-01-01T00:00:00+00:00"))
    row = await repo.get_schedule(WS, sid)
    await repo.update_schedule(WS, sid, {"hookPublicIds": ["pub_broken", "pub_fine"]}, expected_version=row["version"])
    await repo.set_status(WS, sid, "paused_system", reason="google_watch_broken", allowed_from=frozenset({"active"}))
    with (
        patch("core.services.automation_hooks.renew", AsyncMock(return_value=1_900_000_000_000)) as renew,
        patch("core.services.automation_hooks.start_keeper", AsyncMock()) as keeper,
    ):
        r = _as(api).post(f"{BASE}/{sid}/resume")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["pausedReason"] is None
    renew.assert_awaited_once_with(WS, "pub_broken")  # the healthy one is left alone
    keeper.assert_awaited_once_with(WS, "pub_broken", 1_900_000_000_000)


async def test_activate_with_a_dead_google_watch_stays_paused(api):
    view = await _active(api)
    sid = view["id"]
    await repo.put_hook(WS, _google_hook(sid, "pub_g", armed=True, expires_at="2020-01-01T00:00:00+00:00"))
    row = await repo.get_schedule(WS, sid)
    await repo.update_schedule(WS, sid, {"hookPublicIds": ["pub_g"]}, expected_version=row["version"])
    await repo.set_status(WS, sid, "paused_system", reason="google_watch_broken", allowed_from=frozenset({"active"}))
    with patch("core.services.automation_hooks.renew", AsyncMock(side_effect=RuntimeError("gog: no scope"))):
        r = _as(api).post(f"{BASE}/{sid}/resume")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "google_watch_broken"
    assert (await repo.get_schedule(WS, sid))["status"] == "paused_system"


async def test_stale_version_409(api):
    view = await _draft(api)
    r = _as(api).patch(f"{BASE}/{view['id']}", json={"name": "Renamed", "version": view["version"] + 5})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_version"
    r = _as(api).patch(f"{BASE}/{view['id']}", json={"name": "Renamed", "version": view["version"]})
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["version"] == view["version"] + 1


async def test_patch_schedule_syncs_temporal_and_validates(api):
    view = await _draft(api)
    body = {"schedule": {"cron": ["0 17 * * 5"], "tz": "Europe/London", "remainingRuns": 1}, "version": view["version"]}
    r = _as(api).patch(f"{BASE}/{view['id']}", json=body)
    assert r.status_code == 200 and r.json()["schedule"] == body["schedule"]
    assert api.calls[-1] == ("upsert", view["id"], "draft")
    bad = {"schedule": {"cron": ["0 9 * * * *"], "tz": "UTC"}, "version": view["version"] + 1}
    assert _as(api).patch(f"{BASE}/{view['id']}", json=bad).status_code == 400
    assert (
        _as(api, PRIYA, admin=False).patch(f"{BASE}/{view['id']}", json={"name": "x", "version": 2}).status_code == 403
    )


async def test_delete_removes_row_schedule_and_hooks(api):
    view = await _draft(api)
    sid = view["id"]
    row = await repo.get_schedule(WS, sid)
    await repo.update_schedule(WS, sid, {"hookPublicIds": ["pub_1", "pub_2"]}, expected_version=row["version"])
    with patch("core.services.automation_hooks.delete", AsyncMock()) as hooks_delete:
        r = _as(api).delete(f"{BASE}/{sid}")
    assert r.status_code == 204
    assert [c.args for c in hooks_delete.await_args_list] == [(WS, "pub_1"), (WS, "pub_2")]
    assert ("delete", sid) in api.calls
    assert await repo.get_schedule(WS, sid) is None
    assert _as(api).delete(f"{BASE}/{sid}").status_code == 404


# --- run now --------------------------------------------------------------------------------------------


async def test_run_now_starts_the_fire_workflow(api):
    view = await _active(api)
    sid = view["id"]
    r = _as(api).post(f"{BASE}/{sid}/run")
    assert r.status_code == 200, r.text
    trigger_id = r.json()["triggerId"]
    ulid = trigger_id.removeprefix(f"manual:{sid}:")
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", ulid)
    kw = api.temporal.start_workflow.await_args.kwargs
    assert api.temporal.start_workflow.await_args.args[0] == "TriggerFireWorkflow"
    assert api.temporal.start_workflow.await_args.args[1] == FireInput(
        WS, sid, "manual", dedupe_key=ulid, actor_user_id=SAM
    )
    assert kw["id"] == f"fire:{WS}:{sid}:manual:{ulid}"
    api.gate.assert_awaited_once()
    assert api.gate.await_args.kwargs["session_key"] == f"automation:{sid}"


async def test_run_now_while_running_409(api):
    view = await _active(api)
    assert await repo.claim_run(WS, view["id"], "t-running")
    r = _as(api).post(f"{BASE}/{view['id']}/run")
    assert r.status_code == 409 and r.json()["detail"] == {"code": "run_in_progress", "taskId": "t-running"}
    assert not api.temporal.start_workflow.called


async def test_run_now_gate_chat_blocks(api):
    view = await _active(api)
    api.gate.return_value = {"blocked": True, "code": "out_of_credits", "message": "Add credits."}
    r = _as(api).post(f"{BASE}/{view['id']}/run")
    assert r.status_code == 402 and r.json()["detail"]["code"] == "out_of_credits"
    assert not api.temporal.start_workflow.called


async def test_run_now_needs_an_active_automation(api):
    view = await _draft(api)
    r = _as(api).post(f"{BASE}/{view['id']}/run")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_active"


async def test_run_now_temporal_down_503(api):
    from core.temporal.client import TemporalUnavailable

    view = await _active(api)
    api.temporal.start_workflow = AsyncMock(side_effect=TemporalUnavailable("down"))
    assert _as(api).post(f"{BASE}/{view['id']}/run").status_code == 503


# --- list and runs ----------------------------------------------------------------------------------------


async def test_list_shows_own_room_and_admin_views(api):
    mine = await _draft(api, SAM, deliverTo="none")
    priyas = await _draft(api, PRIYA, deliverTo=f"room:{ROOM}")

    async def member(room_id, kind, user_id):
        return {"id": user_id} if user_id == SAM and room_id == ROOM else None

    with patch("core.repositories.messaging_repo.get_member", side_effect=member):
        sam = _as(api, SAM, admin=False).get(BASE).json()["automations"]
        priya = _as(api, PRIYA, admin=False).get(BASE).json()["automations"]
        admin = _as(api, "user_admin", admin=True).get(BASE).json()["automations"]
    assert {a["id"] for a in sam} == {mine["id"], priyas["id"]}  # own + delivered to a room Sam is in
    assert {a["id"] for a in priya} == {priyas["id"]}  # not Sam's private one
    assert {a["id"] for a in admin} == {mine["id"], priyas["id"]}


async def test_runs_filtered_by_can_view(api):
    view = await _active(api)
    sid = view["id"]
    tasks = {}
    for who in (SAM, PRIYA):
        tid = ts.new_task_id()
        await tasks_repo.create_task(
            WS,
            {
                "taskId": tid,
                "identifier": f"LB-{len(tasks) + 1}",
                "title": "run",
                "status": "queued",
                "audience": {"kind": "person", "userIds": [who]},
                "lane": "shared",
                "triggerId": "sched:s:1",
                "policyRef": ts.schedule_sk(sid),
            },
        )
        tasks[who] = tid
    for tid in tasks.values():
        assert await repo.claim_run(WS, sid, tid)
        await repo.close_run(WS, sid, tid, failed=False)

    async def can_view(task, user_id):
        return user_id in task["audience"].get("userIds", [])

    with patch("core.services.tasks_access.can_view", side_effect=can_view):
        runs = _as(api, SAM, admin=False).get(f"{BASE}/{sid}/runs").json()["runs"]
    assert [r["taskId"] for r in runs] == [tasks[SAM]]
    assert runs[0]["status"] == "queued"


async def test_runs_show_an_idle_skip_in_plain_words(api):
    sid = (await _active(api))["id"]
    await repo.note_skip(WS, sid, reason="idle")
    skipped = _as(api, SAM, admin=False).get(f"{BASE}/{sid}/runs").json()["skipped"]
    assert skipped["text"] == "Skipped — nobody's been active for 3 days" and skipped["at"]


# --- the confirm card (Router proposal) ----------------------------------------------------------------------


@pytest.fixture
def internal(api):
    post = AsyncMock(return_value="msg_1")
    with (
        patch("routers.internal_automations._require_hook_token", AsyncMock(return_value={})),
        patch("core.services.task_conversation.post_as_lightbulb", post),
    ):
        yield SimpleNamespace(api=api, post=post)


async def _chat_trigger(mid="00000000-0000-4000-8000-000000000001", *, root=None):
    tid = ts.trigger_id("chat", mid)
    ref = {"roomId": ROOM, "messageId": mid} | ({"threadRoot": root} if root else {})
    await tasks_repo.put_trigger(
        WS,
        {
            "triggerId": tid,
            "source": "chat",
            "ref": ref,
            "admission": "respond",
            "lane": "shared",
            "actorUserId": SAM,
            "receivedAt": "2026-11-05T10:00:00+00:00",
        },
    )
    return tid


async def test_propose_creates_draft_and_posts_card(internal):
    tid = await _chat_trigger(root="00000000-0000-4000-8000-000000000000")
    body = {
        "name": "Monday signups",
        "instructions": "Post the week's signups here.",
        "cron": ["0 9 * * 1"],
        "triggerIds": [tid],
        "specialistId": "agent_growth",
    }
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 200, r.text
    sid = r.json()["automationId"]
    row = await repo.get_schedule(WS, sid)
    assert row["status"] == "draft" and row["createdBy"] == "router" and row["ownerUserId"] == SAM
    assert row["tz"] == "Europe/London"  # the requester's zone (users.timezone), not a guess
    assert row["deliverTo"] == f"room:{ROOM}" and row["specialist"] == "agent_growth"
    assert ("upsert", sid, "draft") in internal.api.calls  # paused Schedule, so the card can show next runs
    internal.post.assert_awaited_once()
    kw = internal.post.await_args.kwargs
    assert internal.post.await_args.args == (WS, ROOM)
    assert kw["kind"] == "automation_confirm" and kw["event_id"] == f"automation_confirm:{sid}"
    assert kw["thread_root"] == "00000000-0000-4000-8000-000000000000"
    assert kw["attrs"] == {"automationId": sid}
    assert not re.search(r"\d", kw["text"])  # the card renders times from the view; the text names none
    assert kw["text"].endswith("Turn it on with the button on the card.")  # the card's Turn on, never "confirm"


async def test_a_phone_caller_never_proposes_an_automation(internal):
    tid = ts.trigger_id("voice", "call-1", "turn-0")
    await tasks_repo.put_trigger(WS, {
        "triggerId": tid, "source": "voice", "admission": "respond", "lane": "shared", "actorUserId": SAM,
        "ref": {"roomId": ROOM, "messageId": "m", "origin": "phone_contact", "callId": "call-1"},
        "receivedAt": "2026-11-05T10:00:00+00:00",
    })  # fmt: skip
    body = {"name": "Weekly", "instructions": "Email every customer.", "cron": ["0 9 * * 1"], "triggerIds": [tid]}
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"
    assert not internal.post.called and await repo.list_schedules(WS) == []


async def test_a_dated_cron_is_a_one_off_from_propose_and_patch(internal):
    """`0 9 6 11 *` matches every year: stored with no run count it would fire again next 6 Nov."""
    body = {"name": "n", "instructions": "i", "cron": ["0 9 6 11 *"], "triggerIds": [await _chat_trigger()]}
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 200, r.text
    row = await repo.get_schedule(WS, r.json()["automationId"])
    assert row["remainingRuns"] == 1
    patch = {"schedule": {"cron": ["30 8 7 11 *"], "tz": "UTC", "remainingRuns": None}, "version": row["version"]}
    r = _as(internal.api).patch(f"{BASE}/{row['scheduleId']}", json=patch)
    assert r.status_code == 200 and r.json()["schedule"]["remainingRuns"] == 1


async def test_propose_repeat_yearly_keeps_a_dated_cron_unlimited(internal):
    body = {
        "name": "n",
        "instructions": "i",
        "cron": ["0 9 5 11 *"],
        "repeat": "yearly",
        "triggerIds": [await _chat_trigger()],
    }
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 200, r.text
    assert (await repo.get_schedule(WS, r.json()["automationId"]))["remainingRuns"] == 0


async def test_propose_cleans_up_when_the_card_cannot_be_posted(internal):
    tid = await _chat_trigger()
    internal.post.side_effect = RuntimeError("messaging down")
    body = {"name": "n", "instructions": "i", "cron": ["0 9 * * 1"], "triggerIds": [tid]}
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 502 and r.json()["detail"]["code"] == "card_not_posted"
    assert await repo.list_schedules(WS) == []  # no orphan draft
    assert ("delete", internal.api.calls[0][1]) in internal.api.calls  # its paused Schedule went too


async def test_propose_validates_deliver_to(internal):
    tid = await _chat_trigger()
    body = {"name": "n", "instructions": "i", "cron": ["0 9 * * 1"], "triggerIds": [tid], "deliverTo": "everyone"}
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 422


async def test_dm_delivery_is_the_owners_only(api):
    """Audit item 8 review: `dm:` names the automation's owner; anyone else's DM is refused at save time."""
    r = _as(api, SAM, admin=True).post(BASE, json=_IN | {"deliverTo": f"dm:{PRIYA}"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "dm_owner_only"
    view = await _draft(api, SAM, deliverTo=f"dm:{SAM}")
    r = _as(api, PRIYA, admin=True).patch(
        f"{BASE}/{view['id']}", json={"deliverTo": f"dm:{PRIYA}", "version": view["version"]}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "dm_owner_only"  # owner = the row's, not the caller
    r = _as(api, PRIYA, admin=True).patch(
        f"{BASE}/{view['id']}", json={"deliverTo": f"dm:{SAM}", "version": view["version"]}
    )
    assert r.status_code == 200


async def test_propose_resolves_a_room_name_to_its_id(internal):
    """Gate run 3: the Router saved `room:acc-a1-e3ffa8` (the room's name); its runs could never post there and
    the run's end_attempt failed. A name of one room the requester sits in resolves to that room's id."""
    from core.services.messaging import ApiProblem

    tid = await _chat_trigger()
    other = str(uuid.uuid4())
    seats = [{"meta": {"roomId": other, "name": "acc-a1-e3ffa8"}}, {"meta": {"roomId": ROOM, "name": "general"}}]

    async def door(ws, user, room):
        if room != other:
            raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
        return {}, {}

    internal.api.room_door.side_effect = door
    url, auth = f"/api/v1/internal/automations/{WS}/propose", {"Authorization": "Bearer t"}
    body = {"name": "n", "instructions": "i", "cron": ["0 9 * * 1"], "triggerIds": [tid]}
    with patch("core.repositories.messaging_repo.list_rooms_for_member", AsyncMock(return_value=seats)):
        r = internal.api.client.post(url, json=body | {"deliverTo": "room:acc-a1-e3ffa8"}, headers=auth)
        assert r.status_code == 200, r.text
        assert (await repo.get_schedule(WS, r.json()["automationId"]))["deliverTo"] == f"room:{other}"
        r = internal.api.client.post(url, json=body | {"deliverTo": "room:no-such-room"}, headers=auth)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "room_not_found"
        r = internal.api.client.post(url, json=body, headers=auth)  # the default is the trigger's own room
        assert (await repo.get_schedule(WS, r.json()["automationId"]))["deliverTo"] == f"room:{ROOM}"
    twins = seats + [{"meta": {"roomId": str(uuid.uuid4()), "name": "acc-a1-e3ffa8"}}]
    with patch("core.repositories.messaging_repo.list_rooms_for_member", AsyncMock(return_value=twins)):
        r = internal.api.client.post(url, json=body | {"deliverTo": "room:acc-a1-e3ffa8"}, headers=auth)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "room_not_found"  # two rooms by that name
    internal.api.room_door.side_effect = ApiProblem("ROOM_NOT_FOUND", "Room not found.")  # not seated anywhere
    with patch("core.repositories.messaging_repo.list_rooms_for_member", AsyncMock(return_value=seats)):
        r = internal.api.client.post(url, json=body | {"deliverTo": f"room:{other}"}, headers=auth)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "room_not_found"
        r = internal.api.client.post(url, json=body | {"deliverTo": "room:acc-a1-e3ffa8"}, headers=auth)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "room_not_found"


async def test_propose_dm_is_the_requesters_only(internal):
    tid = await _chat_trigger()
    body = {"name": "n", "instructions": "i", "cron": ["0 9 * * 1"], "triggerIds": [tid], "deliverTo": "dm:user_x"}
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "dm_owner_only"
    assert await repo.list_schedules(WS) == []


async def test_propose_needs_a_room(internal):
    body = {"name": "n", "instructions": "i", "cron": ["0 9 * * 1"], "triggerIds": ["chat:nope"]}
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "no_room"
    assert await repo.list_schedules(WS) == []


async def test_propose_rejects_bad_cron(internal):
    tid = await _chat_trigger()
    body = {"name": "n", "instructions": "i", "cron": ["*/5 * * * *"], "triggerIds": [tid]}
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 400 and "10 minutes" in r.json()["detail"]["message"]


async def test_typed_yes_never_activates(internal):
    """Under the confirm card people type "yes" / "turn it on" / "go ahead": the row stays a draft and the
    Schedule is never unpaused; only `POST /activate` activates (UX §2, C20)."""
    from tests.unit.super_agent.orchestration import test_admission as adm

    tid = await _chat_trigger()
    body = {"name": "n", "instructions": "i", "cron": ["0 9 * * 1"], "triggerIds": [tid]}
    r = internal.api.client.post(
        f"/api/v1/internal/automations/{WS}/propose", json=body, headers={"Authorization": "Bearer t"}
    )
    sid = r.json()["automationId"]
    # Own MonkeyPatch, undone before `api` exits: the function-scoped `monkeypatch` tears down after `api`'s
    # patch() block and would restore the mocked messaging_repo.get_member for every later test.
    with pytest.MonkeyPatch.context() as mp:
        unpause = AsyncMock()
        mp.setattr(automation_schedule, "unpause", unpause)
        # Drive admission the way O's harness does, with the same fakes.
        w = _admission_world(mp, adm)
        for seq, text in enumerate(["yes", "turn it on", "go ahead"], start=1):
            msg = adm._msg(seq, targets=[{"agentId": adm.CEO, "eventKind": "user_request"}])
            msg["text"] = text
            w["msgs"][seq] = msg
            await adm.admission.admit_pointer({"kind": "admit", "roomId": adm.ROOM, "throughSeq": seq})
        assert len(w["router"]) == 3  # the Router was asked, as for any message
        assert (await repo.get_schedule(WS, sid))["status"] == "draft"
        assert not unpause.called
        assert [c for c in internal.api.calls if c[2] == "active"] == []


def _admission_world(monkeypatch, adm):
    """O's `world` fixture body, applied by hand (fixtures are not callable)."""
    w = {
        "meta": {"roomId": adm.ROOM, "ownerId": WS, "kind": "channel", "name": "general"},
        "msgs": {},
        "router": [],
        "inputs": [],
        "jev": [],
        "jev_answer": {"to": "router", "specialist": adm.FIN, "confidence": 0.9},
        "listen": "active",
        "temporal_up": True,
    }

    async def get_room(room_id):
        return w["meta"]

    async def get_message(room_id, seq):
        return w["msgs"].get(seq)

    async def get_member(room_id, kind, mid):
        return {"id": adm.CEO, "kind": "agent", "listenMode": w["listen"]} if mid == adm.CEO else None

    async def foreign(room_id, owner):
        return False

    async def front(ws, *, room_id=None):
        return adm.CEO

    async def signal_router(ref):
        w["router"].append(ref)

    async def signal_input(ws, task_id, ref):
        w["inputs"].append((task_id, ref))

    async def route(meta, msg, members, history, specialists):
        return w["jev_answer"]

    async def nothing(*a, **k):
        return []

    async def agents(ws):
        return [{"agentId": adm.FIN, "name": "Finance", "listenMode": "active"}]

    async def ingest(ws, meta, msg, *, task=None):
        return True

    monkeypatch.setattr(adm.messaging_repo, "get_room", get_room)
    monkeypatch.setattr(adm.messaging_repo, "get_message", get_message)
    monkeypatch.setattr(adm.messaging_repo, "get_member", get_member)
    monkeypatch.setattr(adm.messaging_repo, "has_foreign_members", foreign)
    monkeypatch.setattr(adm.messaging_repo, "list_members", nothing)
    monkeypatch.setattr(adm.messaging_repo, "list_messages_before", nothing)
    monkeypatch.setattr("core.services.task_conversation.front_agent_id", front)
    monkeypatch.setattr("core.repositories.agent_registry_repo.list_agents", agents)
    monkeypatch.setattr(adm.temporal, "signal_router", signal_router)
    monkeypatch.setattr(adm.temporal, "signal_task_input", signal_input)
    monkeypatch.setattr(adm.admission.jev, "route_for_router", route)
    monkeypatch.setattr(adm.admission.room_memory, "ingest_message", ingest)
    return w


async def test_propose_takes_a_bare_trigger_id_and_here(internal):
    """Dev walk R2: the Router sent `trigger_ids: ["fdab1932-…"]` (no `chat:`) and the proposal 400'd `no_room`.
    A bare id resolves to its chat trigger; "here" and "#general" are the trigger's room; the trigger is marked
    as answered by the card (R7)."""
    tid = await _chat_trigger()
    url, auth = f"/api/v1/internal/automations/{WS}/propose", {"Authorization": "Bearer t"}
    body = {"name": "n", "instructions": "i", "cron": ["0 9 * * 1"], "triggerIds": [tid.removeprefix("chat:")]}
    seats = [{"meta": {"roomId": ROOM, "name": "general"}}]
    with patch("core.repositories.messaging_repo.list_rooms_for_member", AsyncMock(return_value=seats)):
        for deliver in (None, "here", "#general", "room:#general"):
            r = internal.api.client.post(url, json=body | ({"deliverTo": deliver} if deliver else {}), headers=auth)
            assert r.status_code == 200, (deliver, r.text)
            sid = r.json()["automationId"]
            assert (await repo.get_schedule(WS, sid))["deliverTo"] == f"room:{ROOM}", deliver
            assert (await tasks_repo.get_trigger(WS, tid))["routedTo"] == {"automationId": sid}


async def test_a_workspace_owned_automation_is_paused_and_turned_on_by_any_org_admin(api):
    """A workspace-owned schedule is controlled by an org admin, never an ordinary member."""
    sid = "workspace-reminder"
    await repo.create_schedule(
        WS,
        {
            "scheduleId": sid,
            "ownerUserId": WS,
            "name": "Reminder",
            "cron": ["0 8 * * *"],
            "tz": "UTC",
            "policyText": "Check in",
            "deliverTo": "none",
        },
    )
    await repo.set_status(WS, sid, "active", reason=None, allowed_from=frozenset({"draft"}))
    assert _as(api, PRIYA, admin=False).post(f"{BASE}/{sid}/pause").status_code in (403, 404)
    assert _as(api, PRIYA, admin=True).post(f"{BASE}/{sid}/pause").json()["status"] == "paused"
    assert _as(api, SAM, admin=False).post(f"{BASE}/{sid}/activate").status_code in (403, 404)
    assert _as(api, SAM, admin=True).post(f"{BASE}/{sid}/activate").json()["status"] == "active"
