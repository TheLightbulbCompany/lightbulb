# Teams Agent Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add agent management to `/teams` — edit reporting lines, pause/resume, and delete agents (with Friday/Atlas guarded) — while keeping role/title/capabilities/icon editing single-sourced in the `/chat` control panel. Also fold in the kanban drag-bug fix.

**Architecture:** Per the spec ([2026-05-29-teams-agent-management-design.md](../specs/2026-05-29-teams-agent-management-design.md)), data is edited where it lives: Paperclip-only fields (`reports_to`, pause/resume) via the Teams BFF (REST); `delete` via the gateway `agents.delete` RPC (cascades to Paperclip + workspace, guarded pre-forward for seeded agents); OpenClaw-sourced meta stays read-only in `/teams`. No second writer to `params.paperclip`, so no divergence.

**Tech Stack:** Python 3.12 / FastAPI / pytest (backend); Next.js / React / TypeScript / SWR / vitest + @testing-library/react (frontend); `@dnd-kit` (kanban).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `apps/backend/core/services/paperclip_admin_client.py` | Paperclip REST client | **Modify**: add `pause_agent` / `resume_agent` |
| `apps/backend/routers/teams/agents.py` | Teams BFF agent routes | **Modify**: add `POST /agents/{id}/pause` + `/resume` |
| `apps/backend/routers/websocket_chat.py` | gateway RPC `$default` | **Modify**: pre-forward seeded-delete guard |
| `apps/backend/core/services/paperclip_openclaw_pairing.py` | Paperclip↔OpenClaw helpers | **Modify**: add `is_seeded_agent` helper |
| `apps/frontend/src/components/teams/shared/types.ts` | shared types | **Modify**: extend `CompanyAgent` |
| `apps/frontend/src/components/teams/agents/hooks/useAgentMutations.ts` | agent write hook | **Create** |
| `apps/frontend/src/components/teams/agents/ReportsToPicker.tsx` | manager picker | **Create** |
| `apps/frontend/src/components/teams/agents/AgentManageSection.tsx` | manage UI (reports-to + actions + read-only meta) | **Create** |
| `apps/frontend/src/components/teams/panels/AgentDetailPanel.tsx` | agent detail hub | **Modify**: mount `AgentManageSection` |
| `apps/frontend/src/components/teams/panels/AgentsListPanel.tsx` | agents list | **Modify**: row quick actions |
| `apps/frontend/src/components/teams/inbox/KanbanCard.tsx` | kanban card | **Modify**: drag fix (started) |

Seeded-agent rule (used in several tasks): an agent is **seeded** iff `role == "ceo"` (Friday) **or** `name == "Atlas"`. Centralize this as one helper per layer so the rule never drifts.

---

## Task 1: `pause_agent` / `resume_agent` on the admin client

**Files:**
- Modify: `apps/backend/core/services/paperclip_admin_client.py` (after `delete_agent`, ~line 699)
- Test: `apps/backend/tests/unit/services/test_paperclip_admin_client_pause_resume.py` (create)

- [ ] **Step 1.1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_paperclip_admin_client_pause_resume.py
import httpx
import pytest

from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError


def _client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://paperclip.local", transport=transport)
    return PaperclipAdminClient(http_client=http), http


@pytest.mark.asyncio
async def test_pause_agent_posts_to_pause_route():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"id": "a1", "status": "paused"})

    client, http = _client(handler)
    try:
        out = await client.pause_agent(session_cookie="c", agent_id="a1")
    finally:
        await http.aclose()
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/agents/a1/pause")
    assert out["status"] == "paused"


@pytest.mark.asyncio
async def test_resume_agent_posts_to_resume_route():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "a1", "status": "idle"})

    client, http = _client(handler)
    try:
        await client.resume_agent(session_cookie="c", agent_id="a1")
    finally:
        await http.aclose()
    assert seen["url"].endswith("/api/agents/a1/resume")


@pytest.mark.asyncio
async def test_pause_agent_raises_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client, http = _client(handler)
    try:
        with pytest.raises(PaperclipApiError) as exc:
            await client.pause_agent(session_cookie="c", agent_id="a1")
    finally:
        await http.aclose()
    assert exc.value.status_code == 500
```

- [ ] **Step 1.2: Run it — verify FAIL**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_admin_client_pause_resume.py -v`
Expected: FAIL — `AttributeError: 'PaperclipAdminClient' object has no attribute 'pause_agent'`.

- [ ] **Step 1.3: Implement** — add directly after the `delete_agent` method (~line 699):

```python
    async def pause_agent(self, *, session_cookie: str, agent_id: str) -> dict:
        """Pause an agent (stops it running on events).

        Maps to ``POST /api/agents/{agentId}/pause`` (upstream
        ``paperclip/server/src/routes/agents.ts``). Not company-scoped —
        Paperclip authorizes from the session cookie.
        """
        return await self._post(
            f"/api/agents/{agent_id}/pause",
            json={},
            session_cookie=session_cookie,
        )

    async def resume_agent(self, *, session_cookie: str, agent_id: str) -> dict:
        """Resume a paused agent. Maps to ``POST /api/agents/{agentId}/resume``."""
        return await self._post(
            f"/api/agents/{agent_id}/resume",
            json={},
            session_cookie=session_cookie,
        )
```

