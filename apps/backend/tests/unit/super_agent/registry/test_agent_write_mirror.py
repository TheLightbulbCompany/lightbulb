"""Every Paperclip agent write mirrors into the AGENT# registry (wave1-R Task 3, review focus 1)."""

import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.repositories import agent_registry_repo as repo
from core.services import agent_registry
from core.services.paperclip_admin_client import PaperclipAdminClient

AID = "22222222-2222-4222-8222-222222222222"
CO = "co-1"
BACKEND = Path(__file__).resolve().parents[4]


def _agent(**over):
    return {"id": AID, "companyId": CO, "name": "Research", "role": "researcher", "status": "idle"} | over


def _client(state):
    """A real admin client over a fake Paperclip that keeps one agent."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == f"/api/companies/{CO}/agents":
            state.update(_agent(name=json.loads(req.content)["name"]))
        elif req.method == "PATCH":
            state.update(json.loads(req.content))
        elif req.url.path.endswith("/pause"):
            state["status"] = "paused"
        elif req.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json=dict(state))

    return PaperclipAdminClient(
        http_client=httpx.AsyncClient(base_url="http://pc", transport=httpx.MockTransport(handler))
    )


@pytest.fixture
def company_lookup():
    ws = MagicMock(org_id="org_a")
    pc_repo = MagicMock()
    pc_repo.find_workspace_by_company = AsyncMock(side_effect=lambda c: ws if c == CO else None)
    with patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=pc_repo):
        yield pc_repo


async def _write(client, op):
    if op == "create":
        return await client.create_agent(
            session_cookie="c",
            company_id=CO,
            name="Research",
            role="researcher",
            adapter_type="hermes_gateway",
            adapter_config={},
        )
    if op == "patch":
        return await client.patch_agent(session_cookie="c", agent_id=AID, body={"name": "Renamed"})
    if op == "update":
        return await client.update_agent(session_cookie="c", agent_id=AID, title="Lead")
    if op == "pause":
        return await client.pause_agent(session_cookie="c", agent_id=AID)
    return await client.delete_agent(session_cookie="c", agent_id=AID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "expect"),
    [
        ("create", {"name": "Research", "status": "active"}),
        ("patch", {"name": "Renamed"}),
        ("update", {"title": "Lead"}),
        ("pause", {"status": "paused"}),
        ("delete", {"status": "deleted"}),
    ],
)
async def test_each_admin_write_mirrors(tasks_table, company_lookup, op, expect):
    state = _agent()
    client = _client(state)
    if op != "create":
        await repo.put_new("org_a", agent_registry.row_from_paperclip(state))
    await _write(client, op)
    row = await repo.get("org_a", AID)
    assert {k: row[k] for k in expect} == expect
    assert await repo.owner_of(AID) == "org_a"


@pytest.mark.asyncio
async def test_mirror_failure_never_fails_the_paperclip_write(ddb, company_lookup):
    """Env master on but the table is missing: the admin call still returns Paperclip's agent."""
    with patch.object(agent_registry, "put_metric") as metric:
        agent = await _write(_client(_agent()), "create")
    assert agent["id"] == AID
    metric.assert_called_with("agent_registry.mirror", dimensions={"result": "error"})


@pytest.mark.asyncio
async def test_mirror_unknown_company_logs_and_skips(tasks_table, company_lookup, caplog):
    await agent_registry.mirror_from_paperclip(_agent(companyId="co-unknown"))
    assert await repo.owner_of(AID) is None
    assert "agent_registry.mirror_skipped" in caplog.text
    assert "Research" not in caplog.text  # ids only


@pytest.mark.asyncio
async def test_proxied_agent_write_mirrors(tasks_table, company_lookup):
    """The Paperclip UI's edits reach Paperclip through routers/paperclip_proxy, not the admin client."""
    from routers import paperclip_proxy

    await paperclip_proxy._mirror_agent_write(
        "PATCH", f"api/agents/{AID}", 200, json.dumps(_agent(name="Via UI")).encode()
    )
    assert (await repo.get("org_a", AID))["name"] == "Via UI"
    await paperclip_proxy._mirror_agent_write("DELETE", f"api/agents/{AID}", 200, b"")
    assert (await repo.get("org_a", AID))["status"] == "deleted"


@pytest.mark.asyncio
async def test_rejected_hire_mirrors_the_termination(tasks_table, company_lookup):
    """Paperclip terminates a rejected hire on its own side; the reject re-reads and mirrors it."""
    state = _agent(status="pending_approval")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/approvals/ap1/reject":
            state["status"] = "terminated"
            return httpx.Response(200, json={"id": "ap1", "status": "rejected", "payload": {"agentId": AID}})
        return httpx.Response(200, json=dict(state))

    client = PaperclipAdminClient(
        http_client=httpx.AsyncClient(base_url="http://pc", transport=httpx.MockTransport(handler))
    )
    await repo.put_new("org_a", agent_registry.row_from_paperclip(state))
    await client.reject_approval(session_cookie="c", approval_id="ap1", reason="no")
    assert (await repo.get("org_a", AID))["status"] == "deleted"


# Agent write routes on Paperclip: create, and PATCH/DELETE/pause/resume of one agent.
_AGENT_WRITE = re.compile(r"/api/(?:companies/[^/\"']+/agents|agents/[^/\"']+(?:/(?:pause|resume|terminate))?)[\"']?$")


def test_all_write_doors_use_the_admin_client():
    """No module outside the admin client (and the mirrored proxy) calls Paperclip's agent write routes."""
    offenders = []
    for root in ("core", "routers", "scripts"):
        for path in (BACKEND / root).rglob("*.py"):
            if path.name in {"paperclip_admin_client.py", "paperclip_proxy.py"}:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in {
                    "post",
                    "patch",
                    "delete",
                    "put",
                    "request",
                }:
                    text = ast.unparse(node.args[0]) if node.args else ""
                    if "/api/" in text and _AGENT_WRITE.search(text.rstrip(")")):
                        offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert offenders == []
