"""Ideas (spec 2026-10-14 §4.4): the catalog, GET /ideas, Start and dismiss."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.repositories import messaging_repo as repo
from core.services import ideas, messaging, proactive
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
    from core.repositories.workspace_repo import WorkspaceRepo

    row = SimpleNamespace(status="active", starter_agent_ids=STARTERS, onboarding_goal_id=None, company_id="company-1")
    roster = [{"id": agent_id, "name": key.title()} for key, agent_id in STARTERS.items()]
    monkeypatch.setattr(WorkspaceRepo, "resolve_owner_row", AsyncMock(return_value=row))
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
    assert set(body) == {"ideas", "anyStarted", "anyDismissed", "firstRunTaskId"}
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


@pytest.mark.asyncio
async def test_an_in_flight_start_is_409(workspace):
    await repo.claim_idea_start(OWNER, "books-cleanup", "user_A")
    with pytest.raises(HTTPException) as err:
        await ideas.start(AUTH, "books-cleanup")
    assert err.value.status_code == 409


def test_sales_tax_is_priced_per_state():
    idea = ideas.catalog()["sales-tax-registration"]
    assert idea.price_unit == "state" and "per state" in idea.why


# ---- a started idea leaves the list ------------------------------------------------------------


# ---- size: task and quick ---------------------------------------------------------------------


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
async def test_the_first_runs_ideas_are_marked_first_run(workspace):
    """UX slice 6: the first run's ideas are the owner's "Pick a first move"; the list names that task to re-read on."""
    first = await ideas.suggest(OWNER, _suggestion(task_id=proactive.first_run_task_id()))
    other = await ideas.suggest(OWNER, _suggestion("Reply to the 3 refund emails", task_id="another-task"))
    listed = await ideas.list_ideas(AUTH)
    cards = {c["id"]: c for c in listed["ideas"]}
    assert cards[first["id"]]["firstRun"] is True and "firstRun" not in cards[other["id"]]
    assert listed["firstRunTaskId"] == proactive.first_run_task_id()


@pytest.mark.asyncio
async def test_a_started_first_move_whose_task_finished_is_not_a_first_move_again(workspace, monkeypatch):
    """Started is decided: once its task ends the idea is listed again, never as "Pick a first move"."""
    added = await ideas.suggest(OWNER, _suggestion(task_id=proactive.first_run_task_id()))
    await repo.claim_idea_start(OWNER, added["id"], "user_A")
    await repo.update_idea_start(OWNER, added["id"], "user_A", {"state": "open", "taskId": "t-done"})
    monkeypatch.setattr(proactive, "request_open", AsyncMock(return_value=False))  # the task finished
    card = next(c for c in (await ideas.list_ideas(AUTH))["ideas"] if c["id"] == added["id"])
    assert "firstRun" not in card


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


def test_a_suggestion_needs_a_size_and_at_most_three_deliverables():
    with pytest.raises(ValueError):
        _suggestion(size="huge")
    with pytest.raises(ValueError):
        ideas.Suggestion(agent_id="a", title="Three", why="why it", deliverables=["a", "b", "c", "d"], size="task")


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


# ---- workspaces older than the starter roster -------------------------------------------------

FRIDAY = "20000000-0000-4000-8000-000000000001"
ATLAS = "20000000-0000-4000-8000-000000000002"


@pytest.fixture
def founder_workspace(table, monkeypatch):  # noqa: F811
    """No starter roster: Friday leads, Atlas reports to Friday."""
    from core.repositories.workspace_repo import WorkspaceRepo

    row = SimpleNamespace(status="active", starter_agent_ids=None, onboarding_goal_id=None)
    roster = [
        {"id": ATLAS, "name": "Atlas", "role": "general", "reportsTo": FRIDAY},
        {"id": FRIDAY, "name": "Friday", "role": "general", "reportsTo": None},
    ]
    monkeypatch.setattr(WorkspaceRepo, "resolve_owner_row", AsyncMock(return_value=row))
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


def test_every_idea_has_needs_and_done_means():
    for idea in ideas.catalog().values():
        assert idea.needs or idea.browser or idea.person, idea.id
        assert idea.done_means and len(idea.done_means) <= 200, idea.id
        for need in idea.needs:
            assert need.access in ("read", "write"), idea.id


def test_every_need_names_a_known_service():
    from core.services.credentials import registry

    for idea in ideas.catalog().values():
        for need in idea.needs:
            if need.service in ideas.PLATFORM_SERVICES:
                continue
            registry.get_provider(need.service)  # raises for an unknown key


@pytest.mark.asyncio
async def test_needs_and_done_means_are_not_served(workspace):
    first = (await ideas.list_ideas(AUTH))["ideas"][0]
    assert not {"needs", "browser", "person", "doneMeans"} & first.keys()


def test_work_description_is_plain_for_a_suggestion_without_needs():
    job = ideas.Job(
        "s-1", "Answer reviews", "Fourteen reviews wait.", ["Every review answered"], "task", {"id": "a", "name": "A"}
    )
    text = ideas.work_description(job)
    assert text == "Fourteen reviews wait.\n\nWhat's included:\n- Every review answered"


@pytest.mark.asyncio
async def test_app_idea_round_trips_kind_and_origin(workspace):
    added = await ideas.suggest(
        OWNER,
        _suggestion("Build a refund tracker", kind="app", app_kind="internal_tool", origin="Asked 5 times this month"),
    )
    card = next(c for c in (await ideas.list_ideas(AUTH))["ideas"] if c["id"] == added["id"])
    assert (card["kind"], card["appKind"], card["origin"]) == ("app", "internal_tool", "Asked 5 times this month")
    with pytest.raises(ValueError):
        _suggestion(kind="app", app_kind="spaceship")
    with pytest.raises(ValueError):
        _suggestion(kind="app")  # an app idea needs its app kind


# ---- Super-agent workspaces: Let's do it starts a request; Not now is remembered (product shape, 5 Nov 2026) ----


@pytest.fixture
def super_agent(workspace, monkeypatch):
    """The switch on: no Paperclip. The Router side (admission, tasks) is faked; the task rows live in a dict."""
    from core.repositories import tasks_repo
    from core.services import admission, task_conversation

    monkeypatch.setattr(task_conversation, "front_agent_id", AsyncMock(return_value=STARTERS["ceo"]))
    monkeypatch.setattr(messaging, "_agent_dm_room_id", AsyncMock(return_value="room-dm-A"))
    tasks: dict[str, dict] = {}
    enqueued: list = []

    async def enqueue(ws, trigger, *, room_id=None):
        enqueued.append((ws, trigger, room_id))
        return True

    async def by_status(ws, status, **_):
        return [t for t in tasks.values() if t["status"] == status], None

    async def row_now(ws, trigger, task_id, start, room_id):
        rows.append((trigger.trigger_id, task_id, start.specialist, room_id))

    rows: list = []
    monkeypatch.setattr(admission, "enqueue_trigger", enqueue)
    monkeypatch.setattr(proactive, "_row_now", row_now)
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(side_effect=lambda ws, tid: tasks.get(tid)))
    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", by_status)
    return SimpleNamespace(tasks=tasks, enqueued=enqueued, rows=rows)


@pytest.mark.asyncio
async def test_super_agent_lets_do_it_starts_a_request_in_the_lightbulb_dm(super_agent):
    added = await ideas.suggest(OWNER, _suggestion())
    started = await ideas.start(AUTH, added["id"])
    ((ws, trigger, room_id),) = super_agent.enqueued
    assert (ws, room_id, trigger.source, trigger.admission) == (OWNER, "room-dm-A", "idea", "respond")
    assert trigger.actor_user_id == "user_A" and trigger.lane != "shared"  # the person's private lane
    start = trigger.ref["start"]
    assert start["title"] == "Answer the 14 open Shopify reviews" and start["visibility"] == "person"
    assert start["specialist"] == STARTERS["growth"] and "Every review answered" in start["brief"]
    assert started == {"roomId": "room-dm-A", "taskId": trigger.ref["taskId"]}
    # Dev walk 7 (W7-1): the request row exists when start answers, so the pane it opens never says "isn't
    # available" while the Router's queue catches up.
    assert super_agent.rows == [(trigger.trigger_id, started["taskId"], STARTERS["growth"], "room-dm-A")]
    # Twice is one request while it is open; the idea leaves the list.
    super_agent.tasks[started["taskId"]] = {"taskId": started["taskId"], "status": "working", "title": "x"}
    assert await ideas.start(AUTH, added["id"]) == started and len(super_agent.enqueued) == 1
    assert all(c["id"] != added["id"] for c in (await ideas.list_ideas(AUTH))["ideas"])
    # Done: it can be started again (a new request).
    super_agent.tasks[started["taskId"]]["status"] = "resolved"
    assert any(c["id"] == added["id"] for c in (await ideas.list_ideas(AUTH))["ideas"])
    again = await ideas.start(AUTH, added["id"])
    assert again["taskId"] != started["taskId"] and len(super_agent.enqueued) == 2


@pytest.mark.asyncio
async def test_super_agent_catalog_idea_by_the_ceo_goes_to_lightbulb(super_agent):
    ceo_idea = next(i for i in ideas.catalog().values() if i.owner_role == "ceo")
    await ideas.start(AUTH, ceo_idea.id)
    ((_, trigger, _),) = super_agent.enqueued
    assert trigger.ref["start"]["specialist"] is None  # Lightbulb itself, not a specialist


@pytest.mark.asyncio
async def test_a_dismissed_title_is_not_suggested_again_for_14_days(workspace, monkeypatch):
    from datetime import datetime, timedelta, timezone

    added = await ideas.suggest(OWNER, _suggestion())
    await ideas.dismiss(AUTH, added["id"])
    assert (await ideas.suggest(OWNER, _suggestion(" answer the 14 open shopify REVIEWS")))["status"] == "duplicate"
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    repo._table().update_item(  # noqa: SLF001
        Key={"pk": repo.owner_pk(OWNER), "sk": f"{repo.IDEA_DISMISSED_PREFIX}{added['id']}"},
        UpdateExpression="SET dismissedAt = :t",
        ExpressionAttributeValues={":t": old},
    )
    again = await ideas.suggest(OWNER, _suggestion())
    assert again["status"] == "added"
    assert [c["id"] for c in (await ideas.list_ideas(AUTH))["ideas"] if c.get("suggested")] == [again["id"]]


@pytest.mark.asyncio
async def test_super_agent_suggestion_matching_an_open_request_is_a_duplicate(super_agent):
    super_agent.tasks["t1"] = {"taskId": "t1", "status": "working", "title": "Answer the 14 open Shopify reviews"}
    assert (await ideas.suggest(OWNER, _suggestion()))["status"] == "duplicate"
    super_agent.tasks["t1"]["status"] = "resolved"
    assert (await ideas.suggest(OWNER, _suggestion()))["status"] == "added"


@pytest.mark.asyncio
async def test_one_run_files_at_most_three_ideas(workspace):
    task = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"
    for n in range(3):
        assert (await ideas.suggest(OWNER, _suggestion(f"Job {n}", task_id=task)))["status"] == "added"
    assert (await ideas.suggest(OWNER, _suggestion("Job 3", task_id=task)))["status"] == "run_cap"
    assert (await ideas.suggest(OWNER, _suggestion("Job 3")))["status"] == "added"  # another run, or none
