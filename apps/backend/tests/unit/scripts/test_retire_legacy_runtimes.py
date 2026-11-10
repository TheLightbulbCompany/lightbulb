"""The legacy-runtime retirement tool refuses every unsafe ARN and mutates nothing on a dry run."""

import json
from unittest.mock import MagicMock

import pytest

from scripts import retire_legacy_runtimes as mod

NOW = 1_800_000_000.0
OWNER = "org_3JCsS1jjkMfABApmwvSwN3b112c"
NAME = mod.runtime_name_for(OWNER, "prod")
ARN = f"arn:aws:bedrock-agentcore:{mod.REGION}:{mod.ACCOUNT}:runtime/{NAME}-LMmcth5Rf2"
POOL = f"arn:aws:bedrock-agentcore:{mod.REGION}:{mod.ACCOUNT}:runtime/lightbulb_prod_hermes_pool-4EZsNl7K5Z"


class NotFound(Exception):
    pass


@pytest.fixture
def world():
    control, ddb, cloudwatch = MagicMock(), MagicMock(), MagicMock()
    control.exceptions.ResourceNotFoundException = NotFound
    control.get_agent_runtime.return_value = {
        "agentRuntimeArn": ARN,
        "status": "READY",
        "environmentVariables": {"ISOL8_OWNER_ID": OWNER},
    }
    ddb.get_item.return_value = {"Item": {"owner_id": {"S": OWNER}, "status": {"S": "running"}}}
    ddb.scan.return_value = {"Items": []}
    cloudwatch.get_metric_data.return_value = {"MetricDataResults": [{"StatusCode": "Complete", "Values": []}]}
    session = MagicMock()
    session.client.side_effect = lambda name, **_: {
        "bedrock-agentcore-control": control,
        "dynamodb": ddb,
        "cloudwatch": cloudwatch,
    }[name]
    return session, control, ddb, cloudwatch


def _refusals(world, arns=(ARN,), apply=True):
    session, control, *_ = world
    receipt = mod.run(session, "prod", apply, list(arns), now=NOW)
    return receipt, [r for v in receipt["verdicts"] for r in v["refusals"]]


def test_safe_runtime_is_deleted_only_on_apply(world):
    session, control, *_ = world
    dry = mod.run(session, "prod", False, [ARN], now=NOW)
    assert dry["ok"] and dry["deleted"] == []
    control.delete_agent_runtime.assert_not_called()

    applied = mod.run(session, "prod", True, [ARN], now=NOW)
    assert applied["deleted"] == [ARN]
    control.delete_agent_runtime.assert_called_once_with(agentRuntimeId=ARN.rsplit("/", 1)[-1])


def test_dry_run_makes_no_mutating_call(world):
    session, control, ddb, cloudwatch = world
    mod.run(session, "prod", False, [ARN], now=NOW)
    called = {c[0] for m in (control, ddb, cloudwatch) for c in m.method_calls}
    assert called <= {"get_agent_runtime", "get_item", "scan", "get_metric_data"}


def test_pool_is_refused_by_name_before_any_read(world):
    session, control, *_ = world
    receipt, refusals = _refusals(world, [POOL])
    assert refusals == ["Hermes pool runtime"]
    control.get_agent_runtime.assert_not_called()
    control.delete_agent_runtime.assert_not_called()


def test_other_env_and_foreign_arns_are_refused(world):
    dev = ARN.replace("lb_prod_", "lb_dev_")
    foreign = ARN.replace(mod.ACCOUNT, "111111111111")
    receipt, refusals = _refusals(world, [dev, foreign])
    assert not receipt["ok"] and receipt["deleted"] == []
    assert refusals == ["not a prod per-owner runtime name", "not a runtime ARN in this account/region"]


def test_not_in_allowlist_is_never_touched(tmp_path):
    path = tmp_path / "allow.json"
    path.write_text(json.dumps({"dev": [], "prod": []}))
    assert mod.load_allowlist("prod", path) == []
    path.write_text(json.dumps({"dev": [], "prod": [ARN, ARN]}))
    with pytest.raises(ValueError):
        mod.load_allowlist("prod", path)


def test_checked_in_allowlist_excludes_pools():
    for env in ("dev", "prod"):
        assert not [a for a in mod.load_allowlist(env) if "pool" in a]


@pytest.mark.parametrize("field", ["agentcore_runtime_arn", "agentcore_shared_runtime_arn"])
def test_owner_row_still_pointing_refuses(world, field):
    world[2].get_item.return_value = {"Item": {"owner_id": {"S": OWNER}, field: {"S": ARN}}}
    receipt, refusals = _refusals(world)
    assert refusals == ["owner row still points at this runtime"]
    world[1].delete_agent_runtime.assert_not_called()


def test_active_session_refuses(world):
    world[2].scan.return_value = {
        "Items": [
            {"owner_id": {"S": f"__session__#{OWNER}"}, "arn": {"S": ARN}, "warm_until": {"N": str(int(NOW) - 60)}}
        ]
    }
    _, refusals = _refusals(world)
    assert refusals == [f"session __session__#{OWNER} active on this runtime"]
    world[1].delete_agent_runtime.assert_not_called()


def test_task_session_wake_lease_refuses_and_other_owner_rows_are_ignored(world):
    world[2].scan.return_value = {
        "Items": [
            {"owner_id": {"S": f"__session__#{OWNER}#task#t1"}, "waking_until": {"N": str(int(NOW) + 30)}},
            {"owner_id": {"S": f"__session__#{OWNER}X"}, "arn": {"S": ARN}, "warm_until": {"N": str(int(NOW))}},
        ]
    }
    _, refusals = _refusals(world)
    assert refusals == [f"live wake lease on __session__#{OWNER}#task#t1"]


def test_old_session_estimate_does_not_refuse(world):
    stale = int(NOW) - mod.ACTIVE_WINDOW_S - 1
    world[2].scan.return_value = {
        "Items": [{"owner_id": {"S": f"__session__#{OWNER}"}, "arn": {"S": ARN}, "warm_until": {"N": str(stale)}}]
    }
    assert _refusals(world)[1] == []


def test_recent_invocations_refuse(world):
    world[3].get_metric_data.return_value = {"MetricDataResults": [{"StatusCode": "Complete", "Values": [3.0]}]}
    _, refusals = _refusals(world)
    assert refusals == ["3 invocations in the last 7 days"]


def test_owner_mismatch_refuses(world):
    world[1].get_agent_runtime.return_value["environmentVariables"] = {"ISOL8_OWNER_ID": "org_someoneElse"}
    assert _refusals(world)[1] == ["runtime identity does not match its owner"]


def test_one_refusal_blocks_the_whole_apply(world):
    other = ARN.replace("LMmcth5Rf2", "Zzzzzzzzzz")
    world[1].get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{mod.REGION}:{mod.ACCOUNT}:runtime/{agentRuntimeId}",
        "environmentVariables": {"ISOL8_OWNER_ID": OWNER},
    }
    world[2].get_item.return_value = {"Item": {"agentcore_runtime_arn": {"S": other}}}
    receipt, _ = _refusals(world, [ARN, other])
    assert not receipt["ok"] and receipt["deleted"] == []
    world[1].delete_agent_runtime.assert_not_called()


def test_already_deleted_runtime_is_reported_not_deleted(world):
    world[1].get_agent_runtime.side_effect = NotFound()
    receipt, _ = _refusals(world)
    assert receipt["ok"] and receipt["verdicts"][0]["absent"] and receipt["deleted"] == []
