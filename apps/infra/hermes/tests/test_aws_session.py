"""The session's scoped AWS credentials: taken from each invoke, used for S3 and memory."""
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import bridge
from bridge import server, sync

AUTH = {"authorization": "Bearer gw-test"}


def _header(key="ASIA1", hours=1):
    return json.dumps({"accessKeyId": key, "secretAccessKey": "s", "sessionToken": "t",
                       "expiration": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()})


@pytest.fixture
def boto(monkeypatch):
    """boto3 is not installed here: record what each client was built with."""
    monkeypatch.setattr(bridge, "AWS_SESSION", {})
    monkeypatch.setattr(sync, "_S3_CLIENT", None)
    builds = []
    fake = types.ModuleType("boto3")
    fake.client = lambda service, **kw: (builds.append((service, kw.get("aws_access_key_id"))), object())[1]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return builds


def test_an_invoke_brings_the_credentials_and_s3_uses_them(boto):
    server.handle_plugin("/plugins/lightbulb-channel/nope", "POST", dict(AUTH, **{bridge.AWS_SESSION_HEADER: _header()}), {})
    assert bridge.aws_session_kwargs() == {"aws_access_key_id": "ASIA1", "aws_secret_access_key": "s", "aws_session_token": "t"}
    first = sync._s3()
    assert sync._s3() is first and boto == [("s3", "ASIA1")]
    bridge.set_aws_session(_header("ASIA2"))  # a later invoke's fresh set
    assert sync._s3() is not first and boto[-1] == ("s3", "ASIA2")


def test_no_header_keeps_the_execution_role(boto):
    server.handle_plugin("/plugins/lightbulb-channel/nope", "POST", AUTH, {})
    sync._s3()
    assert boto == [("s3", None)]


def test_expired_or_malformed_credentials_are_never_used(boto):
    bridge.set_aws_session("not json")
    bridge.set_aws_session(json.dumps({"accessKeyId": "ASIA1"}))
    assert bridge.AWS_SESSION == {}
    bridge.set_aws_session(_header(hours=-1))
    assert bridge.aws_session_kwargs() == {}


def test_the_credentials_never_reach_the_process_env(boto):
    bridge.set_aws_session(_header())
    assert "ASIA1" not in json.dumps(dict(__import__("os").environ))
