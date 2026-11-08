"""Unit tests for the first-run brief staging service.

Mirrors tests/unit/services/test_friday_identity.py: Workspace is a
MagicMock; we assert exact workspace-relative paths. Staging itself is
unconditional now (the company-scoped onboarding claim in
paperclip_repo.py is the one-shot marker, not BRIEF.md's existence).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.onboarding_brief import (
    capture_first_workspace_message,
    first_message_intake,
    is_root_ceo,
    render_brief_md,
    stage_first_run_files,
)

FRIDAY_ID = "agent-uuid-123"
OWNER = "user_abc"


@pytest.mark.asyncio
async def test_first_canonical_human_message_captures_eligible_specialist():
    starter_ids = {
        "ceo": "10000000-0000-4000-8000-000000000001",
        "engineering": "10000000-0000-4000-8000-000000000002",
        "finance": "10000000-0000-4000-8000-000000000003",
        "growth": "10000000-0000-4000-8000-000000000004",
        "analytics": "10000000-0000-4000-8000-000000000005",
        "customer_operations": "10000000-0000-4000-8000-000000000006",
    }
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(
        return_value=SimpleNamespace(status="active", starter_agent_ids=starter_ids, funnel_state="not_started")
    )
    repo.put_pending_brief = AsyncMock(return_value=True)
    reconcile = AsyncMock()
    message = {
        "roomId": __import__("core.services.messaging", fromlist=["default_room_id"]).default_room_id(OWNER),
        # A human can win the room-created/welcome-message race and become
        # sequence 1. Durable author order, not a fixed sequence, identifies
        # onboarding intake.
        "messageSeq": 1,
        "messageId": "msg_first",
        "text": "Build a calmer way for clinics to schedule patients",
        "author": {"kind": "human", "id": "user_member"},
        # The CEO is observe; normal dispatch selected the mentioned specialist.
        "dispatchTargets": [{"agentId": starter_ids["engineering"], "eventKind": "user_request"}],
    }
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch("core.services.onboarding_brief.reconcile_pending_brief", reconcile),
        patch(
            "core.repositories.messaging_repo.list_messages_between",
            new=AsyncMock(
                side_effect=lambda _room, _start, through: (
                    [] if through == 0 else [message, {"messageSeq": 2, "author": {"kind": "agent"}}]
                )
            ),
        ),
    ):
        assert await capture_first_workspace_message(OWNER, message) is True
        assert await capture_first_workspace_message(OWNER, {**message, "messageSeq": 3}) is False

    repo.put_pending_brief.assert_awaited_once_with(
        OWNER,
        path="new",
        user_input=message["text"],
        detail=None,
        brief_text=message["text"],
        submitted_by="user_member",
        source_message_id="msg_first",
        target_agent_id=starter_ids["engineering"],
    )
    reconcile.assert_awaited_once_with(OWNER)


@pytest.mark.asyncio
async def test_deleted_specialist_does_not_block_capture_for_remaining_target():
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(
        return_value=SimpleNamespace(
            status="active",
            starter_agent_ids={"ceo": FRIDAY_ID},
            funnel_state="not_started",
        )
    )
    repo.put_pending_brief = AsyncMock(return_value=True)
    message = {
        "roomId": __import__("core.services.messaging", fromlist=["default_room_id"]).default_room_id(OWNER),
        "messageSeq": 2,
        "messageId": "msg_after_delete",
        "text": "Help me relaunch the product",
        "author": {"kind": "human", "id": OWNER},
        "dispatchTargets": [{"agentId": FRIDAY_ID, "eventKind": "room_event"}],
    }
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch("core.services.onboarding_brief.reconcile_pending_brief", new=AsyncMock()),
        patch(
            "core.repositories.messaging_repo.list_messages_between",
            new=AsyncMock(return_value=[{"messageSeq": 1, "author": {"kind": "agent"}}]),
        ),
    ):
        assert await capture_first_workspace_message(OWNER, message) is True

    assert repo.put_pending_brief.await_args.kwargs["target_agent_id"] == FRIDAY_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "room, history, captured, closed",
    [
        # super-agent: the first human message after the welcome in the owner's Lightbulb DM
        ("lightbulb-dm", [("agent", "first_run")], True, True),
        # ...also when the owner wrote before setup finished: the welcome follows their message
        ("lightbulb-dm", [("human", None), ("agent", "first_run")], True, True),
        ("lightbulb-dm", [("agent", "first_run"), ("human", None)], False, False),  # already answered
        ("lightbulb-dm", [("agent", None)], False, False),  # any other young room is not the first run
        ("general", [("agent", "team_welcome")], False, False),  # super-agent #general holds only the team line
        ("general", [("agent", None)], True, False),  # flag off: unchanged
    ],
)
async def test_the_first_run_is_captured_where_the_welcome_is(room, history, captured, closed):
    from core.services.messaging import default_room_id

    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(
        return_value=SimpleNamespace(status="active", starter_agent_ids={"ceo": FRIDAY_ID}, funnel_state="not_started")
    )
    repo.put_pending_brief = AsyncMock(return_value=True)
    earlier = [
        {"messageSeq": seq, "author": {"kind": kind}, **({"kind": mkind} if mkind else {})}
        for seq, (kind, mkind) in enumerate(history, start=1)
    ]
    message = {
        "roomId": default_room_id(OWNER) if room == "general" else room,
        "messageSeq": len(earlier) + 1,
        "messageId": "msg_first",
        "text": "Here's our website: acme.com",
        "author": {"kind": "human", "id": OWNER},
        "dispatchTargets": [{"agentId": FRIDAY_ID, "eventKind": "user_request"}],
    }
    close = AsyncMock()
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch("core.services.onboarding_brief.reconcile_pending_brief", new=AsyncMock()),
        patch("core.repositories.messaging_repo.list_messages_between", new=AsyncMock(return_value=earlier)),
        patch("core.services.messaging.close_first_run", new=close),
    ):
        assert await capture_first_workspace_message(OWNER, message) is captured
        # A busy room is never read as the first-run DM.
        assert await capture_first_workspace_message(OWNER, {**message, "roomId": "x", "messageSeq": 40}) is False
    assert close.await_count == int(closed)


@pytest.mark.asyncio
async def test_a_short_first_answer_still_closes_the_first_run():
    """ "ok" is no brief, but it is the owner's answer: the next load lands on Home."""
    close = AsyncMock()
    welcome = [{"messageSeq": 1, "author": {"kind": "agent"}, "kind": "first_run"}]
    message = {"roomId": "lightbulb-dm", "messageSeq": 2, "messageId": "m", "text": "ok",
               "author": {"kind": "human", "id": OWNER}, "dispatchTargets": []}  # fmt: skip
    with (
        patch("core.repositories.messaging_repo.list_messages_between", new=AsyncMock(return_value=welcome)),
        patch("core.services.messaging.close_first_run", new=close),
    ):
        assert await capture_first_workspace_message(OWNER, message) is False
    close.assert_awaited_once_with("lightbulb-dm")


