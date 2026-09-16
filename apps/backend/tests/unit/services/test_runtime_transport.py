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


class TestEcsTransport:
    """respx 0.20.2 is incompatible with this repo's httpx (see test_owner_posthog.py), so the
    client surface is patched directly — the codebase's established convention."""

    @pytest.mark.asyncio
    async def test_posts_to_the_container_ip(self, monkeypatch):
        seen = {}

        class _Resp:
            status_code = 200
            text = '{"ok": true}'

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, json=None, headers=None):
                seen.update(method=method, url=url, json=json, headers=headers)
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
