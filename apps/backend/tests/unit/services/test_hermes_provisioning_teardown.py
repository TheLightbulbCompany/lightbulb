"""F5 (predeploy review, M6-R38) -- ``hermes_provisioning.teardown_hermes_runtime``, extracted
from ``migrate_workspace_to_hermes.rollback``'s own AgentCore half so an account wipe on an
AgentCore row (``ecs_manager.delete_user_service``) shares the exact same teardown instead of
leaking the runtime/secret/S3 profiles the rollback path already knew to clean up.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import hermes_provisioning

ROW = {
    "owner_id": "org_a",
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1",
    "bedrock_gate_token_hash": "hash-1",
}


@pytest.fixture
def table():
    """The messaging table the ``TEARDOWN#`` rows live in (moto)."""
    from unittest.mock import patch

    import boto3
    from moto import mock_aws

    from core.repositories import messaging_repo

    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="msg",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch.object(messaging_repo, "_table", return_value=t):
            yield t


@pytest.fixture(autouse=True)
def no_browser_state(monkeypatch):
    monkeypatch.setattr("core.repositories.container_repo.get_browser_state", AsyncMock(return_value=None))


def test_delete_profile_prefix_objects_paginates_all_owner_scoped_prefixes(monkeypatch):
    class Paginator:
        def __init__(self):
            self.prefixes = []

        def paginate(self, *, Bucket, Prefix):
            assert Bucket == "test-bucket"
            self.prefixes.append(Prefix)
            if Prefix == "org_a/hermes/":
                return iter(
                    [
                        {"Contents": [{"Key": "org_a/hermes/agent_a/config.yaml"}]},
                        {"Contents": [{"Key": "org_a/hermes/auth.json"}]},
                    ]
                )
            if Prefix == "marketplace-installs/org_a/":
                return iter([{"Contents": [{"Key": "marketplace-installs/org_a/agent_a.json"}]}])
            if Prefix == "browser-state/org_a/":
                return iter([{"Contents": [{"Key": "browser-state/org_a/checkpoint.json"}]}])
            if Prefix == "org_a/browser-extensions/":
                return iter([{"Contents": [{"Key": "org_a/browser-extensions/supademo.zip"}]}])
            raise AssertionError(f"unexpected prefix: {Prefix}")

    class Client:
        def __init__(self):
            self.paginator = Paginator()
            self.deleted = []

        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return self.paginator

        def delete_objects(self, *, Bucket, Delete):
            self.deleted.extend((Bucket, item["Key"]) for item in Delete["Objects"])

    client = Client()
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "test-bucket")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda _name: client))

    hermes_provisioning._delete_profile_prefix_objects("org_a")

    assert client.paginator.prefixes == [
        "org_a/hermes/",
        "marketplace-installs/org_a/",
        "browser-state/org_a/",
        "org_a/browser-extensions/",
    ]
    assert client.deleted == [
        ("test-bucket", "org_a/hermes/agent_a/config.yaml"),
        ("test-bucket", "org_a/hermes/auth.json"),
        ("test-bucket", "marketplace-installs/org_a/agent_a.json"),
        ("test-bucket", "browser-state/org_a/checkpoint.json"),
        ("test-bucket", "org_a/browser-extensions/supademo.zip"),
    ]
    assert all("org_b" not in key for _, key in client.deleted)


