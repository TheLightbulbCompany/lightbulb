"""tasks_repo against a moto `tasks` table built from `tasks_schema` (keys + GSIs). Review focus 3 and 4
(wave-1 plan T): concurrent transitions have one winner; a retried create returns the existing row."""

import boto3
import pytest
from moto import mock_aws

from core import dynamodb, tasks_schema as ts
from core.repositories import tasks_repo as repo

WS = "org_1"


@pytest.fixture
def table(monkeypatch):
    """Own fixture, not conftest: T1 owns this directory's conftest.py (Temporal harness)."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        attrs = {"pk", "sk"} | {a for g in ts.GSIS for a in (g.pk_attr, g.sk_attr)}
        t = resource.create_table(
            TableName=dynamodb.table_name("tasks"),
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": g.name,
                    "KeySchema": [
                        {"AttributeName": g.pk_attr, "KeyType": "HASH"},
                        {"AttributeName": g.sk_attr, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for g in ts.GSIS
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield t


def _task(**over):
    return {
        "taskId": ts.new_task_id(),
        "identifier": "LB-1",
        "title": "Draft the memo",
        "status": "queued",
        "audience": {"kind": "workspace"},
        "lane": "shared",
        "triggerId": ts.trigger_id("chat", "m1"),
    } | over


async def _task_in(table, status: str) -> str:
    """A task sitting in `status` (written raw: some states are not reachable by one legal edge)."""
    row, _ = await repo.create_task(WS, _task())
    if status != "queued":
        table.update_item(
            Key={"pk": row["pk"], "sk": row["sk"]},
            UpdateExpression="SET #s = :s, statusKey = :k",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":k": ts.status_key(WS, status)},
        )
    return row["taskId"]


async def test_create_task_idempotent(table):
    t = _task()
    first, created = await repo.create_task(WS, t)
    again, created_again = await repo.create_task(WS, t | {"title": "Something else"})
    assert (created, created_again) == (True, False)
    assert again["title"] == "Draft the memo"
    assert again["version"] == first["version"] == 1


async def test_create_task_stamps_ws_channel(table, monkeypatch):
    """O9: the projector publishes `task.changed` only for rows that carry the workspace's channel segment."""
    from core.repositories import messaging_repo

    calls = []

    async def ensure_workspace_id(owner_id):
        calls.append(owner_id)
        return "11111111-2222-4333-8444-555555555555"

    monkeypatch.setattr(messaging_repo, "ensure_workspace_id", ensure_workspace_id)
    row, _ = await repo.create_task(WS, _task())
    assert row["wsChannel"] == "11111111-2222-4333-8444-555555555555" and calls == [WS]
    row, _ = await repo.create_task(WS, _task(taskId=ts.new_task_id(), wsChannel="given"))
    assert row["wsChannel"] == "given" and calls == [WS]  # a caller's channel is kept


async def test_attempt_question_effect_rows_stamp_ws_channel(table):
    """Audit #10: attempt, Question and Effect changes publish `task.changed` too, so their rows carry the channel;
    triggers and THREAD# rows (no task view) do not."""
    from core.repositories import effects_repo, messaging_repo, questions_repo

    channel = await messaging_repo.ensure_workspace_id(WS)
    task, _ = await repo.create_task(WS, _task())
    tid = task["taskId"]
    att, _ = await repo.put_attempt(
        WS, tid, 1, {"agentId": "a", "sessionId": "s", "status": "dispatched", "startedAt": "2026-11-05T00:00:00Z"}
    )
    q, _ = await questions_repo.open_question(
        WS,
        {
            "taskId": tid,
            "qid": "q-1",
            "kind": "approval",
            "subkind": "purchase",
            "title": "buy",
            "text": "acme.dev",
            "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
            "askedBy": {"kind": "worker", "attempt": 1},
            "answerers": ["sam"],
            "answererRule": "requester",
            "baseAnswerers": ["sam"],
        },
    )
    eff, _ = await effects_repo.put_effect(
        WS,
        {
            "idemKey": "k1",
            "taskId": tid,
            "attempt": 1,
            "tool": "mail",
            "kind": "mail.send",
            "operands": {},
            "operandsHash": "h",
            "state": "proposed",
        },
    )
    for row in (att, q, eff, await repo._get(WS, att["sk"]), await repo._get(WS, q["sk"])):
        assert row["wsChannel"] == channel
    trig, _ = await repo.put_trigger(
        WS,
        {
            "triggerId": "chat:m1",
            "source": "chat",
            "ref": {},
            "admission": "respond",
            "lane": "shared",
            "receivedAt": "2026-11-05T00:00:00Z",
        },
    )
    assert "wsChannel" not in trig


async def test_create_task_refuses_other_statuses(table):
    with pytest.raises(ValueError):
        await repo.create_task(WS, _task(status="working"))


