"""The CI launcher cannot substitute a definition, command, identity, or unrelated cleanup task."""

from types import SimpleNamespace

import pytest

from scripts import run_runtime_roll as mod

KEY, OWNER = "runtime-roll-12345-1", "org_Test123"
USER = f"AROATEST:{KEY}"
CLUSTER = f"arn:aws:ecs:{mod.REGION}:{mod.ACCOUNT}:cluster/lightbulb-dev-container"
DEFINITION = f"arn:aws:ecs:{mod.REGION}:{mod.ACCOUNT}:task-definition/lightbulb-dev-backend:123"
TASK = CLUSTER.replace(":cluster/", ":task/") + "/" + "a" * 32


@pytest.fixture
def world():
    service = {
        "taskDefinition": DEFINITION,
        "deployments": [{"rolloutState": "COMPLETED"}],
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": ["subnet-existing"],
                "securityGroups": ["sg-existing"],
                "assignPublicIp": "ENABLED",
            }
        },
    }
    definition = {
        "taskDefinitionArn": DEFINITION,
        "family": "lightbulb-dev-backend",
        "taskRoleArn": f"arn:aws:iam::{mod.ACCOUNT}:role/lightbulb-dev-backend-task",
        "executionRoleArn": f"arn:aws:iam::{mod.ACCOUNT}:role/lightbulb-dev-backend-task-execution",
        "containerDefinitions": [
            {
                "name": "backend",
                "image": "existing-image@sha256:abc",
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {"awslogs-group": "/ecs/lightbulb-dev", "awslogs-stream-prefix": "backend"},
                },
            }
        ],
    }
    task = {
        "taskArn": TASK,
        "taskDefinitionArn": DEFINITION,
        "startedBy": KEY,
        "lastStatus": "RUNNING",
        "group": "family:lightbulb-dev-backend",
        "tags": [{"key": "ManagedBy", "value": mod.MANAGED_BY}, {"key": "RollSession", "value": USER}],
    }
    calls = []
    ecs = SimpleNamespace(
        describe_services=lambda **kw: {"services": [service]},
        describe_task_definition=lambda **kw: {"taskDefinition": definition},
        run_task=lambda **kw: calls.append(("run", kw)) or {"tasks": [{"taskArn": TASK}]},
        describe_tasks=lambda **kw: {"tasks": [task]},
        stop_task=lambda **kw: calls.append(("stop", kw)),
    )
    sts = SimpleNamespace(
        get_caller_identity=lambda: {
            "Account": mod.ACCOUNT,
            "UserId": USER,
            "Arn": f"arn:aws:sts::{mod.ACCOUNT}:assumed-role/lightbulb-dev-runtime-roll/{KEY}",
        }
    )
    return SimpleNamespace(ecs=ecs, sts=sts, service=service, definition=definition, task=task, calls=calls)


@pytest.mark.parametrize("owner", ["--all", "org_a; echo bad", "org_$(bad)", "user_123", "org_", "org_a/b"])
def test_invalid_owner_cannot_become_a_shell_command_or_fleet_roll(owner):
    with pytest.raises(ValueError):
        mod.validate("prod", owner)


def test_only_main_workflow_identity_is_accepted():
    assert mod.run_key({"GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1"}) == KEY
    with pytest.raises(ValueError):
        mod.run_key({"GITHUB_REF": "refs/pull/1/merge", "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1"})


@pytest.mark.parametrize("change", ["rolling", "wrong_family", "wrong_role", "sidecar", "wrong_logs"])
def test_deployed_source_is_validated_before_launch(world, change):
    if change == "rolling":
        world.service["deployments"] = [{"rolloutState": "IN_PROGRESS"}]
    elif change == "wrong_family":
        world.service["taskDefinition"] = DEFINITION.replace("backend:", "watchdog:")
    elif change == "wrong_role":
        world.definition["taskRoleArn"] = "arn:aws:iam::877352799272:role/admin"
    elif change == "sidecar":
        world.definition["containerDefinitions"].append({"name": "other"})
    else:
        world.definition["containerDefinitions"][0]["logConfiguration"]["options"]["awslogs-group"] = "/unrelated"
    with pytest.raises(ValueError):
        mod.plan("dev", OWNER, KEY, world.ecs, world.sts)
    assert world.calls == []