@pytest.mark.asyncio
async def test_teardown_stops_session_deletes_runtime_revokes_hash_and_deletes_the_secret_in_order(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda *a: calls.append("stop_session"))
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda *a: calls.append("delete_runtime"))
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: calls.append("delete_secret"))
    delete_hashes = AsyncMock(side_effect=lambda hashes: calls.append("revoke_token"))
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", delete_hashes)
    delete_profiles = AsyncMock(side_effect=AssertionError("must not delete S3 profiles on a rollback"))
    monkeypatch.setattr(hermes_provisioning, "_delete_profile_prefix_objects", delete_profiles)

    await hermes_provisioning.teardown_hermes_runtime("org_a", container_row=ROW, delete_profiles=False)

    assert calls == ["stop_session", "delete_runtime", "revoke_token", "delete_secret"]
    delete_hashes.assert_awaited_once_with(["hash-1"])
    delete_profiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_teardown_tolerates_a_missing_runtime_and_hash(monkeypatch):
    """No ARN and no gate-token hash on the row (e.g. torn down right after prepare, before
    create) -- nothing to stop/delete/revoke on those fronts, but the boot secret is still
    force-deleted."""
    stop_session = AsyncMock(side_effect=AssertionError("nothing to stop"))
    delete_runtime = AsyncMock(side_effect=AssertionError("nothing to delete"))
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", stop_session)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", delete_runtime)
    delete_hashes = AsyncMock(side_effect=AssertionError("no hash to revoke"))
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", delete_hashes)
    deleted_secret = {}
    monkeypatch.setattr(
        hermes_provisioning, "_delete_boot_secret", lambda owner_id: deleted_secret.setdefault("owner_id", owner_id)
    )

    await hermes_provisioning.teardown_hermes_runtime(
        "org_a", container_row={"owner_id": "org_a"}, delete_profiles=False
    )

    stop_session.assert_not_called()
    delete_runtime.assert_not_called()
    delete_hashes.assert_not_called()
    assert deleted_secret["owner_id"] == "org_a"


@pytest.mark.asyncio
async def test_teardown_with_delete_profiles_true_also_deletes_the_s3_prefix(monkeypatch):
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: None)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", AsyncMock())
    deleted = {}
    monkeypatch.setattr(
        hermes_provisioning, "_delete_profile_prefix_objects", lambda owner_id: deleted.setdefault("owner_id", owner_id)
    )

    await hermes_provisioning.teardown_hermes_runtime("org_a", container_row=ROW, delete_profiles=True)

    assert deleted["owner_id"] == "org_a"


@pytest.mark.asyncio
async def test_teardown_is_best_effort_when_a_step_raises(monkeypatch):
    """An already-cold session, an already-gone runtime, a stranded gate-token row, or a
    transient Secrets Manager error must never stop the rest of the teardown from running."""
    calls: list[str] = []

    def _raise_stop(*a):
        calls.append("stop_session")
        raise RuntimeError("unknown session")

    def _raise_delete(*a):
        calls.append("delete_runtime")
        raise RuntimeError("already gone")

    async def _raise_revoke(hashes):
        calls.append("revoke_token")
        raise RuntimeError("ddb down")

    def _record_secret(owner_id):
        calls.append("delete_secret")

    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", _raise_stop)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", _raise_delete)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", _raise_revoke)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", _record_secret)

    await hermes_provisioning.teardown_hermes_runtime(
        "org_a", container_row=ROW, delete_profiles=False
    )  # must not raise

    assert calls == ["stop_session", "delete_runtime", "revoke_token", "delete_secret"]


@pytest.mark.asyncio
async def test_teardown_s3_delete_failure_is_also_best_effort(monkeypatch):
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: None)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", AsyncMock())

    def _raise(owner_id):
        raise RuntimeError("s3 unavailable")

    monkeypatch.setattr(hermes_provisioning, "_delete_profile_prefix_objects", _raise)

    # Must not raise.
    await hermes_provisioning.teardown_hermes_runtime("org_a", container_row=ROW, delete_profiles=True)