- [ ] **Step 1.4: Run it — verify PASS**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_admin_client_pause_resume.py -v`
Expected: PASS (3 tests).

- [ ] **Step 1.5: Commit**

```bash
git add apps/backend/core/services/paperclip_admin_client.py apps/backend/tests/unit/services/test_paperclip_admin_client_pause_resume.py
git commit -m "feat(teams): paperclip admin client pause_agent/resume_agent"
```

---

## Task 2: BFF pause/resume routes

**Files:**
- Modify: `apps/backend/routers/teams/agents.py` (after `delete_agent`, ~line 432)
- Test: `apps/backend/tests/unit/routers/teams/test_agents_pause_resume.py` (create)

- [ ] **Step 2.1: Write the failing test**

```python
# apps/backend/tests/unit/routers/teams/test_agents_pause_resume.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers.teams import agents as agents_mod
from routers.teams.deps import TeamsContext


def _ctx():
    return TeamsContext(
        user_id="u1", org_id="org1", owner_id="org1",
        company_id="co1", paperclip_user_id="pcu1", session_cookie="cookie1",
    )


@pytest.mark.asyncio
async def test_pause_agent_route_calls_admin_pause():
    fake_admin = MagicMock()
    fake_admin.pause_agent = AsyncMock(return_value={"id": "a1", "status": "paused"})
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        out = await agents_mod.pause_agent("a1", ctx=_ctx())
    fake_admin.pause_agent.assert_awaited_once_with(agent_id="a1", session_cookie="cookie1")
    assert out["status"] == "paused"


@pytest.mark.asyncio
async def test_resume_agent_route_calls_admin_resume():
    fake_admin = MagicMock()
    fake_admin.resume_agent = AsyncMock(return_value={"id": "a1", "status": "idle"})
    with patch("routers.teams.agents._admin", return_value=fake_admin):
        out = await agents_mod.resume_agent("a1", ctx=_ctx())
    fake_admin.resume_agent.assert_awaited_once_with(agent_id="a1", session_cookie="cookie1")
    assert out["status"] == "idle"
```

- [ ] **Step 2.2: Run it — verify FAIL**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_agents_pause_resume.py -v`
Expected: FAIL — `AttributeError: module 'routers.teams.agents' has no attribute 'pause_agent'`.

- [ ] **Step 2.3: Implement** — add after the `delete_agent` handler (~line 432) in `agents.py`:

```python
@router.post("/agents/{agent_id}/pause")
async def pause_agent(agent_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Pause an agent (stops it running on events)."""
    return await _admin().pause_agent(
        agent_id=agent_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/agents/{agent_id}/resume")
async def resume_agent(agent_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Resume a paused agent."""
    return await _admin().resume_agent(
        agent_id=agent_id,
        session_cookie=ctx.session_cookie,
    )
```

- [ ] **Step 2.4: Run it — verify PASS**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_agents_pause_resume.py -v`
Expected: PASS (2 tests).

- [ ] **Step 2.5: Commit**

```bash
git add apps/backend/routers/teams/agents.py apps/backend/tests/unit/routers/teams/test_agents_pause_resume.py
git commit -m "feat(teams): BFF pause/resume agent routes"
```

---

## Task 3: `is_seeded_agent` helper

**Files:**
- Modify: `apps/backend/core/services/paperclip_openclaw_pairing.py` (add a helper; it already has `fetch_paperclip_agents_for_owner`)
- Test: `apps/backend/tests/unit/services/test_is_seeded_agent.py` (create)

- [ ] **Step 3.1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_is_seeded_agent.py
from unittest.mock import AsyncMock, patch

import pytest

from core.services import paperclip_openclaw_pairing as pop


@pytest.mark.asyncio
async def test_friday_ceo_is_seeded():
    agents = [{"id": "a1", "name": "Friday", "role": "ceo"}, {"id": "a2", "name": "Bob", "role": "engineer"}]
    with patch.object(pop, "fetch_paperclip_agents_for_owner", new=AsyncMock(return_value=agents)):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a1") is True


@pytest.mark.asyncio
async def test_atlas_by_name_is_seeded():
    agents = [{"id": "a3", "name": "Atlas", "role": "general"}]
    with patch.object(pop, "fetch_paperclip_agents_for_owner", new=AsyncMock(return_value=agents)):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a3") is True


@pytest.mark.asyncio
async def test_regular_agent_not_seeded():
    agents = [{"id": "a2", "name": "Bob", "role": "engineer"}]
    with patch.object(pop, "fetch_paperclip_agents_for_owner", new=AsyncMock(return_value=agents)):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="a2") is False


@pytest.mark.asyncio
async def test_unknown_agent_not_seeded():
    with patch.object(pop, "fetch_paperclip_agents_for_owner", new=AsyncMock(return_value=[])):
        assert await pop.is_seeded_agent(owner_id="o1", agent_id="ghost") is False
```

