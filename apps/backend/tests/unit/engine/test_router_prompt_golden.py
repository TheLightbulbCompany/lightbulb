"""The Router's prompt is byte-identical after the meeting surface moved out of the engine (engine-service Task 1).

`router_prompt_golden.json` holds the instructions and batch input `router_turn` sent for a meeting batch and a plain
batch, captured from the code before the move. A change that alters either string is a prompt change, not a refactor.
"""

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.temporal import activities as acts

from ..super_agent.orchestration.test_router_turn import ROOM, _run, _trig, world  # noqa: F401
from ..super_agent.orchestration.test_tasks_repo import table  # noqa: F401

GOLDEN = json.loads((Path(__file__).parent / "router_prompt_golden.json").read_text())
NOW = "Thursday 5 Nov 2026, 9:59 PM New York time (America/New_York)"


@pytest.fixture
def fixed(world, monkeypatch):  # noqa: F811
    from core.voice.meetings import meeting_context

    async def now(ws, user_id):
        return NOW

    monkeypatch.setattr(acts, "_local_now", now)
    monkeypatch.setattr(meeting_context, "authorize", AsyncMock())
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "x" * 32)
    world["decisions"] = []
    return world


async def _prompt(world, trigger_ids) -> dict:  # noqa: F811
    await _run(trigger_ids)
    _identity, request = world["runs"][-1]
    return {"instructions": request.instructions, "input": request.input}


async def test_meeting_prompt_identical(fixed):
    live = await _trig(
        "chat:meet-live",
        ref={
            "roomId": ROOM,
            "messageId": str(uuid.UUID(int=1)),
            "origin": "meeting",
            "meetingBotId": "bot-1",
            "meetingInCallAt": 100,
        },
    )
    done = await _trig(
        "meeting-followups:room:100",
        source="webhook",
        admission="candidate",
        ref={"roomId": ROOM, "origin": "meeting", "meetingFollowups": {"requestId": "notes:1"}, "text": "Completed."},
    )
    assert await _prompt(fixed, [live, done]) == GOLDEN["meeting"]


async def test_plain_prompt_identical(fixed):
    plain = await _trig("chat:plain", ref={"roomId": ROOM, "messageId": str(uuid.UUID(int=2))})
    assert await _prompt(fixed, [plain]) == GOLDEN["plain"]
