"""The official a2a-sdk client against our A2A door: card discovery, SendMessage, GetTask."""

import pytest
from a2a.client import A2ACardResolver, ClientCallContext, ClientConfig, ClientFactory
from a2a.client.service_parameters import ServiceParametersFactory, with_a2a_extensions
from a2a.types import GetTaskRequest, Message, Part, Role, SendMessageRequest, TaskState

from tests.unit.routers.conftest import a2a_env, repos  # noqa: F401

URI = "https://thelightbulb.company/a2a/ext/authority/v1"
AUTH = {
    "operation": {"skill": "message", "resource": "agent:friday", "operands": {"text": "hello"}},
    "intent": "say hello",
}

pytestmark = pytest.mark.integration


async def test_official_client_fetches_card_and_sends_message(async_client_org_admin, a2a_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", "http://test")
    card = await A2ACardResolver(async_client_org_admin, "http://test").get_agent_card(
        "/a2a/org_test_456/friday/agent-card.json"
    )
    assert card.capabilities.extensions[0].uri == URI and card.capabilities.extensions[0].required is True
    client = ClientFactory(ClientConfig(httpx_client=async_client_org_admin, streaming=False)).create(card)
    message = Message(message_id="m1", role=Role.ROLE_USER, parts=[Part(text="hello")], extensions=[URI])
    message.metadata.update({URI: AUTH})
    ctx = ClientCallContext(service_parameters=ServiceParametersFactory.create([with_a2a_extensions([URI])]))
    [resp] = [r async for r in client.send_message(SendMessageRequest(message=message), context=ctx)]
    assert resp.task.status.state == TaskState.TASK_STATE_WORKING
    task = await client.get_task(GetTaskRequest(id=resp.task.id), context=ctx)
    assert task.id == resp.task.id and task.metadata[URI]["rule"] == "r1"


async def test_card_signature_verifies_against_keys_json_and_tamper_fails(async_client_org_admin, a2a_env, monkeypatch):  # noqa: F811
    import jwt
    from a2a.utils.signing import InvalidSignaturesError, create_signature_verifier

    monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", "http://test")
    card = await A2ACardResolver(async_client_org_admin, "http://test").get_agent_card(
        "/a2a/org_test_456/friday/agent-card.json"
    )
    jwks = (await async_client_org_admin.get("/a2a/keys.json")).json()["keys"]
    keys = {k["kid"]: jwt.PyJWK.from_dict(k) for k in jwks}
    verify = create_signature_verifier(lambda kid, jku: keys[kid], ["EdDSA"])
    verify(card)
    card.name = "Mallory"
    with pytest.raises(InvalidSignaturesError):
        verify(card)