- [ ] **Step 3.2: Run it — verify FAIL**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_is_seeded_agent.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'is_seeded_agent'`.

- [ ] **Step 3.3: Implement** — `fetch_paperclip_agents_for_owner` is keyword-only and takes injected deps `(*, owner_id, repo, admin_client)`, so the helper constructs them itself (mirroring `paperclip_agent_lifecycle.delete_paperclip_for_chat`). Add `from core.config import settings` to the imports (the module imports `httpx`, `PaperclipRepo`, `PaperclipAdminClient` already but not `settings`). Then add:

```python
async def is_seeded_agent(*, owner_id: str, agent_id: str) -> bool:
    """Whether ``agent_id`` is an Isol8-seeded agent (Friday or Atlas).

    Seeded agents are part of the provisioning contract and must not be
    deleted. Identification: Friday = the ``role == "ceo"`` agent (robust
    against rename); Atlas = ``name == "Atlas"``. (A durable
    ``params.paperclip.seeded`` marker is future hardening — see roadmap I.)

    Best-effort: ``fetch_paperclip_agents_for_owner`` already returns [] on
    Paperclip availability issues; we also swallow construction errors and
    a not-found agent so a transient lookup never blocks deletion.
    """
    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        agents = await fetch_paperclip_agents_for_owner(
            owner_id=owner_id,
            repo=PaperclipRepo(table_name="paperclip-companies"),
            admin_client=PaperclipAdminClient(http_client=http),
        )
    except Exception:  # noqa: BLE001 — never block on a lookup failure
        return False
    finally:
        await http.aclose()
    for a in agents:
        if a.get("id") == agent_id:
            return a.get("role") == "ceo" or a.get("name") == "Atlas"
    return False
```

The Step 3.1 test patches `fetch_paperclip_agents_for_owner` with an `AsyncMock`, so the real `repo`/`admin_client`/HTTP construction is bypassed — the test is unchanged.

- [ ] **Step 3.4: Run it — verify PASS**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_is_seeded_agent.py -v`
Expected: PASS (4 tests).

- [ ] **Step 3.5: Commit**

```bash
git add apps/backend/core/services/paperclip_openclaw_pairing.py apps/backend/tests/unit/services/test_is_seeded_agent.py
git commit -m "feat(teams): is_seeded_agent helper (Friday/Atlas identification)"
```

---

## Task 4: Pre-forward seeded-delete guard in the gateway `$default`

The `agents.delete` cleanup at `websocket_chat.py:967` runs *after* OpenClaw deleted the agent — too late to guard. Add the guard **before** `pool.send_rpc` (line 940), beside the `agents.create`/`agents.update` interceptors.

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` (before `pool = get_gateway_pool()`, ~line 940)
- Test: `apps/backend/tests/unit/routers/test_websocket_chat_delete_guard.py` (create)

- [ ] **Step 4.1: Write the failing test**

```python
# apps/backend/tests/unit/routers/test_websocket_chat_delete_guard.py
from unittest.mock import AsyncMock, patch

import pytest

from routers import websocket_chat as wc


@pytest.mark.asyncio
async def test_seeded_delete_is_rejected_before_forward():
    sent = {}

    def send_message(connection_id, payload):
        sent["payload"] = payload

    with (
        patch.object(wc, "is_seeded_agent", new=AsyncMock(return_value=True)),
        patch.object(wc, "management_api") as mgmt,
    ):
        mgmt.send_message.side_effect = send_message
        consumed = await wc._guard_seeded_delete(
            owner_id="o1", method="agents.delete",
            params={"agentId": "friday-uuid"},
            connection_id="conn1", req_id="r1",
        )
    assert consumed is True
    assert sent["payload"]["ok"] is False
    assert "can't be deleted" in sent["payload"]["error"]["message"].lower()


@pytest.mark.asyncio
async def test_non_seeded_delete_is_not_consumed():
    with patch.object(wc, "is_seeded_agent", new=AsyncMock(return_value=False)):
        consumed = await wc._guard_seeded_delete(
            owner_id="o1", method="agents.delete",
            params={"agentId": "bob-uuid"},
            connection_id="conn1", req_id="r1",
        )
    assert consumed is False


@pytest.mark.asyncio
async def test_non_delete_method_is_not_consumed():
    consumed = await wc._guard_seeded_delete(
        owner_id="o1", method="agents.update",
        params={"agentId": "x"}, connection_id="c", req_id="r",
    )
    assert consumed is False
```

- [ ] **Step 4.2: Run it — verify FAIL**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat_delete_guard.py -v`
Expected: FAIL — `AttributeError: module 'routers.websocket_chat' has no attribute '_guard_seeded_delete'`.

- [ ] **Step 4.3: Implement**

First add the import near the other `paperclip_openclaw_pairing` imports at the top of `websocket_chat.py`:

```python
from core.services.paperclip_openclaw_pairing import is_seeded_agent
```

Add the guard helper (module level, near the other `_intercept_*` helpers):

```python
async def _guard_seeded_delete(
    *,
    owner_id: str,
    method: str,
    params: dict | None,
    connection_id: str,
    req_id: str,
) -> bool:
    """Reject deletion of seeded agents (Friday/Atlas) BEFORE forwarding the
    RPC to OpenClaw (the post-forward cleanup is too late — the container has
    already deleted the agent by then).

    Returns True if it consumed the request (sent an error res; caller must
    return without forwarding). False otherwise (caller forwards normally).
    """
    if method != "agents.delete":
        return False
    agent_id = (params or {}).get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return False
    if not await is_seeded_agent(owner_id=owner_id, agent_id=agent_id):
        return False
    management_api.send_message(
        connection_id,
        {
            "type": "res",
            "id": req_id,
            "ok": False,
            "error": {
                "message": "Friday and Atlas are managed by Isol8 and can't be deleted.",
            },
        },
    )
    return True
```

