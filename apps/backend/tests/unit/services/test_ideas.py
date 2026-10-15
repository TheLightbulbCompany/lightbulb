"""Ideas (spec 2026-10-14 §4.4): the catalog, GET /ideas, Start and dismiss."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.repositories import messaging_repo as repo
from core.services import hermes_profiles, ideas, messaging, task_board
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401  (the moto messaging table)

OWNER = "org_1"
STARTERS = {
    "ceo": "10000000-0000-4000-8000-000000000001",
    "engineering": "10000000-0000-4000-8000-000000000002",
    "finance": "10000000-0000-4000-8000-000000000003",
    "growth": "10000000-0000-4000-8000-000000000004",
    "analytics": "10000000-0000-4000-8000-000000000005",
    "customer_operations": "10000000-0000-4000-8000-000000000006",
}
AUTH = AuthContext(user_id="user_A", org_id=OWNER, org_role="org:member")


@pytest.fixture
def workspace(table, monkeypatch):  # noqa: F811
    """An active workspace with the six starters on the roster; people resolve by id."""
    from core.repositories.paperclip_repo import PaperclipRepo

    row = SimpleNamespace(status="active", starter_agent_ids=STARTERS, onboarding_goal_id=None)
    roster = [{"id": agent_id, "name": key.title()} for key, agent_id in STARTERS.items()]
    monkeypatch.setattr(PaperclipRepo, "resolve_owner_row", AsyncMock(return_value=row))
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))
    monkeypatch.setattr(messaging, "human_display_name", lambda user: asyncio.sleep(0, result=f"Name {user}"))
    monkeypatch.setattr(messaging, "_validate_human_in_owner", AsyncMock())


# ---- the catalog ------------------------------------------------------------------------------


def test_the_catalog_loads_and_every_entry_validates():
    catalog = ideas.catalog()
    assert len(catalog) == 35
    assert sum(i.kind == "agency" for i in catalog.values()) == 20  # the 15 + the next five
    assert sum(i.kind == "everyday" for i in catalog.values()) == 15
    for idea in catalog.values():
        assert idea.owner_role in ideas.ROLES
        assert len(idea.deliverables) == 3
        if idea.agency_price_low is not None:
            assert 0 < idea.agency_price_low <= idea.agency_price_high
            assert idea.price_unit in ("one_time", "month", "state") and idea.agency_turnaround
        for heading in ("## Steps", "## Decisions", "## Done proves", "## Honesty limits"):
            assert heading in idea.guide, (idea.id, heading)


def test_no_idea_promises_a_result_it_cannot_guarantee():
    """Research section C: promise the action, never the result."""
    for idea in ideas.catalog().values():
        card = " ".join([idea.title, idea.why, *idea.deliverables]).lower()
        for word in ("guarantee", "compliant", "audit-ready", "#1", "rank first", "booked", "confirmed", "agreed"):
            assert word not in card, (idea.id, word)


def test_an_invalid_entry_is_refused():
    good = ideas.catalog()["website-rebuild"].model_dump()
    with pytest.raises(ValueError):
        ideas.Idea.model_validate({**good, "agency_price_low": 20000})  # above high
    with pytest.raises(ValueError):
        ideas.Idea.model_validate({**good, "price_unit": None})  # half a price
    with pytest.raises(ValueError):
        ideas.Idea.model_validate({**good, "owner_role": "legal"})
    with pytest.raises(ValueError):
        ideas.Idea.model_validate({**good, "deliverables": ["one", "two"]})
    with pytest.raises(ValueError):
        ideas.Idea.model_validate({**good, "guide": good["guide"].replace("## Honesty", "## Notes")})


# ---- GET /ideas -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_is_agency_first_then_by_title_with_agents_and_no_prices(workspace):
    body = await ideas.list_ideas(AUTH)
    cards = body["ideas"]
    assert len(cards) == 35
    assert [(c["kind"] != "agency", c["title"]) for c in cards] == sorted(
        (c["kind"] != "agency", c["title"]) for c in cards
    )
    assert cards[0]["kind"] == "agency" and cards[-1]["kind"] == "everyday"
    first = cards[0]
    assert set(first) == {"id", "ownerRole", "title", "why", "deliverables", "ourTurnaround", "yourPart", "kind",
                          "businessTypes", "agentId", "agentName"}  # fmt: skip
    # No prices in the app (owner, 2026-10-15): they stay in the YAML for marketing only.
    assert set(body) == {"ideas", "running", "anyStarted", "anyDismissed"}
    assert body["anyStarted"] is False and body["anyDismissed"] is False
    assert first["agentId"] == STARTERS[first["ownerRole"]]


@pytest.mark.asyncio
async def test_dismissed_ideas_and_ideas_without_an_agent_are_left_out(workspace, monkeypatch):
    await ideas.dismiss(AUTH, "website-rebuild")
    roster = [{"id": a, "name": r.title()} for r, a in STARTERS.items() if r != "growth"]
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))
    body = await ideas.list_ideas(AUTH)
    ids = {c["id"] for c in body["ideas"]}
    assert "website-rebuild" not in ids and body["anyDismissed"] is True
    assert not ids & {i.id for i in ideas.catalog().values() if i.owner_role == "growth"}
    assert "books-cleanup" in ids


@pytest.mark.asyncio
async def test_unknown_ids_are_404(workspace):
    for call in (ideas.dismiss(AUTH, "nope"), ideas.start(AUTH, "nope")):
        with pytest.raises(HTTPException) as err:
            await call
        assert err.value.status_code == 404


# ---- Start ------------------------------------------------------------------------------------


@pytest.fixture
def paperclip(workspace, monkeypatch):
    from core.services import paperclip_admin_client

    admin = SimpleNamespace(
        create_goal=AsyncMock(),
        create_project=AsyncMock(side_effect=lambda **_: {"id": f"project-{admin.create_project.await_count}"}),
        get_project=AsyncMock(return_value={"status": "in_progress"}),
        list_projects=AsyncMock(return_value=[]),
        list_issues=AsyncMock(return_value=[]),
    )
    ctx = SimpleNamespace(owner_id=OWNER, company_id="company-1", session_cookie="cookie")
    monkeypatch.setattr(task_board, "_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(paperclip_admin_client, "shared_admin_client", lambda: admin)
    monkeypatch.setattr(hermes_profiles, "write_profile_file", AsyncMock())
    monkeypatch.setattr(ideas, "_approvals_cache", {})
    return admin


async def _lines(room_id):
    messages, _ = await repo.list_messages(room_id, 0, 100)
    return [m for m in messages if m["author"]["kind"] == "system"]


@pytest.mark.asyncio
async def test_start_twice_makes_one_project_one_guide_and_one_line(paperclip):
    first = await ideas.start(AUTH, "books-cleanup")
    second = await ideas.start(AUTH, "books-cleanup")
    assert first == second == {"roomId": first["roomId"], "projectId": "project-1"}

    assert paperclip.create_project.await_count == 1
    body = paperclip.create_project.await_args.kwargs["body"]
    assert (body["name"], body["leadAgentId"]) == (ideas.catalog()["books-cleanup"].title, STARTERS["finance"])
    assert body["idempotencyKey"].startswith("lightbulb-idea-project:v1:") and len(body["idempotencyKey"]) <= 255
    # Never a company-level goal: an idea cannot replace the company's default goal.
    paperclip.create_goal.assert_not_awaited()
    assert "goalIds" not in body
    hermes_profiles.write_profile_file.assert_awaited_once_with(
        OWNER, STARTERS["finance"], "ideas/books-cleanup.md", ideas.catalog()["books-cleanup"].guide
    )
    lines = await _lines(first["roomId"])
    assert len(lines) == 1
    assert lines[0]["text"].startswith("Name user_A started **Your books set up")
    assert "ideas/books-cleanup.md" in lines[0]["text"] and "references/ideas.md" in lines[0]["text"]
    # This start's own project and DM: two starts of one idea with one agent stay apart.
    assert f"Project `{first['projectId']}`, room `{first['roomId']}`." in lines[0]["text"]
    assert lines[0]["dispatchTargets"] == [{"agentId": STARTERS["finance"], "eventKind": "user_request"}]
    # The line sits in the starter's DM with the owner agent.
    assert await repo.get_member(first["roomId"], "agent", STARTERS["finance"]) is not None
    assert (await repo.get_room(first["roomId"]))["kind"] == "dm"


@pytest.mark.asyncio
async def test_a_finished_project_lets_the_idea_start_again(paperclip):
    first = await ideas.start(AUTH, "books-cleanup")
    paperclip.get_project.return_value = {"status": "completed"}
    again = await ideas.start(AUTH, "books-cleanup")
    assert again["projectId"] == "project-2" != first["projectId"]
    assert again["roomId"] == first["roomId"]
    assert len(await _lines(first["roomId"])) == 2


@pytest.mark.asyncio
async def test_a_failed_start_releases_the_claim_and_reuses_its_project(paperclip):
    hermes_profiles.write_profile_file.side_effect = [RuntimeError("s3 down"), None]
    with pytest.raises(RuntimeError):
        await ideas.start(AUTH, "books-cleanup")
    result = await ideas.start(AUTH, "books-cleanup")
    assert result["projectId"] == "project-1"
    assert paperclip.create_project.await_count == 1


@pytest.mark.asyncio
async def test_an_in_flight_start_is_409(paperclip):
    await repo.claim_idea_start(OWNER, "books-cleanup", "user_A")
    with pytest.raises(HTTPException) as err:
        await ideas.start(AUTH, "books-cleanup")
    assert err.value.status_code == 409
    paperclip.create_goal.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_member_cannot_start(paperclip, monkeypatch):
    monkeypatch.setattr(
        task_board, "_context", AsyncMock(side_effect=HTTPException(409, "team workspace not provisioned"))
    )
    with pytest.raises(HTTPException) as err:
        await ideas.start(AUTH, "books-cleanup")
    assert err.value.status_code == 409
    paperclip.create_goal.assert_not_awaited()
    hermes_profiles.write_profile_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_member_gets_their_own_start(paperclip):
    a = await ideas.start(AUTH, "books-cleanup")
    b = await ideas.start(AuthContext(user_id="user_B", org_id=OWNER, org_role="org:member"), "books-cleanup")
    assert a["projectId"] != b["projectId"] and a["roomId"] != b["roomId"]


@pytest.mark.asyncio
async def test_the_project_joins_the_onboarding_goal_when_there_is_one(paperclip, monkeypatch):
    from core.repositories.paperclip_repo import PaperclipRepo

    row = SimpleNamespace(status="active", starter_agent_ids=STARTERS, onboarding_goal_id="goal-onboarding")
    monkeypatch.setattr(PaperclipRepo, "resolve_owner_row", AsyncMock(return_value=row))
    await ideas.start(AUTH, "books-cleanup")
    assert paperclip.create_project.await_args.kwargs["body"]["goalIds"] == ["goal-onboarding"]
    paperclip.create_goal.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_crash_after_the_project_create_replays_the_same_project_key(paperclip, monkeypatch):
    real = repo.update_idea_start
    calls = []

    async def flaky(owner, idea, user, fields):
        calls.append(fields)
        if len(calls) == 1:  # the projectId write is lost: the process "dies" right after the create
            raise RuntimeError("crash")
        return await real(owner, idea, user, fields)

    monkeypatch.setattr(repo, "update_idea_start", flaky)
    with pytest.raises(RuntimeError):
        await ideas.start(AUTH, "books-cleanup")
    await ideas.start(AUTH, "books-cleanup")
    keys = [c.kwargs["body"]["idempotencyKey"] for c in paperclip.create_project.await_args_list]
    assert len(keys) == 2 and keys[0] == keys[1]  # the fork replays the first project for the same key


@pytest.mark.asyncio
async def test_start_unarchives_an_archived_dm(paperclip):
    room_id = await messaging._agent_dm_room_id(OWNER, "user_A", STARTERS["finance"])
    await repo.patch_room_meta(room_id, {"archivedAt": "2026-10-14T00:00:00Z", "archivedBy": "user_A"})
    result = await ideas.start(AUTH, "books-cleanup")
    assert result["roomId"] == room_id
    assert not (await repo.get_room(room_id)).get("archivedAt")
    assert len(await _lines(room_id)) == 1


@pytest.mark.asyncio
async def test_a_paperclip_failure_is_a_clean_502_and_frees_the_claim(paperclip):
    from core.services.paperclip_admin_client import PaperclipApiError

    paperclip.create_project.side_effect = PaperclipApiError("POST /projects -> 503", status_code=503, body="")
    with pytest.raises(HTTPException) as err:
        await ideas.start(AUTH, "books-cleanup")
    assert err.value.status_code == 502 and "Try Start again" in err.value.detail
    won, _ = await repo.claim_idea_start(OWNER, "books-cleanup", "user_A")
    assert won


@pytest.mark.asyncio
async def test_a_repeat_start_answers_with_the_stored_ids_when_paperclip_is_down(paperclip):
    from core.services.paperclip_admin_client import PaperclipApiError

    first = await ideas.start(AUTH, "books-cleanup")
    paperclip.get_project.side_effect = PaperclipApiError("GET /projects -> 500", status_code=500, body="")
    assert await ideas.start(AUTH, "books-cleanup") == first


@pytest.mark.asyncio
async def test_get_lists_running_starts_with_progress(paperclip):
    started = await ideas.start(AUTH, "books-cleanup")
    paperclip.list_projects.return_value = [{"id": started["projectId"], "status": "in_progress"}]
    paperclip.list_issues.return_value = [{"status": "todo"}, {"status": "done"}, {"status": "done"}]
    running = (await ideas.list_ideas(AUTH))["running"]
    assert running == [
        {
            "ideaId": "books-cleanup",
            "title": ideas.catalog()["books-cleanup"].title,
            "agentId": STARTERS["finance"],
            "agentName": "Finance",
            "roomId": started["roomId"],
            "projectId": started["projectId"],
            "starterId": "user_A",
            "starterName": "Name user_A",
            "createdAt": running[0]["createdAt"],
            "progress": {"todo": 1, "done": 2},
        }
    ]
    assert running[0]["createdAt"]
    assert paperclip.list_issues.await_args.kwargs["params"]["projectId"] == started["projectId"]


@pytest.mark.asyncio
async def test_get_drops_progress_when_paperclip_fails_and_hides_finished_projects(paperclip, monkeypatch):
    from core.services.paperclip_admin_client import PaperclipApiError

    started = await ideas.start(AUTH, "books-cleanup")
    paperclip.list_issues.side_effect = PaperclipApiError("GET /issues -> 500", status_code=500, body="")
    running = (await ideas.list_ideas(AUTH))["running"]
    assert running[0]["projectId"] == started["projectId"] and "progress" not in running[0]

    paperclip.list_issues.side_effect = None
    paperclip.list_projects.return_value = [{"id": started["projectId"], "status": "completed"}]
    assert (await ideas.list_ideas(AUTH))["running"] == []


def test_sales_tax_is_priced_per_state():
    idea = ideas.catalog()["sales-tax-registration"]
    assert idea.price_unit == "state" and "per state" in idea.why


# ---- the plan an agent filed for a start ------------------------------------------------------

START = {"agentId": "agent-g", "projectId": "p-1", "createdAt": "2026-10-15T10:00:00+00:00"}


def _approval(id_, at, *, agent="agent-g", title="Plan: Your CRM", projects=None, kind="request_board_approval"):
    payload = {"title": title, "summary": "s", **({"projectIds": projects} if projects is not None else {})}
    return {
        "id": id_,
        "type": kind,
        "status": "pending",
        "requestedByAgentId": agent,
        "createdAt": at,
        "payload": payload,
    }


def test_no_plan_until_the_agent_files_one():
    assert ideas.plan_for(START, [], allow_untagged=True) is None
    others = [
        _approval("before", "2026-10-15T09:59:59Z"),
        _approval("other-agent", "2026-10-15T11:00:00Z", agent="agent-x"),
        _approval("not-a-plan", "2026-10-15T11:00:00Z", title="Approve the budget"),
        _approval("strategy", "2026-10-15T11:00:00Z", kind="approve_ceo_strategy"),
        _approval("other-project", "2026-10-15T11:00:00Z", projects=["p-2"]),
    ]
    assert ideas.plan_for(START, others, allow_untagged=True) is None


def test_the_newest_plan_wins_and_a_plan_naming_the_project_wins_over_one_naming_none():
    both = [_approval("old", "2026-10-15T11:00:00Z"), _approval("new", "2026-10-15T12:00:00Z")]
    found = ideas.plan_for(START, both, allow_untagged=True)
    assert found == {"approvalId": "new", "status": "pending", "title": "Plan: Your CRM"}
    tagged = [
        _approval("tagged", "2026-10-15T11:00:00Z", projects=["p-1"]),
        _approval("untagged", "2026-10-15T12:00:00Z"),
    ]
    assert ideas.plan_for(START, tagged, allow_untagged=True)["approvalId"] == "tagged"


@pytest.mark.asyncio
async def test_get_carries_each_starts_plan(paperclip):
    started = await ideas.start(AUTH, "books-cleanup")
    paperclip.list_projects.return_value = [{"id": started["projectId"], "status": "in_progress"}]
    paperclip.list_approvals = AsyncMock(
        return_value=[
            _approval("ap-1", "2999-01-01T00:00:00Z", agent=STARTERS["finance"], title="Plan: Books"),
        ]
    )
    [item] = (await ideas.list_ideas(AUTH))["running"]
    assert item["plan"] == {"approvalId": "ap-1", "status": "pending", "title": "Plan: Books"}
    assert paperclip.list_approvals.await_count == 1

    paperclip.list_approvals = AsyncMock(return_value={"approvals": []})
    ideas._approvals_cache.clear()
    [item] = (await ideas.list_ideas(AUTH))["running"]
    assert item["plan"] is None


@pytest.mark.asyncio
async def test_two_starts_with_one_agent_take_only_the_plan_that_names_their_project(paperclip):
    books = await ideas.start(AUTH, "books-cleanup")
    refund = await ideas.start(AUTH, "claim-refund")
    finance = STARTERS["finance"]
    paperclip.list_approvals = AsyncMock(return_value=[_approval("untagged", "2999-01-01T00:00:00Z", agent=finance)])
    running = (await ideas.list_ideas(AUTH))["running"]
    assert [r["plan"] for r in running] == [None, None]  # it could be either idea's: neither shows it

    ideas._approvals_cache.clear()
    paperclip.list_approvals = AsyncMock(
        return_value=[_approval("tagged", "2999-01-01T00:00:00Z", agent=finance, projects=[refund["projectId"]])]
    )
    plans = {r["projectId"]: r["plan"] for r in (await ideas.list_ideas(AUTH))["running"]}
    assert plans[books["projectId"]] is None
    assert plans[refund["projectId"]]["approvalId"] == "tagged"


@pytest.mark.asyncio
async def test_the_approvals_list_is_read_once_per_few_seconds_per_company(paperclip):
    await ideas.start(AUTH, "books-cleanup")
    paperclip.list_approvals = AsyncMock(return_value=[])
    await ideas.list_ideas(AUTH)
    await ideas.list_ideas(AUTH)
    assert paperclip.list_approvals.await_count == 1


@pytest.mark.asyncio
async def test_any_start_hides_the_welcome_even_after_its_project_finished(paperclip):
    started = await ideas.start(AUTH, "books-cleanup")
    paperclip.list_projects.return_value = [{"id": started["projectId"], "status": "completed"}]
    body = await ideas.list_ideas(AUTH)
    assert body["running"] == [] and body["anyStarted"] is True


@pytest.mark.asyncio
async def test_an_approvals_failure_leaves_the_plan_out_and_the_read_answers(paperclip):
    from core.services.paperclip_admin_client import PaperclipApiError

    await ideas.start(AUTH, "books-cleanup")
    paperclip.list_approvals = AsyncMock(
        side_effect=PaperclipApiError("GET /approvals -> 500", status_code=500, body="")
    )
    paperclip.list_issues.return_value = [{"status": "done"}]
    [item] = (await ideas.list_ideas(AUTH))["running"]
    assert "plan" not in item and item["progress"] == {"done": 1}
