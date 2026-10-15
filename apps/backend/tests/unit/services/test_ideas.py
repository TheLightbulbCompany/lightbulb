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


def test_every_idea_has_a_size_agency_jobs_are_projects_and_errands_tasks():
    sizes = {i.id: i.size for i in ideas.catalog().values()}
    assert set(sizes.values()) <= {"project", "task", "quick"}
    assert all(i.size == "project" for i in ideas.catalog().values() if i.kind == "agency")
    assert all(i.size in ("task", "quick") for i in ideas.catalog().values() if i.kind == "everyday")
    good = ideas.catalog()["crm-setup"].model_dump()
    with pytest.raises(ValueError):
        ideas.Idea.model_validate({**good, "size": "epic"})
    with pytest.raises(ValueError):
        ideas.Idea.model_validate({k: v for k, v in good.items() if k != "size"})


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
        ideas.Idea.model_validate({**good, "guide": "steps"})  # no guide any more: an idea is its card


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
                          "size", "businessTypes", "emoji", "agentId", "agentName"}  # fmt: skip
    assert len({c["emoji"] for c in cards}) == 35  # every idea its own picture
    # No prices in the app (owner, 2026-10-15): they stay in the YAML for marketing only.
    assert set(body) == {"ideas", "anyStarted", "anyDismissed"}
    assert body["anyStarted"] is False and body["anyDismissed"] is False
    assert first["agentId"] == STARTERS[first["ownerRole"]]