@pytest.mark.asyncio
async def test_first_human_message_without_dispatch_target_does_not_seed_onboarding():
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(
        return_value=SimpleNamespace(
            status="active",
            starter_agent_ids={
                "ceo": FRIDAY_ID,
                "engineering": "engineering-id",
                "finance": "finance-id",
                "growth": "growth-id",
                "analytics": "analytics-id",
                "customer_operations": "customer-operations-id",
            },
            funnel_state="not_started",
        )
    )
    repo.put_pending_brief = AsyncMock(return_value=True)
    message = {
        "roomId": __import__("core.services.messaging", fromlist=["default_room_id"]).default_room_id(OWNER),
        "messageSeq": 2,
        "messageId": "msg_observed_only",
        "text": "Nobody should be assigned this",
        "author": {"kind": "human", "id": OWNER},
        "dispatchTargets": [],
    }
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch(
            "core.repositories.messaging_repo.list_messages_between",
            new=AsyncMock(return_value=[{"messageSeq": 1, "author": {"kind": "agent"}}]),
        ),
        patch("core.services.onboarding_brief.reconcile_pending_brief", new=AsyncMock()),
    ):
        assert await capture_first_workspace_message(OWNER, message) is False

    repo.resolve_owner_row.assert_not_awaited()
    repo.put_pending_brief.assert_not_awaited()


