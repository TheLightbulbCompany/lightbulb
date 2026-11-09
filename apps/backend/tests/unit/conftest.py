import pytest


@pytest.fixture(autouse=True)
def _hints_drained_inline(request, monkeypatch):
    """publish_resources_changed fans out in a background task since #3909. Tests across the tree
    assert the hint right after the call that caused it, so drain it inline for them; the two
    tests of the background behaviour itself opt out by name."""
    name = request.node.name
    if "off_the_callers_frame" in name or "coalesce" in name:
        return
    import asyncio as _asyncio

    from core.services import local_session_relay as _relay

    real = _relay.publish_resources_changed

    async def drained(*args, **kwargs):
        await real(*args, **kwargs)
        await _asyncio.gather(*list(_relay._hint_tasks))

    monkeypatch.setattr(_relay, "publish_resources_changed", drained)


@pytest.fixture(autouse=True)
def _tasks_table_under_moto(request, monkeypatch):
    """Since the super-agent switch retired, every workspace reads the `tasks` table (workspace META, the agent
    registry, Questions, requests). A test that builds its moto tables by hand gets that table too, created the
    first time `get_table("tasks")` is asked for it. ponytail: one describe per lookup; per-file fixtures if slow.
    The registry suite builds (or leaves out) the table on purpose."""
    if "/super_agent/registry/" in str(request.node.fspath):
        return
    from moto.core.models import MockAWS

    from core import dynamodb

    real = dynamodb._get_resource

    class _Resource:
        def __init__(self, resource):
            self._resource = resource

        def __getattr__(self, name):
            return getattr(self._resource, name)

        def Table(self, name):  # noqa: N802 -- boto3's name
            if MockAWS._mocks_active and name == dynamodb.table_name("tasks"):
                client = self._resource.meta.client
                try:
                    client.describe_table(TableName=name)
                except Exception as exc:  # noqa: BLE001 -- botocore's per-client error class; match on the code
                    if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ResourceNotFoundException":
                        raise
                    from tests.unit.super_agent.registry.conftest import _create_tasks_table

                    _create_tasks_table(self._resource)
            return self._resource.Table(name)

    monkeypatch.setattr(dynamodb, "_get_resource", lambda: _Resource(real()))


@pytest.fixture(autouse=True)
def _no_registry_outside_moto(request, monkeypatch):
    """A test with no moto DynamoDB (repos mocked by hand) predates the always-on registry: its workspace has no
    `AGENT#` rows, so no front agent and no Router room, exactly the room-run path it pins. The super-agent suites
    mock the registry their own way and keep the real one."""
    if "/super_agent/" in str(request.node.fspath):
        return
    from moto.core.models import MockAWS

    from core.repositories import agent_registry_repo

    real = agent_registry_repo.list_agents

    async def list_agents(*args, **kwargs):
        return await real(*args, **kwargs) if MockAWS._mocks_active else []

    monkeypatch.setattr(agent_registry_repo, "list_agents", list_agents)

    from core.services import session_chat

    real_default = session_chat._default_agents

    async def default_agents(*args, **kwargs):  # the Paperclip fallback for a front agent
        return await real_default(*args, **kwargs) if MockAWS._mocks_active else []

    monkeypatch.setattr(session_chat, "_default_agents", default_agents)

    # The same no-registry workspace has neither a saved front-door choice nor a
    # workspace META row. The canonical resolver now reads both before its legacy
    # fallback. Keep real reads under moto and let individual tests override these
    # defaults, just as they override list_agents above.
    from core.repositories import messaging_repo
    from core.repositories.workspace_repo import WorkspaceRepo

    real_voice_agent = messaging_repo.get_voice_agent
    real_owner_row = WorkspaceRepo.resolve_owner_row

    async def voice_agent(*args, **kwargs):
        return await real_voice_agent(*args, **kwargs) if MockAWS._mocks_active else None

    async def owner_row(self, *args, **kwargs):
        return await real_owner_row(self, *args, **kwargs) if MockAWS._mocks_active else None

    monkeypatch.setattr(messaging_repo, "get_voice_agent", voice_agent)
    monkeypatch.setattr(WorkspaceRepo, "resolve_owner_row", owner_row)