# Sorted: frozenset order varies per process (hash seed), and xdist workers must collect the same ids.
EDGES = sorted((frm, to) for to, froms in repo.TRANSITIONS.items() for frm in froms if frm is not None)
NON_EDGES = [
    (frm, to)
    for to, froms in repo.TRANSITIONS.items()
    for frm in ("proposed", "queued", "working", "waiting", "idle", "resolved", "failed")
    if frm not in froms
]


@pytest.mark.parametrize(("frm", "to"), EDGES)
async def test_transition_allowed(table, frm, to):
    task_id = await _task_in(table, frm)
    row = await repo.transition_task(WS, task_id, to=to)
    assert row["status"] == to
    assert row["version"] == 2
    assert row["statusKey"] == f"{WS}#{to}"


@pytest.mark.parametrize(("frm", "to"), NON_EDGES)
async def test_transition_rejected(table, frm, to):
    task_id = await _task_in(table, frm)
    with pytest.raises(repo.IllegalTransition) as err:
        await repo.transition_task(WS, task_id, to=to)
    assert err.value.current == frm
    assert (await repo.get_task(WS, task_id))["version"] == 1


async def test_stopped_is_resolved_with_reason(table):
    task_id = await _task_in(table, "working")
    row = await repo.transition_task(WS, task_id, to="resolved", reason="stopped")
    assert (row["status"], row["statusReason"]) == ("resolved", "stopped")


async def test_version_conflict(table):
    task_id = await _task_in(table, "queued")
    await repo.update_task_fields(WS, task_id, {"title": "v2"}, expected_version=1)
    with pytest.raises(repo.VersionConflict):
        await repo.transition_task(WS, task_id, to="working", expected_version=1)


async def test_concurrent_transition_one_winner(table):
    """Two writers that read the same row: the second write's condition (stale version, or a from-status that
    is no longer current) is what refuses it. Sequential on purpose: moto has no write locking, so a real race
    under xdist can let both win; DynamoDB's conditional write is the guard being tested."""
    task_id = await _task_in(table, "queued")
    won = await repo.transition_task(WS, task_id, to="working", expected_version=1)
    assert won["version"] == 2
    with pytest.raises(repo.VersionConflict):  # same expected version as the winner
        await repo.transition_task(WS, task_id, to="working", expected_version=1)
    with pytest.raises(repo.IllegalTransition) as err:  # no version: the from-status set refuses it
        await repo.transition_task(WS, task_id, to="working")
    assert err.value.current == "working"
    assert (await repo.get_task(WS, task_id))["version"] == 2


async def test_update_task_fields_refuses_status(table):
    task_id = await _task_in(table, "queued")
    with pytest.raises(ValueError):
        await repo.update_task_fields(WS, task_id, {"status": "idle"}, expected_version=1)


async def test_subscribe_writes_sub_and_thread_atomically(table):
    task_id = await _task_in(table, "working")
    await repo.subscribe(WS, task_id, "room:r1", created_by="router")
    tree = await repo.get_task_tree(WS, task_id)
    assert [s["threadKey"] for s in tree["subscriptions"]] == ["room:r1"]
    assert await repo.task_for_thread(WS, "room:r1") == task_id

    other = await _task_in(table, "working")
    with pytest.raises(repo.TasksRepoError):
        await repo.subscribe(WS, other, "room:r1", created_by="person")
    assert (await repo.get_task_tree(WS, other))["subscriptions"] == []
    assert await repo.task_for_thread(WS, "room:r1") == task_id


async def test_subscribe_transaction_is_all_or_nothing(table, monkeypatch):
    """The THREAD row's condition is the guard: lose the race after the read and neither row lands."""
    task_id, other = await _task_in(table, "working"), await _task_in(table, "working")
    real_get = repo._get

    async def stale_read(ws, sk):  # the read misses the other task's THREAD row written just before
        return None if sk == ts.thread_sk("room:r2") else await real_get(ws, sk)

    await repo.subscribe(WS, other, "room:r2", created_by="router")
    monkeypatch.setattr(repo, "_get", stale_read)
    with pytest.raises(repo.TasksRepoError):
        await repo.subscribe(WS, task_id, "room:r2", created_by="router")
    monkeypatch.setattr(repo, "_get", real_get)
    assert (await repo.get_task_tree(WS, task_id))["subscriptions"] == []
    assert await repo.task_for_thread(WS, "room:r2") == other


async def test_unsubscribe_hides_thread(table):
    task_id = await _task_in(table, "working")
    await repo.subscribe(WS, task_id, "mail:t1", created_by="router")
    await repo.unsubscribe(WS, task_id, "mail:t1")
    assert await repo.task_for_thread(WS, "mail:t1") is None
    # a later task can take the thread over; the earlier one is kept in history
    nxt = await _task_in(table, "working")
    await repo.subscribe(WS, nxt, "mail:t1", created_by="router")
    assert await repo.task_for_thread(WS, "mail:t1") == nxt
    assert (await repo.batch_get(WS, [ts.thread_sk("mail:t1")]))[0]["history"] == [task_id]