Wire it in immediately **before** `pool = get_gateway_pool()` (line 940):

```python
        if await _guard_seeded_delete(
            owner_id=owner_id,
            method=method,
            params=params,
            connection_id=connection_id,
            req_id=req_id,
        ):
            return

        pool = get_gateway_pool()
```

- [ ] **Step 4.4: Run it — verify PASS**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat_delete_guard.py -v`
Expected: PASS (3 tests).

- [ ] **Step 4.5: Commit**

```bash
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_chat_delete_guard.py
git commit -m "feat(teams): block seeded-agent deletion pre-forward in gateway \$default"
```

---

## Task 5: Extend the `CompanyAgent` type

**Files:**
- Modify: `apps/frontend/src/components/teams/shared/types.ts` (the `CompanyAgent` interface, ~line 185)

- [ ] **Step 5.1: Replace the interface** (no test — type-only; consumers in later tasks exercise it):

```ts
export interface CompanyAgent {
  id: string;
  name: string;
  iconUrl?: string | null;
  /** Paperclip agent role e.g. "ceo" | "engineer" | "general". */
  role?: string | null;
  /** Short one-liner shown in the roster. */
  title?: string | null;
  /** Longer freeform description Friday reads when orchestrating. */
  capabilities?: string | null;
  /** Paperclip icon-picker value (opaque AGENT_ICON_NAMES string). */
  icon?: string | null;
  /** Runtime status: "idle" | "running" | "error" | "paused" | "terminated". */
  status?: string | null;
  /** ID of the agent this one reports to (null/absent = root). */
  reportsTo?: string | null;
}
```

- [ ] **Step 5.2: Typecheck**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: PASS (no new type errors; `OrgChartAgent extends CompanyAgent` still valid — it re-declares `reportsTo`/`status`/`role` compatibly).

- [ ] **Step 5.3: Commit**

```bash
git add apps/frontend/src/components/teams/shared/types.ts
git commit -m "feat(teams): extend CompanyAgent with role/title/capabilities/icon/status/reportsTo"
```

---

## Task 6: `useAgentMutations` hook

reports_to / pause / resume go via the BFF (`useTeamsApi`); delete goes via the gateway RPC (`useGatewayRpcMutation`). All invalidate the `/agents` SWR key so the org chart + list refresh.

**Files:**
- Create: `apps/frontend/src/components/teams/agents/hooks/useAgentMutations.ts`
- Test: `apps/frontend/src/__tests__/teams/useAgentMutations.test.tsx` (create)

- [ ] **Step 6.1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/useAgentMutations.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const patch = vi.fn();
const post = vi.fn();
const callRpc = vi.fn();
const swrMutate = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: vi.fn(), post, patch, del: vi.fn() }),
}));
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpcMutation: () => callRpc,
}));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";

beforeEach(() => { patch.mockReset(); post.mockReset(); callRpc.mockReset(); swrMutate.mockReset(); });

describe("useAgentMutations", () => {
  it("setReportsTo PATCHes reportsTo and revalidates /agents", async () => {
    patch.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.setReportsTo("a1", "mgr1"); });
    expect(patch).toHaveBeenCalledWith("/agents/a1", { reportsTo: "mgr1" });
    expect(swrMutate).toHaveBeenCalled();
  });

  it("setReportsTo with null sends empty string (no-manager)", async () => {
    patch.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.setReportsTo("a1", null); });
    expect(patch).toHaveBeenCalledWith("/agents/a1", { reportsTo: "" });
  });

  it("pause/resume POST to the right routes", async () => {
    post.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.pause("a1"); });
    expect(post).toHaveBeenCalledWith("/agents/a1/pause", {});
    await act(async () => { await result.current.resume("a1"); });
    expect(post).toHaveBeenCalledWith("/agents/a1/resume", {});
  });

  it("remove deletes via gateway agents.delete RPC", async () => {
    callRpc.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.remove("a1"); });
    expect(callRpc).toHaveBeenCalledWith("agents.delete", { agentId: "a1", deleteFiles: true });
    expect(swrMutate).toHaveBeenCalled();
  });
});
```

- [ ] **Step 6.2: Run it — verify FAIL**

Run: `cd apps/frontend && pnpm test -- useAgentMutations`
Expected: FAIL — cannot resolve `@/components/teams/agents/hooks/useAgentMutations`.

- [ ] **Step 6.3: Implement**