def test_launch_uses_exact_deployed_definition_and_only_fixed_command_override(world):
    receipt = mod.plan("dev", OWNER, KEY, world.ecs, world.sts)
    launched = mod.launch(receipt, world.ecs)
    request = world.calls[0][1]
    assert (
        request["taskDefinition"] == DEFINITION
        and request["networkConfiguration"] == world.service["networkConfiguration"]
    )
    assert request["count"] == 1 and request["enableExecuteCommand"] is False
    assert request["overrides"] == {
        "containerOverrides": [
            {
                "name": "backend",
                "command": ["uv", "run", "python", "-m", "scripts.roll_hermes_runtimes", "--owner-id", OWNER],
            }
        ]
    }
    assert request["tags"] == [{"key": "ManagedBy", "value": mod.MANAGED_BY}, {"key": "RollSession", "value": USER}]
    assert launched["taskArn"] == TASK
    mod.launch(receipt, world.ecs)
    assert (
        world.calls[0][1]["clientToken"] == world.calls[1][1]["clientToken"]
    )  # lost RunTask response cannot make a second task


@pytest.mark.parametrize("change", ["service", "other_session", "unmanaged", "other_arn", "other_definition"])
def test_cleanup_never_stops_an_unrelated_task(world, change):
    receipt = mod.launch(mod.plan("dev", OWNER, KEY, world.ecs, world.sts), world.ecs)
    if change == "service":
        world.task["group"] = "service:lightbulb-dev-backend"
    elif change == "other_session":
        world.task["tags"][1]["value"] = "AROATEST:another-job"
    elif change == "unmanaged":
        world.task["tags"] = []
    elif change == "other_arn":
        world.task["taskArn"] = TASK + "other"
    else:
        world.task["taskDefinitionArn"] = DEFINITION + "0"
    with pytest.raises(ValueError):
        mod.cleanup(receipt, world.ecs)
    assert [name for name, _ in world.calls] == ["run"]


def test_cleanup_stops_only_owned_running_task_and_skips_stopped_task(world):
    receipt = mod.launch(mod.plan("dev", OWNER, KEY, world.ecs, world.sts), world.ecs)
    mod.cleanup(receipt, world.ecs)
    assert world.calls[-1] == ("stop", {"cluster": CLUSTER, "task": TASK, "reason": "Runtime rollout workflow cleanup"})
    world.task["lastStatus"] = "STOPPED"
    mod.cleanup(receipt, world.ecs)
    assert len(world.calls) == 2


def test_wait_requires_the_backend_exit_status_and_logs_are_sanitized(world):
    receipt = mod.launch(mod.plan("dev", OWNER, KEY, world.ecs, world.sts), world.ecs)
    world.task.update(lastStatus="STOPPED", containers=[{"name": "backend", "exitCode": 0}])
    assert mod.wait(receipt, world.ecs)["exitCode"] == 0
    logs = SimpleNamespace(
        get_log_events=lambda **kw: {
            "events": [
                {"message": "token=do-not-print"},
                {"message": OWNER + " updated"},
                {"message": "org_Other current"},
            ]
        }
    )
    assert mod.result_line(receipt, logs) == "updated"
    logs = SimpleNamespace(get_log_events=lambda **kw: {"events": [{"message": "no verified result"}]})
    assert mod.result_line(receipt, logs) is None


def test_result_waits_for_cloudwatch_ingestion_without_repeating_rollout(world):
    receipt = mod.launch(mod.plan("dev", OWNER, KEY, world.ecs, world.sts), world.ecs)
    clock = [0]
    requests = []

    def read(**kwargs):
        requests.append(kwargs)
        return {"events": [] if len(requests) == 1 else [{"message": OWNER + " updated-session-busy"}]}

    result = mod.await_result(
        receipt,
        SimpleNamespace(get_log_events=read),
        now=lambda: clock[0],
        pause=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    assert result == "updated-session-busy" and len(requests) == 2
    assert [name for name, _ in world.calls] == ["run"]


def test_missing_result_has_bounded_read_wait(world):
    receipt = mod.launch(mod.plan("dev", OWNER, KEY, world.ecs, world.sts), world.ecs)
    clock = [0]
    logs = SimpleNamespace(get_log_events=lambda **kw: {"events": []})
    assert (
        mod.await_result(
            receipt, logs, now=lambda: clock[0], pause=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        )
        is None
    )
    assert clock[0] == 30
