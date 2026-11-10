"""Retire legacy per-owner AgentCore runtime definitions, one exact allowlist at a time.

Dry run by default: reads only. ``--apply`` deletes ONLY the AgentCore runtime definition
(``DeleteAgentRuntime``) for ARNs that are in the checked-in allowlist and pass every refusal
below. It never calls the workspace teardown seam and never touches S3 profiles, memory,
boot secrets / network identity seeds, gate tokens, the owner's containers row, or the
per-owner IAM role. The role stays because it is still referenced after its runtime is gone:
the AppSync handler (api-stack ``OWNER_ROLE_PREFIX``) and an owner's AWS-connector trust pin
to it, and roll/provision re-ensure it.

Refusals (any one refuses the ARN; ``--apply`` deletes nothing if any ARN is refused):
  * not in the allowlist for this environment, or not this account/region/env's ARN shape
  * either Hermes pool, by name
  * the runtime's own ISOL8_OWNER_ID is missing or does not derive its name
  * the owner's containers row still names it (agentcore_runtime_arn / agentcore_shared_runtime_arn)
  * any owner or task session row names it within ACTIVE_WINDOW_S, or holds a live wake lease
  * CloudWatch shows an invocation on it in the last ACTIVITY_DAYS days

Delete this tool once every legacy runtime is gone; git history is the operational record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ACCOUNT = "877352799272"
REGION = "us-east-1"
ALLOWLIST = Path(__file__).with_name("legacy_runtime_retirement_allowlist.json")
POOL_NAMES = {"lightbulb_dev_hermes_pool", "lightbulb_prod_hermes_pool"}
SESSION_PREFIX = "__session__#"  # container_repo.SESSION_KEY_PREFIX
# Session estimate + AgentCore idle timeout (5 min) + runtime_transport.LIVE_GRACE_S, with room.
ACTIVE_WINDOW_S = 3600
ACTIVITY_DAYS = 7
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]")  # agentcore_runtime.runtime_name_for
_MAX_NAME_LEN = 48


def runtime_name_for(owner_id: str, env: str) -> str:
    """Copy of ``agentcore_runtime.runtime_name_for`` (that one reads env from settings)."""
    digest = hashlib.sha256(owner_id.encode()).hexdigest()[:12]
    prefix = f"lb_{env}_"
    room = _MAX_NAME_LEN - len(prefix) - len(digest) - 1
    return f"{prefix}{_NAME_SAFE.sub('_', owner_id)[: max(room, 0)]}_{digest}"


def load_allowlist(env: str, path: Path = ALLOWLIST) -> list[str]:
    arns = json.loads(path.read_text())[env]
    if len(arns) != len(set(arns)):
        raise ValueError("duplicate ARN in allowlist")
    return arns


def _attr(item: dict, name: str):
    value = item.get(name)
    return None if value is None else next(iter(value.values()))


def session_rows(ddb, table: str, owner_id: str) -> list[dict]:
    """The owner's workspace session row and every task session row (``#task#``)."""
    kwargs = {
        "TableName": table,
        "ConsistentRead": True,
        "FilterExpression": "begins_with(owner_id, :p)",
        "ProjectionExpression": "owner_id, arn, warm_until, waking_until",
        "ExpressionAttributeValues": {":p": {"S": f"{SESSION_PREFIX}{owner_id}"}},
    }
    rows = []
    while True:
        page = ddb.scan(**kwargs)
        for item in page["Items"]:
            key = _attr(item, "owner_id")
            if key == f"{SESSION_PREFIX}{owner_id}" or key.startswith(f"{SESSION_PREFIX}{owner_id}#task#"):
                rows.append(item)
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def invocations(cloudwatch, arn: str, name: str, now: float) -> float:
    end = datetime.fromtimestamp(now, UTC)
    out = cloudwatch.get_metric_data(
        StartTime=end - timedelta(days=ACTIVITY_DAYS),
        EndTime=end,
        MetricDataQueries=[
            {
                "Id": "inv",
                "MetricStat": {
                    "Period": 3600,
                    "Stat": "Sum",
                    "Metric": {
                        "Namespace": "AWS/Bedrock-AgentCore",
                        "MetricName": "Invocations",
                        "Dimensions": [
                            {"Name": "Resource", "Value": arn},
                            {"Name": "Name", "Value": f"{name}::DEFAULT"},
                            {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                        ],
                    },
                },
            }
        ],
    )["MetricDataResults"][0]
    if out["StatusCode"] != "Complete":
        raise RuntimeError(f"incomplete CloudWatch read for {arn}")
    return sum(out["Values"])


def check(arn: str, env: str, *, control, ddb, cloudwatch, now: float) -> dict:
    """Read-only verdict for one allowlisted ARN: ``{"arn", "refusals": [...], ...}``."""
    verdict: dict = {"arn": arn, "refusals": []}
    refuse = verdict["refusals"].append
    match = re.fullmatch(rf"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/(([A-Za-z0-9_]+)-[A-Za-z0-9]+)", arn)
    if not match:
        refuse("not a runtime ARN in this account/region")
        return verdict
    runtime_id, name = match.groups()
    if name in POOL_NAMES or "pool" in name:
        refuse("Hermes pool runtime")
        return verdict
    if not name.startswith(f"lb_{env}_"):
        refuse(f"not a {env} per-owner runtime name")
        return verdict
    try:
        runtime = control.get_agent_runtime(agentRuntimeId=runtime_id)
    except control.exceptions.ResourceNotFoundException:
        verdict["absent"] = True
        return verdict
    owner_id = (runtime.get("environmentVariables") or {}).get("ISOL8_OWNER_ID")
    verdict.update(owner_id=owner_id, status=runtime.get("status"), name=name)
    if runtime.get("agentRuntimeArn") != arn or not owner_id or runtime_name_for(owner_id, env) != name:
        refuse("runtime identity does not match its owner")
        return verdict

    table = f"lightbulb-{env}-containers"
    row = ddb.get_item(TableName=table, Key={"owner_id": {"S": owner_id}}, ConsistentRead=True).get("Item") or {}
    if arn in (_attr(row, "agentcore_runtime_arn"), _attr(row, "agentcore_shared_runtime_arn")):
        refuse("owner row still points at this runtime")
    for item in session_rows(ddb, table, owner_id):
        key = _attr(item, "owner_id")
        if float(_attr(item, "waking_until") or 0) > now:
            refuse(f"live wake lease on {key}")
        if _attr(item, "arn") == arn and float(_attr(item, "warm_until") or 0) > now - ACTIVE_WINDOW_S:
            refuse(f"session {key} active on this runtime")
    count = invocations(cloudwatch, arn, name, now)
    verdict["invocations_7d"] = count
    if count:
        refuse(f"{count:g} invocations in the last {ACTIVITY_DAYS} days")
    return verdict


def run(session, env: str, apply: bool, allowlist: list[str], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    control = session.client("bedrock-agentcore-control", region_name=REGION)
    ddb = session.client("dynamodb", region_name=REGION)
    cloudwatch = session.client("cloudwatch", region_name=REGION)
    verdicts = [check(a, env, control=control, ddb=ddb, cloudwatch=cloudwatch, now=now) for a in allowlist]
    refused = [v for v in verdicts if v["refusals"]]
    receipt = {"environment": env, "apply": apply, "verdicts": verdicts, "deleted": []}
    if apply and not refused:
        for v in verdicts:
            if not v.get("absent"):
                control.delete_agent_runtime(agentRuntimeId=v["arn"].rsplit("/", 1)[-1])
                receipt["deleted"].append(v["arn"])
    receipt["ok"] = not refused
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", choices=("dev", "prod"), required=True)
    parser.add_argument("--apply", action="store_true", help="delete; default is a read-only dry run")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    import boto3

    receipt = run(boto3.Session(), args.environment, args.apply, load_allowlist(args.environment))
    text = json.dumps(receipt, indent=2, default=str)
    print(text)
    if args.receipt:
        args.receipt.write_text(text)
    return 0 if receipt["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
