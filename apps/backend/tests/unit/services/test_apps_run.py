"""A builder agent's dead turn resumes with no person acting (core/services/apps_run.py)."""

import asyncio
import gzip
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
            room={"roomId": ROOM},
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
        monkeypatch.setattr(apps_run.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: w.room))
        monkeypatch.setattr(apps_service.wf_repo, "list_for_owner", AsyncMock(side_effect=lambda o: w.rows))
        monkeypatch.setattr(apps_run.billing_repo, "get_by_owner_id", AsyncMock(return_value={}))
        monkeypatch.setattr(apps_run, "budget_block_reason", AsyncMock(side_effect=lambda o, a: w.reason))
        monkeypatch.setattr(apps_run, "shared_admin_client", lambda: admin)
        monkeypatch.setattr(apps_run, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
        monkeypatch.setattr(apps_run, "resolve_company_id_for_owner", AsyncMock(return_value="co_1"))
        monkeypatch.setattr(settings, "APPS_BUILD_WAKE", True)
        yield w


async def _app(**fields) -> dict:
    """An agent-built app whose builder uploaded a real source just now, unless a test says otherwise."""
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
    fields.setdefault("build", {"source_at": apps_run._now(), "builder": AGENT})
    return await apps_repo.patch(row["app_id"], fields)


def _active(app: dict) -> list[dict]:
    """Every resource provisioned, so the app can be ready."""
    base = {"app_id": app["app_id"], "state": "active"}
    return [
        {**base, "provider_name": "Vercel", "service_ref": "project", "environment": "prod"},
        {**base, "provider_name": "Vercel", "service_ref": "project", "environment": "dev"},
        {**base, "provider_name": "Neon", "service_ref": "postgres", "environment": "prod"},
    ]


def _turn(run="r1", **extra) -> dict:
    return {"runId": run, "roomId": ROOM, "agentId": AGENT, "eventKind": "user_request", "outcome": "error", **extra}


async def _fail(**kw) -> str | None:
    return await apps_run.on_turn_failed(OWNER, _turn(**kw), budget_reason=None, agent_name="CEO")


async def _build(app: dict) -> dict:
    return (await apps_repo.get(app["app_id"], consistent_read=True)).get("build") or {}


async def _at_due(world, app: dict) -> None:
    world.now = int((await _build(app))["due_at"])
    await apps_sweeper.sweep_once()


def _tgz(body: str, *, stamp: int = 0, file_mtime: int = 0) -> bytes:
    """One file; ``stamp`` is the gzip header's time, as bsdtar writes it."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        info = tarfile.TarInfo("a.txt")
        info.size, info.mtime = len(body), file_mtime
        tf.addfile(info, io.BytesIO(body.encode()))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=stamp) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


async def _upload(monkeypatch, tree: str, *, agent: str = "agent", stamp: int = 0) -> None:
    """``tree`` names what the archive holds: the same name is the same tree."""
    monkeypatch.setattr(apps_service, "_check_room", AsyncMock())
    monkeypatch.setattr(apps_service.apps_source, "put", AsyncMock(return_value=f"sid_{tree}_{stamp}"))
    await apps_service.put_source(
        OWNER,
        name="crew",
        room_id=ROOM,
        commit="c",
        archive=_tgz(tree, stamp=stamp),
        actor=apps_service.BuilderCaller(OWNER, agent, "agent"),
    )


def _two_agents(world) -> dict:
    other = {"kind": "agent", "id": "agt_0", "displayName": "Ada", "listenMode": "mentions"}
    world.members = [other, CEO]  # agt_0 sorts first: any guess would pick it
    return other


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
    "build,actor,members,running,fallback,strict",
    [
        ({"builder": "b"}, "agent", ["a", "b"], [], "b", "b"),  # the record first
        ({"builder": "b"}, "a", ["a", "b"], [], "b", "b"),  # even over a real actor
        ({"builder": "gone"}, "b", ["a", "b"], [], "b", "b"),  # the record left: the actor
        ({}, "b", ["a", "b"], [], "b", "b"),
        ({}, "agent", ["a"], [], "a", None),  # below here only a line that must reach someone guesses
        ({}, "agent", ["a", "b"], ["b"], "b", None),
        ({}, "agent", ["a", "b"], ["a", "b"], "a", None),
        ({}, "agent", ["a", "b"], [], "a", None),
        ({}, "agent", [], [], None, None),
    ],
)
async def test_builder_order(world, build, actor, members, running, fallback, strict):
    world.members = [{"kind": "agent", "id": m, "listenMode": "mentions"} for m in members]
    world.members.append({"kind": "agent", "id": "obs", "listenMode": "observe"})
    world.open_runs = [{"agentId": r} for r in running]
    app = {"actor_id": actor, "owner_id": OWNER, "build": build}
    assert ((await apps_room._builder(app, ROOM)) or {}).get("id") == fallback
    assert ((await apps_room._builder(app, ROOM, guess=False)) or {}).get("id") == strict


async def test_ask_agent_to_fix_addresses_the_builder_when_the_actor_is_the_placeholder(world, monkeypatch):
    app = await _app(publish_error={"message": "boom"})
    post = AsyncMock()
    monkeypatch.setattr(apps_service.messaging, "require_room_member", AsyncMock())
    monkeypatch.setattr(apps_service.messaging, "create_human_message", post)
    monkeypatch.setattr(apps_service, "_scrubber", AsyncMock(return_value=lambda s: s))
    await apps_service.ask_agent_to_fix(OWNER, app["app_id"], auth=AuthContext(user_id="u", org_id=OWNER))
    assert [m.id for m in post.await_args.args[2].mentions] == [AGENT]


# --- recording the builder at the upload ---------------------------------------------------------


@pytest.mark.parametrize(
    "agent,running,expect",
    [
        ("agent", [AGENT], AGENT),  # the placeholder: the open run in the room names it
        ("agent", [AGENT, AGENT], AGENT),  # two runs of one agent still name it
        ("agent", ["agt_0", AGENT], None),  # two agents: no guess
        ("agent", [], None),
        ("agt_header", [AGENT], "agt_header"),  # a real header wins
    ],
)
async def test_the_upload_records_the_real_builder(world, monkeypatch, agent, running, expect):
    app = await _app(build={})
    world.open_runs = [{"agentId": r} for r in running]
    await _upload(monkeypatch, "src_1", agent=agent)
    assert (await _build(app))["builder"] == expect


async def test_a_later_upload_it_cannot_name_keeps_the_record(world, monkeypatch):
    app = await _app()
    await _upload(monkeypatch, "src_2")
    assert (await _build(app))["builder"] == AGENT


async def test_two_agents_the_recorded_builder_is_woken_and_the_other_is_not(world, monkeypatch):
    other = _two_agents(world)
    app = await _app(build={})
    world.open_runs = [{"agentId": AGENT}]
    await _upload(monkeypatch, "src_1")
    world.open_runs = []
    assert await _fail(agentId=other["id"]) is None  # the other agent's dead chat: no wake
    assert (await _build(app)).get("due_at") is None
    assert await _fail() is not None
    await _at_due(world, app)
    assert world.lines[0]["dispatch_targets"] == [{"agentId": AGENT, "eventKind": "user_request"}]


async def test_two_agents_and_no_record_gives_no_wake_but_the_approval_line_still_reaches_someone(world, monkeypatch):
    _two_agents(world)
    app = await _app(build={"source_at": apps_run._now()})
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None

    post = AsyncMock()
    monkeypatch.setattr(apps_room.messaging, "require_room_member", AsyncMock())
    monkeypatch.setattr(apps_room.messaging, "create_human_message", post)
    await apps_room.post_approval_line(
        owner_id=OWNER,
        user_id="user_1",
        workflow={"app_id": app["app_id"], "workflow_id": "w1", "provider_name": "vercel", "environment": "prod"},
    )
    assert [m.id for m in post.await_args.args[2].mentions] == ["agt_0"]


async def test_one_agent_and_no_record_gives_no_wake(world):
    """No guess even when only one agent could have built it: nothing named it at the upload."""
    app = await _app(build={"source_at": apps_run._now()})
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_due_wake_with_the_builder_gone_wakes_nobody(world):
    app = await _app()
    await _fail()
    build = await _build(app)
    await apps_repo.patch(app["app_id"], {"build": {**build, "builder": None}})
    await _at_due(world, app)
    assert [line["dispatch_targets"] for line in world.lines] == [[]]
    assert "needs a person" in world.lines[0]["text"] and not world.issues


async def test_a_new_source_after_a_crashed_wake_takes_the_next_key(world, monkeypatch):
    app = await _app()
    await _fail()
    real = apps_repo.patch
    calls = {"n": 0}

    async def dies_after_the_post(app_id, fields, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise asyncio.CancelledError
        return await real(app_id, fields, **kw)

    monkeypatch.setattr(apps_repo, "patch", dies_after_the_post)
    world.now = int((await _build(app))["due_at"])
    with pytest.raises(asyncio.CancelledError):
        await apps_run.advance(app)
    monkeypatch.setattr(apps_repo, "patch", real)
    world.now += 10
    await _upload(monkeypatch, "src_2")
    await _fail(run="r2")
    await _at_due(world, app)
    assert len(world.lines) == 2  # not swallowed by the crashed wake's receipt


async def test_a_turn_hook_holding_a_copy_from_before_a_new_source_loses(world, monkeypatch):
    app = await _app()
    stale = await apps_repo.get(app["app_id"], consistent_read=True)
    world.now += 10
    await _upload(monkeypatch, "src_2")
    assert await apps_run._write(apps_run.plain(stale), due_at=world.now) is None
    assert (await _build(app))["due_at"] is None


async def test_stop_on_the_recorded_builders_run_cancels_with_two_agents(world):
    _two_agents(world)
    app = await _app()
    await _fail()
    await apps_run.on_run_ended(OWNER, _turn(run="r2", outcome="aborted"))
    assert (await _build(app))["due_at"] is None


async def test_the_upload_cannot_overwrite_a_claim_that_landed_since_its_read(world, monkeypatch):
    app = await _app()
    await _fail()
    stale = await apps_repo.get(app["app_id"], consistent_read=True)
    world.now = int((await _build(app))["due_at"])
    await apps_run.advance(app)  # the sweeper claims and sends wake 1
    real, reads = apps_repo.get, []

    async def stale_first(app_id, **kw):
        reads.append(app_id)
        return stale if len(reads) == 1 else await real(app_id, **kw)

    monkeypatch.setattr(apps_repo, "get", stale_first)
    await apps_run.record_upload(app, AGENT, "agent", files={"a.txt": b"new"})
    monkeypatch.setattr(apps_repo, "get", real)
    build = await _build(app)
    assert len(reads) == 2  # the stale write lost and it read again
    assert build["wake"]["n"] == 1 and build["attempts"]["wake"] == 0


# --- only a real build's errored turn counts ------------------------------------------------------


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


@pytest.mark.parametrize("turn", [{"agentId": "someone_else"}, {"roomId": "other_room"}, {"eventKind": "room_event"}])
async def test_a_turn_that_is_not_the_builders_gives_no_wake(world, turn):
    app = await _app()
    assert await _fail(**turn) is None
    assert (await _build(app)).get("due_at") is None


async def test_an_unrelated_failed_question_in_an_apps_room_gets_todays_note(world, monkeypatch):
    """The builder, the app's room, no source change for hours; other writes keep bumping updated_at."""
    app = await _app()
    world.now += apps_run.BUILDING_S + 60
    monkeypatch.setattr(apps_repo, "utc_now_iso", lambda: datetime.fromtimestamp(world.now, UTC).isoformat())
    await apps_repo.patch(app["app_id"], {"last_opened_at": "x"})  # updated_at is now: it must not count
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_an_app_with_no_recorded_source_change_gives_no_wake(world):
    app = await _app(build={"builder": AGENT})
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_rolled_back_live_app_gives_no_wake(world):
    """Rollback clears live_source_id; a wake could rebuild over the owner's rollback."""
    app = await _app(
        state="live",
        live_version=1,
        live_source_id=None,
        rolled_back=True,
        sources={"src_1": {"at": "2026-10-30T00:00:00Z"}},
        preview={"deployment_id": "d"},
    )
    world.rows = _active(app)
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_rebuilt_put_back_serving_an_older_source_gives_no_wake(world):
    """The rebuilding rollback path sets no rolled_back flag: its release is newer than any source."""
    app = await _app(
        state="live",
        live_version=2,
        live_source_id="src_1",
        sources={"src_1": {"at": "2026-10-30T00:00:00Z"}, "src_2": {"at": "2026-10-30T01:00:00Z"}},
        versions=[{"n": 2, "source_id": "src_1", "at": "2026-10-30T02:00:00Z", "live": True}],
        preview={"deployment_id": "d"},
    )
    world.rows = _active(app)
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_wake_already_due_when_the_owner_rolls_back_is_dropped(world):
    app = await _app()
    await _fail()
    await apps_repo.patch(app["app_id"], {"rolled_back": True})
    await _at_due(world, app)
    assert not world.lines and (await _build(app))["due_at"] is None


async def test_an_app_the_builder_asked_to_remove_gives_no_wake(world):
    app = await _app(destroy_requested_by="agent")
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_provisioning_failure_is_the_owners_move(world):
    app = await _app()
    world.rows = [{**_active(app)[0], "state": "failed"}]
    assert apps_service._state(app, await apps_service._rows(app))[0] == "failed"
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_refused_credential_is_the_owners_move(world):
    app = await _app(publish_error={"code": apps_service._REFUSAL_CODE, "message": "m", "next": "n"})
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_failed_build_is_still_the_agents_move(world):
    app = await _app(error={"code": "build_failed", "message": "tsc", "next": "fix"})
    assert apps_service._state(app, await apps_service._rows(app))[0] == "failed"
    assert await _fail() is not None


async def test_waiting_on_approvals_gives_no_wake(world):
    app = await _app()
    world.rows = [{**_active(app)[0], "state": "awaiting_approval"}]
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_chat_failure_in_a_finished_apps_room_gives_no_wake(world):
    app = await _app(
        state="live",
        live_version=1,
        live_source_id="src_1",
        sources={"src_1": {"at": "2026-10-30T00:00:00Z"}},
        preview={"deployment_id": "d"},
    )
    world.rows = _active(app)
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_live_app_with_newer_source_is_being_built(world):
    app = await _app(
        state="live",
        live_version=1,
        live_source_id="src_1",
        sources={"src_1": {"at": "2026-10-30T00:00:00Z"}, "src_2": {"at": "2026-10-30T01:00:00Z"}},
        preview={"deployment_id": "d"},
    )
    world.rows = _active(app)
    assert await _fail() is not None


async def test_a_turn_that_dies_during_a_deployment_resumes_after_it(world):
    app = await _app(preview_pending={"deployment_id": "d"})
    assert await _fail() is not None
    await _at_due(world, app)
    assert not world.lines  # the server is working
    await apps_repo.patch(app["app_id"], {"preview_pending": None})
    await _at_due(world, app)
    assert len(world.lines) == 1


async def test_a_first_preview_waiting_for_a_person_gives_no_wake(world):
    app = await _app(preview={"deployment_id": "d"})
    world.rows = _active(app)
    assert apps_service._state(app, await apps_service._rows(app))[0] == "ready"
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_wake_due_when_the_first_preview_lands_is_dropped(world):
    app = await _app()
    await _fail()
    await apps_repo.patch(app["app_id"], {"preview": {"deployment_id": "d"}})
    world.rows = _active(app)
    await _at_due(world, app)
    assert not world.lines and (await _build(app))["due_at"] is None


@pytest.mark.parametrize("reason", [None, "included_daily_cap"])
async def test_a_schedule_that_did_not_land_keeps_todays_note(world, monkeypatch, reason):
    """The compare-and-set lost: saying "Trying again" would be a promise nothing holds."""
    await _app()
    monkeypatch.setattr(apps_repo, "patch", AsyncMock(return_value=None))
    assert await apps_run.on_turn_failed(OWNER, _turn(), budget_reason=reason, agent_name="CEO") is None


async def test_a_person_built_app_gives_no_wake(world):
    app = await _app(actor_kind="person")
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_an_archived_room_gets_no_wake_and_no_issue(world):
    app = await _app()
    await _fail()
    world.room = {"roomId": ROOM, "archivedAt": "2026-10-30T00:00:00Z"}
    await _at_due(world, app)
    assert not world.lines and not world.issues and world.issue_calls == 0
    assert (await _build(app))["due_at"] is None


async def test_a_room_archived_as_the_wake_posts_stops_before_any_issue(world, monkeypatch):
    app = await _app()
    await _fail()
    monkeypatch.setattr(
        apps_run.messaging_repo, "create_message", AsyncMock(side_effect=apps_run.messaging_repo.RoomArchived(ROOM))
    )
    await _at_due(world, app)  # the room wake is refused
    world.room = {"roomId": ROOM, "archivedAt": "2026-10-30T00:00:00Z"}
    await _at_due(world, app)
    assert world.issue_calls == 0 and (await _build(app))["due_at"] is None


# --- a completed turn arms nothing ----------------------------------------------------------------


async def test_a_completed_turn_arms_nothing(world):
    """ "Tell me when you've decided": no timer, no wakes, no stuck line."""
    app = await _app()
    await apps_run.on_run_ended(OWNER, _turn(outcome="completed"))
    assert (await _build(app)).get("due_at") is None
    world.now += 3 * 3600
    await apps_sweeper.sweep_once()
    assert not world.lines and not world.issues


async def test_a_completed_builder_turn_settles_a_pending_wake(world):
    app = await _app()
    await _fail()
    await apps_run.on_run_ended(OWNER, _turn(run="r2", outcome="completed"))
    assert (await _build(app))["due_at"] is None
    world.now += 3600
    await apps_sweeper.sweep_once()
    assert not world.lines


async def test_an_ambient_completed_turn_settles_nothing(world):
    app = await _app()
    await _fail()
    await apps_run.on_run_ended(OWNER, _turn(run="r2", outcome="completed", eventKind="room_event"))
    assert (await _build(app))["due_at"] is not None


# --- the send ---------------------------------------------------------------------------------------


async def test_two_sweepers_give_one_message(world, monkeypatch):
    """Both read the row before either wrote, in a fixed order: the compare-and-set admits one. Not
    asyncio.gather: moto's conditional update is not atomic across threads, DynamoDB's is."""
    real_get = apps_repo.get
    app = await _app()
    await _fail()
    world.now = int((await _build(app))["due_at"])
    before = await apps_repo.get(app["app_id"], consistent_read=True)
    monkeypatch.setattr(apps_repo, "get", AsyncMock(return_value=before))
    await apps_run.advance(app)
    await apps_run.advance(app)
    assert len(world.lines) == 1
    monkeypatch.setattr(apps_repo, "get", real_get)
    assert (await _build(app))["attempts"]["wake"] == 1  # the loser spent nothing

    world.status = "blocked"
    await _fail()
    world.now = int((await _build(app))["due_at"])
    before = await apps_repo.get(app["app_id"], consistent_read=True)
    monkeypatch.setattr(apps_repo, "get", AsyncMock(return_value=before))
    await apps_run.advance(app)
    await apps_run.advance(app)
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
    assert (await _build(app))["due_at"] is None  # stopped: not rescheduled, only the receipt would hide it
    await apps_sweeper.sweep_once()
    assert len(world.lines) == 2  # one line, ever


async def test_a_running_issue_is_not_woken_over(world):
    app = await _app()
    await _fail()
    await _at_due(world, app)
    await _fail(run="r2")
    await _at_due(world, app)
    assert len(world.issues) == 1
    await _at_due(world, app)
    assert len(world.issues) == 1 and int((await _build(app))["due_at"]) == world.now + apps_run.HOLD_S


async def _to_issue(world, app) -> None:
    await _fail()
    await _at_due(world, app)
    await _fail(run="r2")
    await _at_due(world, app)
    assert len(world.issues) == 1


async def test_a_finished_issue_ends_the_wakes(world):
    app = await _app()
    await _to_issue(world, app)
    world.status = "done"
    await _at_due(world, app)
    assert (await _build(app))["due_at"] is None and len(world.issues) == 1


async def test_a_cancelled_issue_stops_further_wakes_until_a_new_source(world, monkeypatch):
    app = await _app()
    await _to_issue(world, app)
    world.status = "cancelled"
    await _at_due(world, app)
    assert (await _build(app))["due_at"] is None and len(world.issues) == 1
    assert await _fail(run="r3") is None
    assert (await _build(app))["due_at"] is None
    await _upload(monkeypatch, "src_new")
    assert await _fail(run="r4") is not None


async def test_a_cancelled_issue_past_the_hold_still_stops(world):
    app = await _app()
    await _to_issue(world, app)
    world.status = "cancelled"
    build = await _build(app)
    await apps_repo.patch(app["app_id"], {"build": {**build, "wake": {**build["wake"], "at": 1}}})
    await _at_due(world, app)
    assert len(world.issues) == 1 and (await _build(app))["stopped"] is True


async def test_an_issue_stuck_open_past_the_hold_counts_as_dead(world):
    app = await _app()
    await _to_issue(world, app)
    build = await _build(app)
    await apps_repo.patch(app["app_id"], {"build": {**build, "wake": {**build["wake"], "at": 1}}})
    await _at_due(world, app)
    assert len(world.issues) == 2


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


@pytest.mark.parametrize("stop", ["aborted_run", "abort_requested"])
async def test_a_stop_never_retries(world, stop):
    app = await _app()
    if stop == "aborted_run":
        await _fail()
        await apps_run.on_run_ended(OWNER, _turn(run="r2", outcome="aborted"))
        assert (await _build(app))["due_at"] is None
    else:
        assert await _fail(abortRequestedAt="2026-10-30T00:00:00Z") is None
        assert (await _build(app)).get("due_at") is None
    world.now += 3 * 3600
    await apps_sweeper.sweep_once()
    assert not world.lines and not world.issues


# --- the count: uploads and the daily ceiling ------------------------------------------------------


async def test_an_identical_upload_resets_nothing(world, monkeypatch):
    app = await _app()
    await _upload(monkeypatch, "src_same")
    source_at = (await _build(app))["source_at"]
    await _fail()
    await _at_due(world, app)
    assert (await _build(app))["attempts"]["wake"] == 1
    await _fail(run="r2")
    due = (await _build(app))["due_at"]
    await _upload(monkeypatch, "src_same")
    build = await _build(app)
    assert build["attempts"]["wake"] == 1 and build["due_at"] == due and build["source_at"] == source_at


async def test_identical_uploads_do_not_keep_an_old_build_looking_recent(world, monkeypatch):
    app = await _app()
    await _upload(monkeypatch, "src_same")
    world.now += apps_run.BUILDING_S + 60
    await _upload(monkeypatch, "src_same")
    assert await _fail() is None
    assert (await _build(app)).get("due_at") is None


async def test_a_dead_turn_after_every_identical_upload_reaches_the_cap(world, monkeypatch):
    app = await _app()
    world.status = "blocked"
    await _upload(monkeypatch, "src_same")
    for i in range(10):
        await _fail(run=f"r{i}")
        if (await _build(app)).get("due_at") is not None:
            await _at_due(world, app)
        await _upload(monkeypatch, "src_same")
    assert sum(1 for line in world.lines if line["dispatch_targets"]) + len(world.issues) == apps_run.MAX_WAKES
    assert sum("needs a person" in line["text"] for line in world.lines) == 1


def test_two_gzips_of_the_same_tar_hash_the_same_and_a_changed_tree_does_not():
    first, second = _tgz("x", stamp=1000), _tgz("x", stamp=2000)
    assert first != second  # source_id hashes these bytes, so it differs
    tree = lambda data: dict(apps_service.apps_source.iter_files(data))  # noqa: E731
    content_hash = apps_service.apps_source.content_hash
    assert content_hash(tree(first)) == content_hash(tree(second))
    assert content_hash(tree(_tgz("x", file_mtime=5))) == content_hash(tree(first))  # a touch is no change
    assert content_hash(tree(_tgz("y"))) != content_hash(tree(first))
    assert content_hash({"ab": b"c"}) != content_hash({"a": b"bc"})
    assert content_hash({"a": b"x"}) != content_hash({"b": b"x"})  # a rename is a change


async def test_an_up_that_repacks_the_same_tree_resets_nothing(world, monkeypatch):
    """bsdtar on macOS: same tree, new gzip time, new source_id."""
    app = await _app()
    await _upload(monkeypatch, "tree", stamp=1000)
    source_at = (await _build(app))["source_at"]
    await _fail()
    await _at_due(world, app)
    assert (await _build(app))["attempts"]["wake"] == 1
    world.now += 10
    await _upload(monkeypatch, "tree", stamp=2000)
    assert set((await apps_repo.get(app["app_id"]))["sources"]) >= {"sid_tree_1000", "sid_tree_2000"}
    build = await _build(app)
    assert build["attempts"]["wake"] == 1 and build["source_at"] == source_at


async def test_a_new_source_resets_the_count_and_the_next_wake_gets_its_own_key(world, monkeypatch):
    app = await _app()
    await _fail()
    await _at_due(world, app)
    assert (await _build(app))["attempts"]["wake"] == 1
    world.now += 10
    await _upload(monkeypatch, "src_2")
    build = await _build(app)
    assert build["attempts"]["wake"] == 0 and build["due_at"] is None and build["wake"]["n"] == 1
    assert build["source_at"] == world.now

    assert "in a minute" in await _fail(run="r2")
    await _at_due(world, app)
    assert len(world.lines) == 2  # a room wake again, not swallowed by wake 1's receipt
    assert world.lines[1]["dispatch_targets"]


async def test_the_daily_ceiling_holds_when_every_upload_has_new_bytes(world, monkeypatch):
    """A source_id that changes every time, on a paid or own-key plan with no allowance to run out."""
    app = await _app()
    world.status = "blocked"
    world.now = int(datetime(2026, 10, 30, 1, tzinfo=UTC).timestamp())
    monkeypatch.setattr(apps_repo, "utc_now_iso", lambda: datetime.fromtimestamp(world.now, UTC).isoformat())
    for i in range(10):
        await _upload(monkeypatch, f"src_{i}")
        await _fail(run=f"r{i}")
        if (await _build(app)).get("due_at") is not None:
            await _at_due(world, app)
    assert sum(1 for line in world.lines if line["dispatch_targets"]) + len(world.issues) == apps_run.DAILY_WAKE_CEILING


async def test_the_daily_ceiling_starts_over_on_a_new_utc_day(world):
    app = await _app()
    build = await _build(app)
    day = {"date": apps_run._date(world.now), "wakes": apps_run.DAILY_WAKE_CEILING}
    await apps_repo.patch(app["app_id"], {"build": {**build, "day": day}})
    assert await _fail() is None
    world.now = int(datetime.fromtimestamp(world.now, UTC).replace(hour=0, minute=0).timestamp()) + 86400
    build = await _build(app)
    await apps_repo.patch(app["app_id"], {"build": {**build, "source_at": world.now}})
    assert await _fail() is not None


# --- the allowance ---------------------------------------------------------------------------------


async def _capped_app(world, monkeypatch, at: datetime) -> dict:
    world.now = int(at.timestamp())
    monkeypatch.setattr(apps_repo, "utc_now_iso", lambda: datetime.fromtimestamp(world.now, UTC).isoformat())
    return await _app()


async def test_a_spent_allowance_waits_for_midnight_with_jitter_and_spends_no_wake(world, monkeypatch):
    app = await _capped_app(world, monkeypatch, datetime(2026, 10, 30, 16, 34, tzinfo=UTC))
    midnight = int(datetime(2026, 10, 31, tzinfo=UTC).timestamp())
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


async def test_only_one_wake_after_the_reset_per_app(world, monkeypatch):
    """A second would spend the new day's allowance on a turn that keeps dying."""
    app = await _capped_app(world, monkeypatch, datetime(2026, 10, 30, 20, tzinfo=UTC))
    await apps_run.on_turn_failed(OWNER, _turn(), budget_reason="included_daily_cap", agent_name="CEO")
    await _at_due(world, app)
    assert len(world.lines) == 1
    assert await _fail(run="r2") is None
    await _at_due(world, app)  # wake 1's own next check
    assert not world.issues and sum(bool(line["dispatch_targets"]) for line in world.lines) == 1
    assert "needs a person" in world.lines[-1]["text"]


async def test_a_capped_owners_failed_chat_arms_no_midnight_wake(world, monkeypatch):
    """Not mid-build: the owner keeps today's budget note, and tomorrow's allowance stays unspent."""
    app = await _capped_app(world, monkeypatch, datetime(2026, 10, 30, 8, tzinfo=UTC))
    world.now += apps_run.BUILDING_S + 60
    line = await apps_run.on_turn_failed(OWNER, _turn(), budget_reason="included_daily_cap", agent_name="CEO")
    assert line is None
    assert (await _build(app)).get("due_at") is None


async def test_a_cap_found_at_wake_time_posts_once_and_waits(world):
    """The gate stamps the cap after the refusal, so the note can miss it and the sweeper catches it."""
    app = await _app()
    await _fail()
    world.reason = "trial_daily_cap"
    await _at_due(world, app)
    assert [line["dispatch_targets"] for line in world.lines] == [[]]
    assert "continues tomorrow" in world.lines[0]["text"]
    build = await _build(app)
    assert build["due_at"] > world.now and not build.get("attempts") and build["resume"] is True


async def test_an_owner_who_stays_capped_gets_one_line_not_one_a_day(world):
    world.now = world.now // 86400 * 86400 + 12 * 3600  # noon UTC: the next midnight is past BUILDING_S
    app = await _app()
    await _fail()
    world.reason = "included_daily_cap"
    await _at_due(world, app)
    assert len(world.lines) == 1
    await _at_due(world, app)  # next midnight, still capped, the source long stale
    assert len(world.lines) == 1 and (await _build(app))["due_at"] is None


async def test_an_allowance_that_does_not_come_back_gives_no_wake(world):
    app = await _app()
    assert await apps_run.on_turn_failed(OWNER, _turn(), budget_reason="out_of_credits", agent_name="CEO") is None
    assert (await _build(app)).get("due_at") is None


# --- the flag ----------------------------------------------------------------------------------------


def test_the_flag_defaults_off():
    assert type(settings).model_fields["APPS_BUILD_WAKE"].default is False


async def test_the_flag_off_is_todays_behaviour(world, monkeypatch):
    app = await _app()
    monkeypatch.setattr(settings, "APPS_BUILD_WAKE", False)
    assert await _fail() is None
    await _upload(monkeypatch, "src_2")
    assert (await _build(app)).get("due_at") is None and (await _build(app))["source_at"] == world.now
    await apps_run.on_run_ended(OWNER, _turn(outcome="completed"))
    await apps_repo.patch(app["app_id"], {"build": {"due_at": 1}})
    await apps_sweeper.sweep_once()
    assert not world.lines


async def test_the_flag_off_records_no_upload(world, monkeypatch):
    app = await _app(build={})
    world.now += 100
    monkeypatch.setattr(settings, "APPS_BUILD_WAKE", False)
    await _upload(monkeypatch, "src_2")
    assert not await _build(app)


async def test_a_wake_left_over_from_a_flag_off_spell_is_dropped(world):
    app = await _app()
    await _fail()
    world.now = int((await _build(app))["due_at"]) + apps_run.BUILDING_S + 60
    await apps_sweeper.sweep_once()
    assert not world.lines and (await _build(app))["due_at"] is None


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


async def test_an_unrelated_question_through_the_note_keeps_todays_text(note, world):
    await _app()
    world.now += apps_run.BUILDING_S + 60
    await messaging.post_run_failure_message(OWNER, _turn(), "error", "model_unavailable")
    assert note.await_args.kwargs["text"].endswith("Resend in a bit.")


async def test_a_hook_that_breaks_leaves_todays_note(note, monkeypatch):
    await _app()
    monkeypatch.setattr(apps_repo, "list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down")))
    await messaging.post_run_failure_message(OWNER, _turn(), "error", "model_unavailable")
    assert note.await_args.kwargs["text"].endswith("Resend in a bit.")


