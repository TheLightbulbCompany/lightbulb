"""Memory tags and the run ``memory`` block (spec 07 §3.2, §5.2; wave2-M Task 2)."""

import re

import pytest

from core import tasks_schema as ts
from core.services import memory_tags
from core.services import runtime_runs as rr
from core.services.secret_shaped import redact

TASK_ID = "0b6c0d2e-4a8f-4c1e-9d55-7f3a2b1c0e99"
TAG = re.compile(r"[a-zA-Z0-9\s._:/=+@-]{1,128}")


def _task(**over) -> dict:
    base = {
        "taskId": TASK_ID,
        "lane": "shared",
        "ownerUserId": "user_sam",
        "specialist": "agent-finance",
        "audience": {"kind": "room", "userIds": [], "roomId": "room_launch"},
    }
    return {**base, **over}


def test_encode_thread_hashes_off_pattern_keys():
    hashed = memory_tags.encode_thread("pr:org/repo#42")
    assert hashed.startswith("h:") and len(hashed) == 42 and TAG.fullmatch(hashed)
    assert memory_tags.encode_thread("pr:org/repo#42") == hashed  # stable
    assert memory_tags.encode_thread("room:r1:m9") == "room:r1:m9"
    assert memory_tags.encode_thread("x" * 129).startswith("h:")  # over the 128 cap


def test_tags_drop_empty_and_off_pattern():
    out = memory_tags.tags(taskId=TASK_ID, threadId="pr:org/repo#42", specialist="bad#id", lane="", source=None)
    assert out == {"taskId": TASK_ID, "threadId": memory_tags.encode_thread("pr:org/repo#42")}
    assert all(TAG.fullmatch(v) for v in out.values())
    with pytest.raises(TypeError):
        memory_tags.tags(userId="user_sam")  # only the six known keys


def test_worker_block_private_task_uses_owner_member():
    block = memory_tags.block_for_worker(
        _task(lane="private", audience={"kind": "person", "userIds": ["user_sam"], "roomId": None})
    )
    assert block == {
        "lane": "private",
        "member": "user_sam",
        "audience": {"kind": "person", "userId": "user_sam"},
        "tags": {
            "taskId": TASK_ID,
            "threadId": ts.task_room_id(TASK_ID),
            "specialist": "agent-finance",
            "source": "worker",
        },
        "sync": "events",
    }


def test_worker_block_shared_room_task_has_room_audience():
    block = memory_tags.block_for_worker(_task())
    assert block["lane"] == "shared" and block["member"] is None
    assert block["audience"] == {"kind": "room", "roomId": "room_launch"}
    assert block["sync"] == "events" and block["tags"]["source"] == "worker"
    ws = memory_tags.block_for_worker(_task(audience={"kind": "workspace", "userIds": [], "roomId": None}))
    assert ws["audience"] == {"kind": "workspace"} and ws["member"] is None


def test_worker_block_private_task_without_owner_refuses():
    """A private task with no owner has no lane to write to: refuse rather than fall back to shared."""
    with pytest.raises(ValueError):
        memory_tags.block_for_worker(_task(lane="private", ownerUserId=None))


def test_router_block_private_lane_never_shared_audience():
    block = memory_tags.block_for_router("private", "user_priya", "room_dm")
    assert block["lane"] == "private" and block["member"] == "user_priya"
    assert block["audience"] == {"kind": "person", "userId": "user_priya"}
    with pytest.raises(ValueError):
        memory_tags.block_for_router("private", None, "room_dm")


def test_router_block_sync_none():
    shared = memory_tags.block_for_router("shared", "user_sam", "room_launch")
    assert shared["sync"] == "none" and shared["member"] is None
    assert shared["audience"] == {"kind": "room", "roomId": "room_launch"}
    assert memory_tags.block_for_router("private", "user_priya", None)["sync"] == "none"


def test_redact_masks_secret_shapes():
    key = "sk-" + "a1B2" * 8
    out = redact(f"use key {key} and AKIA{'Z' * 16} at https://bob:hunter22@example.com")
    assert key not in out and "AKIA" not in out and "hunter22" not in out
    assert out.count("[REDACTED]") == 3
    assert redact("the launch is on Monday") == "the launch is on Monday"


def test_run_request_wire_omits_memory_when_none():
    legacy = rr.RunRequest(input="go", instructions="be Finance").to_wire()
    assert "memory" not in legacy
    block = memory_tags.block_for_worker(_task())
    wire = rr.RunRequest(input="go", instructions="be Finance", memory=block).to_wire()
    assert wire["memory"] == block
    assert {k: v for k, v in wire.items() if k != "memory"} == legacy
