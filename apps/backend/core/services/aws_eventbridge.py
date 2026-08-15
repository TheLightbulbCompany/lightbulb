"""CloudWatch alarm delivery through a connected owner's AWS account.

The Settings AWS integration is the only credential rail. The backend assumes
the owner's connector role, then creates an EventBridge Connection, API
Destination, rule, and target that POST ALARM transitions to a Paperclip
routine webhook. No Isol8 platform-ops token is involved.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import integrations_repo
from core.services.credentials import integration_secret_store

_ROLE_ARN_RE = re.compile(r"^arn:(?P<partition>aws(?:-[a-z]+)?):iam::(?P<account>\d{12}):role/.+$")
_INVOKE_ROLE_NAME = "isol8-eventbridge-api-destination"
_TARGET_ID = "isol8"
_PERMISSION_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


class AwsEventBridgePermissionError(ValueError):
    """The connected role cannot create or invoke an API Destination."""


async def _connected_session(owner_id: str) -> tuple[boto3.Session, str, str, str]:
    status = await integrations_repo.get(owner_id, "aws")
    if not status or status.get("status") != "connected":
        raise LookupError("AWS is not connected")

    credential = await integration_secret_store.get(owner_id, "aws")
    role_arn = (credential or {}).get("role_arn")
    external_id = (credential or {}).get("external_id")
    region = (credential or {}).get("region") or settings.AWS_REGION
    match = _ROLE_ARN_RE.fullmatch(role_arn or "")
    if not match or not external_id:
        raise LookupError("AWS connection is incomplete — reconnect it in Integrations")

    def _assume() -> boto3.Session:
        response = boto3.client("sts", region_name=settings.AWS_REGION).assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"isol8-automation-{hashlib.sha256(owner_id.encode()).hexdigest()[:12]}",
            ExternalId=external_id,
            DurationSeconds=3600,
        )
        creds = response["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )

    session = await run_in_thread(_assume)
    return session, region, match.group("partition"), match.group("account")


async def list_alarm_resources(owner_id: str) -> list[dict[str, str]]:
    """List the connected region's alarms plus an account-wide choice."""

    session, region, _partition, _account = await _connected_session(owner_id)

    def _list() -> list[dict[str, str]]:
        client = session.client("cloudwatch", region_name=region)
        alarms: list[dict[str, str]] = []
        paginator = client.get_paginator("describe_alarms")
        for page in paginator.paginate(AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
            for key in ("MetricAlarms", "CompositeAlarms"):
                for alarm in page.get(key) or []:
                    arn = alarm.get("AlarmArn")
                    name = alarm.get("AlarmName")
                    if isinstance(arn, str) and isinstance(name, str):
                        alarms.append({"id": arn, "label": name})
        alarms.sort(key=lambda row: row["label"].casefold())
        return [{"id": "*", "label": f"All CloudWatch alarms in {region}"}, *alarms]

    try:
        return await run_in_thread(_list)
    except ClientError as exc:
        raise RuntimeError("AWS could not list CloudWatch alarms") from exc


def _names(owner_id: str, trigger_public_id: str) -> dict[str, str]:
    digest = hashlib.sha256(f"{owner_id}:{trigger_public_id}".encode()).hexdigest()[:20]
    base = f"isol8-{digest}"
    return {
        "connection_name": f"{base}-connection",
        "destination_name": f"{base}-destination",
        "rule_name": f"{base}-alarm",
    }


def _delete_sync(session: boto3.Session, region: str, binding: dict[str, Any]) -> None:
    client = session.client("events", region_name=region)

    def _ignore_missing(fn, **kwargs) -> None:
        try:
            fn(**kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "ResourceNotFoundException",
                "ResourceNotFound",
            }:
                raise

    rule_name = binding["rule_name"]
    _ignore_missing(client.remove_targets, Rule=rule_name, Ids=[_TARGET_ID], Force=True)
    _ignore_missing(client.delete_rule, Name=rule_name, Force=True)
    _ignore_missing(client.delete_api_destination, Name=binding["destination_name"])
    _ignore_missing(client.delete_connection, Name=binding["connection_name"])


def _register_sync(
    session: boto3.Session,
    *,
    region: str,
    partition: str,
    account_id: str,
    endpoint: str,
    webhook_secret: str,
    alarm_resource: str,
    names: dict[str, str],
) -> None:
    client = session.client("events", region_name=region)
    binding = {**names, "region": region}
    try:
        connection = client.create_connection(
            Name=names["connection_name"],
            Description="Isol8 Paperclip routine authorization",
            AuthorizationType="API_KEY",
            AuthParameters={
                "ApiKeyAuthParameters": {
                    "ApiKeyName": "Authorization",
                    "ApiKeyValue": f"Bearer {webhook_secret}",
                }
            },
        )
        state = connection.get("ConnectionState")
        for _ in range(20):
            if state == "AUTHORIZED":
                break
            if state in {"DEAUTHORIZED", "FAILED"}:
                raise RuntimeError(f"EventBridge connection entered {state}")
            time.sleep(0.25)
            state = client.describe_connection(Name=names["connection_name"]).get("ConnectionState")
        else:
            raise RuntimeError("EventBridge connection did not become authorized")

        destination = client.create_api_destination(
            Name=names["destination_name"],
            Description="Deliver CloudWatch ALARM transitions to Isol8",
            ConnectionArn=connection["ConnectionArn"],
            InvocationEndpoint=endpoint,
            HttpMethod="POST",
            InvocationRateLimitPerSecond=1,
        )
        event_pattern: dict[str, Any] = {
            "source": ["aws.cloudwatch"],
            "detail-type": ["CloudWatch Alarm State Change"],
            "detail": {"state": {"value": ["ALARM"]}},
        }
        if alarm_resource != "*":
            event_pattern["resources"] = [alarm_resource]
        client.put_rule(
            Name=names["rule_name"],
            Description="Wake an Isol8 routine when a CloudWatch alarm enters ALARM",
            EventPattern=json.dumps(event_pattern, separators=(",", ":"), sort_keys=True),
            State="ENABLED",
        )
        target_result = client.put_targets(
            Rule=names["rule_name"],
            Targets=[
                {
                    "Id": _TARGET_ID,
                    "Arn": destination["ApiDestinationArn"],
                    "RoleArn": (f"arn:{partition}:iam::{account_id}:role/{_INVOKE_ROLE_NAME}"),
                    "RetryPolicy": {
                        "MaximumEventAgeInSeconds": 86400,
                        "MaximumRetryAttempts": 185,
                    },
                }
            ],
        )
        if target_result.get("FailedEntryCount", 0):
            raise RuntimeError("EventBridge rejected the API Destination target")
    except Exception:
        try:
            _delete_sync(session, region, binding)
        except Exception:
            pass
        raise


async def register_alarm_destination(
    owner_id: str,
    *,
    trigger_public_id: str,
    endpoint: str,
    webhook_secret: str,
    alarm_resource: str,
) -> dict[str, str]:
    resources = await list_alarm_resources(owner_id)
    if alarm_resource not in {row["id"] for row in resources}:
        raise ValueError("That CloudWatch alarm is not in your connected AWS account")

    session, region, partition, account_id = await _connected_session(owner_id)
    names = _names(owner_id, trigger_public_id)
    try:
        await run_in_thread(
            _register_sync,
            session,
            region=region,
            partition=partition,
            account_id=account_id,
            endpoint=endpoint,
            webhook_secret=webhook_secret,
            alarm_resource=alarm_resource,
            names=names,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in _PERMISSION_CODES:
            raise AwsEventBridgePermissionError(
                "CloudWatch alarm delivery needs PowerUser AWS access and the latest Isol8 connector stack"
            ) from exc
        raise
    return {
        **names,
        "region": region,
        "resource": alarm_resource,
        "trigger_public_id": trigger_public_id,
    }


async def delete_alarm_destination(owner_id: str, binding: dict[str, Any]) -> None:
    session, connected_region, _partition, _account = await _connected_session(owner_id)
    region = binding.get("region") or connected_region
    try:
        await run_in_thread(_delete_sync, session, region, binding)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in _PERMISSION_CODES:
            raise AwsEventBridgePermissionError("AWS denied cleanup of the Isol8 EventBridge destination") from exc
        raise
