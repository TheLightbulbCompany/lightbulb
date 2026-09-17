"""The transport seam: one way to reach a gateway, whichever platform hosts it."""

import json

import pytest

from core.services import runtime_transport as rt


class TestSessionId:
    """A workspace gets ONE session id. This is the single-writer guarantee, not a detail:
    two live sessions mean two gateways on one EFS mount and two litestream replicators on one
    S3 prefix, which corrupts the replica set silently."""

    def test_is_stable_for_an_owner(self):
        assert rt.session_id_for("org_abc") == rt.session_id_for("org_abc")

    def test_differs_between_owners(self):
        assert rt.session_id_for("org_abc") != rt.session_id_for("org_xyz")

    def test_meets_the_33_char_floor_even_for_a_short_owner_id(self):
        # AgentCore rejects a shorter id; a one-character owner must still clear the floor.
        assert len(rt.session_id_for("x")) >= 33

    def test_a_long_owner_id_is_not_truncated_into_a_collision(self):
        a = rt.session_id_for("org_" + "a" * 60)
        b = rt.session_id_for("org_" + "a" * 59 + "b")
        assert a != b


class TestHostSelection:
    def test_absent_flag_means_ecs(self):
        # Every pre-migration row has no runtime_host; it must keep dialling its IP.
        assert rt.host_for({}) == "ecs"

    def test_unknown_value_falls_back_to_ecs(self):
        assert rt.host_for({"runtime_host": "wat"}) == "ecs"

    def test_agentcore_is_honoured(self):
        assert rt.host_for({"runtime_host": "agentcore"}) == "agentcore"


class TestIsAgentcoreOwner:
    """M6-R26 fix round 1, Important 3: a read failure must default to ECS
    (False) with a warning, never propagate -- every new caller added during
    the M6 rollout treats this as a routine per-request check, and before
    that rollout nothing on these doors read this table at all."""

    @pytest.mark.asyncio
    async def test_read_failure_defaults_to_ecs(self, monkeypatch, caplog):
        async def _raise(owner_id):
            raise Exception("dynamodb throttled")

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _raise)

        with caplog.at_level("WARNING"):
            result = await rt.is_agentcore_owner("org_1")

        assert result is False
        assert "org_1" in caplog.text

    @pytest.mark.asyncio
    async def test_agentcore_row_reads_true(self, monkeypatch):
        async def _get(owner_id):
            return {"runtime_host": "agentcore"}

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _get)
        assert await rt.is_agentcore_owner("org_1") is True

    @pytest.mark.asyncio
    async def test_no_row_reads_false(self, monkeypatch):
        async def _get(owner_id):
            return None

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _get)
        assert await rt.is_agentcore_owner("org_1") is False


class TestEcsTransport:
    """respx 0.20.2 is incompatible with this repo's httpx (see test_owner_posthog.py), so the
    client surface is patched directly — the codebase's established convention."""

    @pytest.mark.asyncio
    async def test_posts_to_the_container_ip(self, monkeypatch):
        seen = {}

        class _Resp:
            status_code = 200
            text = '{"ok": true}'
            headers = {}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, json=None, headers=None, content=None):
                seen.update(method=method, url=url, json=json, headers=headers, content=content)
                return _Resp()

        monkeypatch.setattr("httpx.AsyncClient", _Client)
        res = await rt.request(
            {"runtime_host": "ecs"},
            "/plugins/lightbulb-channel/rpc",
            ip="10.0.0.5",
            json_body={"method": "health"},
            headers={"Authorization": "Bearer tok"},
        )
        assert res.status_code == 200
        assert res.json() == {"ok": True}
        # The URL shape the fleet depends on today.
        assert seen["url"] == "http://10.0.0.5:18789/plugins/lightbulb-channel/rpc"
        assert seen["method"] == "POST"
        assert seen["headers"]["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_refuses_without_an_ip(self):
        with pytest.raises(ValueError, match="requires a container ip"):
            await rt.request({}, "/healthz", ip=None)

    @pytest.mark.asyncio
    async def test_raw_content_is_forwarded_verbatim_and_wins_over_json(self, monkeypatch):
        # Webhook proxies forward the sender's bytes untouched (signatures cover them).
        seen = {}

        class _Resp:
            status_code = 200
            text = "ok"
            headers = {"content-type": "text/plain"}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, json=None, headers=None, content=None):
                seen.update(json=json, content=content)
                return _Resp()

        monkeypatch.setattr("httpx.AsyncClient", _Client)
        res = await rt.request({}, "/hooks/x", ip="10.0.0.5", json_body={"ignored": 1}, content=b"a=1&b=2")
        assert seen["content"] == b"a=1&b=2"
        assert seen["json"] is None
        assert res.headers["content-type"] == "text/plain"