def test_render_brief_md_contains_all_fields():
    md = render_brief_md(
        path="grow",
        user_input="https://acmeconstruction.com",
        detail="residential builds in Austin",
        display_name="Max",
        email="max@acme.com",
        target_is_ceo=True,
    )
    assert "# BRIEF.md" in md
    assert "grow" in md
    assert "https://acmeconstruction.com" in md
    assert "residential builds in Austin" in md
    assert "Max" in md
    assert "max@acme.com" in md


def test_render_brief_md_carries_the_first_run_result_contract():
    """T13: the first run ends with one document, one decision, one connect card, three offers."""
    md = render_brief_md(
        path="new", user_input="a bakery", detail=None, display_name="Ann", email="a@x.com", target_is_ceo=True
    )
    assert "three sentences at most" in md
    assert "approve_ceo_strategy" in md
    assert "request_approval" in md
    assert "chat?panel=connections&provider=<slug>" in md
    assert "Three offers" in md
    assert "Only you speak" in md
    assert "{one line}" in md  # format() escapes rendered, not raised


def test_render_brief_md_specialist_target_files_a_board_approval_not_a_ceo_strategy():
    """The first message can be dispatched to a specialist; only the root CEO files approve_ceo_strategy."""
    md = render_brief_md(
        path="new", user_input="a bakery", detail=None, display_name="Ann", email="a@x.com", target_is_ceo=False
    )
    assert "approve_ceo_strategy" not in md
    assert "request_board_approval" in md
    assert "request_approval" in md


def test_is_root_ceo_is_role_keyed_not_name_keyed():
    assert is_root_ceo({"id": "a", "role": "ceo", "name": "Engineering"})
    assert not is_root_ceo({"id": "b", "role": "engineer", "name": "CEO"})
    assert not is_root_ceo({"id": "c", "role": "ceo", "reportsTo": "a"})
    assert not is_root_ceo(None)


def test_render_brief_md_omits_detail_line_when_none():
    md = render_brief_md(
        path="new",
        user_input="a social media agency for restaurants",
        detail=None,
        display_name="Ben",
        email="ben@x.com",
        target_is_ceo=True,
    )
    assert "Extra detail" not in md


def test_render_brief_md_no_idea_tells_friday_to_pick_one():
    """The "you pick" button sends no input; the absence is the instruction."""
    md = render_brief_md(
        path="new",
        user_input=None,
        detail=None,
        display_name="Prabuddha",
        email="prabuddha@gmail.com",
        target_is_ceo=True,
    )
    assert "job zero" in md
    # She must know she has a lead (the email) and a way to follow it.
    assert "prabuddha@gmail.com" in md
    assert "web_search" in md
    # Committing beats stalling — the run has to complete if they tab away.
    assert "do not stall" in md
    # Never render a Python None into the brief.
    assert "None" not in md


async def test_staging_overwrites_a_partial_prior_attempt(monkeypatch):
    """The claim decides one-shot-ness now, so staging always writes.

    A prior attempt that staged files and then failed leaves BRIEF.md behind.
    Returning early on that would strand the goal and project this run just
    seeded, so we overwrite instead.
    """
    write_profile_file = AsyncMock(return_value="BRIEF.md")
    monkeypatch.setattr("core.services.hermes_profiles.write_profile_file", write_profile_file)

    await stage_first_run_files(owner_id="org_abc", friday_agent_id="friday-uuid", brief_md="brief")
    await stage_first_run_files(owner_id="org_abc", friday_agent_id="friday-uuid", brief_md="brief again")

    assert [call.args for call in write_profile_file.await_args_list] == [
        ("org_abc", "friday-uuid", "BRIEF.md", "brief"),
        ("org_abc", "friday-uuid", "BRIEF.md", "brief again"),
    ]