def _seam(monkeypatch, *, row, owed=None):
    """delete_workspace_runtime with every collaborator stubbed; returns the call log."""
    calls: list[str] = []
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr("core.repositories.container_repo.delete", AsyncMock(return_value=row))
    monkeypatch.setattr("core.repositories.container_repo.delete_browser_state", AsyncMock())

    async def _teardown(*a, **kw):
        calls.append("runtime")

    async def _forget(owner_id):
        calls.append(f"forget:{owner_id}")
        return owed  # the member key when the forget is owed (member parked)

    monkeypatch.setattr(hermes_provisioning, "teardown_hermes_runtime", _teardown)
    monkeypatch.setattr("core.services.workspace_memory.forget_workspace_authored", _forget)
    monkeypatch.setattr(hermes_provisioning, "_delete_profile_prefix_objects", lambda *a: None)

    async def _reap(*a, **kw):
        calls.append("reap")
        return {}

    monkeypatch.setattr("core.billing.bedrock_aip.reap_owner_bedrock_resources", _reap)
    monkeypatch.setattr("core.repositories.channel_link_repo.sweep_by_owner", AsyncMock(return_value=0))

    async def _wipe(owner_id):
        calls.append(f"memory:{owner_id}")

    monkeypatch.setattr("core.services.workspace_memory.delete_owner_memory", _wipe)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [ROW, None], ids=["with-row", "row-less-retry"])
async def test_the_seam_wipes_workspace_memory_with_the_profiles_even_on_a_row_less_retry(monkeypatch, row):
    """Workspace memory is the owner's data like the profiles are. A retried wipe arrives with the
    row already gone, so the memory wipe cannot live inside the ``if container`` branch. It runs
    AFTER the reap, which needs this call's deleted-row image."""
    calls = _seam(monkeypatch, row=row)
    await hermes_provisioning.delete_workspace_runtime("org_a", delete_profiles=True)
    # The cloud member forgets what it signed while it still runs; a row-less retry has no runtime.
    assert calls == (["forget:org_a", "runtime"] if row else []) + ["reap", "memory:org_a"]


@pytest.mark.asyncio
async def test_a_warm_member_forgets_and_the_runtime_goes_in_the_request(monkeypatch):
    calls = _seam(monkeypatch, row=ROW)
    report: dict = {}
    await hermes_provisioning.delete_workspace_runtime("org_a", delete_profiles=True, report=report)
    assert calls == ["forget:org_a", "runtime", "reap", "memory:org_a"] and report == {"runtime": "done"}
    assert not hermes_provisioning._runtime_teardowns


# A hang guard, not a speed assertion: each teardown task makes a dozen moto calls on worker
# threads, which took over 1 s on a loaded 2-core hosted runner under xdist (TimeoutError).
TASK_BUDGET_S = 15


def _parked(monkeypatch, answers):
    """A parked member: the seam schedules the teardown; ``answers`` is each forget try's outcome."""
    import asyncio

    calls = _seam(monkeypatch, row=ROW, owed="memberkey")
    monkeypatch.setattr(hermes_provisioning, "FORGET_RETRY_S", 0.01)
    monkeypatch.setattr(hermes_provisioning, "FORGET_WINDOW_S", 0.05)
    tries = iter(answers)

    async def _call(owner_id, method, params, *, target, **kw):
        assert (method, params, target) == ("memory.forget_author", {"author": "memberkey"}, ROW)
        ok = next(tries, False)
        calls.append(f"try:{ok}")
        if not ok:
            raise asyncio.TimeoutError
        return {"forgotten": 2}

    monkeypatch.setattr("core.services.container_rpc.call", _call)
    return calls


@pytest.mark.asyncio
async def test_a_parked_member_keeps_the_runtime_until_the_forget_lands(monkeypatch, table):
    import asyncio
    import time

    calls = _parked(monkeypatch, [False, False, True])
    report: dict = {}
    await hermes_provisioning.delete_workspace_runtime("org_a", delete_profiles=True, report=report)
    assert report == {"runtime": "scheduled"} and "runtime" not in calls
    (row,) = table.scan()["Items"]
    table.put_item(
        Item={"pk": "MEMWIPE#org_a", "sk": "A#memberkey"}
    )  # what the stubbed forget owed  # durable before the task runs: a restart leaves the sweep this
    assert row["pk"] == "TEARDOWN#" and row["ownerId"] == "org_a" and row["author"] == "memberkey"
    assert row["row"] == ROW and row["dueAt"] > time.time() + 150
    (task,) = hermes_provisioning._runtime_teardowns
    await asyncio.wait_for(task, TASK_BUDGET_S)
    assert calls[calls.index("try:True") :] == ["try:True", "runtime"]
    assert calls.count("try:False") == 2 and calls.index("memory:org_a") < calls.index("runtime")
    assert table.scan()["Items"] == []