class TestAgentCoreTransport:
    @pytest.mark.asyncio
    async def test_unwraps_the_contract_server_envelope(self, monkeypatch):
        captured = {}

        class _Body:
            def read(self):
                # What the contract server returns: the gateway's own status and body.
                return json.dumps({"status": 200, "body": {"ok": True, "payload": {"n": 1}}}).encode()

        class _Client:
            def invoke_agent_runtime(self, **kw):
                captured.update(kw)
                return {"statusCode": 200, "response": _Body()}

        monkeypatch.setattr("boto3.client", lambda *a, **k: _Client())
        res = await rt.request(
            {
                "runtime_host": "agentcore",
                "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:::runtime/r",
                "owner_id": "org_abc",
            },
            "/plugins/lightbulb-channel/rpc",
            json_body={"method": "health"},
            headers={"Authorization": "Bearer tok"},
        )
        assert res.status_code == 200
        assert res.json()["payload"]["n"] == 1
        # The envelope the contract server expects.
        sent = json.loads(captured["payload"].decode())
        assert sent["path"] == "/plugins/lightbulb-channel/rpc"
        assert sent["method"] == "POST"
        assert sent["headers"]["Authorization"] == "Bearer tok"
        # And the session id is the owner's stable one, never a random per-call id.
        assert captured["runtimeSessionId"] == rt.session_id_for("org_abc")

    @pytest.mark.asyncio
    async def test_propagates_a_gateway_error_status(self, monkeypatch):
        class _Body:
            def read(self):
                return json.dumps({"status": 401, "body": "Unauthorized"}).encode()

        monkeypatch.setattr(
            "boto3.client",
            lambda *a, **k: type(
                "C", (), {"invoke_agent_runtime": lambda self, **kw: {"statusCode": 200, "response": _Body()}}
            )(),
        )
        res = await rt.request({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x")
        assert res.status_code == 401

    @pytest.mark.asyncio
    async def test_contract_server_503_while_booting_is_surfaced(self, monkeypatch):
        # The gateway is still starting; callers must see a retryable failure, not a 200.
        class _Body:
            def read(self):
                return b'{"error":"gateway still booting"}'

        monkeypatch.setattr(
            "boto3.client",
            lambda *a, **k: type(
                "C", (), {"invoke_agent_runtime": lambda self, **kw: {"statusCode": 503, "response": _Body()}}
            )(),
        )
        res = await rt.request({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x")
        assert res.status_code == 503

    @pytest.mark.asyncio
    async def test_refuses_without_a_runtime_arn(self):
        with pytest.raises(ValueError, match="agentcore_runtime_arn"):
            await rt.request({"runtime_host": "agentcore", "owner_id": "o"}, "/x")

    @pytest.mark.asyncio
    async def test_never_resends_and_the_read_timeout_is_the_callers_budget(self, monkeypatch):
        """boto3's defaults would re-send a timed-out POST (a duplicate turn) and cap every call
        at 60s regardless of the caller's budget. The budget is the caller's: room ingress passes
        one that outlasts a cold start; a probe passes a short one."""
        seen = {}

        class _Body:
            def read(self):
                return b'{"status": 200, "body": ""}'

        def _client(name, config=None, **kw):
            seen.update(name=name, config=config)
            return type(
                "C", (), {"invoke_agent_runtime": lambda self, **kw: {"statusCode": 200, "response": _Body()}}
            )()

        monkeypatch.setattr("boto3.client", _client)
        await rt.request(
            {"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x", timeout_s=45.0
        )
        assert seen["name"] == "bedrock-agentcore"
        assert seen["config"].retries == {"total_max_attempts": 1}
        assert seen["config"].read_timeout == 45.0 + 5.0  # the ECS transport's margin, same shape
        assert seen["config"].connect_timeout == 10

    @pytest.mark.asyncio
    async def test_a_boto_read_timeout_is_an_httpx_read_timeout(self, monkeypatch):
        # So the work consumer's ``except httpx.TimeoutException`` retries it, unchanged.
        import httpx
        from botocore.exceptions import ReadTimeoutError

        def _boom(self, **kw):
            raise ReadTimeoutError(endpoint_url="https://bedrock-agentcore")

        monkeypatch.setattr("boto3.client", lambda *a, **k: type("C", (), {"invoke_agent_runtime": _boom})())
        with pytest.raises(httpx.ReadTimeout):
            await rt.request({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x")

    @pytest.mark.asyncio
    async def test_any_other_boto_failure_is_an_httpx_connect_error(self, monkeypatch):
        import httpx
        from botocore.exceptions import ClientError

        def _boom(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "InvokeAgentRuntime")

        monkeypatch.setattr("boto3.client", lambda *a, **k: type("C", (), {"invoke_agent_runtime": _boom})())
        with pytest.raises(httpx.ConnectError, match="AccessDenied"):
            await rt.request({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x")

    @pytest.mark.asyncio
    async def test_raw_content_rides_the_envelope_as_text_and_reply_headers_come_back(self, monkeypatch):
        captured = {}

        class _Body:
            def read(self):
                return json.dumps({"status": 200, "headers": {"content-type": "text/plain"}, "body": "pong"}).encode()

        class _Client:
            def invoke_agent_runtime(self, **kw):
                captured.update(kw)
                return {"statusCode": 200, "response": _Body()}

        monkeypatch.setattr("boto3.client", lambda *a, **k: _Client())
        res = await rt.request(
            {"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x", "owner_id": "o"},
            "/hooks/x",
            content=b"a=1&b=2",
            json_body={"ignored": 1},
        )
        sent = json.loads(captured["payload"].decode())
        assert sent["body"] == "a=1&b=2"
        assert res.text == "pong"
        assert res.headers == {"content-type": "text/plain"}

    @pytest.mark.asyncio
    async def test_a_slow_invoke_is_an_httpx_read_timeout_not_a_bare_asyncio_one(self, monkeypatch):
        # A cold session can outlast a short caller budget; every caller catches httpx, not asyncio.
        import threading

        import httpx

        release = threading.Event()

        def _slow(self, **kw):
            release.wait(5)
            return {"statusCode": 200, "response": b'{"status":200,"body":""}'}

        monkeypatch.setattr("boto3.client", lambda *a, **k: type("C", (), {"invoke_agent_runtime": _slow})())
        original_wait_for = rt.asyncio.wait_for
        monkeypatch.setattr(rt.asyncio, "wait_for", lambda aw, timeout: original_wait_for(aw, 0.05))
        try:
            with pytest.raises(httpx.ReadTimeout):
                await rt.request({"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x")
        finally:
            release.set()