```ts
// apps/frontend/src/components/teams/agents/hooks/useAgentMutations.ts
"use client";

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";

// All /teams reads use keys prefixed "/teams"; the agents list key is
// "/teams/agents". Invalidate it after every mutation so the org chart and
// agents list re-render with fresh data (status dots also update via events).
const AGENTS_KEY = "/teams/agents";

export function useAgentMutations() {
  const { patch, post } = useTeamsApi();
  const callRpc = useGatewayRpcMutation();
  const { mutate } = useSWRConfig();

  const revalidate = useCallback(() => {
    mutate(AGENTS_KEY);
  }, [mutate]);

  const setReportsTo = useCallback(
    async (agentId: string, managerId: string | null) => {
      // Empty string clears the manager (root); BFF forwards it to Paperclip.
      await patch(`/agents/${agentId}`, { reportsTo: managerId ?? "" });
      revalidate();
    },
    [patch, revalidate],
  );

  const pause = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/pause`, {});
      revalidate();
    },
    [post, revalidate],
  );

  const resume = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/resume`, {});
      revalidate();
    },
    [post, revalidate],
  );

  // Delete goes through the OpenClaw gateway (removes the container entry +
  // cascades to Paperclip via the websocket_chat interceptor). The Teams BFF
  // DELETE would only remove the Paperclip row, which a reprovision resurrects.
  const remove = useCallback(
    async (agentId: string) => {
      await callRpc("agents.delete", { agentId, deleteFiles: true });
      revalidate();
    },
    [callRpc, revalidate],
  );

  return { setReportsTo, pause, resume, remove };
}
```

- [ ] **Step 6.4: Run it — verify PASS**

Run: `cd apps/frontend && pnpm test -- useAgentMutations`
Expected: PASS (4 tests).

- [ ] **Step 6.5: Commit**

```bash
git add apps/frontend/src/components/teams/agents/hooks/useAgentMutations.ts apps/frontend/src/__tests__/teams/useAgentMutations.test.tsx
git commit -m "feat(teams): useAgentMutations (reports-to/pause/resume via BFF, delete via gateway)"
```

---

## Task 7: `ReportsToPicker` component

Self-contained (no upstream vendored). A `<select>` of candidate managers, excluding self and any agent that reports (transitively) to this one (cycle prevention), plus a "No manager" option.

**Files:**
- Create: `apps/frontend/src/components/teams/agents/ReportsToPicker.tsx`
- Test: `apps/frontend/src/__tests__/teams/ReportsToPicker.test.tsx` (create)

- [ ] **Step 7.1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/ReportsToPicker.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { ReportsToPicker, candidateManagers } from "@/components/teams/agents/ReportsToPicker";

const AGENTS: CompanyAgent[] = [
  { id: "a", name: "Friday", reportsTo: null },
  { id: "b", name: "Bob", reportsTo: "a" },
  { id: "c", name: "Cara", reportsTo: "b" },
];

describe("candidateManagers", () => {
  it("excludes self and descendants (cycle prevention)", () => {
    // For Bob (b), candidates exclude b itself and its descendant c.
    const ids = candidateManagers(AGENTS, "b").map((a) => a.id);
    expect(ids).toEqual(["a"]);
  });
});