@pytest.mark.asyncio
async def test_the_sweep_runs_a_past_due_teardown_and_deletes_its_row(monkeypatch, table):
    """The backend task stopped inside the window: the next sweep tick finishes the teardown."""
    import time

    from core.services import memory_api

    calls = _parked(monkeypatch, [True])
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    now = int(time.time())
    base = {"pk": "TEARDOWN#", "ownerId": "org_a", "row": ROW, "author": "memberkey", "expiresAt": now + 999}
    table.put_item(Item={**base, "sk": f"org_a#{ROW['agentcore_runtime_arn']}", "dueAt": now - 1})
    table.put_item(Item={**base, "sk": "org_a#not-yet", "dueAt": now + 999})
    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", "", raising=False)
    monkeypatch.setattr(memory_api.container_repo, "scan_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(memory_api.evaluator, "collect_due", AsyncMock(return_value=0))
    await memory_api.sweep_once()
    assert calls == ["try:True", "runtime"]
    assert [i["sk"] for i in table.scan()["Items"]] == ["org_a#not-yet"]


@pytest.mark.asyncio
async def test_a_member_that_never_answers_still_loses_its_runtime_after_the_window(monkeypatch, table):
    import asyncio

    calls = _parked(monkeypatch, [])
    await hermes_provisioning.delete_workspace_runtime("org_a", delete_profiles=True)
    (task,) = hermes_provisioning._runtime_teardowns
    await asyncio.wait_for(task, TASK_BUDGET_S)
    assert calls[-1] == "runtime" and "try:True" not in calls and calls.count("try:False") >= 2


@pytest.mark.asyncio
async def test_a_workspace_provisioned_again_mid_window_keeps_its_names(monkeypatch, table):
    """The new boot secret carries the old member key over: no late forget, and only the captured
    runtime and its gate token are deleted -- never the owner-keyed secret, state or profiles."""
    import asyncio

    calls = _parked(monkeypatch, [False, True])
    await hermes_provisioning.delete_workspace_runtime("org_a", delete_profiles=True)
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={**ROW, "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r2"}),
    )
    deleted, revoked = [], AsyncMock()
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", deleted.append)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", revoked)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: calls.append("secret"))
    (task,) = hermes_provisioning._runtime_teardowns
    await asyncio.wait_for(task, TASK_BUDGET_S)
    assert deleted == [ROW["agentcore_runtime_arn"]]
    revoked.assert_awaited_once_with(["hash-1"])
    assert "try:True" not in calls and "runtime" not in calls and "secret" not in calls


@pytest.mark.asyncio
async def test_a_reprovision_keeps_workspace_memory(monkeypatch):
    calls = _seam(monkeypatch, row=ROW)
    await hermes_provisioning.delete_workspace_runtime("org_a")
    assert "memory:org_a" not in calls and "forget:org_a" not in calls


@pytest.mark.asyncio
async def test_a_failed_memory_wipe_fails_the_seam(monkeypatch):
    """Records never expire: 'teardown incomplete, retry me', like a partial reap."""
    _seam(monkeypatch, row=ROW)
    monkeypatch.setattr(
        "core.services.workspace_memory.delete_owner_memory", AsyncMock(side_effect=RuntimeError("throttled"))
    )
    with pytest.raises(RuntimeError, match="throttled"):
        await hermes_provisioning.delete_workspace_runtime("org_a", delete_profiles=True)


@pytest.mark.asyncio
async def test_teardown_deletes_network_state_even_without_delete_profiles(monkeypatch):
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: None)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", AsyncMock())
    deleted: list[list[str]] = []
    monkeypatch.setattr(hermes_provisioning, "_delete_prefixes", lambda prefixes: deleted.append(prefixes))

    await hermes_provisioning.teardown_hermes_runtime("org_a", container_row=ROW, delete_profiles=False)

    assert deleted == [["org_a/network/"]]
