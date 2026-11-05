"""Workspace META off ``paperclip-companies`` (wave1-R Task 6): the copy script and the tasks-store scans.
Review focus 4: a write landing between copy and flip is carried by the second pass, never by overwriting
newer META."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core import tasks_schema as ts
from core.repositories import paperclip_repo as pr
from core.repositories.paperclip_repo import PaperclipRepo, PaperclipWorkspace
from scripts import copy_workspace_meta as cwm
from tests.unit.repositories.test_paperclip_repo import create_companies_table, create_tasks_table

SKIPPED = ("company_id", "agent_keys", "agent_cost_remainder", "automation_paused_at", "automation_pause_token")


@pytest.fixture
def tables():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        old, tasks = create_companies_table(resource, cwm.OLD_TABLE), create_tasks_table(resource)
        with (
            patch("core.dynamodb._table_prefix", ""),
            patch("core.dynamodb._dynamodb_resource", resource),
            patch.object(cwm.settings, "ENVIRONMENT", "dev"),
            patch.object(cwm.settings, "SUPER_AGENT_ENV_ENABLED", True),
        ):
            yield old, tasks


def _repo(on_tasks: bool) -> PaperclipRepo:
    repo = PaperclipRepo(table_name=cwm.OLD_TABLE)
    repo._meta_on_tasks = lambda: on_tasks
    return repo


async def _seed(org: str = "org_a", **over) -> PaperclipRepo:
    """A full old-store workspace: onboarding, members, roster, an agent key, a pause record, a pending brief."""
    repo = _repo(False)
    now = datetime.now(timezone.utc)
    ws = PaperclipWorkspace(
        org_id=org,
        company_id="co_1",
        status="active",
        created_at=now,
        updated_at=now,
        provisioner_user_id="user_p",
        member_user_ids={"user_p", "user_q"},
        starter_agent_ids={"ceo": "a-ceo", "customer_operations": "a-ops"},
        seeded_agent_ids=["a-old"],
        onboarding_generation="01G",
        funnel_state="not_started",
        **over,
    )
    await repo.put_workspace(ws)
    await repo.claim_first_contact(org, onboarding_generation="01G")
    await repo.put_agent_key(org, "a-ceo", "pcp_live")
    await repo.add_agent_cost_remainder(org, "a-ceo", 7)
    await repo.record_paused_automation(org, agent_ids=["a-ceo"], routine_ids=["r1"], token="t")
    await repo.put_pending_brief(org, path="new", user_input="idea", detail=None, brief_text="b", submitted_by="m")
    return repo


def _old_rows(old) -> dict[str, dict]:
    return {i["user_id"]: i for i in old.scan()["Items"]}


def _meta(tasks, org: str = "org_a") -> dict:
    return tasks.get_item(Key={"pk": ts.ws_pk(org), "sk": ts.META_SK})["Item"]


async def test_copy_round_trip(tables, capsys):
    old, _ = tables
    await _seed()
    await cwm.main(["--all"])  # dry run writes nothing
    assert sorted(await cwm.verify(None)) == ["org_a META: missing", "org_a PENDING_BRIEF: missing"]

    assert await cwm.main(["--all", "--apply"]) == 0
    assert await cwm.verify(None) == []
    assert await cwm.main(["--all", "--verify"]) == 0

    # The repo on tasks reads the same workspace and brief the old store held.
    before = await _repo(False).get_workspace("org_a")
    assert await _repo(True).get_workspace("org_a") == before
    assert await _repo(True).get_pending_brief("org_a") == await _repo(False).get_pending_brief("org_a")

    source = _old_rows(old)
    for key in source:
        old.delete_item(Key={"user_id": key})
    assert await cwm.main(["--all", "--reverse", "--apply"]) == 0
    restored = _old_rows(old)
    assert restored == {k: {a: v for a, v in row.items() if not pr.old_row_only(a)} for k, row in source.items()}


async def test_skipped_attrs_never_copied(tables):
    _, tasks = tables
    await _seed()
    assert await cwm.main(["--all", "--apply"]) == 0
    meta = _meta(tasks)
    assert meta["metaKind"] == "workspace" and meta["workBackend"] == "paperclip"
    assert meta["firstContactState"] == "sending" and meta["memberUserIds"] == {"user_p", "user_q"}
    assert not [k for k in meta if pr.old_row_only(pr._snake(k))]
    assert not {"companyId", "agentKeys", "agentCostRemainder", "automationPauseToken"} & set(meta)


async def test_tasks_store_keeps_paperclip_attrs_on_the_old_row(tables):
    """The readers of the skipped attrs (every Paperclip call's company_id, the pause fence, agent keys)
    keep working on tasks: those attrs stay on the old row and get_workspace reads them back."""
    old, tasks = tables
    repo = _repo(True)
    now = datetime.now(timezone.utc)
    await repo.put_workspace(
        PaperclipWorkspace(org_id="org_n", company_id="co_n", status="provisioning", created_at=now, updated_at=now)
    )
    assert await repo.update_status("org_n", status="active")
    assert await repo.record_paused_automation("org_n", agent_ids=["a"], routine_ids=[], token="t")
    assert await repo.put_agent_key("org_n", "a", "k") == "k"
    ws = await repo.get_workspace("org_n")
    assert (ws.company_id, ws.status, ws.automation_pause_token) == ("co_n", "active", "t")
    assert await repo.pause_token_current("org_n", "t")
    assert await repo.org_company_ids("org_n") == {"co_n"}
    assert (await repo.find_workspace_by_company("co_n")).org_id == "org_n"
    meta = _meta(tasks, "org_n")
    assert not [k for k in meta if pr.old_row_only(pr._snake(k))]
    assert set(old.get_item(Key={"user_id": "org_n"})["Item"]) == {
        "user_id", "org_id", "status", "company_id", "agent_keys", "automation_paused_at",
        "automation_pause_token", "automation_paused_agent_ids", "automation_paused_routine_versions",
    }  # fmt: skip


async def test_copy_apply_takes_newer_old_row_only(tables, capsys):
    old, tasks = tables
    await _seed()
    assert await cwm.main(["--all", "--apply"]) == 0
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

    # META newer (a write after the flip): a second pass leaves it, verify reports meta_newer and passes.
    key = {"pk": ts.ws_pk("org_a"), "sk": ts.META_SK}
    tasks.update_item(Key=key, UpdateExpression="SET lastError = :e, updatedAt = :t",
                      ExpressionAttributeValues={":e": "meta wins", ":t": later})  # fmt: skip
    assert await cwm.copy_forward(None, apply=True) == []
    assert _meta(tasks)["lastError"] == "meta wins"
    assert await cwm.verify(None) == ["org_a META: meta_newer"]
    assert await cwm.main(["--all", "--verify"]) == 0

    # Old row newer (a funnel claim between copy and flip): the second pass carries it.
    even_later = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    old.update_item(Key={"user_id": "org_a"}, UpdateExpression="SET funnel_state = :s, updated_at = :t",
                    ExpressionAttributeValues={":s": "submitted", ":t": even_later})  # fmt: skip
    assert await cwm.verify(None) != []
    assert await cwm.copy_forward(None, apply=True) == ["org_a META: copied"]
    meta = _meta(tasks)
    assert meta["funnelState"] == "submitted" and "lastError" not in meta
    assert await cwm.main(["--all", "--verify"]) == 0

    # A drifted field on the same timestamp is a failure.
    tasks.update_item(Key=key, UpdateExpression="SET funnelState = :s", ExpressionAttributeValues={":s": "complete"})
    assert await cwm.verify("org_a") == ["org_a META: field:funnel_state"]
    assert await cwm.main(["--owner-id", "org_a", "--verify"]) == 1


async def test_scans_use_ws_meta_gsi(tables):
    repo = _repo(True)
    now = datetime.now(timezone.utc)
    for org, status, purge in (("org_a", "active", None), ("org_d", "disabled", now - timedelta(days=1))):
        await repo.put_workspace(
            PaperclipWorkspace(
                org_id=org, company_id=f"co_{org}", status=status, created_at=now, updated_at=now,
                member_user_ids={"user_1"}, scheduled_purge_at=purge,
            )
        )  # fmt: skip
    await repo.put_pending_brief("org_a", path="p", user_input=None, detail=None, brief_text=None, submitted_by="u")

    calls = []
    real = pr.run_in_thread

    async def spy(func, *args, **kwargs):
        calls.append((func.__name__, kwargs.get("IndexName")))
        return await real(func, *args, **kwargs)

    with patch.object(pr, "run_in_thread", spy):
        assert await repo.list_active_workspace_members() == {"org_a": {"user_1"}}
        assert [w.org_id for w in await repo.scan_purge_due(now)] == ["org_d"]
    assert ("query", "ws-meta") in calls
    assert not [c for c in calls if c[0] == "scan" or c[1] == "by-status-purge-at"]


async def test_refuses_prod_and_env_master_off(tables):
    with patch.object(cwm.settings, "ENVIRONMENT", "prod"):
        assert await cwm.main(["--all", "--apply"]) == 2
    with patch.object(cwm.settings, "SUPER_AGENT_ENV_ENABLED", False):
        assert await cwm.main(["--all", "--verify"]) == 2


async def test_reverse_never_overwrites_an_old_row_newer_than_meta(tables):
    """Rollback after a revert: a write that landed on the old row after the copy wins."""
    old, tasks = tables
    await _seed()
    assert await cwm.main(["--all", "--apply"]) == 0
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    old.update_item(
        Key={"user_id": "org_a"},
        UpdateExpression="SET updated_at = :u, funnel_state = :f",
        ExpressionAttributeValues={":u": later, ":f": "completed"},
    )
    lines = await cwm.copy_reverse(None, apply=True)
    assert "org_a: skipped (old row newer)" in lines
    row = old.get_item(Key={"user_id": "org_a"})["Item"]
    assert (row["updated_at"], row["funnel_state"]) == (later, "completed")