async def test_staging_writes_the_agents_profile(monkeypatch):
    """Task-14 row 5: the old EFS write SUCCEEDED into a tree no Hermes agent reads — the
    founder's description of the company they want run never reached the CEO's first turn while
    the funnel reported complete. It goes to the profile the bridge pulls."""
    write_profile_file = AsyncMock(return_value="BRIEF.md")
    monkeypatch.setattr("core.services.hermes_profiles.write_profile_file", write_profile_file)

    await stage_first_run_files(owner_id="org_abc", friday_agent_id="friday-uuid", brief_md="brief")

    write_profile_file.assert_awaited_once_with("org_abc", "friday-uuid", "BRIEF.md", "brief")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Here's our website: https://acme.com/pricing", ("grow", "https://acme.com/pricing")),
        ("Here's our website: acme.com", ("grow", "acme.com")),
        ("Here's our website: (https://acme.com).", ("grow", "https://acme.com")),
        ("Here's our website: <https://acme.com/about>", ("grow", "https://acme.com/about")),
        ("Here's our website: example.xn--p1ai", ("grow", "example.xn--p1ai")),
        ("Here's our website: bücher.de", ("grow", "bücher.de")),
        ("bücher.de/katalog", ("grow", "bücher.de/katalog")),
        ("Here's our website: my-site.com", ("grow", "my-site.com")),
        ("foo-bar.co.uk", ("grow", "foo-bar.co.uk")),
        ("Here's our website: münchen-shop.de", ("grow", "münchen-shop.de")),
        (
            "Here's our website: https://example.com/products/widget_(blue)",
            ("grow", "https://example.com/products/widget_(blue)"),
        ),
        ("Here's our website: (https://example.com/a_(b)).", ("grow", "https://example.com/a_(b)")),
        ("acme.co", ("grow", "acme.co")),
        ("I'm starting a competitor to calendly.com", ("new", "I'm starting a competitor to calendly.com")),
        ("I am starting a bakery, see instagram.com/x", ("new", "I am starting a bakery, see instagram.com/x")),
        ("Reach me at jane@gmail.com", ("new", "Reach me at jane@gmail.com")),
        ("I'm starting a dog-walking service in Austin", ("new", "I'm starting a dog-walking service in Austin")),
        ("Not sure yet. Pick something useful and show me how you work.", ("new", None)),
        ("not sure yet, you choose", ("new", None)),
        ("We ship 3.5 tonnes a week", ("new", "We ship 3.5 tonnes a week")),
        ("Here's our website: acme.xyz", ("grow", "acme.xyz")),
        ("Here's our website: acme.com.au", ("grow", "acme.com.au")),
        ("Here's our website: shop.acme.tech/about", ("grow", "shop.acme.tech/about")),
        ("We build on node.js", ("new", "We build on node.js")),
        ("Here's our website: acme.sh", ("grow", "acme.sh")),
        ("Our website is https://acme.com/pricing — we sell candles", ("grow", "https://acme.com/pricing")),
        ("https://acme.com", ("grow", "https://acme.com")),
        ("<https://acme.com>", ("grow", "https://acme.com")),
        ("We sell on amazon.com and etsy", ("new", "We sell on amazon.com and etsy")),
        ("Here's our website: coming soon", ("new", "Here's our website: coming soon")),
        ("3.5 tonnes a week", ("new", "3.5 tonnes a week")),
    ],
)
def test_first_message_intake_picks_the_path_and_the_goal_input(text, expected):
    assert first_message_intake(text) == expected


@pytest.mark.asyncio
async def test_a_url_in_the_first_message_lands_as_grow_with_the_url_as_input():
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=SimpleNamespace(status="active", funnel_state="not_started"))
    repo.put_pending_brief = AsyncMock(return_value=True)
    message = {
        "roomId": __import__("core.services.messaging", fromlist=["default_room_id"]).default_room_id(OWNER),
        "messageSeq": 1,
        "messageId": "m1",
        "author": {"kind": "human", "id": "user_1"},
        "text": "Here's our website: https://acme.com",
        "dispatchTargets": [{"agentId": FRIDAY_ID, "eventKind": "user_request"}],
    }
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch("core.services.onboarding_brief.reconcile_pending_brief", new=AsyncMock()),
        patch("core.repositories.messaging_repo.list_messages_between", new=AsyncMock(return_value=[])),
    ):
        assert await capture_first_workspace_message(OWNER, message) is True
    kwargs = repo.put_pending_brief.await_args.kwargs
    assert kwargs["path"] == "grow"
    assert kwargs["user_input"] == "https://acme.com"
    assert kwargs["detail"] == "Here's our website: https://acme.com"
    assert kwargs["brief_text"] == "Here's our website: https://acme.com"


