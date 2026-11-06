"""Dev walk 16 (W16-6): GET /messaging/rooms 500'd with "KeyConditionExpressions must only contain one condition per
key" because boto3 shares one condition builder per client across threads. Unlocked, this loop produced
"(#n0 = :v4 AND #n0 BETWEEN :v0 AND :v1)" thousands of times."""

import sys
import threading

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.transform import TransformationInjector

import core.dynamodb  # noqa: F401 -- installs the lock


def test_concurrent_key_conditions_keep_their_own_placeholders():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    injector = TransformationInjector()
    model = boto3.client("dynamodb", region_name="us-east-1").meta.service_model.operation_model("Query")
    bad: list[str] = []

    def run():
        for _ in range(500):
            params = {"TableName": "t", "KeyConditionExpression": Key("pk").eq("a") & Key("sk").between("b", "c")}
            injector.inject_condition_expressions(params, model)
            if len(params["ExpressionAttributeNames"]) != 2 or len(params["ExpressionAttributeValues"]) != 3:
                bad.append(params["KeyConditionExpression"])

    try:
        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert bad == []
