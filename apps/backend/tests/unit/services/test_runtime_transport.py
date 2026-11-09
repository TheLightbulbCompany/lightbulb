"""The transport seam: one way to reach a gateway, whichever platform hosts it."""

import json

import pytest
from unittest.mock import patch

from core.services import runtime_transport as rt


@pytest.fixture(autouse=True)
def no_existing_session(monkeypatch):
    monkeypatch.setattr("core.repositories.container_repo.get_session_sync", lambda *a, **k: {})


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


class TestNoTransportSelection:
    """There is one runtime, so this module offers NO way to ask which one an owner is on.
    The classification helpers (``host_for``, ``is_agentcore_owner``) and the address sentinel
    they fed (``AGENTCORE_ADDRESS``, ``GATEWAY_PORT``) are deleted: anything reintroducing them
    is reintroducing a branch with one live side."""

    def test_the_module_exposes_no_classifier(self):
        for gone in ("host_for", "is_agentcore_owner", "AGENTCORE_ADDRESS", "GATEWAY_PORT"):
            assert not hasattr(rt, gone), gone

    @pytest.mark.asyncio
    async def test_a_request_addresses_the_row_and_reads_nothing(self, monkeypatch):
        """Addressing is the row's own ``agentcore_runtime_arn`` -- a request never looks an
        owner up, so a caller that already holds the row pays for no second read."""

        async def _boom(owner_id, **kw):
            raise AssertionError("runtime_transport must not read the containers table")

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _boom)
        with pytest.raises(ValueError, match="agentcore_runtime_arn"):
            await rt.request({"owner_id": "org_1"}, "/health")


class TestAgentCoreTransport:
    @pytest.mark.asyncio
    async def test_a_turn_invoke_reads_no_row_and_carries_no_switch_state(self, monkeypatch):
        """Task 8a: there is no provider-switch state to gate or stamp a turn with -- a switch
        is a profile rewrite the bridge picks up on its next turn."""

        async def _boom(owner_id, **kw):
            raise AssertionError("a turn must not read the containers table")

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _boom)
        owner = {"owner_id": "org_a", "agentcore_runtime_arn": "arn:runtime/abc"}
        captured = []

        class Client:
            def invoke_agent_runtime(self, **kwargs):
                captured.append(json.loads(kwargs["payload"].decode()))
                return {"statusCode": 200, "response": b'{"status":202,"body":"accepted"}'}

        monkeypatch.setattr("boto3.client", lambda *a, **kw: Client())
        for path in ("/plugins/lightbulb-channel/inbound/ceo", "/internal/paperclip/v1/runs"):
            await rt.request(owner, path, method="POST", headers={"Authorization": "Bearer gw"}, json_body={})
        assert all("X-Lightbulb-Provider-Generation" not in e["headers"] for e in captured)
        assert len(captured) == 2

    @pytest.mark.asyncio
    async def test_every_invoke_carries_the_vault_token_and_only_a_row_with_an_id_derives_one(self, monkeypatch):
        """The bridge learns the website-login vault token ONLY from the backend's invokes: the
        boot secret no longer carries it, because the execution role (which the agent's shell
        reaches over the metadata service) can read that secret."""
        from core.services.agentcore_runtime import derive_vault_token

        captured = []

        class Client:
            def invoke_agent_runtime(self, **kwargs):
                captured.append(json.loads(kwargs["payload"].decode()))
                return {"statusCode": 200, "response": b'{"status":200,"body":{}}'}

        monkeypatch.setattr("boto3.client", lambda *a, **kw: Client())
        row = {"owner_id": "org_a", "id": "row-1", "agentcore_runtime_arn": "arn:runtime/abc"}
        await rt.request(row, "/plugins/lightbulb-channel/rpc", headers={"Authorization": "Bearer gw"}, json_body={})
        await rt.request(row, "/plugins/lightbulb-channel/rpc", json_body={})
        await rt.request({**row, "id": ""}, "/plugins/lightbulb-channel/rpc", json_body={})
        token = derive_vault_token("org_a", "row-1")
        tenant = {rt.TENANT_HEADER: "org_a"}
        assert captured[0]["headers"] == {"Authorization": "Bearer gw", rt.VAULT_HEADER: token, **tenant}
        assert captured[1]["headers"] == {rt.VAULT_HEADER: token, **tenant}
        assert captured[2]["headers"] == tenant

    @pytest.mark.asyncio
    async def test_every_invoke_names_its_tenant_the_wake_too(self, monkeypatch):
        """The bridge binds a session to the tenant of its first invoke and refuses any other,
        so the cold-start health call must carry it as well as the call it wakes for."""
        captured = []

        class Client:
            def invoke_agent_runtime(self, **kwargs):
                captured.append(json.loads(kwargs["payload"].decode()))
                return {"statusCode": 200, "response": b'{"status":200,"body":{}}'}

        monkeypatch.setattr("boto3.client", lambda *a, **kw: Client())
        # patch.object, not monkeypatch: conftest stubs this with patch.object, and a monkeypatch undo
        # would put that stub back after conftest restored the real one (leaking it into later files).
        with patch.object(rt, "_ensure_session", lambda owner_id, arn, deadline, wake: wake()):  # a cold session
            await rt.request(
                {"owner_id": "org_a", "agentcore_runtime_arn": "arn:runtime/abc"},
                "/plugins/lightbulb-channel/rpc",
                json_body={},
            )
        assert len(captured) == 2  # the wake, then the call
        assert all(e["headers"][rt.TENANT_HEADER] == "org_a" for e in captured)

    @pytest.mark.asyncio
    async def test_every_invoke_carries_the_owners_boot_values_the_wake_too(self, monkeypatch):
        """A shared runtime has no boot secret: its session takes these from the invoke that binds it,
        which may be the wake, so both carry them. Derived and read from the row, never a runtime env."""
        from core.config import settings

        captured = []

        class Client:
            def invoke_agent_runtime(self, **kwargs):
                captured.append(json.loads(kwargs["payload"].decode()))
                return {"statusCode": 200, "response": b'{"status":200,"body":{}}'}

        monkeypatch.setattr("boto3.client", lambda *a, **kw: Client())
        monkeypatch.setattr(settings, "GATE_BEARER_HMAC_KEY", "k" * 32)
        monkeypatch.setattr(rt, "_network_seed", lambda owner, row: f"seed-{owner}-{row}")
        row = {
            "owner_id": "org_a",
            "id": "row-1",
            "agentcore_runtime_arn": "arn:runtime/abc",
            "hook_token": "hook-a",
            "gateway_token": "gw-a",
            "browser_profile_id": "prof-a",
            "network_workers": True,
            "max_concurrent_turns": 6,
        }
        with patch.object(rt, "_ensure_session", lambda owner_id, arn, deadline, wake: wake()):
            await rt.request(row, "/plugins/lightbulb-channel/rpc", json_body={})
        from core.billing.bedrock_gate_tokens import derive_gate_bearer
        from core.services import network_proof

        boots = [json.loads(e["headers"][rt.BOOT_HEADER]) for e in captured]
        assert len(boots) == 2 and boots[0] == boots[1] == {
            "gatewayToken": "gw-a",
            "hookToken": "hook-a",
            "bedrockBearer": derive_gate_bearer("org_a", "row-1", 0),
            "networkKey": "seed-org_a-row-1",
            "browserProfileId": "prof-a",
            "networkWorkers": True,
            "maxConcurrentTurns": 6,
            **({"issuerKey": network_proof.public_key()["env"]} if settings.NETWORK_PROOF_KEY else {}),
        }

    def test_no_gateway_token_no_boot_values(self):
        assert rt._boot_header({"owner_id": "org_a", "hook_token": "h"}) == {}

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
        # The budget LEFT (after the wake gate) plus the margin; nothing waited here, so ~all of it.
        assert seen["config"].read_timeout == pytest.approx(45.0 + 5.0, abs=0.5)
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


