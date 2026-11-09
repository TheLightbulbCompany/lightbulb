"""CI launcher for one idle-safe workspace roll, using the already deployed backend task.

The launcher never reads Secrets Manager or changes a task definition/service/image. The temporary
Fargate task gets the backend's existing configuration and runs the checked-in rollout CLI. Its
IAM session can observe/stop only tasks created with that same session's immutable creation tags.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time

ACCOUNT, REGION = "877352799272", "us-east-1"
MANAGED_BY = "lightbulb-runtime-roll"
WAIT_S, POLL_S = 900, 10


def validate(environment: str, owner_id: str) -> None:
    if environment not in {"dev", "prod"} or not re.fullmatch(r"org_[A-Za-z0-9]{1,100}", owner_id):
        raise ValueError("one dev/prod workspace org ID is required")


def run_key(environ=os.environ) -> str:
    if environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("runtime rollout is main-only")
    run_id, attempt = environ.get("GITHUB_RUN_ID", ""), environ.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("a GitHub workflow run and attempt are required")
    return f"runtime-roll-{run_id}-{attempt}"


def save(path: Path, receipt: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.replace(path)


def plan(environment: str, owner_id: str, key: str, ecs, sts) -> dict:
    validate(environment, owner_id)
    identity = sts.get_caller_identity()
    if identity.get("Account") != ACCOUNT or not identity.get("Arn", "").endswith(
        f":assumed-role/lightbulb-{environment}-runtime-roll/{key}"
    ):
        raise ValueError("not the dedicated rollout IAM session")
    cluster = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/lightbulb-{environment}-container"
    service_name = f"lightbulb-{environment}-backend"
    response = ecs.describe_services(cluster=cluster, services=[service_name])
    if response.get("failures") or len(response.get("services", [])) != 1:
        raise ValueError("backend service unavailable")
    service = response["services"][0]
    deployments = service.get("deployments") or []
    if len(deployments) != 1 or deployments[0].get("rolloutState") != "COMPLETED":
        raise ValueError("backend deployment must be stable before rollout")
    definition = service.get("taskDefinition", "")
    if not re.fullmatch(rf"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{service_name}:[0-9]+", definition):
        raise ValueError("unexpected backend task definition")
    task = ecs.describe_task_definition(taskDefinition=definition)["taskDefinition"]
    containers = task.get("containerDefinitions") or []
    if (
        task.get("taskDefinitionArn") != definition
        or task.get("family") != service_name
        or task.get("taskRoleArn") != f"arn:aws:iam::{ACCOUNT}:role/lightbulb-{environment}-backend-task"
        or task.get("executionRoleArn") != f"arn:aws:iam::{ACCOUNT}:role/lightbulb-{environment}-backend-task-execution"
        or len(containers) != 1
        or containers[0].get("name") != "backend"
    ):
        raise ValueError("unexpected backend identity or container layout")
    network = service.get("networkConfiguration") or {}
    if not (network.get("awsvpcConfiguration") or {}).get("subnets"):
        raise ValueError("backend VPC configuration unavailable")
    log_config = containers[0].get("logConfiguration") or {}
    log_options = log_config.get("options") or {}
    if (
        log_config.get("logDriver") != "awslogs"
        or log_options.get("awslogs-group") != f"/ecs/lightbulb-{environment}"
        or log_options.get("awslogs-stream-prefix") != "backend"
    ):
        raise ValueError("unexpected backend log destination")
    return {
        "environment": environment,
        "ownerId": owner_id,
        "cluster": cluster,
        "taskDefinition": definition,
        "image": containers[0]["image"],
        "networkConfiguration": network,
        "sessionUserId": identity["UserId"],
        "startedBy": key,
        "logGroup": log_options["awslogs-group"],
        "state": "planned",
    }


def launch(receipt: dict, ecs) -> dict:
    token = hashlib.sha256(f"{receipt['startedBy']}:{receipt['environment']}:{receipt['ownerId']}".encode()).hexdigest()
    response = ecs.run_task(
        cluster=receipt["cluster"],
        taskDefinition=receipt["taskDefinition"],
        launchType="FARGATE",
        count=1,
        networkConfiguration=receipt["networkConfiguration"],
        enableExecuteCommand=False,
        startedBy=receipt["startedBy"],
        clientToken=token,
        tags=[{"key": "ManagedBy", "value": MANAGED_BY}, {"key": "RollSession", "value": receipt["sessionUserId"]}],
        overrides={
            "containerOverrides": [
                {
                    "name": "backend",
                    "command": [
                        "uv",
                        "run",
                        "python",
                        "-m",
                        "scripts.roll_hermes_runtimes",
                        "--owner-id",
                        receipt["ownerId"],
                    ],
                }
            ]
        },
    )
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise RuntimeError("temporary rollout task did not start")
    task = response["tasks"][0]
    arn = task["taskArn"]
    expected = receipt["cluster"].replace(":cluster/", ":task/") + "/"
    if not arn.startswith(expected):
        raise RuntimeError("unexpected launched task identity")
    return {**receipt, "taskArn": arn, "logStream": f"backend/backend/{arn.rsplit('/', 1)[-1]}", "state": "launched"}


def own_task(receipt: dict, ecs) -> dict:
    response = ecs.describe_tasks(cluster=receipt["cluster"], tasks=[receipt["taskArn"]], include=["TAGS"])
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise RuntimeError("rollout task state unavailable")
    task = response["tasks"][0]
    tags = {t["key"]: t["value"] for t in task.get("tags") or []}
    if (
        task.get("taskArn") != receipt["taskArn"]
        or task.get("taskDefinitionArn") != receipt["taskDefinition"]
        or task.get("startedBy") != receipt["startedBy"]
        or task.get("group", "").startswith("service:")
        or tags.get("ManagedBy") != MANAGED_BY
        or tags.get("RollSession") != receipt["sessionUserId"]
    ):
        raise ValueError("refusing to observe or stop a task not owned by this workflow session")
    return task


def cleanup(receipt: dict, ecs) -> None:
    if receipt.get("taskArn") and own_task(receipt, ecs).get("lastStatus") != "STOPPED":
        ecs.stop_task(cluster=receipt["cluster"], task=receipt["taskArn"], reason="Runtime rollout workflow cleanup")


def wait(receipt: dict, ecs, *, now=time.monotonic, pause=time.sleep) -> dict:
    deadline = now() + WAIT_S
    while now() < deadline:
        task = own_task(receipt, ecs)
        if task.get("lastStatus") == "STOPPED":
            container = next((c for c in task.get("containers") or [] if c.get("name") == "backend"), {})
            return {**receipt, "state": "stopped", "exitCode": container.get("exitCode")}
        pause(POLL_S)
    raise TimeoutError("runtime rollout task exceeded the bounded wait")


def result_line(receipt: dict, logs) -> str | None:
    # Only the checked-in CLI's one-line result is returned, never arbitrary backend log content.
    response = logs.get_log_events(
        logGroupName=receipt["logGroup"], logStreamName=receipt["logStream"], startFromHead=True
    )
    pattern = re.compile(re.escape(receipt["ownerId"]) + r" (current|updated|updated-session-busy|update-in-progress)$")
    matches = [pattern.fullmatch(e.get("message", "").strip()) for e in response.get("events") or []]
    return next((m.group(1) for m in reversed(matches) if m), None)


def await_result(receipt: dict, logs, *, now=time.monotonic, pause=time.sleep) -> str | None:
    # STOPPED can precede CloudWatch ingestion; retry reads only, never the rollout action.
    from botocore.exceptions import ClientError

    deadline = now() + 30
    while True:
        try:
            result = result_line(receipt, logs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            result = None
        if result is not None or now() >= deadline:
            return result
        pause(2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", choices=("dev", "prod"))
    parser.add_argument("owner_id")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args(argv)
    validate(args.environment, args.owner_id)
    key = run_key()
    if args.validate_only:
        print("Validated one workspace and main workflow identity.")
        return 0
    if args.receipt is None:
        parser.error("--receipt is required")
    import boto3

    ecs, sts = boto3.client("ecs", region_name=REGION), boto3.client("sts", region_name=REGION)
    if args.cleanup:
        if not args.receipt.exists():
            return 0
        receipt = json.loads(args.receipt.read_text())
        identity = sts.get_caller_identity()
        if (
            receipt.get("environment") != args.environment
            or receipt.get("ownerId") != args.owner_id
            or receipt.get("startedBy") != key
            or receipt.get("sessionUserId") != identity["UserId"]
        ):
            raise ValueError("cleanup receipt does not belong to this workflow")
        cleanup(receipt, ecs)
        return 0
    receipt = plan(args.environment, args.owner_id, key, ecs, sts)
    save(args.receipt, receipt)
    try:
        receipt = launch(receipt, ecs)
        save(args.receipt, receipt)
        receipt = wait(receipt, ecs)
        receipt["rollResult"] = await_result(receipt, boto3.client("logs", region_name=REGION))
        save(args.receipt, receipt)
        print(
            json.dumps(
                {
                    k: receipt.get(k)
                    for k in ("environment", "ownerId", "taskDefinition", "image", "taskArn", "exitCode", "rollResult")
                }
            )
        )
        return 0 if receipt.get("exitCode") == 0 and receipt.get("rollResult") in {"current", "updated"} else 1
    finally:
        cleanup(receipt, ecs)


if __name__ == "__main__":

    def interrupted(_signum, _frame):
        raise InterruptedError("workflow interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    raise SystemExit(main())