@pytest.mark.asyncio
async def test_a_not_sure_yet_message_keeps_the_owners_words_as_detail():
    text = "Not sure yet which market to target, but we're building a scheduling tool for dentists"
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=SimpleNamespace(status="active", funnel_state="not_started"))
    repo.put_pending_brief = AsyncMock(return_value=True)
    message = {
        "roomId": __import__("core.services.messaging", fromlist=["default_room_id"]).default_room_id(OWNER),
        "messageSeq": 1,
        "messageId": "m1",
        "author": {"kind": "human", "id": "user_1"},
        "text": text,
        "dispatchTargets": [{"agentId": FRIDAY_ID, "eventKind": "user_request"}],
    }
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch("core.services.onboarding_brief.reconcile_pending_brief", new=AsyncMock()),
        patch("core.repositories.messaging_repo.list_messages_between", new=AsyncMock(return_value=[])),
    ):
        assert await capture_first_workspace_message(OWNER, message) is True
    kwargs = repo.put_pending_brief.await_args.kwargs
    assert kwargs["path"] == "new"
    assert kwargs["user_input"] is None
    assert kwargs["detail"] == text
    assert kwargs["brief_text"] == text


@pytest.mark.asyncio
async def test_a_new_path_message_lands_with_no_detail():
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=SimpleNamespace(status="active", funnel_state="not_started"))
    repo.put_pending_brief = AsyncMock(return_value=True)
    message = {
        "roomId": __import__("core.services.messaging", fromlist=["default_room_id"]).default_room_id(OWNER),
        "messageSeq": 1,
        "messageId": "m1",
        "author": {"kind": "human", "id": "user_1"},
        "text": "I'm starting a bakery",
        "dispatchTargets": [{"agentId": FRIDAY_ID, "eventKind": "user_request"}],
    }
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch("core.services.onboarding_brief.reconcile_pending_brief", new=AsyncMock()),
        patch("core.repositories.messaging_repo.list_messages_between", new=AsyncMock(return_value=[])),
    ):
        assert await capture_first_workspace_message(OWNER, message) is True
    kwargs = repo.put_pending_brief.await_args.kwargs
    assert kwargs["path"] == "new"
    assert kwargs["detail"] is None


def _desk_repo(ids, *, pending=None, row=None):
    repo = MagicMock()
    repo.get_pending_brief = AsyncMock(
        return_value={"path": "grow", "input": "https://acme.example", "submitted_by": "user_m"}
        if pending is None
        else pending
    )
    repo.resolve_owner_row = AsyncMock(return_value=row or SimpleNamespace(starter_agent_ids=ids))
    return repo


@pytest.mark.asyncio
async def test_start_dms_customer_operations_with_the_prompt():
    from core.services.onboarding_brief import FRONT_DESK_BUILD_PROMPT, start_front_desk_build_for_line

    post = AsyncMock(return_value=SimpleNamespace(message_id="m1"))
    dm = AsyncMock(return_value="room-dm")
    with (
        patch(
            "core.repositories.workspace_repo.WorkspaceRepo", return_value=_desk_repo({"customer_operations": "co-1"})
        ),
        patch("core.services.messaging.create_human_message", post),
        patch("core.services.messaging._agent_dm_room_id", dm),
    ):
        assert await start_front_desk_build_for_line(OWNER) is True
    dm.assert_awaited_once_with(OWNER, "user_m", "co-1")
    args, kwargs = post.await_args
    assert args[1] == "room-dm"
    assert args[2].text == FRONT_DESK_BUILD_PROMPT.format(url="https://acme.example")
    assert args[2].text.startswith("Set up the front desk for https://acme.example.\n\n1. Read the whole site")
    assert kwargs["via"] == "front_desk"