class TestNotReadyRetry:
    """Optimistic-create contract: a CREATING runtime fails fast and the
    transport retries with backoff instead of anyone polling for READY."""

    def _not_ready_error(self):
        from botocore.exceptions import ClientError

        return ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Agent runtime is still creating"}},
            "InvokeAgentRuntime",
        )

    def _ok_body(self):
        import json as _json

        class _Body:
            def read(self):
                return _json.dumps({"status": 200, "body": {"ok": True}}).encode()

        return {"statusCode": 200, "response": _Body()}

    @pytest.mark.asyncio
    async def test_not_ready_then_success_returns_200(self, monkeypatch):
        calls = []
        monkeypatch.setattr(rt, "_NOT_READY_SLEEP_S", (0.0, 0.0, 0.0, 0.0, 0.0))
        err = self._not_ready_error()
        ok = self._ok_body()
        # Simpler deterministic stub: fail twice with not-ready, then succeed.
        outcomes = [err, err, ok]

        def _next(self, **kw):
            calls.append(kw)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(
            "boto3.client",
            lambda *a, **k: type("C", (), {"invoke_agent_runtime": _next})(),
        )
        res = await rt.request({"agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x", timeout_s=5.0)
        assert res.status_code == 200
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_persistent_not_ready_raises_connect_error(self, monkeypatch):
        import httpx

        calls = []
        err = self._not_ready_error()
        monkeypatch.setattr(rt, "_NOT_READY_SLEEP_S", (0.0, 0.0))
        monkeypatch.setattr(rt.time, "sleep", lambda s: calls.append(s))

        def _next(self, **kw):
            raise err

        monkeypatch.setattr(
            "boto3.client",
            lambda *a, **k: type("C", (), {"invoke_agent_runtime": _next})(),
        )
        with pytest.raises(httpx.ConnectError):
            await rt.request({"agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x", timeout_s=5.0)
        # Initial attempt + 2 budgeted retries, with the contracted delays.
        assert calls == [0.0, 0.0]

    @pytest.mark.asyncio
    async def test_unrecognized_client_error_fails_immediately(self, monkeypatch):
        import httpx
        from botocore.exceptions import ClientError

        calls = []
        monkeypatch.setattr(rt, "_NOT_READY_SLEEP_S", (0.0, 0.0))

        def _next(self, **kw):
            calls.append(kw)
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                "InvokeAgentRuntime",
            )

        monkeypatch.setattr(
            "boto3.client",
            lambda *a, **k: type("C", (), {"invoke_agent_runtime": _next})(),
        )
        with pytest.raises(httpx.ConnectError):
            await rt.request({"agentcore_runtime_arn": "arn:x", "owner_id": "o"}, "/x", timeout_s=5.0)
        assert len(calls) == 1


@pytest.fixture(autouse=True)
def _canonical_pool_configured(monkeypatch):
    monkeypatch.setattr("core.config.settings.HERMES_SHARED_RUNTIME_ARN", "arn:runtime/abc")
