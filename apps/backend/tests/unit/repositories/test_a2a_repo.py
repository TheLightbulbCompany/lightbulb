import asyncio

from core.repositories import a2a_repo


def _task(tid, state="submitted"):
    return {"id": tid, "org": "org_1", "agent": "friday", "state": state, "createdAt": 1, "updatedAt": 1}


def test_put_get_update_task(repos):
    asyncio.run(a2a_repo.put_task("org_1", _task("t1")))
    assert asyncio.run(a2a_repo.get_task("org_1", "t1"))["state"] == "submitted"
    assert asyncio.run(a2a_repo.get_task("org_2", "t1")) is None
    asyncio.run(a2a_repo.update_task("org_1", "t1", state="working", extra={"room_id": "r1"}))
    got = asyncio.run(a2a_repo.get_task("org_1", "t1"))
    assert (got["state"], got["room_id"], got["agent"]) == ("working", "r1", "friday")
    assert got["updatedAt"] >= 1


def test_records_append_in_order_and_once(repos):
    assert asyncio.run(a2a_repo.append_record("org_1", "t1", {"kind": "a"})) == 1
    assert asyncio.run(a2a_repo.append_record("org_1", "t1", {"kind": "b"})) == 2
    assert asyncio.run(a2a_repo.append_record("org_1", "t2", {"kind": "c"})) == 1
    recs = asyncio.run(a2a_repo.list_records("org_1", "t1"))
    assert [r["kind"] for r in recs] == ["a", "b"]
    assert [r["sk"] for r in recs] == ["RECORD#t1#000001", "RECORD#t1#000002"]


def test_list_held_tasks_filters_by_state(repos):
    for tid, st in (("t1", "working"), ("t2", "held"), ("t3", "completed")):
        asyncio.run(a2a_repo.put_task("org_1", _task(tid, st)))
    asyncio.run(a2a_repo.put_task("org_2", _task("t4", "held")))
    assert [t["id"] for t in asyncio.run(a2a_repo.list_held_tasks("org_1"))] == ["t2"]


def test_complete_task_moves_working_to_completed_once(repos):
    asyncio.run(a2a_repo.put_task("org_1", _task("t1", state="working")))
    art = {"artifactId": "m1", "parts": [{"text": "hi"}]}
    assert asyncio.run(a2a_repo.complete_task("org_1", "t1", artifact=art, record="jws1")) is True
    # A racing or retried reply finds the task no longer working.
    assert (
        asyncio.run(a2a_repo.complete_task("org_1", "t1", artifact={**art, "artifactId": "m2"}, record="jws2")) is False
    )
    got = asyncio.run(a2a_repo.get_task("org_1", "t1"))
    assert (got["state"], got["artifact"]["artifactId"], got["record"]) == ("completed", "m1", "jws1")
