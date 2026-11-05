"""automations_repo: Schedule + HOOK# rows with conditional writes (wave-2 lane A, Task 3)."""

from datetime import datetime, timedelta, timezone

import pytest

from core import tasks_schema as ts
from core.repositories import automations_repo as repo
from core.repositories.tasks_repo import IllegalTransition, VersionConflict

WS = "org_1"


def _sched(**over):
    return {
        "scheduleId": "s1",
        "name": "Monday signups",
        "ownerUserId": "user_sam",
        "cron": ["0 9 * * 1"],
        "tz": "America/New_York",
        "policyText": "Post signups here",
        "budgetPerRunUsd": 1.0,
    } | over


def _hook(**over):
    return {
        "publicId": "pub_1",
        "scheduleId": "s1",
        "provider": "stripe",
        "signingMode": "hmac_sha256",
        "secretRef": "hook-pub_1",
    } | over


async def _active(sid="s1"):
    await repo.create_schedule(WS, _sched(scheduleId=sid))
    return await repo.set_status(WS, sid, "active", reason=None, allowed_from=frozenset({"draft"}))


async def test_keys_come_from_tasks_schema(table):
    row, _ = await repo.create_schedule(WS, _sched())
    hook, _ = await repo.put_hook(WS, _hook())
    assert (row["pk"], row["sk"]) == (ts.ws_pk(WS), ts.schedule_sk("s1"))
    assert hook["sk"] == ts.hook_sk("pub_1")


async def test_create_schedule_idempotent(table):
    first, created = await repo.create_schedule(WS, _sched())
    again, created_again = await repo.create_schedule(WS, _sched(name="Other"))
    assert (created, created_again) == (True, False)
    assert again["name"] == "Monday signups"
    assert first["status"] == "draft" and first["runTaskIds"] == []
    assert await repo.get_schedule(WS, "s1") == again


async def test_list_schedules_only_schedules(table):
    await repo.create_schedule(WS, _sched())
    await repo.create_schedule(WS, _sched(scheduleId="s2"))
    await repo.put_hook(WS, _hook())
    await repo.create_schedule("org_2", _sched(scheduleId="s3"))
    assert sorted(r["scheduleId"] for r in await repo.list_schedules(WS)) == ["s1", "s2"]


async def test_claim_run_only_when_active_and_free(table):
    await repo.create_schedule(WS, _sched())
    assert await repo.claim_run(WS, "s1", "t1") is False  # draft
    await repo.set_status(WS, "s1", "active", reason=None, allowed_from=frozenset({"draft"}))
    assert await repo.claim_run(WS, "s1", "t1") is True
    assert await repo.claim_run(WS, "s1", "t1") is True  # an Activity retry with the same task id
    assert await repo.claim_run(WS, "s1", "t2") is False  # overlap
    assert await repo.claim_run(WS, "missing", "t1") is False


async def test_closed_run_cannot_be_claimed_again(table):
    """A retried fire after its fast run already closed must not re-arm the slot (it would never close)."""
    await _active()
    assert await repo.claim_run(WS, "s1", "t1")
    await repo.close_run(WS, "s1", "t1", failed=False)
    assert await repo.claim_run(WS, "s1", "t1") is False
    assert "activeRunTaskId" not in await repo.get_schedule(WS, "s1")
    assert await repo.claim_run(WS, "s1", "t2") is True


async def test_close_run_trims_history_to_50(table):
    await _active()
    for i in range(52):
        assert await repo.claim_run(WS, "s1", f"t{i}")
        row = await repo.close_run(WS, "s1", f"t{i}", failed=False)
    assert len(row["runTaskIds"]) == 50
    assert row["runTaskIds"][0] == "t51" and row["runTaskIds"][-1] == "t2"
    assert "activeRunTaskId" not in row


async def test_close_run_counts_failures_and_resets_on_success(table):
    await _active()
    for i, failed in enumerate([True, True, False, True]):
        await repo.claim_run(WS, "s1", f"t{i}")
        row = await repo.close_run(WS, "s1", f"t{i}", failed=failed)
    assert row["consecutiveFailures"] == 1
    await repo.claim_run(WS, "s1", "t9")
    assert (await repo.close_run(WS, "s1", "t9", failed=True))["consecutiveFailures"] == 2


async def test_close_run_with_an_unknown_outcome_keeps_the_count(table):
    await _active()
    for i in range(2):
        await repo.claim_run(WS, "s1", f"t{i}")
        await repo.close_run(WS, "s1", f"t{i}", failed=True)
    await repo.claim_run(WS, "s1", "t9")
    row = await repo.close_run(WS, "s1", "t9", failed=None)
    assert row["consecutiveFailures"] == 2 and row["runTaskIds"][0] == "t9"


async def test_close_run_retry_is_idempotent(table):
    await _active()
    await repo.claim_run(WS, "s1", "t1")
    first = await repo.close_run(WS, "s1", "t1", failed=True)
    again = await repo.close_run(WS, "s1", "t1", failed=True)
    assert again["consecutiveFailures"] == first["consecutiveFailures"] == 1
    assert again["runTaskIds"] == ["t1"]