@pytest.mark.asyncio
async def test_dismissed_ideas_are_left_out_and_a_missing_starter_falls_back_to_the_ceo(workspace, monkeypatch):
    await ideas.dismiss(AUTH, "website-rebuild")
    roster = [
        {"id": a, "name": r.title(), "role": r, "reportsTo": None if r == "ceo" else STARTERS["ceo"]}
        for r, a in STARTERS.items()
        if r != "growth"
    ]
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))
    body = await ideas.list_ideas(AUTH)
    by_id = {c["id"]: c for c in body["ideas"]}
    assert "website-rebuild" not in by_id and body["anyDismissed"] is True
    growth = [i.id for i in ideas.catalog().values() if i.owner_role == "growth"]
    assert growth and all(by_id[g]["agentId"] == STARTERS["ceo"] for g in growth)
    assert by_id["books-cleanup"]["agentId"] == STARTERS["finance"]


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
        create_issue=AsyncMock(side_effect=lambda **_: {"id": f"issue-{admin.create_issue.await_count}"}),
        get_issue=AsyncMock(return_value={"status": "backlog"}),
    )
    ctx = SimpleNamespace(owner_id=OWNER, company_id="company-1", session_cookie="cookie")
    monkeypatch.setattr(task_board, "_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(paperclip_admin_client, "shared_admin_client", lambda: admin)
    monkeypatch.setattr(hermes_profiles, "write_profile_file", AsyncMock())
    return admin


async def _lines(room_id):
    messages, _ = await repo.list_messages(room_id, 0, 100)
    return [m for m in messages if m.get("via") == "ideas"]


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
    # No files in the agent's profile: the message carries the idea and its project link.
    hermes_profiles.write_profile_file.assert_not_awaited()
    lines = await _lines(first["roomId"])
    assert len(lines) == 1
    idea = ideas.catalog()["books-cleanup"]
    # The person's own message: the idea quoted, the ask, and the project tracking it (that link
    # is how the agent learns where this start's tasks go). No instructions, ids or hidden notes.
    assert lines[0]["author"]["kind"] == "human" and lines[0]["author"]["id"] == "user_A"
    assert lines[0]["text"] == (
        f"> **{idea.title}**\n> {idea.why}\n\nLet's do it!\n"
        f"Tracked in Work: [{idea.title}]({ideas.work_link('project', first['projectId'])})"
    )
    assert "panel=teams-project-detail&detailId=" + first["projectId"] in lines[0]["text"]
    assert "agentNote" not in lines[0]
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
async def test_a_failed_start_releases_the_claim_and_reuses_its_project(paperclip, monkeypatch):
    # A failure after the project was made (posting the DM message): the next Start reuses it.
    real = messaging.create_human_message
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("dynamo down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(messaging, "create_human_message", flaky)
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
    assert err.value.status_code == 502 and "Try again" in err.value.detail
    won, _ = await repo.claim_idea_start(OWNER, "books-cleanup", "user_A")
    assert won


@pytest.mark.asyncio
async def test_a_repeat_start_answers_with_the_stored_ids_when_paperclip_is_down(paperclip):
    from core.services.paperclip_admin_client import PaperclipApiError

    first = await ideas.start(AUTH, "books-cleanup")
    paperclip.get_project.side_effect = PaperclipApiError("GET /projects -> 500", status_code=500, body="")
    assert await ideas.start(AUTH, "books-cleanup") == first


def test_sales_tax_is_priced_per_state():
    idea = ideas.catalog()["sales-tax-registration"]
    assert idea.price_unit == "state" and "per state" in idea.why


# ---- a started idea leaves the list ------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_start_hides_the_welcome_even_after_its_project_finished(paperclip):
    started = await ideas.start(AUTH, "books-cleanup")
    paperclip.list_projects.return_value = [{"id": started["projectId"], "status": "completed"}]
    body = await ideas.list_ideas(AUTH)
    assert body["anyStarted"] is True


# ---- size: task and quick ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_task_idea_makes_one_backlog_task_assigned_to_the_agent_and_links_it(paperclip):
    idea = ideas.catalog()["claim-refund"]
    assert idea.size == "task"
    first = await ideas.start(AUTH, "claim-refund")
    again = await ideas.start(AUTH, "claim-refund")
    assert first == again == {"roomId": first["roomId"], "issueId": "issue-1"}
    paperclip.create_project.assert_not_awaited()
    body = paperclip.create_issue.await_args.kwargs["body"]
    # backlog: the DM message is the one wake; the same title may come again after a finished task.
    assert body["title"] == idea.title and body["status"] == "backlog" and body["allowDuplicate"] is True
    assert body["assigneeAgentId"] == STARTERS["finance"]
    assert idea.why in body["description"] and all(d in body["description"] for d in idea.deliverables)
    assert body["idempotencyKey"].startswith("lightbulb-idea-project:v1:")
    [line] = await _lines(first["roomId"])
    assert line["text"] == (
        f"> **{idea.title}**\n> {idea.why}\n\nLet's do it!\n"
        f"Tracked in Work: [{idea.title}]({ideas.work_link('task', 'issue-1')})"
    )
    assert "panel=teams-issue-detail&detailId=issue-1" in line["text"]


@pytest.mark.asyncio
async def test_a_quick_idea_makes_nothing_in_work_is_never_running_and_can_be_asked_again(paperclip, monkeypatch):
    quick = ideas.catalog()["claim-refund"].model_copy(update={"size": "quick"})
    monkeypatch.setitem(ideas.catalog(), "claim-refund", quick)
    first = await ideas.start(AUTH, "claim-refund")
    assert first == {"roomId": first["roomId"]}
    paperclip.create_project.assert_not_awaited()
    paperclip.create_issue.assert_not_awaited()
    [line] = await _lines(first["roomId"])
    assert line["text"] == f"> **{quick.title}**\n> {quick.why}\n\nLet's do it!"
    body = await ideas.list_ideas(AUTH)
    assert body["anyStarted"] is True
    await ideas.start(AUTH, "claim-refund")  # answered in one go: Let's do it again asks again
    assert len(await _lines(first["roomId"])) == 2


# ---- suggest_idea -----------------------------------------------------------------------------


def _suggestion(title="Answer the 14 open Shopify reviews", size="task", agent=None, **over):
    return ideas.Suggestion(
        agent_id=agent or STARTERS["growth"],
        title=title,
        why="Fourteen reviews from last month have no reply; answering them shows on your store page.",
        deliverables=["Every review answered", "Two refunds flagged to you"],
        size=size,
        **over,
    )


@pytest.mark.asyncio
async def test_a_suggestion_is_stored_and_listed_first_marked_for_you(workspace):
    added = await ideas.suggest(OWNER, _suggestion())
    assert added["status"] == "added" and added["id"].startswith("s-")
    first = (await ideas.list_ideas(AUTH))["ideas"][0]
    assert first["id"] == added["id"] and first["suggested"] is True
    assert (first["agentId"], first["agentName"], first["size"]) == (STARTERS["growth"], "Growth", "task")
    assert "ourTurnaround" not in first and "agencyPriceLow" not in first
    assert first["emoji"] == "💡"  # the agent gave none
    picked = await ideas.suggest(OWNER, _suggestion("Reply to the 3 refund emails", emoji="📮"))
    assert next(c for c in (await ideas.list_ideas(AUTH))["ideas"] if c["id"] == picked["id"])["emoji"] == "📮"


@pytest.mark.asyncio
async def test_suggestions_are_newest_first_deduped_by_title_and_capped(workspace):
    await ideas.suggest(OWNER, _suggestion("Old one"))
    await ideas.suggest(OWNER, _suggestion("New one"))
    titles = [c["title"] for c in (await ideas.list_ideas(AUTH))["ideas"] if c.get("suggested")]
    assert titles == ["New one", "Old one"]
    assert (await ideas.suggest(OWNER, _suggestion("  new ONE! ")))["status"] == "duplicate"
    catalog_title = ideas.catalog()["crm-setup"].title
    assert (await ideas.suggest(OWNER, _suggestion(catalog_title.upper())))["status"] == "duplicate"
    for n in range(8):
        assert (await ideas.suggest(OWNER, _suggestion(f"Job number {n}")))["status"] == "added"
    assert (await ideas.suggest(OWNER, _suggestion("One too many")))["status"] == "cap"
    # A dismissed one frees a place.
    await ideas.dismiss(AUTH, next(c["id"] for c in (await ideas.list_ideas(AUTH))["ideas"] if c.get("suggested")))
    assert (await ideas.suggest(OWNER, _suggestion("One too many")))["status"] == "added"


@pytest.mark.asyncio
async def test_a_dismissed_suggestion_leaves_the_list_and_an_unknown_agent_is_refused(workspace):
    added = await ideas.suggest(OWNER, _suggestion())
    await ideas.dismiss(AUTH, added["id"])
    assert all(c["id"] != added["id"] for c in (await ideas.list_ideas(AUTH))["ideas"])
    with pytest.raises(HTTPException) as err:
        await ideas.suggest(OWNER, _suggestion(agent="not-on-the-team"))
    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_a_suggestion_starts_like_a_catalog_idea_with_its_agent(paperclip):
    added = await ideas.suggest(OWNER, _suggestion(size="project"))
    started = await ideas.start(AUTH, added["id"])
    assert started["projectId"] == "project-1"
    assert paperclip.create_project.await_args.kwargs["body"]["leadAgentId"] == STARTERS["growth"]
    [line] = await _lines(started["roomId"])
    assert line["text"].startswith("> **Answer the 14 open Shopify reviews**")
    # Started: it leaves the list while its project is open.
    assert all(c["id"] != added["id"] for c in (await ideas.list_ideas(AUTH))["ideas"])


def test_a_suggestion_needs_a_size_and_at_most_three_deliverables():
    with pytest.raises(ValueError):
        _suggestion(size="huge")
    with pytest.raises(ValueError):
        ideas.Suggestion(agent_id="a", title="Three", why="why it", deliverables=["a", "b", "c", "d"], size="task")


@pytest.mark.asyncio
async def test_an_agent_written_title_cannot_add_links_or_break_the_work_link(paperclip):
    added = await ideas.suggest(OWNER, _suggestion("Fix [this](https://evil.example) *now*", size="task"))
    started = await ideas.start(AUTH, added["id"])
    [line] = await _lines(started["roomId"])
    assert "[this](https://evil.example)" not in line["text"]
    assert "Fix \\[this\\]\\(https://evil.example\\) \\*now\\*" in line["text"]
    assert line["text"].endswith(f"({ideas.work_link('task', started['issueId'])})")


@pytest.mark.asyncio
async def test_list_and_start_read_the_roster_once(paperclip, monkeypatch):
    # The ideas service's own reads (messaging's DM and send paths have theirs).
    real = ideas._roster
    calls = []

    async def counted(owner):
        calls.append(owner)
        return await real(owner)

    monkeypatch.setattr(ideas, "_roster", counted)
    await ideas.list_ideas(AUTH)
    assert len(calls) == 1
    added = await ideas.suggest(OWNER, _suggestion())
    calls.clear()
    await ideas.start(AUTH, added["id"])
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["title", "why"])
def test_a_suggestion_with_blank_text_is_refused(field):
    good = {
        "agent_id": "a",
        "title": "Answer the reviews",
        "why": "They have no reply.",
        "deliverables": ["x"],
        "size": "task",
    }
    with pytest.raises(ValueError):
        ideas.Suggestion(**{**good, field: "     "})
    with pytest.raises(ValueError):
        ideas.Suggestion(**{**good, field: " \n a \t "})
    with pytest.raises(ValueError):
        ideas.Suggestion(**{**good, "deliverables": ["  "]})


def _ids(body):
    return {c["id"] for c in body["ideas"]}


@pytest.mark.asyncio
async def test_a_started_idea_leaves_the_list_for_everyone_until_its_task_is_closed(paperclip):
    teammate = AuthContext(user_id="user_B", org_id=OWNER, org_role="org:member")
    await ideas.start(AUTH, "claim-refund")  # a task, open (backlog)
    assert "claim-refund" not in _ids(await ideas.list_ideas(AUTH))
    assert "claim-refund" not in _ids(await ideas.list_ideas(teammate))
    paperclip.get_issue.return_value = {"status": "done"}
    assert "claim-refund" in _ids(await ideas.list_ideas(teammate))
    assert (await ideas.start(AUTH, "claim-refund"))["issueId"] == "issue-2"  # and it can start again


@pytest.mark.asyncio
async def test_a_started_project_comes_back_when_it_is_completed_or_deleted(paperclip):
    from core.services.paperclip_admin_client import PaperclipApiError

    started = await ideas.start(AUTH, "books-cleanup")
    paperclip.list_projects.return_value = [{"id": started["projectId"], "status": "in_progress"}]
    assert "books-cleanup" not in _ids(await ideas.list_ideas(AUTH))
    paperclip.list_projects.return_value = [{"id": started["projectId"], "status": "completed"}]
    assert "books-cleanup" in _ids(await ideas.list_ideas(AUTH))
    paperclip.list_projects.return_value = []  # no longer listed...
    paperclip.get_project.side_effect = PaperclipApiError("GET -> 404", status_code=404, body="")  # ...deleted
    assert "books-cleanup" in _ids(await ideas.list_ideas(AUTH))
    assert (await ideas.start(AUTH, "books-cleanup"))["projectId"] == "project-2"


@pytest.mark.asyncio
async def test_a_deleted_task_brings_the_idea_back(paperclip):
    from core.services.paperclip_admin_client import PaperclipApiError

    await ideas.start(AUTH, "claim-refund")
    paperclip.get_issue.side_effect = PaperclipApiError("GET -> 404", status_code=404, body="")
    assert "claim-refund" in _ids(await ideas.list_ideas(AUTH))


@pytest.mark.asyncio
async def test_a_work_item_that_cannot_be_read_keeps_the_idea_hidden(paperclip):
    from core.services.paperclip_admin_client import PaperclipApiError

    await ideas.start(AUTH, "books-cleanup")
    paperclip.list_projects.return_value = []
    paperclip.get_project.side_effect = PaperclipApiError("GET -> 403", status_code=403, body="")
    assert "books-cleanup" not in _ids(await ideas.list_ideas(AUTH))
    paperclip.list_projects.side_effect = PaperclipApiError("GET -> 500", status_code=500, body="")
    assert "books-cleanup" not in _ids(await ideas.list_ideas(AUTH))


@pytest.mark.asyncio
async def test_a_quick_idea_is_hidden_for_a_day_after_it_is_started(paperclip, monkeypatch):
    from datetime import datetime, timedelta, timezone

    quick = ideas.catalog()["claim-refund"].model_copy(update={"size": "quick"})
    monkeypatch.setitem(ideas.catalog(), "claim-refund", quick)
    await ideas.start(AUTH, "claim-refund")
    assert "claim-refund" not in _ids(await ideas.list_ideas(AUTH))
    day_later = datetime.now(timezone.utc) + timedelta(hours=24, minutes=1)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return day_later

    monkeypatch.setattr(ideas, "datetime", Later)
    assert "claim-refund" in _ids(await ideas.list_ideas(AUTH))


# ---- workspaces older than the starter roster -------------------------------------------------

FRIDAY = "20000000-0000-4000-8000-000000000001"
ATLAS = "20000000-0000-4000-8000-000000000002"


@pytest.fixture
def founder_workspace(table, monkeypatch):  # noqa: F811
    """No starter roster: Friday leads, Atlas reports to Friday."""
    from core.repositories.paperclip_repo import PaperclipRepo

    row = SimpleNamespace(status="active", starter_agent_ids=None, onboarding_goal_id=None)
    roster = [
        {"id": ATLAS, "name": "Atlas", "role": "general", "reportsTo": FRIDAY},
        {"id": FRIDAY, "name": "Friday", "role": "general", "reportsTo": None},
    ]
    monkeypatch.setattr(PaperclipRepo, "resolve_owner_row", AsyncMock(return_value=row))
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))


@pytest.mark.asyncio
async def test_a_workspace_without_starters_gives_every_idea_to_its_top_agent(founder_workspace):
    listed = await ideas.list_ideas(AUTH)
    assert len(listed["ideas"]) == len(ideas.catalog())
    assert {card["agentId"] for card in listed["ideas"]} == {FRIDAY}
    assert {card["agentName"] for card in listed["ideas"]} == {"Friday"}


def test_the_top_agent_is_the_ceo_else_the_one_reporting_to_nobody_else_the_first():
    assert ideas._top_agent([{"id": "a", "reportsTo": None}, {"id": "b", "role": "ceo", "reportsTo": "a"}]) == "b"
    assert ideas._top_agent([{"id": "a", "reportsTo": "b"}, {"id": "b", "reportsTo": None}]) == "b"
    assert ideas._top_agent([{"id": "a", "reportsTo": "b"}, {"id": "b", "reportsTo": "a"}]) == "a"
    assert ideas._top_agent([]) is None