describe("ReportsToPicker", () => {
  it("renders No manager + candidates and calls onChange", () => {
    const onChange = vi.fn();
    render(<ReportsToPicker agents={AGENTS} agentId="b" value="a" onChange={onChange} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(null);
  });
});
```

- [ ] **Step 7.2: Run it — verify FAIL**

Run: `cd apps/frontend && pnpm test -- ReportsToPicker`
Expected: FAIL — cannot resolve the module.

- [ ] **Step 7.3: Implement**

```tsx
// apps/frontend/src/components/teams/agents/ReportsToPicker.tsx
"use client";

import type { CompanyAgent } from "@/components/teams/shared/types";

/** Agents eligible to be `agentId`'s manager: everyone except itself and its
 *  descendants (assigning a descendant would create a cycle). */
export function candidateManagers(agents: CompanyAgent[], agentId: string): CompanyAgent[] {
  const descendants = new Set<string>([agentId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const a of agents) {
      if (a.reportsTo && descendants.has(a.reportsTo) && !descendants.has(a.id)) {
        descendants.add(a.id);
        grew = true;
      }
    }
  }
  return agents.filter((a) => !descendants.has(a.id));
}

export function ReportsToPicker({
  agents,
  agentId,
  value,
  onChange,
  disabled,
}: {
  agents: CompanyAgent[];
  agentId: string;
  value: string | null | undefined;
  onChange: (managerId: string | null) => void;
  disabled?: boolean;
}) {
  const candidates = candidateManagers(agents, agentId);
  return (
    <select
      aria-label="Reports to"
      disabled={disabled}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 text-[var(--ink)]"
    >
      <option value="">No manager (top level)</option>
      {candidates.map((a) => (
        <option key={a.id} value={a.id}>
          {a.name}
        </option>
      ))}
    </select>
  );
}
```

- [ ] **Step 7.4: Run it — verify PASS**

Run: `cd apps/frontend && pnpm test -- ReportsToPicker`
Expected: PASS (2 tests).

- [ ] **Step 7.5: Commit**

```bash
git add apps/frontend/src/components/teams/agents/ReportsToPicker.tsx apps/frontend/src/__tests__/teams/ReportsToPicker.test.tsx
git commit -m "feat(teams): ReportsToPicker with cycle-safe candidate manager list"
```

---

## Task 8: `AgentManageSection` + mount in `AgentDetailPanel`

Read-only meta (role/title/capabilities/icon) + a "manage in control panel" pointer; `ReportsToPicker` (BFF); pause/resume + delete actions (guarded for seeded).

**Files:**
- Create: `apps/frontend/src/components/teams/agents/AgentManageSection.tsx`
- Modify: `apps/frontend/src/components/teams/panels/AgentDetailPanel.tsx` (replace the Overview JSON dump + Config tab)
- Test: `apps/frontend/src/__tests__/teams/AgentManageSection.test.tsx` (create)

- [ ] **Step 8.1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/AgentManageSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { CompanyAgent } from "@/components/teams/shared/types";

const setReportsTo = vi.fn();
const pause = vi.fn();
const resume = vi.fn();
const remove = vi.fn();
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({ setReportsTo, pause, resume, remove }),
}));

import { AgentManageSection, isSeededAgent } from "@/components/teams/agents/AgentManageSection";

const FRIDAY: CompanyAgent = { id: "f", name: "Friday", role: "ceo", status: "idle" };
const BOB: CompanyAgent = { id: "b", name: "Bob", role: "engineer", status: "idle" };
const AGENTS = [FRIDAY, BOB];

beforeEach(() => { setReportsTo.mockReset(); pause.mockReset(); resume.mockReset(); remove.mockReset(); });

describe("isSeededAgent", () => {
  it("flags ceo and Atlas", () => {
    expect(isSeededAgent(FRIDAY)).toBe(true);
    expect(isSeededAgent({ id: "x", name: "Atlas", role: "general" })).toBe(true);
    expect(isSeededAgent(BOB)).toBe(false);
  });
});

describe("AgentManageSection", () => {
  it("hides delete for seeded agents", () => {
    render(<AgentManageSection agent={FRIDAY} agents={AGENTS} />);
    expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();
  });

  it("shows delete for regular agents and calls remove", async () => {
    remove.mockResolvedValue(undefined);
    render(<AgentManageSection agent={BOB} agents={AGENTS} />);
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    // confirm step
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("b"));
  });

  it("renders meta read-only (no role <select>)", () => {
    render(<AgentManageSection agent={BOB} agents={AGENTS} />);
    expect(screen.queryByLabelText(/^role$/i)).toBeNull();
    expect(screen.getByText("engineer")).toBeInTheDocument();
  });
});
```

- [ ] **Step 8.2: Run it — verify FAIL**

Run: `cd apps/frontend && pnpm test -- AgentManageSection`
Expected: FAIL — cannot resolve the module.

- [ ] **Step 8.3: Implement `AgentManageSection.tsx`**

```tsx
// apps/frontend/src/components/teams/agents/AgentManageSection.tsx
"use client";

import { useState } from "react";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { EditorialButton } from "@/components/teams/shell";
import { ReportsToPicker } from "@/components/teams/agents/ReportsToPicker";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";

/** Seeded = Isol8 provisioning contract (Friday/Atlas). Mirror of the server
 *  rule in core/services/paperclip_openclaw_pairing.is_seeded_agent. */
export function isSeededAgent(a: CompanyAgent): boolean {
  return a.role === "ceo" || a.name === "Atlas";
}

function MetaRow({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs uppercase tracking-wide text-[var(--mute)]">{label}</span>
      <span className="text-[var(--ink)]">{value?.trim() ? value : "—"}</span>
    </div>
  );
}

export function AgentManageSection({
  agent,
  agents,
}: {
  agent: CompanyAgent;
  agents: CompanyAgent[];
}) {
  const { setReportsTo, pause, resume, remove } = useAgentMutations();
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const seeded = isSeededAgent(agent);
  const paused = agent.status === "paused";

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    try { await fn(); } finally { setBusy(false); }
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Read-only meta — edited in the agent's control panel */}
      <section className="editorial-card p-4 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="font-editorial-serif text-lg">Profile</h3>
          <a
            href={`/chat?agent=${agent.id}`}
            className="text-sm hover:underline"
            style={{ color: "var(--forest)" }}
          >
            Edit in control panel →
          </a>
        </div>
        <div className="grid grid-cols-2 gap-4">
          <MetaRow label="Role" value={agent.role} />
          <MetaRow label="Title" value={agent.title} />
          <MetaRow label="Icon" value={agent.icon} />
          <MetaRow label="Status" value={agent.status} />
        </div>
        <MetaRow label="Capabilities" value={agent.capabilities} />
        <p className="text-xs text-[var(--mute)]">
          Role, title, capabilities and icon are managed in the agent&apos;s control panel.
        </p>
      </section>

      {/* Reporting line — editable here */}
      <section className="editorial-card p-4 flex flex-col gap-2">
        <h3 className="font-editorial-serif text-lg">Reports to</h3>
        <ReportsToPicker
          agents={agents}
          agentId={agent.id}
          value={agent.reportsTo}
          disabled={busy}
          onChange={(managerId) => run(() => setReportsTo(agent.id, managerId))}
        />
      </section>

      {/* Lifecycle actions */}
      <section className="flex items-center gap-2">
        {paused ? (
          <EditorialButton variant="secondary" size="sm" disabled={busy}
            onClick={() => run(() => resume(agent.id))}>
            Resume
          </EditorialButton>
        ) : (
          <EditorialButton variant="secondary" size="sm" disabled={busy}
            onClick={() => run(() => pause(agent.id))}>
            Pause
          </EditorialButton>
        )}
        {!seeded && !confirmDelete && (
          <EditorialButton variant="secondary" size="sm" disabled={busy}
            onClick={() => setConfirmDelete(true)}>
            Delete
          </EditorialButton>
        )}
        {!seeded && confirmDelete && (
          <>
            <span className="text-sm text-[var(--danger)]">Delete {agent.name}?</span>
            <EditorialButton size="sm" disabled={busy}
              onClick={() => run(() => remove(agent.id))}>
              Confirm
            </EditorialButton>
            <EditorialButton variant="secondary" size="sm" disabled={busy}
              onClick={() => setConfirmDelete(false)}>
              Cancel
            </EditorialButton>
          </>
        )}
      </section>
    </div>
  );
}
```

- [ ] **Step 8.4: Mount it in `AgentDetailPanel.tsx`** — replace the `overview` JSON dump and the disabled `config` tab. The panel already reads `/agents/${agentId}`; also read `/agents` for the picker's candidate list. Replace the file body's render with:

```tsx
  const { read } = useTeamsApi();
  const { data: agent } = read<CompanyAgent>(`/agents/${agentId}`);
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: runs } = read<{ runs: RunRow[] }>(`/agents/${agentId}/runs`);
  const [tab, setTab] = useState<string>("manage");
```

Change `TABS` to:

```tsx
const TABS = [
  { value: "manage", label: "Manage" },
  { value: "runs", label: "Runs" },
];
```

Replace the three `tab ===` blocks with the `manage` + `runs` blocks (keep the existing `runs` block verbatim):

```tsx
      {tab === "manage" && agent && (
        <AgentManageSection agent={agent} agents={agentsData?.agents ?? []} />
      )}
```

Add imports at the top:

```tsx
import type { CompanyAgent } from "@/components/teams/shared/types";
import { AgentManageSection } from "@/components/teams/agents/AgentManageSection";
```

Remove the now-unused local `Agent` interface (use `CompanyAgent`).

- [ ] **Step 8.5: Run it — verify PASS**

Run: `cd apps/frontend && pnpm test -- AgentManageSection && cd apps/frontend && pnpm exec tsc --noEmit`
Expected: tests PASS (5); tsc clean.

- [ ] **Step 8.6: Commit**

```bash
git add apps/frontend/src/components/teams/agents/AgentManageSection.tsx apps/frontend/src/components/teams/panels/AgentDetailPanel.tsx apps/frontend/src/__tests__/teams/AgentManageSection.test.tsx
git commit -m "feat(teams): agent manage section (read-only meta + reports-to + pause/resume/delete)"
```

---

## Task 9: Agents-list row quick actions

Add pause/resume + delete to each row (delete hidden for seeded), reusing `useAgentMutations`.

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/AgentsListPanel.tsx` (the `columns` array + the local `Agent` type)
- Test: extend `apps/frontend/src/__tests__/teams/AgentsListPanel.test.tsx`

- [ ] **Step 9.1: Write the failing test** — append to the existing describe block:

```tsx
  it("row pause action calls pause; delete hidden for ceo", async () => {
    const pause = vi.fn().mockResolvedValue(undefined);
    vi.doMock("@/components/teams/agents/hooks/useAgentMutations", () => ({
      useAgentMutations: () => ({ setReportsTo: vi.fn(), pause, resume: vi.fn(), remove: vi.fn() }),
    }));
    mockRead.mockReturnValueOnce({
      data: { agents: [{ id: "f", name: "Friday", role: "ceo", status: "idle" }] },
      isLoading: false, error: null, mutate: mockMutate,
    });
    const { AgentsListPanel: Panel } = await import("@/components/teams/panels/AgentsListPanel");
    render(<Panel />);
    expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /pause/i }));
    await waitFor(() => expect(pause).toHaveBeenCalledWith("f"));
  });
