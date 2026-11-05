"""watchdog_repo against moto (wave2-W Task 1): deterministic findings, counters written before the action and
never reset by task churn, one winner per counter slot, the fleet cap, labels."""

import asyncio

from core import tasks_schema as ts
from core.repositories import tasks_repo, watchdog_repo as repo

from .conftest import WS

TASK = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"
DAY = "20261104"


def _finding(task_id=TASK, check="C1", n=1):
    return {"pk": ts.wd_finding_pk(DAY), "sk": ts.wd_finding_sk(WS, task_id, check, n), "check": check}


def _count(table, pk):
    return len(table.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": pk})["Items"])


async def test_finding_key_dedupes_across_ticks(table, metrics):
    first, wrote = await repo.put_finding(_finding())
    again, wrote_again = await repo.put_finding(_finding() | {"check": "C1", "p": 0.9})
    assert (wrote, wrote_again) == (True, False)
    assert again["findingId"] == first["findingId"] == f"{DAY}#{WS}#{TASK}#C1#1"
    assert "p" not in again
    assert _count(table, ts.wd_finding_pk(DAY)) == 1
    assert [m[0] for m in metrics] == ["watchdog.write"]
    assert repo.finding_key(first["findingId"]) == (first["pk"], first["sk"])


async def test_counter_row_written_before_action(table, metrics):
    assert await repo.spend_action(WS, TASK, "reconcile", limit=2, finding_id="f", by="watchdog") == 1
    row = table.get_item(Key={"pk": ts.ws_pk(WS), "sk": ts.wd_action_sk(TASK, "reconcile", 1)})["Item"]
    assert (row["outcome"], row["n"]) == ("pending", 1)
    assert await repo.spend_action(WS, TASK, "reconcile", limit=2, finding_id="f", by="watchdog") == 2
    assert await repo.spend_action(WS, TASK, "reconcile", limit=2, finding_id="f", by="watchdog") is None
    assert await repo.count_actions(WS, TASK, "reconcile") == 2
    await repo.finish_action(WS, TASK, "reconcile", 1, "failed", "boom")
    row = table.get_item(Key={"pk": ts.ws_pk(WS), "sk": ts.wd_action_sk(TASK, "reconcile", 1)})["Item"]
    assert (row["outcome"], row["reason"]) == ("failed", "boom")


async def test_task_state_change_does_not_reset_counter(table):
    task, _ = await tasks_repo.create_task(
        WS,
        {
            "taskId": TASK,
            "identifier": "LB-1",
            "title": "t",
            "status": "queued",
            "audience": {"kind": "workspace"},
            "lane": "shared",
            "triggerId": ts.trigger_id("chat", "m1"),
        },
    )
    for _ in range(2):
        await repo.spend_action(WS, TASK, "reconcile", limit=5, finding_id="f", by="watchdog")
    await tasks_repo.update_task_fields(WS, TASK, {"title": "churn"}, expected_version=1)
    table.delete_item(Key={"pk": task["pk"], "sk": task["sk"]})
    await tasks_repo.create_task(
        WS, {k: task[k] for k in ("taskId", "identifier", "title", "status", "audience", "lane", "triggerId")}
    )
    assert await repo.count_actions(WS, TASK, "reconcile") == 2


async def test_concurrent_spend_one_winner_per_n(table):
    got = await asyncio.gather(
        *(repo.spend_action(WS, TASK, "reconcile", limit=1, finding_id="f", by="watchdog") for _ in range(2))
    )
    assert sorted(got, key=lambda v: v is None) == [1, None]
    assert await repo.count_actions(WS, TASK, "reconcile") == 1


async def test_take_cap_refuses_at_cap(table):
    assert [await repo.take_cap("act", "2026110410", 2) for _ in range(3)] == [True, True, False]
    assert await repo.take_cap("act", "2026110411", 2) is True
    assert await repo.take_cap("act", "2026110412", 0) is False


async def test_label_overwrites_and_keeps_labelled_by(table):
    f, _ = await repo.put_finding(_finding())
    await repo.label(f["pk"], f["sk"], "fp", "user_admin")
    row = await repo.label(f["pk"], f["sk"], "tp", "user_admin2", note="real orphan")
    assert (row["label"], row["labelledBy"], row["labelNote"]) == ("tp", "user_admin2", "real orphan")
    await repo.set_hint(f["pk"], f["sk"], "person_stopped")
    assert (await repo.get_finding(f["pk"], f["sk"]))["hint"] == "person_stopped"


async def test_list_findings_unlabelled_pages(table):
    rows = [(await repo.put_finding(_finding(check=c)))[0] for c in ("C1", "C2", "C3")]
    await repo.label(rows[0]["pk"], rows[0]["sk"], "tp", "user_admin")
    seen, cursor, pages = [], None, 0
    while True:
        items, cursor = await repo.list_findings(DAY, unlabelled=True, limit=1, cursor=cursor)
        pages += bool(items)
        seen += items
        if not cursor:
            break
    assert pages == 2
    assert sorted(i["check"] for i in seen) == ["C2", "C3"]


async def test_watchdog_rows_invisible_to_task_tree(table):
    await tasks_repo.create_task(
        WS,
        {
            "taskId": TASK,
            "identifier": "LB-1",
            "title": "t",
            "status": "queued",
            "audience": {"kind": "workspace"},
            "lane": "shared",
            "triggerId": ts.trigger_id("chat", "m1"),
        },
    )
    await repo.spend_action(WS, TASK, "reconcile", limit=2, finding_id="f", by="watchdog")
    tree = await tasks_repo.get_task_tree(WS, TASK)
    assert tree["task"]["taskId"] == TASK
    assert tree["attempts"] == tree["subscriptions"] == tree["questions"] == []
