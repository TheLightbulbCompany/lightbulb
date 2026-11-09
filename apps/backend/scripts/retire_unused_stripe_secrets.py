"""One-shot retirement of four unused marketplace/Connect webhook secrets.

Plan is read-only. Execute requires the reviewed metadata hash, repeats all
consumer checks, and uses a fixed 30-day recovery window. Never reads values.
Delete this tool after retirement; git history is the operational record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC
from pathlib import Path

ACCOUNT = "877352799272"
REGION = "us-east-1"
SUFFIXES = ("stripe_connect_webhook_secret", "stripe_marketplace_webhook_secret")


def scope_hash(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_references(root):
    # Search every tracked repository file, including hidden workflows, Lambda
    # packages, docs and non-Python consumers. Only this operation's exact files
    # are excluded: they name the permitted scope, not a credential consumer.
    own_files = (
        "apps/backend/scripts/retire_unused_stripe_secrets.py",
        "apps/backend/scripts/test_retire_unused_stripe_secrets.py",
        ".github/workflows/retire-unused-stripe-secrets.yml",
        "apps/infra/lib/stripe-secret-retirement-bootstrap.ts",
        "apps/infra/test/stripe-secret-retirement-bootstrap.test.ts",
    )
    result = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "--fixed-strings",
            *[part for name in SUFFIXES for part in ("-e", name)],
            "--",
            ".",
            *[f":(exclude){path}" for path in own_files],
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ValueError("Cannot verify complete repository source references")
    return result.stdout.splitlines()


def assert_unreferenced(values, names, label):
    text = json.dumps(values, default=str)
    if any(name in text for name in names):
        raise ValueError(f"Retired secret is still referenced by {label}")


def consumer_checks(session, names):
    """Check deployed templates, all service deployments/tasks, and Lambda config."""
    cf = session.client("cloudformation", region_name=REGION)
    for page in cf.get_paginator("list_stacks").paginate():
        for stack in page["StackSummaries"]:
            if stack["StackStatus"] == "DELETE_COMPLETE" or not stack["StackName"].startswith("lightbulb-"):
                continue
            template = cf.get_template(StackName=stack["StackId"], TemplateStage="Original")["TemplateBody"]
            assert_unreferenced(template, names, stack["StackName"])
    for region in ("us-east-1", "us-west-2"):
        ecs = session.client("ecs", region_name=region)
        definitions = set()
        for page in ecs.get_paginator("list_clusters").paginate():
            for cluster in page["clusterArns"]:
                for services in ecs.get_paginator("list_services").paginate(cluster=cluster):
                    arns = services["serviceArns"]
                    for offset in range(0, len(arns), 10):
                        result = ecs.describe_services(cluster=cluster, services=arns[offset : offset + 10])
                        if result.get("failures") or len(result["services"]) != len(arns[offset : offset + 10]):
                            raise ValueError("Cannot verify all deployed services")
                        for service in result["services"]:
                            definitions.add(service["taskDefinition"])
                            definitions.update(d["taskDefinition"] for d in service.get("deployments", []))
                for status in ("RUNNING", "STOPPED"):
                    for tasks in ecs.get_paginator("list_tasks").paginate(cluster=cluster, desiredStatus=status):
                        arns = tasks["taskArns"]
                        for offset in range(0, len(arns), 100):
                            result = ecs.describe_tasks(cluster=cluster, tasks=arns[offset : offset + 100])
                            if result.get("failures") or len(result["tasks"]) != len(arns[offset : offset + 100]):
                                raise ValueError("Cannot verify all deployed tasks")
                            definitions.update(t["taskDefinitionArn"] for t in result["tasks"])
        for definition in definitions:
            assert_unreferenced(ecs.describe_task_definition(taskDefinition=definition), names, "ECS task definition")
        lm = session.client("lambda", region_name=region)
        for page in lm.get_paginator("list_functions").paginate(FunctionVersion="ALL"):
            for function in page["Functions"]:
                config = lm.get_function_configuration(FunctionName=function["FunctionArn"])
                assert_unreferenced(config.get("Environment", {}), names, "Lambda environment")


def run(session, environment, mode, expected_hash, root, receipt_path=None):
    if environment not in {"dev", "prod"} or mode not in {"plan", "execute"}:
        raise ValueError("Invalid retirement scope")
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise ValueError("Wrong AWS account")
    if source_references(root):
        raise ValueError("A current source consumer still references a retired secret")
    names = [f"lightbulb/{environment}/{suffix}" for suffix in SUFFIXES]
    consumer_checks(session, names)
    sm = session.client("secretsmanager", region_name=REGION)
    rows = []
    for name in names:
        row = sm.describe_secret(SecretId=name)
        if (
            row.get("Name") != name
            or not row.get("ARN", "").startswith(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-")
            or row.get("OwningService")
            or row.get("RotationEnabled")
            or row.get("DeletedDate")
        ):
            raise ValueError("Unexpected, service-owned or already scheduled secret")
        rows.append(
            {"name": name, "arn": row["ARN"], "lastChanged": row["LastChangedDate"].astimezone(UTC).isoformat()}
        )
    receipt = {"environment": environment, "mode": mode, "scopeSha256": scope_hash(rows), "secrets": rows}
    if mode == "execute":
        if expected_hash != receipt["scopeSha256"]:
            raise ValueError("Scope differs from reviewed metadata hash")
        receipt["scheduledArns"] = []
        if receipt_path:
            receipt_path.write_text(json.dumps(receipt, indent=2))
        for row in rows:
            # Metadata was just re-read; no value reads, rewriting, rotation or force deletion.
            sm.delete_secret(SecretId=row["arn"], RecoveryWindowInDays=30)
            receipt["scheduledArns"].append(row["arn"])
            if receipt_path:
                receipt_path.write_text(json.dumps(receipt, indent=2))
        receipt["scheduledRecoveryDays"] = 30
    return receipt


def main():
    import boto3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("dev", "prod"), required=True)
    parser.add_argument("--mode", choices=("plan", "execute"), default="plan")
    parser.add_argument("--expected-scope-sha256", default="")
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    receipt = run(
        boto3.Session(),
        args.environment,
        args.mode,
        args.expected_scope_sha256,
        Path(__file__).parents[3],
        Path(args.receipt),
    )
    Path(args.receipt).write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
