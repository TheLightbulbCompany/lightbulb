"""A builder agent's dead turn resumes with no person acting (core/services/apps_run.py)."""

import asyncio
import io
import os
import tarfile
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext  # noqa: E402
from core.config import settings  # noqa: E402
from core.repositories import apps_repo  # noqa: E402
from core.services import apps_room, apps_run, apps_service, apps_sweeper, messaging  # noqa: E402
from main import app as fastapi_app  # noqa: E402
from routers import apps as apps_router  # noqa: E402

pytestmark = pytest.mark.asyncio

OWNER, ROOM, AGENT = "org_a", "room_1", "agt_1"
CEO = {"kind": "agent", "id": AGENT, "displayName": "CEO", "listenMode": "mentions", "mentionHandle": "ceo"}


@pytest.fixture
def world(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-apps",
            KeySchema=[{"AttributeName": "app_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("app_id", "owner_id", "name")],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner-index",
                    "KeySchema": [
                        {"AttributeName": "owner_id", "KeyType": "HASH"},
                        {"AttributeName": "name", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", resource)
        w = SimpleNamespace(
            now=int(time.time()),
            lines=[],
            receipts={},
            issues={},
            issue_calls=0,
            status="in_progress",
            members=[CEO],
            open_runs=[],
            rows=[],
            reason=None,
        )

        async def create_message(**kw):
            key = kw["idemp_sk"]
            if key not in w.receipts:
                w.receipts[key] = {"messageId": f"m{len(w.receipts)}", **kw}
                w.lines.append(w.receipts[key])
            return w.receipts[key], False

        async def create_issue(*, session_cookie, company_id, body):
            w.issue_calls += 1
            return w.issues.setdefault(body["idempotencyKey"], {"id": f"iss{len(w.issues)}", **body})

        async def get_issue(*, session_cookie, issue_id):
            return {"status": w.status}

        admin = SimpleNamespace(_http=None, create_issue=create_issue, get_issue=get_issue)
        monkeypatch.setattr(apps_run, "_now", lambda: w.now)
        monkeypatch.setattr(apps_run.messaging_repo, "create_message", create_message)
        monkeypatch.setattr(apps_run.messaging_repo, "list_members", AsyncMock(side_effect=lambda r: w.members))
        monkeypatch.setattr(
            apps_run.messaging_repo, "list_open_runs_for_room", AsyncMock(side_effect=lambda o, r: w.open_runs)
        )
        monkeypatch.setattr(apps_service.wf_repo, "list_for_owner", AsyncMock(side_effect=lambda o: w.rows))
        monkeypatch.setattr(apps_run.billing_repo, "get_by_owner_id", AsyncMock(return_value={}))
        monkeypatch.setattr(apps_run, "budget_block_reason", AsyncMock(side_effect=lambda o, a: w.reason))
        monkeypatch.setattr(apps_run, "shared_admin_client", lambda: admin)
        monkeypatch.setattr(apps_run, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
        monkeypatch.setattr(apps_run, "resolve_company_id_for_owner", AsyncMock(return_value="co_1"))
        monkeypatch.setattr(settings, "APPS_BUILD_WAKE", True)
        yield w


async def _app(**fields) -> dict:
    row = await apps_repo.create(
        owner_id=OWNER,
        name="crew",
        room_id=ROOM,
        created_by=None,
        actor_id="agent",  # the placeholder a hook-token CLI stores
        actor_kind="agent",
        origin={"kind": "unknown"},
        services=[],
        open_to="room",
        budget_usd=0,
        display_name="Crew",
    )
    if fields:
        row = await apps_repo.patch(row["app_id"], fields)
    return row


def _turn(run="r1", **extra) -> dict:
    return {"runId": run, "roomId": ROOM, "agentId": AGENT, "eventKind": "user_request", "outcome": "error", **extra}


async def _fail(**kw) -> str | None:
    return await apps_run.on_turn_failed(OWNER, _turn(**kw), budget_reason=None, agent_name="CEO")


async def _build(app: dict) -> dict:
    return (await apps_repo.get(app["app_id"], consistent_read=True)).get("build") or {}


async def _at_due(world, app: dict) -> None:
    world.now = int((await _build(app))["due_at"])
    await apps_sweeper.sweep_once()


# --- the approval line and _builder --------------------------------------------------------------


async def test_approval_line_wakes_the_builder_of_an_app_built_with_no_agent_header(world, monkeypatch):
    """The hook-token CLI sends no agent id, so the row says actor_id="agent", a member that does not
    exist: MEMBER_NOT_FOUND inside a swallowed except, no line, no wake (workshop-signup, 30 Oct)."""
    monkeypatch.setattr(apps_router, "_require_hook_token", AsyncMock(return_value={"owner_id": OWNER}))
    monkeypatch.setattr(apps_router, "require_always_on_entitlement", AsyncMock())
    monkeypatch.setattr(apps_service, "_check_room", AsyncMock())
    monkeypatch.setattr(apps_service.apps_source, "put", AsyncMock(return_value="src_1"))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("a.txt")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    response = TestClient(fastapi_app).post(
        "/api/v1/apps/build/source",
        data={"name": "crew", "room_id": ROOM, "commit": "c1"},
        files={"archive": ("a.tgz", buf.getvalue(), "application/gzip")},
        headers={"Authorization": "Bearer t", "X-Lightbulb-Owner": OWNER},
    )
    assert response.status_code == 200
    app_id = response.json()["app_id"]
    assert (await apps_repo.get(app_id))["actor_id"] == "agent"

    post = AsyncMock()
    monkeypatch.setattr(apps_room.messaging, "require_room_member", AsyncMock())
    monkeypatch.setattr(apps_room.messaging, "create_human_message", post)
    await apps_room.post_approval_line(
        owner_id=OWNER,
        user_id="user_1",
        workflow={"app_id": app_id, "workflow_id": "w1", "provider_name": "vercel", "environment": "prod"},
    )
    post.assert_awaited_once()
    assert [m.id for m in post.await_args.args[2].mentions] == [AGENT]


@pytest.mark.parametrize(
    "actor,members,running,expect",
    [
        ("b", ["a", "b"], [], "b"),  # the actor when it can run
        ("agent", ["a", "b"], ["b"], "b"),  # else the only agent with an open run
        ("agent", ["a", "b"], ["a", "b"], "a"),  # else the first
        ("agent", ["a", "b"], [], "a"),
        ("agent", [], [], None),  # else nobody
    ],
)
async def test_builder_order(world, actor, members, running, expect):
    world.members = [{"kind": "agent", "id": m, "listenMode": "mentions"} for m in members]
    world.members.append({"kind": "agent", "id": "obs", "listenMode": "observe"})
    world.open_runs = [{"agentId": r} for r in running]
    got = await apps_room._builder({"actor_id": actor, "owner_id": OWNER}, ROOM)
    assert (got or {}).get("id") == expect


async def test_ask_agent_to_fix_addresses_the_builder_when_the_actor_is_the_placeholder(world, monkeypatch):
    app = await _app(publish_error={"message": "boom"})
    post = AsyncMock()
    monkeypatch.setattr(apps_service.messaging, "require_room_member", AsyncMock())
    monkeypatch.setattr(apps_service.messaging, "create_human_message", post)
    monkeypatch.setattr(apps_service, "_scrubber", AsyncMock(return_value=lambda s: s))
    await apps_service.ask_agent_to_fix(OWNER, app["app_id"], auth=AuthContext(user_id="u", org_id=OWNER))
    assert [m.id for m in post.await_args.args[2].mentions] == [AGENT]


# --- the failure table ---------------------------------------------------------------------------


async def test_a_dead_turn_says_so_then_wakes_the_builder_in_the_room_with_no_forged_mention(world):
    app = await _app()
    assert await _fail() == "CEO lost its place on Crew. Trying again in a minute."
    assert not world.lines  # only the sweeper wakes
    await apps_sweeper.sweep_once()  # not due yet
    assert not world.lines
    await _at_due(world, app)
    assert len(world.lines) == 1
    line = world.lines[0]
    assert line["author"]["kind"] == "system" and line["mentions"] == []
    assert line["dispatch_targets"] == [{"agentId": AGENT, "eventKind": "user_request"}]
    assert (await _build(app))["wake"]["via"] == "room"


async def test_two_sweepers_give_one_message(world, monkeypatch):
    app = await _app()
    await _fail()
    world.now = int((await _build(app))["due_at"])
    await asyncio.gather(apps_run.advance(app), apps_run.advance(app))
    assert len(world.lines) == 1
    # Two that both read the row before either wrote: the compare-and-set admits one.
    stale = await apps_repo.get(app["app_id"], consistent_read=True)
    await _fail()
    world.now = int((await _build(app))["due_at"])
    before = await apps_repo.get(app["app_id"], consistent_read=True)
    monkeypatch.setattr(apps_repo, "get", AsyncMock(return_value=before))
    await asyncio.gather(apps_run.advance(stale), apps_run.advance(stale))
    assert world.issue_calls == 1  # sent once, not merely absorbed by the idempotency key


async def test_a_crash_after_the_post_resends_under_the_same_key(world, monkeypatch):
    app = await _app()
    await _fail()
    real = apps_repo.patch
    calls = {"n": 0}

    async def dies_on_the_write_after_the_post(app_id, fields, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise asyncio.CancelledError  # the process went away
        return await real(app_id, fields, **kw)

    monkeypatch.setattr(apps_repo, "patch", dies_on_the_write_after_the_post)
    world.now = int((await _build(app))["due_at"])
    with pytest.raises(asyncio.CancelledError):
        await apps_run.advance(app)
    assert len(world.lines) == 1
    assert not (await _build(app))["wake"].get("sent")
    await _at_due(world, app)  # the lease ran out
    assert len(world.lines) == 1  # the receipt absorbed the resend
    build = await _build(app)
    assert build["wake"]["sent"] and build["attempts"]["wake"] == 1  # and the attempt was not spent twice


async def test_a_crash_before_the_post_still_sends_once(world, monkeypatch):
    app = await _app()
    await _fail()
    real = apps_run.messaging_repo.create_message
    monkeypatch.setattr(apps_run.messaging_repo, "create_message", AsyncMock(side_effect=asyncio.CancelledError))
    world.now = int((await _build(app))["due_at"])
    with pytest.raises(asyncio.CancelledError):
        await apps_run.advance(app)
    monkeypatch.setattr(apps_run.messaging_repo, "create_message", real)
    await _at_due(world, app)
    assert len(world.lines) == 1 and (await _build(app))["attempts"]["wake"] == 1


async def test_an_open_run_gives_no_wake(world):
    app = await _app()
    await _fail()
    world.open_runs = [{"agentId": AGENT}]
    await _at_due(world, app)
    assert not world.lines
    assert int((await _build(app))["due_at"]) == world.now + apps_run.HOLD_S
    world.open_runs = []
    await _at_due(world, app)
    assert len(world.lines) == 1


async def test_a_repeated_failure_goes_room_then_issue_then_one_stuck_line(world):
    app = await _app()
    await _fail()
    await _at_due(world, app)
    assert (len(world.lines), len(world.issues)) == (1, 0)

    assert "Trying again in 5 minutes" in await _fail(run="r2")
    assert int((await _build(app))["due_at"]) == world.now + 300
    await _at_due(world, app)
    assert (len(world.lines), len(world.issues)) == (1, 1)
    issue = next(iter(world.issues.values()))
    assert issue["assigneeAgentId"] == AGENT and issue["idempotencyKey"].startswith(f"apps-build:{app['app_id']}:")

    world.status = "blocked"  # the issue's run died
    assert "Trying again in 30 minutes" in await _fail(run="r3")
    await _at_due(world, app)
    assert (len(world.lines), len(world.issues)) == (1, 2)

    assert await _fail(run="r4") is None  # out of wakes: today's note, and the sweeper gives up
    await _at_due(world, app)
    assert len(world.lines) == 2 and "Crew needs a person" in world.lines[1]["text"]
    assert world.lines[1]["dispatch_targets"] == []
    assert (await _build(app))["due_at"] is None

    assert await _fail(run="r5") is None
    assert (await _build(app))["due_at"] is None  # stuck: not rescheduled, only the receipt would hide it
    await apps_sweeper.sweep_once()
    assert len(world.lines) == 2  # one line, ever


async def test_a_repeated_refusal_in_prose_goes_room_then_issue_then_one_stuck_line(world):
    """A model's refusal is a completed turn with prose (bridge/turn.py): the failure hook never sees it."""
    app = await _app()
    world.status = "done"

    async def refuse(run):
        await apps_run.on_run_ended(OWNER, _turn(run=run, outcome="completed"))

    await refuse("r1")
    assert int((await _build(app))["due_at"]) == world.now + apps_run.STALL_S
    await _at_due(world, app)
    assert (len(world.lines), len(world.issues)) == (1, 0)
    await refuse("r2")
    await _at_due(world, app)
    assert (len(world.lines), len(world.issues)) == (1, 1)
    await _at_due(world, app)  # the issue finished with nothing to show: its own schedule carries on
    assert (len(world.lines), len(world.issues)) == (1, 2)
    await _at_due(world, app)
    assert len(world.lines) == 2 and "needs a person" in world.lines[1]["text"]


@pytest.mark.parametrize("stop", ["aborted_run", "abort_requested"])
async def test_a_stop_never_retries(world, stop):
    app = await _app()
    if stop == "aborted_run":
        await _fail()
        await apps_run.on_run_ended(OWNER, _turn(run="r2", outcome="aborted"))
        assert (await _build(app))["due_at"] is None
    else:
        assert await _fail(abortRequestedAt="2026-10-30T00:00:00Z") is None
        assert not (await _build(app))
    world.now += 3 * 3600
    await apps_sweeper.sweep_once()
    assert not world.lines and not world.issues


async def test_a_spent_allowance_waits_for_midnight_with_jitter_and_spends_no_wake(world, monkeypatch):
    frozen = datetime(2026, 10, 30, 16, 34, tzinfo=UTC)
    midnight = int(datetime(2026, 10, 31, tzinfo=UTC).timestamp())
    world.now = int(frozen.timestamp())
    monkeypatch.setattr(apps_repo, "utc_now_iso", frozen.isoformat)
    app = await _app()
    line = await apps_run.on_turn_failed(OWNER, _turn(), budget_reason="included_daily_cap", agent_name="CEO")
    assert line == "Crew continues tomorrow; today's allowance is used."
    build = await _build(app)
    assert midnight <= build["due_at"] <= midnight + 600
    assert not build.get("attempts")
    world.now = midnight - 1
    await apps_sweeper.sweep_once()
    assert not world.lines
    world.now = int(build["due_at"])
    await apps_sweeper.sweep_once()
    assert len(world.lines) == 1 and world.lines[0]["dispatch_targets"]


async def test_a_cap_found_at_wake_time_posts_once_and_waits(world):
    """The gate stamps the cap after the refusal, so the note can miss it and the sweeper catches it."""
    app = await _app()
    await _fail()
    world.reason = "trial_daily_cap"
    await _at_due(world, app)
    assert [line["dispatch_targets"] for line in world.lines] == [[]]
    assert "continues tomorrow" in world.lines[0]["text"]
    build = await _build(app)
    assert build["due_at"] > world.now and not build.get("attempts")


async def test_an_allowance_that_does_not_come_back_gives_no_wake(world):
    app = await _app()
    assert await apps_run.on_turn_failed(OWNER, _turn(), budget_reason="out_of_credits", agent_name="CEO") is None
    assert not await _build(app)


async def test_a_chat_failure_in_a_finished_apps_room_gives_no_wake(world):
    app = await _app(
        state="live",
        live_version=1,
        live_source_id="src_1",
        sources={"src_1": {"at": "2026-10-30T00:00:00Z"}},
        preview={"deployment_id": "d"},
    )
    world.rows = [
        {
            "app_id": app["app_id"],
            "provider_name": "Vercel",
            "service_ref": "project",
            "environment": e,
            "state": "active",
        }
        for e in ("prod", "dev")
    ] + [
        {
            "app_id": app["app_id"],
            "provider_name": "Neon",
            "service_ref": "postgres",
            "environment": "prod",
            "state": "active",
        }
    ]
    assert await _fail() is None
    assert not await _build(app)


async def test_a_live_app_with_newer_source_is_being_built(world):
    app = await _app(
        state="live",
        live_version=1,
        live_source_id="src_1",
        sources={"src_1": {"at": "2026-10-30T00:00:00Z"}, "src_2": {"at": "2026-10-30T01:00:00Z"}},
        preview={"deployment_id": "d"},
    )
    world.rows = [
        {
            "app_id": app["app_id"],
            "provider_name": "Vercel",
            "service_ref": "project",
            "environment": e,
            "state": "active",
        }
        for e in ("prod", "dev")
    ] + [
        {
            "app_id": app["app_id"],
            "provider_name": "Neon",
            "service_ref": "postgres",
            "environment": "prod",
            "state": "active",
        }
    ]
    assert apps_service._state(app, await apps_service._rows(app))[0] == "ready"
    assert await _fail() is not None


@pytest.mark.parametrize(
    "turn",
    [{"agentId": "someone_else"}, {"roomId": "other_room"}, {"eventKind": "room_event"}],
)
async def test_a_turn_that_is_not_the_builders_gives_no_wake(world, turn):
    app = await _app()
    assert await _fail(**turn) is None
    assert not await _build(app)


async def test_an_app_untouched_for_six_hours_gives_no_wake(world):
    app = await _app()
    world.now += 6 * 3600 + 60
    assert await _fail() is None
    assert not await _build(app)


async def test_a_person_built_app_gives_no_wake(world):
    app = await _app(actor_kind="person")
    assert await _fail() is None
    assert not await _build(app)


async def test_a_source_upload_resets_the_count_and_the_next_wake_gets_its_own_key(world, monkeypatch):
    app = await _app()
    await _fail()
    await _at_due(world, app)
    assert (await _build(app))["attempts"]["wake"] == 1
    monkeypatch.setattr(apps_service, "_check_room", AsyncMock())
    monkeypatch.setattr(apps_service.apps_source, "put", AsyncMock(return_value="src_2"))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    await apps_service.put_source(
        OWNER,
        name="crew",
        room_id=ROOM,
        commit="c2",
        archive=buf.getvalue(),
        actor=apps_service.BuilderCaller(OWNER, "agent", "agent"),
    )
    build = await _build(app)
    assert build["attempts"]["wake"] == 0 and build["due_at"] is None and build["wake"]["n"] == 1

    assert "in a minute" in await _fail(run="r2")
    await _at_due(world, app)
    assert len(world.lines) == 2  # a room wake again, not swallowed by wake 1's receipt
    assert world.lines[1]["dispatch_targets"]


async def test_a_completed_turn_with_a_person_next_arms_nothing_and_settles_a_pending_wake(world):
    app = await _app(preview={"deployment_id": "d"})
    world.rows = [
        {
            "app_id": app["app_id"],
            "provider_name": "Vercel",
            "service_ref": "project",
            "environment": e,
            "state": "active",
        }
        for e in ("prod", "dev")
    ] + [
        {
            "app_id": app["app_id"],
            "provider_name": "Neon",
            "service_ref": "postgres",
            "environment": "prod",
            "state": "active",
        }
    ]
    await apps_run.on_run_ended(OWNER, _turn(outcome="completed"))
    assert not await _build(app)  # the preview is up: the person's move
    await _fail()
    await apps_run.on_run_ended(OWNER, _turn(run="r2", outcome="completed"))
    assert (await _build(app))["due_at"] is None
    world.now += 3600
    await apps_sweeper.sweep_once()
    assert not world.lines


async def test_waiting_on_approvals_arms_nothing(world):
    app = await _app()
    world.rows = [
        {
            "app_id": app["app_id"],
            "provider_name": "Vercel",
            "service_ref": "project",
            "environment": "prod",
            "state": "awaiting_approval",
        }
    ]
    await apps_run.on_run_ended(OWNER, _turn(outcome="completed"))
    assert await _fail() is None
    assert not await _build(app)


async def test_a_running_issue_is_not_woken_over(world):
    app = await _app()
    await _fail()
    await _at_due(world, app)
    await _fail(run="r2")
    await _at_due(world, app)
    assert len(world.issues) == 1
    world.status = "in_progress"
    await _at_due(world, app)
    assert len(world.issues) == 1 and int((await _build(app))["due_at"]) == world.now + apps_run.HOLD_S


async def test_a_wake_that_cannot_land_spends_its_attempt(world, monkeypatch):
    app = await _app()
    world.status = "blocked"
    monkeypatch.setattr(apps_run, "resolve_company_id_for_owner", AsyncMock(side_effect=RuntimeError("no company")))
    await _fail()
    await _at_due(world, app)  # room
    await _at_due(world, app)  # issue: fails
    await _at_due(world, app)  # issue: fails
    await _at_due(world, app)  # stuck
    assert len(world.lines) == 2 and "needs a person" in world.lines[1]["text"]


async def test_the_flag_off_is_todays_behaviour(world, monkeypatch):
    app = await _app()
    monkeypatch.setattr(settings, "APPS_BUILD_WAKE", False)
    assert await _fail() is None
    await apps_run.on_run_ended(OWNER, _turn(outcome="completed"))
    assert not await _build(app)
    await apps_repo.patch(app["app_id"], {"build": {"due_at": 1}})
    await apps_sweeper.sweep_once()
    assert not world.lines


# --- the wording, through post_run_failure_message -------------------------------------------------


@pytest.fixture
def note(world, monkeypatch):
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "CEO"}]))
    monkeypatch.setattr(messaging, "count_first_turn_failure", AsyncMock())
    monkeypatch.setattr("core.services.chat_gate.budget_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging.billing_repo, "get_by_owner_id", AsyncMock(return_value={}))
    return create


async def test_a_builders_failed_turn_reads_as_being_resumed(note):
    await _app()
    await messaging.post_run_failure_message(OWNER, _turn(), "error", "model_unavailable")
    kw = note.await_args.kwargs
    assert kw["text"] == "CEO lost its place on Crew. Trying again in a minute."
    assert kw["author"]["kind"] == "system" and kw["dispatch_targets"] == []


async def test_any_other_turn_keeps_todays_text(note):
    await _app()
    await messaging.post_run_failure_message(OWNER, _turn(agentId="other"), "error", "model_unavailable")
    assert note.await_args.kwargs["text"].endswith("Resend in a bit.")


async def test_a_hook_that_breaks_leaves_todays_note(note, monkeypatch):
    await _app()
    monkeypatch.setattr(apps_repo, "list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down")))
    await messaging.post_run_failure_message(OWNER, _turn(), "error", "model_unavailable")
    assert note.await_args.kwargs["text"].endswith("Resend in a bit.")


async def test_the_owners_own_provider_running_out_is_not_resumed(note):
    app = await _app()
    await messaging.post_run_failure_message(OWNER, _turn(), "error", messaging.PROVIDER_QUOTA_EXHAUSTED)
    assert not await _build(app)