```

- [ ] **Step 9.2: Run it — verify FAIL**

Run: `cd apps/frontend && pnpm test -- AgentsListPanel`
Expected: FAIL — no pause button rendered yet.

- [ ] **Step 9.3: Implement** — in `AgentsListPanel.tsx`: extend the local `Agent` type with `status?: string` (already present) and add `role` usage; import the hook and `isSeededAgent`; add an `actions` column before `open`:

```tsx
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { isSeededAgent } from "@/components/teams/agents/AgentManageSection";
```

Inside the component, get the hook: `const { pause, resume, remove } = useAgentMutations();` and add this column to `columns` (before the `open` column):

```tsx
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (a) => {
        const seeded = isSeededAgent(a as unknown as CompanyAgent);
        const paused = a.status === "paused";
        return (
          <span className="flex items-center justify-end gap-2">
            <button
              className="text-sm hover:underline text-[var(--ink-soft)]"
              onClick={() => (paused ? resume(a.id) : pause(a.id))}
            >
              {paused ? "Resume" : "Pause"}
            </button>
            {!seeded && (
              <button
                className="text-sm hover:underline text-[var(--danger)]"
                onClick={() => {
                  if (window.confirm(`Delete ${a.name}? This removes the agent and its workspace.`)) {
                    remove(a.id);
                  }
                }}
              >
                Delete
              </button>
            )}
          </span>
        );
      },
    },