@pytest.mark.asyncio
async def test_start_is_idempotent_per_url():
    from core.services.onboarding_brief import start_front_desk_build_for_line

    post = AsyncMock(return_value=SimpleNamespace(message_id="m1"))
    briefs = [
        {"path": "grow", "input": url, "submitted_by": "u"}
        for url in ("https://Acme.example ", "https://acme.example", "https://other.example")
    ]
    for brief in briefs:
        with (
            patch(
                "core.repositories.workspace_repo.WorkspaceRepo",
                return_value=_desk_repo({"customer_operations": "co-1"}, pending=brief),
            ),
            patch("core.services.messaging.create_human_message", post),
            patch("core.services.messaging._agent_dm_room_id", AsyncMock(return_value="r")),
        ):
            await start_front_desk_build_for_line(OWNER)
    ids = [c.args[2].client_message_id for c in post.await_args_list]
    assert ids[0] == ids[1] != ids[2]


@pytest.mark.asyncio
async def test_no_customer_operations_is_a_noop():
    from core.services.onboarding_brief import start_front_desk_build_for_line

    post = AsyncMock()
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=_desk_repo({"ceo": "x"})),
        patch("core.services.messaging.create_human_message", post),
    ):
        assert await start_front_desk_build_for_line(OWNER) is False
    post.assert_not_awaited()


def _website_capture(start):
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(
        return_value=SimpleNamespace(status="active", starter_agent_ids={"ceo": FRIDAY_ID}, funnel_state="not_started")
    )
    repo.put_pending_brief = AsyncMock(return_value=True)
    message = {
        "roomId": __import__("core.services.messaging", fromlist=["default_room_id"]).default_room_id(OWNER),
        "messageSeq": 2,
        "messageId": "msg_site",
        "text": "Here's our website: https://acme.example",
        "author": {"kind": "human", "id": "user_member"},
        "dispatchTargets": [{"agentId": FRIDAY_ID, "eventKind": "user_request"}],
    }
    return message, (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch("core.services.onboarding_brief.reconcile_pending_brief", new=AsyncMock()),
        patch("core.services.onboarding_brief.start_front_desk_build_for_line", start),
        patch(
            "core.repositories.messaging_repo.list_messages_between",
            new=AsyncMock(return_value=[{"messageSeq": 1, "author": {"kind": "agent"}}]),
        ),
    )


@pytest.mark.asyncio
async def test_first_message_no_longer_starts_the_build():
    """The build starts when the line is recorded (text_lines), not on the first message."""
    start = AsyncMock(return_value="m1")
    message, patches = _website_capture(start)
    with patches[0], patches[1], patches[2], patches[3]:
        assert await capture_first_workspace_message(OWNER, message) is True
    start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending", "row", "expected"),
    [
        ({"path": "grow", "input": "https://acme.example", "submitted_by": "u1"}, None, ("https://acme.example", "u1")),
        ({"path": "new", "input": "a bakery", "submitted_by": "u1"}, None, None),
        (
            {},
            SimpleNamespace(
                onboarding_brief_text="Here's our website: https://acme.example",
                onboarding_submitted_by="u2",
                starter_agent_ids={"customer_operations": "co-1"},
            ),
            ("https://acme.example", "u2"),
        ),
        ({}, SimpleNamespace(onboarding_brief_text=None, onboarding_submitted_by="u2"), None),
    ],
)
async def test_the_build_reads_the_website_from_the_brief(pending, row, expected):
    """The pending brief while it waits; once fulfilled, the owner row's stored brief text."""
    from core.services.onboarding_brief import start_front_desk_build_for_line

    repo = _desk_repo({"customer_operations": "co-1"}, pending=pending, row=row)
    post = AsyncMock(return_value=SimpleNamespace(message_id="m1"))
    dm = AsyncMock(return_value="r")
    with (
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        patch("core.services.messaging.create_human_message", post),
        patch("core.services.messaging._agent_dm_room_id", dm),
    ):
        assert await start_front_desk_build_for_line(OWNER) is (expected is not None)
    if expected:
        url, by = expected
        assert dm.await_args.args[1] == by and url in post.await_args.args[2].text
    else:
        post.assert_not_awaited()