async def test_the_owners_own_provider_running_out_is_not_resumed(note):
    app = await _app()
    await messaging.post_run_failure_message(OWNER, _turn(), "error", messaging.PROVIDER_QUOTA_EXHAUSTED)
    assert (await _build(app)).get("due_at") is None


# --- an app with its own conversation (Task 1 review) ---------------------------------------------


async def test_only_turns_in_the_room_an_app_is_built_in_are_its_build_turns(world):
    """A started app keeps its origin room: its builder's other work there is not this build."""
    app = await _app(conversation_id="conv_1")
    assert await _fail() is None  # a turn in the origin room
    assert (await _build(app)).get("due_at") is None
    assert await _fail(roomId="conv_1") == "CEO lost its place on Crew. Trying again in a minute."


async def test_an_open_run_in_the_older_room_holds_the_wake_of_an_app_opened_mid_build(world, monkeypatch):
    app = await _app(conversation_id="conv_1")
    await _fail(roomId="conv_1")
    runs = AsyncMock(side_effect=lambda o, r: world.open_runs if r == ROOM else [])
    monkeypatch.setattr(apps_run.messaging_repo, "list_open_runs_for_room", runs)
    world.open_runs = [{"agentId": AGENT}]
    await _at_due(world, app)
    assert not world.lines
    world.open_runs = []
    await _at_due(world, app)
    assert len(world.lines) == 1 and world.lines[0]["room_id"] == "conv_1"