```

Add the import `import type { CompanyAgent } from "@/components/teams/shared/types";` and ensure the local `Agent` interface includes `role: string`.

- [ ] **Step 9.4: Run it — verify PASS**

Run: `cd apps/frontend && pnpm test -- AgentsListPanel`
Expected: PASS.

- [ ] **Step 9.5: Commit**

```bash
git add apps/frontend/src/components/teams/panels/AgentsListPanel.tsx apps/frontend/src/__tests__/teams/AgentsListPanel.test.tsx
git commit -m "feat(teams): agents-list row pause/resume/delete quick actions"
```

---

## Task 10: Fold in the kanban drag fix

The `draggable={false}` change is already in the worktree (`KanbanCard.tsx`). Commit it with a regression test asserting the card Link disables native drag.

**Files:**
- Modify: `apps/frontend/src/components/teams/inbox/KanbanCard.tsx` (already changed)
- Test: `apps/frontend/src/__tests__/components/teams/inbox/KanbanCard.test.tsx` (create)

- [ ] **Step 10.1: Write the test**

```tsx
// apps/frontend/src/__tests__/components/teams/inbox/KanbanCard.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@dnd-kit/sortable", () => ({
  useSortable: () => ({ attributes: {}, listeners: {}, setNodeRef: () => {}, transform: null, transition: undefined, isDragging: false }),
}));
vi.mock("@dnd-kit/utilities", () => ({ CSS: { Transform: { toString: () => undefined } } }));

import { KanbanCard } from "@/components/teams/inbox/KanbanCard";
import type { Issue } from "@/components/teams/shared/types";

const ISSUE = { id: "i1", title: "Test", status: "todo" } as Issue;

describe("KanbanCard", () => {
  it("disables native anchor drag so dnd-kit can drive the sort", () => {
    render(<KanbanCard issue={ISSUE} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("draggable", "false");
    expect(link).toHaveAttribute("href", "/teams/issues/i1");
  });
});
```

- [ ] **Step 10.2: Run it — verify PASS** (the fix is already in place)

Run: `cd apps/frontend && pnpm test -- KanbanCard`
Expected: PASS. (If it fails because `draggable={false}` is missing, add it to the `<Link>` in `KanbanCard.tsx`.)

- [ ] **Step 10.3: Commit**

```bash
git add apps/frontend/src/components/teams/inbox/KanbanCard.tsx apps/frontend/src/__tests__/components/teams/inbox/KanbanCard.test.tsx
git commit -m "fix(teams): disable native anchor drag on kanban cards so dnd-kit drag works"
```

---

## Task 11: Full verification

**Files:** none

- [ ] **Step 11.1: Backend suite + lint**

Run: `cd apps/backend && uv run pytest tests/ -q && uv run ruff check . && uv run ruff format --check .`
Expected: green. (`git status --short` clean — commit any formatter edits.)

- [ ] **Step 11.2: Frontend tests + typecheck + lint + build**

Run: `cd apps/frontend && pnpm test && pnpm exec tsc --noEmit && pnpm run lint && pnpm run build`
Expected: green.

- [ ] **Step 11.3: Manual smoke (record in PR description)**

Confirm against a live `/teams`: open an agent → Manage tab shows read-only meta + reports-to picker + pause/delete; set a manager → org chart re-renders with the edge; Friday/Atlas show no Delete; pause toggles to Resume; kanban card drags between columns and stays.

---

## Self-Review

**Spec coverage:**
- reports_to editing → Tasks 5–8 (`ReportsToPicker`, `useAgentMutations.setReportsTo`, `AgentManageSection`). BFF already supports `reports_to` (PatchAgentBody) — exercised, no route change needed.
- pause/resume → Tasks 1, 2, 6, 8, 9 (admin client + BFF routes + hook + UI).
- delete (gateway cascade) → Tasks 6, 8, 9; seeded guard → Tasks 3, 4 (server) + 8, 9 (UI hide).
- read-only meta + "manage in control panel" pointer → Task 8.
- single-editor rule (no `/teams` meta write) → respected: no gateway meta-write task exists; meta is display-only.
- kanban drag fix folded in → Task 10.
- Out of scope honored: no adapter editing, no budget control, no `_build_agents_list` re-seed (roadmap row I), no agent creation changes.

**Placeholder scan:** none — every step has full code + exact commands + expected output.

**Type consistency:** `CompanyAgent` (Task 5) gains `role/title/capabilities/icon/status/reportsTo`; `useAgentMutations` exposes `setReportsTo/pause/resume/remove` (Task 6) used identically in Tasks 8–9; `isSeededAgent` defined once (Task 8) and imported in Task 9; `ReportsToPicker` props (`agents/agentId/value/onChange/disabled`) match its consumer in Task 8; server `is_seeded_agent` (Task 3) consumed in Task 4.

**Open items (carried from spec, non-blocking):** `/chat?agent=<id>` deep-link param for the "edit in control panel" link — verify the control panel reads it; if not, point to `/chat` and the user selects the agent. `reportsTo: ""` clears the manager — verify Paperclip treats empty as root (else map `""`→null in `patch_agent`).