async def test_put_trigger_dedup(table):
    trig = {
        "triggerId": ts.trigger_id("chat", "m9"),
        "source": "chat",
        "admission": "respond",
        "lane": "shared",
        "receivedAt": "2026-11-04T00:00:00+00:00",
    }
    _, created = await repo.put_trigger(WS, trig)
    _, again = await repo.put_trigger(WS, trig)
    assert (created, again) == (True, False)
    assert (await repo.get_trigger(WS, trig["triggerId"]))["source"] == "chat"


async def test_attempt_update_respects_allowed_status(table):
    task_id = await _task_in(table, "working")
    att, created = await repo.put_attempt(
        WS, task_id, 1, {"agentId": "a1", "sessionId": "s1", "status": "ended", "startedAt": "2026-11-04T00:00:00Z"}
    )
    assert created and att["runId"] == ts.run_id(WS, task_id, 1)
    assert (await repo.put_attempt(WS, task_id, 1, {**att, "status": "running"}))[1] is False
    with pytest.raises(repo.IllegalTransition):
        await repo.update_attempt(
            WS, task_id, 1, {"outcome": "completed"}, allowed_status=frozenset({"dispatched", "running"})
        )
    assert (await repo.get_attempt(WS, task_id, 1))["outcome"] is None


async def test_list_by_status_pages(table):
    for _ in range(3):
        await repo.create_task(WS, _task())
    page1, cursor = await repo.list_tasks_by_status(WS, "queued", limit=2)
    assert len(page1) == 2 and cursor
    page2, cursor2 = await repo.list_tasks_by_status(WS, "queued", limit=2, cursor=cursor)
    assert len(page2) == 1 and cursor2 is None
    assert {t["taskId"] for t in page1} & {t["taskId"] for t in page2} == set()


async def test_keys_come_from_tasks_schema(table):
    row, _ = await repo.create_task(WS, _task())
    got = await repo.get_task(WS, row["taskId"])
    assert (got["pk"], got["sk"]) == (ts.ws_pk(WS), ts.task_sk(row["taskId"]))
    assert got["workflowId"] == ts.task_workflow_id(WS, row["taskId"])


async def test_creations_first_request_wins_and_the_watchdog_caps_are_not_creations(table):
    """An app's Creation row names the request that made it; a later put keeps it. `CAP#act#…` rows share the prefix."""
    table.put_item(Item={"pk": ts.ws_pk(WS), "sk": ts.wd_cap_sk("act", "2026-11-05"), "type": "wdCap", "n": 1})
    first, made = await repo.put_creation(WS, {"capId": "app_1", "kind": "app", "taskId": "t1"})
    again, made_again = await repo.put_creation(WS, {"capId": "app_1", "kind": "app", "taskId": "t2"})
    assert (made, made_again, again["taskId"]) == (True, False, "t1")
    assert [(c["capId"], c["kind"], c["taskId"]) for c in await repo.list_creations(WS)] == [("app_1", "app", "t1")]


async def test_add_task_output_appends_once_and_keeps_the_newest(table, monkeypatch):
    """UX slice 5: what a task made stays on its row for every status; a retried write is a no-op."""
    row, _ = await repo.create_task(WS, _task())
    tid = row["taskId"]
    out = {"id": "effect:e1", "kind": "mail", "title": "Send 'Q3' to a@b.c", "ref": {"to": "a@b.c"}}
    assert await repo.add_task_output(WS, tid, out) is True
    assert await repo.add_task_output(WS, tid, out) is False  # same id: already there
    task = await repo.get_task(WS, tid)
    ((stored,),) = [task["outputs"]]
    assert stored["id"] == "effect:e1" and stored["createdAt"] and task["version"] == row["version"] + 1
    monkeypatch.setattr(repo, "MAX_OUTPUTS", 2)
    for i in range(3):
        await repo.add_task_output(WS, tid, {"id": f"file:{i}", "kind": "file", "title": f"f{i}.txt"})
    assert [o["id"] for o in (await repo.get_task(WS, tid))["outputs"]] == ["file:1", "file:2"]
    assert await repo.add_task_output(WS, "no-such-task", out) is False


async def test_answered_since_reads_only_answered_questions_in_the_window(table):
    """The trial recap's read: answered Questions at or after the cut, never open ones or other task rows."""
    from core.repositories import questions_repo

    task, _ = await repo.create_task(WS, _task())
    tid = task["taskId"]
    for qid in ("q-1", "q-2"):
        await questions_repo.open_question(
            WS,
            {
                "taskId": tid,
                "qid": qid,
                "kind": "approval",
                "title": qid,
                "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
                "askedBy": {"kind": "worker", "attempt": 1},
                "answerers": ["sam"],
            },
        )
    await questions_repo.record_answer(
        WS, tid, "q-1", option_id="deny", trigger_id=None, by="sam", client_answer_id="c1", expected_version=None
    )
    got = await questions_repo.answered_since(WS, "2000-01-01T00:00:00+00:00")
    assert [(q["qid"], q["answer"]["optionId"]) for q in got] == [("q-1", "deny")]
    assert await questions_repo.answered_since(WS, "2999-01-01T00:00:00+00:00") == []