async def test_close_run_other_task_conflicts(table):
    await _active()
    await repo.claim_run(WS, "s1", "t1")
    with pytest.raises(VersionConflict):
        await repo.close_run(WS, "s1", "t2", failed=False)


async def test_update_stale_version_conflicts(table):
    row, _ = await repo.create_schedule(WS, _sched())
    new = await repo.update_schedule(WS, "s1", {"cron": ["0 10 * * 1"]}, expected_version=row["version"])
    assert new["cron"] == ["0 10 * * 1"] and new["version"] == 2
    with pytest.raises(VersionConflict):
        await repo.update_schedule(WS, "s1", {"name": "x"}, expected_version=1)
    with pytest.raises(ValueError):
        await repo.update_schedule(WS, "s1", {"status": "active"}, expected_version=2)
    # snake_case spellings of writer-owned fields are refused too, not written as stray attributes
    for bad in ("active_run_task_id", "run_task_ids", "consecutive_failures", "paused_reason", "schedule_id"):
        with pytest.raises(ValueError):
            await repo.update_schedule(WS, "s1", {bad: "x"}, expected_version=2)


async def test_update_schedule_snake_case_fields_are_stored_camel(table):
    row, _ = await repo.create_schedule(WS, _sched())
    new = await repo.update_schedule(WS, "s1", {"deliver_to": "room:r1"}, expected_version=row["version"])
    assert new["deliverTo"] == "room:r1" and "deliver_to" not in new


async def test_set_status_allowed_from_and_reason(table):
    await repo.create_schedule(WS, _sched())
    with pytest.raises(IllegalTransition):
        await repo.set_status(WS, "s1", "paused", reason=None, allowed_from=frozenset({"active"}))
    await repo.set_status(WS, "s1", "active", reason=None, allowed_from=frozenset({"draft"}))
    row = await repo.set_status(WS, "s1", "paused_system", reason="failures", allowed_from=frozenset({"active"}))
    assert (row["status"], row["pausedReason"]) == ("paused_system", "failures")
    row = await repo.set_status(WS, "s1", "active", reason=None, allowed_from=frozenset({"paused_system"}))
    assert "pausedReason" not in row


async def test_delete_schedule_only_if_status(table):
    await repo.create_schedule(WS, _sched())
    await repo.set_status(WS, "s1", "active", reason=None, allowed_from=frozenset({"draft"}))
    assert await repo.delete_schedule(WS, "s1", only_if_status="draft") is False  # activated since the scan
    assert await repo.get_schedule(WS, "s1") is not None
    await repo.set_status(WS, "s1", "paused", reason=None, allowed_from=frozenset({"active"}))
    assert await repo.delete_schedule(WS, "s1", only_if_status="paused") is True
    assert await repo.get_schedule(WS, "s1") is None


async def test_delete_schedule(table):
    await repo.create_schedule(WS, _sched())
    await repo.delete_schedule(WS, "s1")
    await repo.delete_schedule(WS, "s1")  # already gone is fine
    assert await repo.get_schedule(WS, "s1") is None


async def test_put_hook_refuses_none_mode(table):
    with pytest.raises(ValueError):
        await repo.put_hook(WS, _hook(signingMode="none"))
    _, created = await repo.put_hook(WS, _hook())
    _, again = await repo.put_hook(WS, _hook(provider="github"))
    assert (created, again) == (True, False)
    assert (await repo.get_hook(WS, "pub_1"))["provider"] == "stripe"
    assert await repo.get_hook("org_2", "pub_1") is None  # the key binds the owner


async def test_update_hook(table):
    hook, _ = await repo.put_hook(WS, _hook())
    row = await repo.update_hook(WS, "pub_1", {"armed": True, "provider_hook_id": "we_1"}, expected_version=1)
    assert row["armed"] is True and row["providerHookId"] == "we_1" and row["version"] == 2
    with pytest.raises(VersionConflict):  # a concurrent writer already moved it on
        await repo.update_hook(WS, "pub_1", {"armed": False}, expected_version=1)
    for bad in ({"publicId": "other"}, {"sig_fail_count": 0}, {"PublicId": "x"}, {"SigFailCount": 1}, {"Armed": True}):
        with pytest.raises(ValueError):
            await repo.update_hook(WS, "pub_1", bad, expected_version=2)
    with pytest.raises(VersionConflict):
        await repo.update_hook(WS, "missing", {"armed": True}, expected_version=1)


async def test_note_sig_failure_writes_once_a_minute(table):
    await repo.put_hook(WS, _hook())
    t0 = datetime(2027, 1, 4, 14, 0, tzinfo=timezone.utc)
    assert await repo.note_sig_failure(WS, "pub_1", now=t0) is True
    assert await repo.note_sig_failure(WS, "pub_1", now=t0 + timedelta(seconds=59)) is False
    assert await repo.note_sig_failure(WS, "pub_1", now=t0 + timedelta(seconds=61)) is True
    row = await repo.get_hook(WS, "pub_1")
    assert row["sigFailCount"] == 2
    assert row["lastSigFailAt"] == (t0 + timedelta(seconds=61)).isoformat()
    assert await repo.note_sig_failure(WS, "missing", now=t0) is False
