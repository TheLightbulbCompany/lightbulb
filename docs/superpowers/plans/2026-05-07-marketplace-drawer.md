# In-Chat Marketplace Drawer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a right-side `<aside>` drawer in `/chat` with three views (Browse, My Listings, Publish wizard) that replaces the existing GallerySection rail and `PublishAgentModal`. Preserves chat session, uses the existing publish + checkout backends, adds two small backend endpoints to surface seller-side data.

**Architecture:** New `<MarketplaceDrawerProvider>` (React context) wraps the chat page. New `<MarketplaceDrawer />` is `position: fixed` overlay rendering one of three view components. Publishing reuses the existing 3-call chain (`POST /listings` → `POST /listings/{id}/artifact-from-agent` → `POST /listings/{id}/submit`). Two new backend endpoints: extend `/listings/search` with `seller_id`+`status_in`, add `GET /me/agents-summary`.

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript, Tailwind CSS v4, SWR, Vitest, FastAPI, pytest, boto3 DynamoDB.

**Spec:** `docs/superpowers/specs/2026-05-07-marketplace-drawer-design.md`

**Branch:** `feat/marketplace-drawer` (worktree at `../isol8-marketplace-drawer`).

---

## Files at a glance

**Backend — modify:**
- `apps/backend/core/services/marketplace_search.py` — add `include_unpublished` flag
- `apps/backend/routers/marketplace_listings.py` — extend `/listings/search`, add `/me/agents-summary`
- `apps/backend/tests/unit/services/test_marketplace_search.py` — coverage for new flag
- `apps/backend/tests/unit/routers/test_marketplace_listings.py` — coverage for both router changes

**Frontend — add:**
- `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawer.tsx`
- `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx`
- `apps/frontend/src/components/marketplace/drawer/BrowseView.tsx`
- `apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx`
- `apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx`
- `apps/frontend/src/components/marketplace/drawer/wizardSteps.tsx`
- `apps/frontend/src/components/marketplace/drawer/MarketplaceToast.tsx`
- Tests for each above under `apps/frontend/tests/unit/components/marketplace/drawer/`

**Frontend — modify:**
- `apps/frontend/src/lib/marketplace/api.ts` — `searchListings`, `myListings`, `myAgentsSummary` helpers
- `apps/frontend/src/app/chat/page.tsx` — wrap in provider, mount drawer + toast
- `apps/frontend/src/components/chat/GallerySection.tsx` — shrink to 2-button block
- `apps/frontend/src/components/chat/ChatLayout.tsx` — `new-arrival` class wiring + sidebar gold pub-dot
- `apps/frontend/src/app/globals.css` — `slideIn` keyframe + `.agent-item.new-arrival` rule
- `apps/frontend/src/hooks/useCatalog.ts` — comment update

**Frontend — delete:**
- `apps/frontend/src/components/marketplace/PublishAgentModal.tsx`
- `apps/frontend/src/components/chat/AgentDetailPanel.tsx` (chat-side only — teams version stays)
- `apps/frontend/src/components/chat/GalleryItemRow.tsx`
- `apps/frontend/tests/unit/components/marketplace/PublishAgentModal.test.tsx`
- Tests for the chat-side AgentDetailPanel + GalleryItemRow if they exist

**AgentsPanel removals** (5 distinct points in `apps/frontend/src/components/control/panels/AgentsPanel.tsx`):
- Line 12: `Upload` import in lucide-react block
- Line 18: `import { PublishAgentModal } from "@/components/marketplace/PublishAgentModal"`
- Lines 38-40: `publishingAgent` state + comment
- Lines 187-194: `<Button onClick={() => setPublishingAgent(selected)}>` with `Upload` icon
- Lines 215-225: `{publishingAgent && <PublishAgentModal ... />}` mount

---

## Task 0: Setup + sanity

**Files:** none (read-only).

- [ ] **Step 1: Confirm worktree + branch**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8-marketplace-drawer
git status -sb
```

Expected: `## feat/marketplace-drawer` and a clean tree (the spec commit is already there).

- [ ] **Step 2: Run baseline backend tests**

```bash
cd apps/backend
uv run pytest tests/unit/routers/test_marketplace_listings.py tests/unit/services/test_marketplace_search.py -q
```

Expected: all green. If anything fails, stop and reconcile main first.

- [ ] **Step 3: Run baseline frontend tests**

```bash
cd apps/frontend
pnpm test -- --run marketplace
```

Expected: green (current marketplace tests including PublishAgentModal pass).

---

## Task 1: Extend `marketplace_search.search()` with `include_unpublished`

**Files:**
- Modify: `apps/backend/core/services/marketplace_search.py`
- Test: `apps/backend/tests/unit/services/test_marketplace_search.py`

The service already accepts `seller_id` but hard-codes `status="published"` filter at line 134. We add a flag so seller-scoped callers can read all statuses.

- [ ] **Step 1: Read the current `search()` signature**

```bash
sed -n '105,145p' apps/backend/core/services/marketplace_search.py
```

Confirm the snapshot shape and where the `published` filter is applied.

- [ ] **Step 2: Write the failing test**

Append to `apps/backend/tests/unit/services/test_marketplace_search.py`:

```python
@pytest.mark.asyncio
async def test_search_includes_unpublished_when_flag_set(monkeypatch):
    """When include_unpublished=True, draft + review + published rows all surface."""
    snapshot = [
        {"listing_id": "l1", "slug": "a", "name": "A", "status": "published",
         "seller_id": "u_seller", "format": "openclaw", "tags": [], "version": 1,
         "description_md": "", "created_at": "2026-05-01", "published_at": "2026-05-01",
         "price_cents": 0, "install_count": 0, "manifest_json": {}, "updated_at": "2026-05-01"},
        {"listing_id": "l2", "slug": "b", "name": "B", "status": "draft",
         "seller_id": "u_seller", "format": "openclaw", "tags": [], "version": 1,
         "description_md": "", "created_at": "2026-05-02", "published_at": None,
         "price_cents": 0, "install_count": 0, "manifest_json": {}, "updated_at": "2026-05-02"},
        {"listing_id": "l3", "slug": "c", "name": "C", "status": "review",
         "seller_id": "u_seller", "format": "openclaw", "tags": [], "version": 1,
         "description_md": "", "created_at": "2026-05-03", "published_at": None,
         "price_cents": 0, "install_count": 0, "manifest_json": {}, "updated_at": "2026-05-03"},
    ]
    from core.services import marketplace_search
    monkeypatch.setattr(marketplace_search, "_load_snapshot", AsyncMock(return_value=snapshot))

    items = await marketplace_search.search(
        seller_id="u_seller", include_unpublished=True, limit=10
    )
    statuses = {item["status"] for item in items}
    assert statuses == {"published", "draft", "review"}


@pytest.mark.asyncio
async def test_search_default_filters_to_published(monkeypatch):
    """Default behavior unchanged: only published rows surface."""
    snapshot = [
        {"listing_id": "l1", "slug": "a", "name": "A", "status": "published",
         "seller_id": "u_seller", "format": "openclaw", "tags": [], "version": 1,
         "description_md": "", "created_at": "2026-05-01", "published_at": "2026-05-01",
         "price_cents": 0, "install_count": 0, "manifest_json": {}, "updated_at": "2026-05-01"},
        {"listing_id": "l2", "slug": "b", "name": "B", "status": "draft",
         "seller_id": "u_seller", "format": "openclaw", "tags": [], "version": 1,
         "description_md": "", "created_at": "2026-05-02", "published_at": None,
         "price_cents": 0, "install_count": 0, "manifest_json": {}, "updated_at": "2026-05-02"},
    ]
    from core.services import marketplace_search
    monkeypatch.setattr(marketplace_search, "_load_snapshot", AsyncMock(return_value=snapshot))

    items = await marketplace_search.search(seller_id="u_seller", limit=10)
    statuses = {item["status"] for item in items}
    assert statuses == {"published"}
```

If `AsyncMock` isn't imported, add `from unittest.mock import AsyncMock` at the top of the test file. Match the helper-loading pattern of existing tests in this file.

- [ ] **Step 3: Run, confirm fail**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_marketplace_search.py::test_search_includes_unpublished_when_flag_set -v
```

Expected: FAIL with `unexpected keyword argument 'include_unpublished'` or similar.

- [ ] **Step 4: Implement**

Edit `apps/backend/core/services/marketplace_search.py:109-140`:

```python
async def search(
    *,
    query: str | None = None,
    format: str | None = None,
    limit: int = 24,
    seller_id: str | None = None,
    include_unpublished: bool = False,
):
    """... existing docstring ...

    Args:
        ... existing args ...
        include_unpublished: If True, return draft/review/retired/taken_down
            rows in addition to published. Caller must enforce auth (only
            seller-scoped reads should set this). Default False = the public
            search semantics.
    """
    snapshot = await _load_snapshot()
    if include_unpublished:
        candidates = list(snapshot)
    else:
        candidates = [li for li in snapshot if li.get("status") == "published"]
    if format:
        candidates = [li for li in candidates if li.get("format") == format]
    if seller_id:
        candidates = [li for li in candidates if li.get("seller_id") == seller_id]
    # ... existing scoring + sorting + limit ...
```

Preserve the rest of the function below the changed block exactly as-is.

- [ ] **Step 5: Run, confirm pass**

```bash
uv run pytest tests/unit/services/test_marketplace_search.py -v
```

Expected: all green (existing tests + 2 new).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/marketplace_search.py apps/backend/tests/unit/services/test_marketplace_search.py
git commit -m "feat(marketplace-search): add include_unpublished flag for seller-scoped reads"
```

---

## Task 2: Extend `/listings/search` + add `/me/agents-summary`

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py`
- Test: `apps/backend/tests/unit/routers/test_marketplace_listings.py`

Two changes in one task because they're tightly coupled (same router, same auth pattern).

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/routers/test_marketplace_listings.py`:

```python
@patch("routers.marketplace_listings.marketplace_search.search")
def test_search_seller_scoped_with_status_in(mock_search, client, auth_override):
    """Authed seller can read their own listings across statuses."""
    mock_search.return_value = [
        {"listing_id": "l1", "status": "draft", "seller_id": "u_seller", "name": "A"},
    ]
    with auth_override("u_seller", "seller@example.com"):
        resp = client.get(
            "/api/v1/marketplace/listings/search?seller_id=u_seller&status_in=draft,review,published"
        )
    assert resp.status_code == 200
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["seller_id"] == "u_seller"
    assert call_kwargs["include_unpublished"] is True


@patch("routers.marketplace_listings.marketplace_search.search")
def test_search_seller_scoped_rejects_other_users_unpublished(mock_search, client, auth_override):
    """status_in is ignored when seller_id != caller — enforces published-only semantics."""
    mock_search.return_value = []
    with auth_override("u_attacker", "attacker@example.com"):
        resp = client.get(
            "/api/v1/marketplace/listings/search?seller_id=u_victim&status_in=draft"
        )
    assert resp.status_code == 200
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["seller_id"] == "u_victim"
    # include_unpublished must be False since caller != seller_id
    assert call_kwargs.get("include_unpublished", False) is False


def test_search_anonymous_published_only(client):
    """No auth + no seller_id = today's behavior."""
    resp = client.get("/api/v1/marketplace/listings/search?format=openclaw&limit=5")
    assert resp.status_code == 200


@patch("routers.marketplace_listings.get_workspace")
def test_my_agents_summary_returns_per_agent_counts(mock_workspace_factory, client, auth_override):
    workspace = mock_workspace_factory.return_value
    workspace.read_openclaw_config.return_value = {
        "agents": {
            "list": [
                {
                    "id": "agent_main",
                    "identity": {"name": "main"},
                    "skills": {"list": [{"name": "s1"}, {"name": "s2"}]},
                    "channels": {"list": [{"name": "slack"}]},
                },
                {
                    "id": "agent_pulse",
                    "identity": {"name": "Pulse"},
                    "skills": {"list": [{"name": "x"}, {"name": "y"}, {"name": "z"}]},
                    "channels": {"list": []},
                },
            ]
        }
    }
    workspace.read_cron_jobs.return_value = [
        {"agent_id": "agent_pulse", "schedule": "0 9 * * *"},
        {"agent_id": "agent_pulse", "schedule": "0 17 * * *"},
        {"agent_id": "agent_main", "schedule": "*/5 * * * *"},
    ]
    with auth_override("u_seller", "seller@example.com"):
        resp = client.get("/api/v1/marketplace/me/agents-summary")
    assert resp.status_code == 200
    body = resp.json()
    by_id = {a["agent_id"]: a for a in body["agents"]}
    assert by_id["agent_main"]["skill_count"] == 2
    assert by_id["agent_main"]["channel_count"] == 1
    assert by_id["agent_main"]["cron_count"] == 1
    assert by_id["agent_pulse"]["skill_count"] == 3
    assert by_id["agent_pulse"]["channel_count"] == 0
    assert by_id["agent_pulse"]["cron_count"] == 2


@patch("routers.marketplace_listings.get_workspace")
def test_my_agents_summary_no_config_returns_empty(mock_workspace_factory, client, auth_override):
    workspace = mock_workspace_factory.return_value
    workspace.read_openclaw_config.return_value = None
    with auth_override("u_seller", "seller@example.com"):
        resp = client.get("/api/v1/marketplace/me/agents-summary")
    assert resp.status_code == 200
    assert resp.json() == {"agents": []}


def test_my_agents_summary_unauthorized(client):
    resp = client.get("/api/v1/marketplace/me/agents-summary")
    assert resp.status_code in (401, 403)
```

If `auth_override` fixture or `client` fixture differs from this signature, match the existing patterns in this test file. Don't invent new fixtures.

- [ ] **Step 2: Run, confirm fail**

```bash
uv run pytest tests/unit/routers/test_marketplace_listings.py::test_my_agents_summary_returns_per_agent_counts tests/unit/routers/test_marketplace_listings.py::test_search_seller_scoped_with_status_in -v
```

Expected: 404s on the endpoints / unexpected query-arg failures on search.

- [ ] **Step 3: Implement `/listings/search` extension**

Update `apps/backend/routers/marketplace_listings.py`. Replace the existing `search_listings` route handler:

```python
@router.get("/listings/search")
async def search_listings(
    response: Response,
    auth: Annotated[AuthContext | None, Depends(get_optional_user)] = None,
    q: str | None = Query(default=None, description="Whitespace-tokenized query"),
    format: str | None = Query(default=None, pattern="^(openclaw|skillmd)$"),
    limit: int = Query(default=24, ge=1, le=100),
    seller_id: str | None = Query(default=None),
    status_in: str | None = Query(
        default=None,
        description="Comma-separated statuses (draft,review,published,retired,taken_down). Honored only when seller_id matches the calling user.",
    ),
):
    """Browse / search listings.

    Default behavior (anonymous or non-seller caller): returns
    status='published' only. The chat drawer's My Listings tab calls
    with ``seller_id=<own user_id>`` and an authed token to receive
    its own draft/review/retired rows alongside published ones.
    """
    response.headers["Cache-Control"] = "public, max-age=60"
    include_unpublished = (
        seller_id is not None
        and auth is not None
        and seller_id == auth.user_id
        and status_in is not None
    )
    items = await marketplace_search.search(
        query=q,
        format=format,
        limit=limit,
        seller_id=seller_id,
        include_unpublished=include_unpublished,
    )
    if include_unpublished and status_in:
        wanted = {s.strip() for s in status_in.split(",") if s.strip()}
        if wanted:
            items = [item for item in items if item.get("status") in wanted]
    return {"items": items}
```

If `get_optional_user` doesn't exist as a dependency, define it next to `get_current_user` in `core/auth.py`:

```python
async def get_optional_user(
    request: Request,
) -> AuthContext | None:
    """Like get_current_user but returns None when no Authorization header is present."""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    try:
        return await get_current_user(request)
    except HTTPException:
        return None
```

If a similar helper already exists, use that instead.

- [ ] **Step 4: Implement `/me/agents-summary` endpoint**

Append to `apps/backend/routers/marketplace_listings.py` (after the existing `seller_eligibility`-replacement / artifact upload section):

```python
@router.get("/me/agents-summary")
async def my_agents_summary(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Per-agent skill / channel / cron counts from the caller's
    workspace openclaw.json. Drives the in-chat marketplace drawer's
    Publish wizard pick-card sublines (Step 1) and the bundled-summary
    line in the review preview (Step 4). Reads EFS directly via
    the workspace helper — no container interaction required.
    """
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()
    config = workspace.read_openclaw_config(owner_id) or {}
    agents = (config.get("agents") or {}).get("list", []) or []
    cron_jobs = workspace.read_cron_jobs(owner_id) or []
    return {
        "agents": [
            {
                "agent_id": a.get("id"),
                "name": (a.get("identity") or {}).get("name") or a.get("id"),
                "skill_count": len((a.get("skills") or {}).get("list", []) or []),
                "channel_count": len((a.get("channels") or {}).get("list", []) or []),
                "cron_count": sum(1 for c in cron_jobs if c.get("agent_id") == a.get("id")),
            }
            for a in agents
            if a.get("id")
        ],
    }
```

Add to imports at the top of the file if not present:

```python
from core.containers import get_workspace
```

- [ ] **Step 5: Run, confirm all pass**

```bash
uv run pytest tests/unit/routers/test_marketplace_listings.py -v
```

Expected: all green including the 6 new tests.

- [ ] **Step 6: Lint**

```bash
uv run ruff check routers/marketplace_listings.py core/services/marketplace_search.py core/auth.py
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/marketplace_listings.py apps/backend/core/services/marketplace_search.py apps/backend/tests/unit/routers/test_marketplace_listings.py apps/backend/core/auth.py
git commit -m "feat(marketplace): seller-scoped listing search + /me/agents-summary endpoint"
```

---

## Task 3: Frontend API helpers

**Files:**
- Modify: `apps/frontend/src/lib/marketplace/api.ts`
- Test: `apps/frontend/tests/unit/lib/marketplace/api.test.ts` (create if absent)

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/tests/unit/lib/marketplace/api.test.ts`:

```ts
import { describe, expect, it, vi, beforeEach } from "vitest";
import { searchListings, myListings, myAgentsSummary } from "@/lib/marketplace/api";

const fetchSpy = vi.fn();
vi.stubGlobal("fetch", fetchSpy);

describe("marketplace api", () => {
  beforeEach(() => fetchSpy.mockReset());

  it("searchListings sends q + category + sort", async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [] }),
    });
    await searchListings({ q: "atlas", category: "engineering", sort: "recent" });
    const url = (fetchSpy.mock.calls[0][0] as string);
    expect(url).toMatch(/q=atlas/);
    expect(url).toMatch(/category=engineering/);
    expect(url).toMatch(/sort=recent/);
    expect(url).toMatch(/format=openclaw/);
  });

  it("myListings includes seller_id and status_in with auth header", async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [] }),
    });
    await myListings({ sellerId: "u_me", jwt: "tok_abc" });
    const url = (fetchSpy.mock.calls[0][0] as string);
    expect(url).toMatch(/seller_id=u_me/);
    expect(url).toMatch(/status_in=draft%2Creview%2Cpublished%2Cretired%2Ctaken_down/);
    const headers = (fetchSpy.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer tok_abc");
  });

  it("myAgentsSummary returns the agents array", async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        agents: [
          { agent_id: "a1", name: "main", skill_count: 2, channel_count: 1, cron_count: 0 },
        ],
      }),
    });
    const result = await myAgentsSummary({ jwt: "tok_abc" });
    expect(result.agents).toHaveLength(1);
    expect(result.agents[0].skill_count).toBe(2);
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
cd apps/frontend
pnpm test -- --run lib/marketplace/api
```

Expected: FAIL — exports not found.

- [ ] **Step 3: Implement**

Append to `apps/frontend/src/lib/marketplace/api.ts`:

```ts
export interface SearchOptions {
  q?: string;
  category?: string;
  sort?: "installs" | "recent";
  limit?: number;
}

export async function searchListings(
  opts: SearchOptions = {},
): Promise<{ items: Listing[] }> {
  const params = new URLSearchParams();
  if (opts.q) params.set("q", opts.q);
  if (opts.category) params.set("category", opts.category);
  if (opts.sort) params.set("sort", opts.sort);
  params.set("format", "openclaw");
  params.set("limit", String(opts.limit ?? 60));
  const resp = await fetch(`${API}/marketplace/listings/search?${params}`, {
    next: { revalidate: 60 },
  });
  if (!resp.ok) throw new Error(`searchListings failed: ${resp.status}`);
  return (await resp.json()) as { items: Listing[] };
}

export async function myListings(opts: {
  sellerId: string;
  jwt: string;
  statusIn?: string[];
}): Promise<{ items: Listing[] }> {
  const params = new URLSearchParams();
  params.set("seller_id", opts.sellerId);
  params.set(
    "status_in",
    (opts.statusIn ?? ["draft", "review", "published", "retired", "taken_down"]).join(","),
  );
  params.set("limit", "100");
  const resp = await fetch(`${API}/marketplace/listings/search?${params}`, {
    headers: { Authorization: `Bearer ${opts.jwt}` },
  });
  if (!resp.ok) throw new Error(`myListings failed: ${resp.status}`);
  return (await resp.json()) as { items: Listing[] };
}

export interface AgentSummary {
  agent_id: string;
  name: string;
  skill_count: number;
  channel_count: number;
  cron_count: number;
}

export async function myAgentsSummary(opts: { jwt: string }): Promise<{ agents: AgentSummary[] }> {
  const resp = await fetch(`${API}/marketplace/me/agents-summary`, {
    headers: { Authorization: `Bearer ${opts.jwt}` },
  });
  if (!resp.ok) throw new Error(`myAgentsSummary failed: ${resp.status}`);
  return (await resp.json()) as { agents: AgentSummary[] };
}
```

- [ ] **Step 4: Run, confirm pass**

```bash
pnpm test -- --run lib/marketplace/api
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/lib/marketplace/api.ts apps/frontend/tests/unit/lib/marketplace/api.test.ts
git commit -m "feat(frontend-api): searchListings, myListings, myAgentsSummary helpers"
```

---

## Task 4: `MarketplaceDrawerProvider` + `useMarketplaceDrawer` hook

**Files:**
- Create: `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx`
- Test: `apps/frontend/tests/unit/components/marketplace/drawer/MarketplaceDrawerProvider.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/tests/unit/components/marketplace/drawer/MarketplaceDrawerProvider.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, act } from "@testing-library/react";
import {
  MarketplaceDrawerProvider,
  useMarketplaceDrawer,
} from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

let captured: ReturnType<typeof useMarketplaceDrawer> | null = null;

function Probe() {
  captured = useMarketplaceDrawer();
  return null;
}

function renderWithProvider() {
  return render(
    <MarketplaceDrawerProvider>
      <Probe />
    </MarketplaceDrawerProvider>,
  );
}

describe("MarketplaceDrawerProvider", () => {
  it("starts closed in browse view", () => {
    renderWithProvider();
    expect(captured?.state.isOpen).toBe(false);
    expect(captured?.state.view).toBe("browse");
  });

  it("open() sets isOpen=true and switches view", () => {
    renderWithProvider();
    act(() => captured?.open({ view: "publish" }));
    expect(captured?.state.isOpen).toBe(true);
    expect(captured?.state.view).toBe("publish");
  });

  it("close() resets transient wizard state", () => {
    renderWithProvider();
    act(() => captured?.open({ view: "publish", agentId: "a1" }));
    expect(captured?.state.publishAgentId).toBe("a1");
    act(() => captured?.close());
    expect(captured?.state.isOpen).toBe(false);
    expect(captured?.state.publishAgentId).toBeUndefined();
  });

  it("setView remembers prevView when entering publish from mine", () => {
    renderWithProvider();
    act(() => captured?.open({ view: "mine" }));
    act(() => captured?.setView("publish"));
    expect(captured?.state.prevView).toBe("mine");
  });

  it("showInstallToast sets toast state and clears after dismiss", () => {
    renderWithProvider();
    act(() => captured?.showInstallToast({ agentName: "Pulse", agentId: "a1" }));
    expect(captured?.state.toast).toEqual({ agentName: "Pulse", agentId: "a1" });
    act(() => captured?.dismissToast());
    expect(captured?.state.toast).toBeNull();
  });

  it("setFreshlyInstalled tracks the agent id", () => {
    renderWithProvider();
    act(() => captured?.setFreshlyInstalled("a_new"));
    expect(captured?.state.freshlyInstalledAgentId).toBe("a_new");
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run MarketplaceDrawerProvider
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx`:

```tsx
"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export type DrawerView = "browse" | "mine" | "publish";

export interface DrawerState {
  isOpen: boolean;
  view: DrawerView;
  prevView?: DrawerView;
  publishAgentId?: string;
  freshlyInstalledAgentId: string | null;
  toast: { agentName: string; agentId: string } | null;
}

export interface OpenOptions {
  view: DrawerView;
  agentId?: string;
}

interface DrawerContextValue {
  state: DrawerState;
  open: (opts: OpenOptions) => void;
  close: () => void;
  setView: (view: DrawerView) => void;
  showInstallToast: (toast: { agentName: string; agentId: string }) => void;
  dismissToast: () => void;
  setFreshlyInstalled: (agentId: string | null) => void;
}

const Ctx = createContext<DrawerContextValue | null>(null);

const INITIAL: DrawerState = {
  isOpen: false,
  view: "browse",
  freshlyInstalledAgentId: null,
  toast: null,
};

export function MarketplaceDrawerProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DrawerState>(INITIAL);

  const open = useCallback((opts: OpenOptions) => {
    setState((prev) => ({
      ...prev,
      isOpen: true,
      view: opts.view,
      publishAgentId: opts.agentId,
      prevView: opts.view === "publish" ? prev.view : prev.prevView,
    }));
  }, []);

  const close = useCallback(() => {
    setState((prev) => ({
      ...prev,
      isOpen: false,
      publishAgentId: undefined,
      // toast persists across drawer close — it has its own dismiss timer
    }));
  }, []);

  const setView = useCallback((view: DrawerView) => {
    setState((prev) => ({
      ...prev,
      view,
      prevView: view === "publish" ? prev.view : prev.prevView,
      publishAgentId: view === "publish" ? prev.publishAgentId : undefined,
    }));
  }, []);

  const showInstallToast = useCallback((toast: { agentName: string; agentId: string }) => {
    setState((prev) => ({ ...prev, toast }));
  }, []);

  const dismissToast = useCallback(() => {
    setState((prev) => ({ ...prev, toast: null }));
  }, []);

  const setFreshlyInstalled = useCallback((agentId: string | null) => {
    setState((prev) => ({ ...prev, freshlyInstalledAgentId: agentId }));
  }, []);

  const value = useMemo<DrawerContextValue>(
    () => ({ state, open, close, setView, showInstallToast, dismissToast, setFreshlyInstalled }),
    [state, open, close, setView, showInstallToast, dismissToast, setFreshlyInstalled],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useMarketplaceDrawer(): DrawerContextValue {
  const ctx = useContext(Ctx);
  if (!ctx) {
    throw new Error("useMarketplaceDrawer must be used inside <MarketplaceDrawerProvider>");
  }
  return ctx;
}
```

- [ ] **Step 4: Run, confirm pass**

```bash
pnpm test -- --run MarketplaceDrawerProvider
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx apps/frontend/tests/unit/components/marketplace/drawer/MarketplaceDrawerProvider.test.tsx
git commit -m "feat(drawer): MarketplaceDrawerProvider context + hook"
```

---

## Task 5: `MarketplaceDrawer` shell + scrim + header

**Files:**
- Create: `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawer.tsx`
- Test: `apps/frontend/tests/unit/components/marketplace/drawer/MarketplaceDrawer.test.tsx`

This task only builds the shell + header. The three views are placeholders that get filled in later tasks.

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/tests/unit/components/marketplace/drawer/MarketplaceDrawer.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawer";
import {
  MarketplaceDrawerProvider,
  useMarketplaceDrawer,
} from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

// Stub view components (filled in later tasks)
vi.mock("@/components/marketplace/drawer/BrowseView", () => ({
  BrowseView: () => <div data-testid="browse-view">browse</div>,
}));
vi.mock("@/components/marketplace/drawer/MyListingsView", () => ({
  MyListingsView: () => <div data-testid="my-listings-view">my listings</div>,
}));
vi.mock("@/components/marketplace/drawer/PublishWizard", () => ({
  PublishWizard: () => <div data-testid="publish-wizard">publish</div>,
}));

function Wrapper({ children }: { children: React.ReactNode }) {
  return <MarketplaceDrawerProvider>{children}</MarketplaceDrawerProvider>;
}

function OpenButton({ view }: { view: "browse" | "mine" | "publish" }) {
  const { open } = useMarketplaceDrawer();
  return <button onClick={() => open({ view })}>open-{view}</button>;
}

describe("MarketplaceDrawer", () => {
  it("renders nothing when closed", () => {
    render(<MarketplaceDrawer />, { wrapper: Wrapper });
    expect(screen.queryByTestId("browse-view")).not.toBeInTheDocument();
  });

  it("opens with browse view by default and shows tabs + publish button + full-page link", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OpenButton view="browse" />
        <MarketplaceDrawer />
      </>,
      { wrapper: Wrapper },
    );
    await user.click(screen.getByText("open-browse"));
    expect(screen.getByTestId("browse-view")).toBeVisible();
    expect(screen.getByRole("button", { name: /browse/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /my listings/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /publish your agent/i })).toBeVisible();
    expect(screen.getByRole("link", { name: /open in full page/i })).toHaveAttribute(
      "href",
      "/marketplace",
    );
  });

  it("clicking a tab switches the view in place", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OpenButton view="browse" />
        <MarketplaceDrawer />
      </>,
      { wrapper: Wrapper },
    );
    await user.click(screen.getByText("open-browse"));
    await user.click(screen.getByRole("button", { name: /my listings/i }));
    expect(screen.getByTestId("my-listings-view")).toBeVisible();
    expect(screen.queryByTestId("browse-view")).not.toBeInTheDocument();
  });

  it("publish mode swaps the header (back arrow + step label, no tabs)", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OpenButton view="publish" />
        <MarketplaceDrawer />
      </>,
      { wrapper: Wrapper },
    );
    await user.click(screen.getByText("open-publish"));
    expect(screen.getByTestId("publish-wizard")).toBeVisible();
    expect(screen.getByRole("button", { name: /^marketplace$/i })).toBeVisible(); // back arrow has "Marketplace" text
    expect(screen.queryByRole("button", { name: /^browse$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /open in full page/i })).not.toBeInTheDocument();
  });

  it("clicking the close button closes the drawer", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OpenButton view="browse" />
        <MarketplaceDrawer />
      </>,
      { wrapper: Wrapper },
    );
    await user.click(screen.getByText("open-browse"));
    await user.click(screen.getByRole("button", { name: /close drawer/i }));
    expect(screen.queryByTestId("browse-view")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run MarketplaceDrawer
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawer.tsx`:

```tsx
"use client";

import Link from "next/link";
import { ChevronLeft, X, Plus, ExternalLink } from "lucide-react";

import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import { BrowseView } from "./BrowseView";
import { MyListingsView } from "./MyListingsView";
import { PublishWizard } from "./PublishWizard";

export function MarketplaceDrawer() {
  const { state, close, setView, open } = useMarketplaceDrawer();

  if (!state.isOpen) return null;

  const isPublish = state.view === "publish";

  return (
    <>
      {/* Scrim */}
      <div
        onClick={close}
        className="fixed inset-0 z-50 bg-[rgba(20,30,25,0.45)] transition-opacity duration-250"
        aria-hidden="true"
      />
      <aside
        data-mode={isPublish ? "publish" : ""}
        className="fixed top-0 right-0 z-[60] h-screen w-[min(920px,98vw)] bg-[#faf7f2] border-l border-[#e0dbd0] shadow-[-30px_0_80px_rgba(10,30,20,0.16)] flex flex-col transform translate-x-0 transition-transform duration-350"
        aria-label="Marketplace drawer"
      >
        {/* HEADER */}
        <div className="px-6 py-3.5 border-b border-[#e0dbd0] bg-[#faf7f2] flex items-center justify-between gap-3.5">
          {isPublish ? <PublishHeader /> : <DefaultHeader />}
          <div className="flex items-center gap-2.5 flex-shrink-0">
            {!isPublish && (
              <>
                <button
                  type="button"
                  onClick={() => open({ view: "publish" })}
                  className="bg-[#06402b] hover:bg-[#0a5a3d] text-white rounded-full px-4 py-2 text-[13px] font-semibold inline-flex items-center gap-1.5"
                >
                  <Plus className="w-3.5 h-3.5" />
                  Publish your agent
                </button>
                <Link
                  href="/marketplace"
                  target="_blank"
                  className="text-[12.5px] text-[#5b5b5b] hover:text-[#1a1a1a] inline-flex items-center gap-1"
                >
                  Open in full page <ExternalLink className="w-3 h-3" />
                </Link>
              </>
            )}
            <button
              type="button"
              onClick={close}
              aria-label="Close drawer"
              className="w-8 h-8 rounded-full border border-[#e0dbd0] bg-white text-[#5b5b5b] hover:text-[#1a1a1a] flex items-center justify-center"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* BODY */}
        <div className="flex-1 overflow-y-auto px-7 pt-6 pb-20">
          {state.view === "browse" && <BrowseView />}
          {state.view === "mine" && <MyListingsView />}
          {state.view === "publish" && <PublishWizard />}
        </div>
      </aside>
    </>
  );
}

function DefaultHeader() {
  const { state, setView } = useMarketplaceDrawer();
  return (
    <div className="flex items-center gap-4 min-w-0">
      <span className="font-editorial-serif italic text-[13px] text-[#5b5b5b]">Marketplace</span>
      <div className="bg-[#f3eee5] rounded-full p-1 flex gap-1">
        <button
          type="button"
          onClick={() => setView("browse")}
          className={`px-4 py-1.5 rounded-full text-[13px] font-medium ${
            state.view === "browse"
              ? "bg-white text-[#1a1a1a] shadow-sm"
              : "text-[#5b5b5b]"
          }`}
        >
          Browse
        </button>
        <button
          type="button"
          onClick={() => setView("mine")}
          className={`px-4 py-1.5 rounded-full text-[13px] font-medium ${
            state.view === "mine"
              ? "bg-white text-[#1a1a1a] shadow-sm"
              : "text-[#5b5b5b]"
          }`}
        >
          My listings
        </button>
      </div>
    </div>
  );
}

function PublishHeader() {
  const { state, setView } = useMarketplaceDrawer();
  const previous = state.prevView ?? "browse";
  return (
    <div className="flex items-center gap-3.5 min-w-0">
      <button
        type="button"
        onClick={() => setView(previous)}
        className="inline-flex items-center gap-1.5 text-[13px] text-[#5b5b5b] hover:text-[#1a1a1a] hover:bg-[#f3eee5] rounded-full px-2.5 py-1.5"
      >
        <ChevronLeft className="w-3.5 h-3.5" />
        Marketplace
      </button>
      <span className="font-editorial-serif italic text-[14px] text-[#5b5b5b]">
        Publish an agent
      </span>
    </div>
  );
}
```

- [ ] **Step 4: Run, confirm pass**

```bash
pnpm test -- --run MarketplaceDrawer
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/MarketplaceDrawer.tsx apps/frontend/tests/unit/components/marketplace/drawer/MarketplaceDrawer.test.tsx
git commit -m "feat(drawer): drawer shell + scrim + header (default and publish modes)"
```

---

## Task 6: View placeholders so the drawer compiles

**Files:**
- Create: `apps/frontend/src/components/marketplace/drawer/BrowseView.tsx`
- Create: `apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx`
- Create: `apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx`

These will be filled in by Tasks 7–14. For now, render a placeholder so the drawer can mount.

- [ ] **Step 1: Create three placeholder files**

```tsx
// apps/frontend/src/components/marketplace/drawer/BrowseView.tsx
"use client";
export function BrowseView() {
  return <div className="font-editorial-serif italic text-[#8a8578]">Browse view — coming up.</div>;
}
```

```tsx
// apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx
"use client";
export function MyListingsView() {
  return (
    <div className="font-editorial-serif italic text-[#8a8578]">My listings view — coming up.</div>
  );
}
```

```tsx
// apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx
"use client";
export function PublishWizard() {
  return <div className="font-editorial-serif italic text-[#8a8578]">Publish wizard — coming up.</div>;
}
```

- [ ] **Step 2: Verify the build doesn't break**

```bash
pnpm tsc --noEmit 2>&1 | grep "marketplace/drawer" | head
```

Expected: clean (no errors specific to the drawer module).

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/BrowseView.tsx apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx
git commit -m "feat(drawer): placeholder view components"
```

---

## Task 7: Wire drawer into the chat page

**Files:**
- Modify: `apps/frontend/src/app/chat/page.tsx`

- [ ] **Step 1: Edit the page to wrap children + mount the drawer**

Open `apps/frontend/src/app/chat/page.tsx`. At the top, add:

```tsx
import { MarketplaceDrawerProvider } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { MarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawer";
```

Inside the existing `ChatPageInner` return JSX, wrap the entire current return in `<MarketplaceDrawerProvider>` and append `<MarketplaceDrawer />` at the end:

```tsx
return (
  <MarketplaceDrawerProvider>
    <GatewayProvider>
      {/* existing ChatLayout / AgentChatWindow / ControlPanelRouter content unchanged */}
    </GatewayProvider>
    <MarketplaceDrawer />
  </MarketplaceDrawerProvider>
);
```

The exact wrapping shape depends on the current page structure — preserve the existing component tree, just wrap with the provider and add the drawer mount.

- [ ] **Step 2: Smoke check in dev**

```bash
cd apps/frontend && pnpm dev
```

Visit `http://localhost:3000/chat`. The page should still render the chat shell (drawer is hidden because `isOpen=false`).

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/app/chat/page.tsx
git commit -m "feat(chat): mount MarketplaceDrawer + provider on /chat"
```

---

## Task 8: Replace `GallerySection` with the 2-button block

**Files:**
- Modify: `apps/frontend/src/components/chat/GallerySection.tsx`
- Modify: `apps/frontend/tests/unit/components/chat/GallerySection.test.tsx` (if exists; otherwise create)

- [ ] **Step 1: Write the failing test**

Replace any existing `apps/frontend/tests/unit/components/chat/GallerySection.test.tsx` with:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { GallerySection } from "@/components/chat/GallerySection";
import { MarketplaceDrawerProvider, useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

let captured: ReturnType<typeof useMarketplaceDrawer> | null = null;
function Probe() {
  captured = useMarketplaceDrawer();
  return null;
}

describe("GallerySection (2-button marketplace block)", () => {
  it("renders Browse and Publish buttons under a 'Marketplace' label", () => {
    render(
      <MarketplaceDrawerProvider>
        <GallerySection />
      </MarketplaceDrawerProvider>,
    );
    expect(screen.getByText(/marketplace/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /^browse$/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /publish/i })).toBeVisible();
  });

  it("Browse opens drawer in browse view", async () => {
    const user = userEvent.setup();
    render(
      <MarketplaceDrawerProvider>
        <GallerySection />
        <Probe />
      </MarketplaceDrawerProvider>,
    );
    await user.click(screen.getByRole("button", { name: /^browse$/i }));
    expect(captured?.state.isOpen).toBe(true);
    expect(captured?.state.view).toBe("browse");
  });

  it("Publish opens drawer in publish view", async () => {
    const user = userEvent.setup();
    render(
      <MarketplaceDrawerProvider>
        <GallerySection />
        <Probe />
      </MarketplaceDrawerProvider>,
    );
    await user.click(screen.getByRole("button", { name: /publish/i }));
    expect(captured?.state.isOpen).toBe(true);
    expect(captured?.state.view).toBe("publish");
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run GallerySection
```

Expected: FAIL — current GallerySection renders the catalog rail, not the 2-button block.

- [ ] **Step 3: Implement**

Replace the entire contents of `apps/frontend/src/components/chat/GallerySection.tsx`:

```tsx
"use client";

import { Plus } from "lucide-react";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

export function GallerySection() {
  const { open } = useMarketplaceDrawer();
  return (
    <div className="mt-4 border-t border-[#e0dbd0] pt-3 px-2">
      <div className="font-editorial-serif italic text-[13.5px] text-[#5b5b5b] pb-2 px-1">
        Marketplace
      </div>
      <div className="flex gap-1.5">
        <button
          type="button"
          onClick={() => open({ view: "browse" })}
          className="flex-1 px-2.5 py-2 text-[12.5px] font-medium text-[#1a1a1a] border border-[#e0dbd0] rounded-[10px] hover:border-[#06402b] hover:text-[#06402b] hover:bg-white transition-colors"
        >
          Browse
        </button>
        <button
          type="button"
          onClick={() => open({ view: "publish" })}
          className="flex-1 px-2.5 py-2 text-[12.5px] font-medium text-[#1a1a1a] border border-[#e0dbd0] rounded-[10px] hover:border-[#06402b] hover:text-[#06402b] hover:bg-white transition-colors inline-flex items-center justify-center gap-1.5"
        >
          <Plus className="w-3 h-3" />
          Publish
        </button>
      </div>
    </div>
  );
}
```

This file goes from ~72 lines to ~30. The `<AgentDetailPanel>` mount and the `useCatalog`/agents-list logic are gone.

- [ ] **Step 4: Update the GallerySection prop type usage in `ChatLayout.tsx`**

`ChatLayout.tsx` previously passed `onAgentDeployed` to `<GallerySection>`. Search for the call site:

```bash
grep -n "<GallerySection" apps/frontend/src/components/chat/ChatLayout.tsx
```

Remove the `onAgentDeployed` prop from the `<GallerySection ... />` usage. The new component takes no props.

- [ ] **Step 5: Run, confirm pass**

```bash
pnpm test -- --run GallerySection
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/chat/GallerySection.tsx apps/frontend/src/components/chat/ChatLayout.tsx apps/frontend/tests/unit/components/chat/GallerySection.test.tsx
git commit -m "feat(chat-sidebar): replace GallerySection rail with 2-button Marketplace block"
```

---

## Task 9: BrowseView — search + chips + card grid

**Files:**
- Replace: `apps/frontend/src/components/marketplace/drawer/BrowseView.tsx`
- Test: `apps/frontend/tests/unit/components/marketplace/drawer/BrowseView.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/tests/unit/components/marketplace/drawer/BrowseView.test.tsx`:

```tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockSearch = vi.fn();
vi.mock("@/lib/marketplace/api", () => ({
  searchListings: (opts: unknown) => mockSearch(opts),
}));
vi.mock("@/hooks/useCatalog", () => ({
  useCatalog: () => ({ installedSlugs: new Set(["pulse"]) }),
}));

import { BrowseView } from "@/components/marketplace/drawer/BrowseView";

const SAMPLE = [
  {
    listing_id: "l1", slug: "atlas", name: "Atlas", description_md: "Engineering helper",
    seller_id: "u_curator", price_cents: 0, status: "published", format: "openclaw",
    tags: ["engineering"], version: 1, created_at: "2026-05-01", published_at: "2026-05-01",
  },
  {
    listing_id: "l2", slug: "pulse", name: "Pulse", description_md: "Sales digest",
    seller_id: "u_curator", price_cents: 1200, status: "published", format: "openclaw",
    tags: ["sales"], version: 1, created_at: "2026-05-01", published_at: "2026-05-01",
  },
];

describe("BrowseView", () => {
  beforeEach(() => mockSearch.mockReset());

  it("renders cards from searchListings response", async () => {
    mockSearch.mockResolvedValueOnce({ items: SAMPLE });
    render(<BrowseView />);
    await waitFor(() => expect(screen.getByText("Atlas")).toBeInTheDocument());
    expect(screen.getByText("Pulse")).toBeInTheDocument();
  });

  it("already-installed listings show 'Hired ✓' instead of Hire", async () => {
    mockSearch.mockResolvedValueOnce({ items: SAMPLE });
    render(<BrowseView />);
    await waitFor(() => expect(screen.getByText("Pulse")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /hired ✓/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^hire$/i })).toBeVisible(); // for Atlas (not installed)
  });

  it("category chip click refetches with the category param", async () => {
    mockSearch.mockResolvedValue({ items: [] });
    const user = userEvent.setup();
    render(<BrowseView />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: /sales/i }));
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(2));
    expect(mockSearch).toHaveBeenLastCalledWith(
      expect.objectContaining({ category: "sales" }),
    );
  });

  it("renders six category chips per spec", async () => {
    mockSearch.mockResolvedValueOnce({ items: [] });
    render(<BrowseView />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalled());
    ["all", "sales", "engineering", "operations", "research", "finance"].forEach((c) => {
      expect(screen.getByRole("button", { name: new RegExp(`^${c}$`, "i") })).toBeVisible();
    });
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run BrowseView
```

Expected: FAIL — placeholder doesn't expose Atlas/Pulse.

- [ ] **Step 3: Implement**

Replace `apps/frontend/src/components/marketplace/drawer/BrowseView.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { Search } from "lucide-react";

import { searchListings } from "@/lib/marketplace/api";
import { useCatalog } from "@/hooks/useCatalog";
import type { Listing } from "@/lib/marketplace/types";
import { ListingCard } from "./ListingCard";

const CATEGORIES = ["all", "sales", "engineering", "operations", "research", "finance"] as const;

export function BrowseView() {
  const [items, setItems] = useState<Listing[]>([]);
  const [q, setQ] = useState("");
  const [category, setCategory] = useState<string>("all");
  const [loading, setLoading] = useState(true);
  const { installedSlugs } = useCatalog() as unknown as { installedSlugs: Set<string> };

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const handle = setTimeout(() => {
      searchListings({
        q: q || undefined,
        category: category === "all" ? undefined : category,
      })
        .then((r) => {
          if (!alive) return;
          setItems(r.items);
          setLoading(false);
        })
        .catch(() => {
          if (alive) setLoading(false);
        });
    }, 300);
    return () => {
      alive = false;
      clearTimeout(handle);
    };
  }, [q, category]);

  return (
    <div>
      <div className="flex gap-2.5 mb-4 items-center flex-wrap">
        <div className="flex-1 min-w-[220px] flex items-center gap-2.5 bg-white border border-[#e0dbd0] rounded-xl px-3.5 py-2.5">
          <Search className="w-4 h-4 text-[#5b5b5b]" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search agents, skills, or tags…"
            className="flex-1 outline-0 bg-transparent text-sm"
          />
        </div>
        <button className="bg-white border border-[#e0dbd0] rounded-xl px-3.5 py-2.5 text-[13px] text-[#5b5b5b]">
          Most installed ▾
        </button>
      </div>
      <div className="flex gap-2 flex-wrap mb-4">
        {CATEGORIES.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setCategory(c)}
            className={`px-3 py-1.5 rounded-full text-[12.5px] border ${
              category === c
                ? "bg-[#06402b] text-white border-[#06402b]"
                : "bg-transparent text-[#5b5b5b] border-[#e0dbd0]"
            }`}
          >
            {c[0].toUpperCase() + c.slice(1)}
          </button>
        ))}
      </div>
      <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(240px,1fr))]">
        {loading && items.length === 0 ? (
          <p className="font-editorial-serif italic text-[#8a8578] col-span-full">Loading…</p>
        ) : items.length === 0 ? (
          <p className="font-editorial-serif italic text-[#8a8578] col-span-full">
            No agents fit that brief.
          </p>
        ) : (
          items.map((listing) => (
            <ListingCard
              key={listing.listing_id}
              listing={listing}
              installed={installedSlugs?.has?.(listing.slug) ?? false}
            />
          ))
        )}
      </div>
    </div>
  );
}
```

Also create the supporting card component `apps/frontend/src/components/marketplace/drawer/ListingCard.tsx`:

```tsx
"use client";

import Link from "next/link";
import type { Listing } from "@/lib/marketplace/types";
import { useHire } from "./useHire";

export function ListingCard({ listing, installed }: { listing: Listing; installed: boolean }) {
  const { hire, isHiring } = useHire();
  const isFree = listing.price_cents === 0;
  const tagline = listing.description_md.split(/\n{2,}/, 1)[0]?.replace(/[#*_`]/g, "").trim() || "";

  return (
    <article
      className={`relative rounded-2xl border p-5 flex flex-col gap-3 min-h-[200px] transition ${
        installed
          ? "border-[#aac6b4] bg-[#f4f8f4]"
          : "border-[#e0dbd0] bg-white hover:-translate-y-0.5 hover:shadow-md hover:border-[#cfc7b8]"
      }`}
    >
      {installed && (
        <span className="absolute top-3 right-3.5 text-[9.5px] tracking-widest text-[#06402b] font-bold">
          INSTALLED
        </span>
      )}
      <Link
        href={`/marketplace/listing/${listing.slug}`}
        target="_blank"
        className="font-editorial-serif text-lg leading-tight text-[#1a1a1a] no-underline hover:underline"
      >
        {listing.name}
      </Link>
      <p className="text-[13px] text-[#5b5b5b] leading-snug flex-1 line-clamp-2">
        {tagline}
      </p>
      <div className="flex items-center justify-between pt-2.5 border-t border-dashed border-[#e0dbd0]">
        <span className="text-[11.5px] italic text-[#5b5b5b]">by {listing.seller_id}</span>
        {installed ? (
          <button
            disabled
            className="text-[#06402b] border border-[#06402b] bg-transparent px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold opacity-70"
          >
            Hired ✓
          </button>
        ) : (
          <button
            onClick={() => hire(listing)}
            disabled={isHiring}
            className="bg-[#06402b] text-white px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold hover:bg-[#0a5a3d] disabled:opacity-60"
          >
            Hire
          </button>
        )}
      </div>
    </article>
  );
}
```

`useHire` is implemented in Task 10 — for now stub it with a placeholder so the test compiles. Create `apps/frontend/src/components/marketplace/drawer/useHire.ts`:

```ts
"use client";
export function useHire() {
  return {
    hire: (_listing: unknown) => Promise.resolve(),
    isHiring: false,
  };
}
```

- [ ] **Step 4: Run, confirm pass**

```bash
pnpm test -- --run BrowseView
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/BrowseView.tsx apps/frontend/src/components/marketplace/drawer/ListingCard.tsx apps/frontend/src/components/marketplace/drawer/useHire.ts apps/frontend/tests/unit/components/marketplace/drawer/BrowseView.test.tsx
git commit -m "feat(drawer): BrowseView with search, chips, and card grid"
```

---

## Task 10: BrowseView Hire flow (free + paid)

**Files:**
- Replace: `apps/frontend/src/components/marketplace/drawer/useHire.ts`
- Test: `apps/frontend/tests/unit/components/marketplace/drawer/useHire.test.ts`

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/tests/unit/components/marketplace/drawer/useHire.test.ts`:

```ts
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

const deployFn = vi.fn();
const checkoutFn = vi.fn();

vi.mock("@/hooks/useCatalog", () => ({
  useCatalog: () => ({ deploy: deployFn }),
}));
vi.mock("@/lib/marketplace/api", () => ({
  checkout: (opts: unknown) => checkoutFn(opts),
}));

const showInstallToast = vi.fn();
const close = vi.fn();
const setFreshlyInstalled = vi.fn();

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ showInstallToast, close, setFreshlyInstalled }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("tok_abc") }),
  useUser: () => ({ user: { primaryEmailAddress: { emailAddress: "buyer@example.com" } } }),
}));

vi.stubGlobal("location", { href: "http://localhost:3000/chat", origin: "http://localhost:3000" });

import { useHire } from "@/components/marketplace/drawer/useHire";

describe("useHire", () => {
  beforeEach(() => {
    deployFn.mockReset();
    checkoutFn.mockReset();
    showInstallToast.mockReset();
    close.mockReset();
    setFreshlyInstalled.mockReset();
  });

  it("free listing: calls deploy, shows toast, closes drawer", async () => {
    deployFn.mockResolvedValueOnce({ slug: "atlas", agent_uuid: "ag_42" });
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire({
        listing_id: "l1",
        slug: "atlas",
        name: "Atlas",
        price_cents: 0,
      } as unknown as Parameters<typeof result.current.hire>[0]);
    });
    expect(deployFn).toHaveBeenCalledWith("atlas");
    expect(setFreshlyInstalled).toHaveBeenCalledWith("ag_42");
    expect(showInstallToast).toHaveBeenCalledWith({ agentName: "Atlas", agentId: "ag_42" });
    await waitFor(() => expect(close).toHaveBeenCalled(), { timeout: 1500 });
  });

  it("paid listing: redirects to Stripe checkout via window.location", async () => {
    checkoutFn.mockResolvedValueOnce({ checkout_url: "https://checkout.stripe.com/abc", session_id: "cs_1" });
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire({
        listing_id: "l2",
        slug: "pulse",
        name: "Pulse",
        price_cents: 1200,
      } as unknown as Parameters<typeof result.current.hire>[0]);
    });
    expect(window.location.href).toBe("https://checkout.stripe.com/abc");
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run useHire
```

Expected: FAIL — current stub doesn't dispatch.

- [ ] **Step 3: Implement**

Replace `apps/frontend/src/components/marketplace/drawer/useHire.ts`:

```ts
"use client";

import { useState, useCallback } from "react";
import { useAuth, useUser } from "@clerk/nextjs";

import { useCatalog } from "@/hooks/useCatalog";
import { checkout } from "@/lib/marketplace/api";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import type { Listing } from "@/lib/marketplace/types";

export function useHire() {
  const { deploy } = useCatalog() as unknown as {
    deploy: (slug: string) => Promise<{ slug: string; agent_uuid?: string }>;
  };
  const { close, showInstallToast, setFreshlyInstalled } = useMarketplaceDrawer();
  const { getToken } = useAuth();
  const { user } = useUser();
  const [isHiring, setIsHiring] = useState(false);

  const hire = useCallback(
    async (listing: Listing) => {
      setIsHiring(true);
      try {
        if (listing.price_cents === 0) {
          const result = await deploy(listing.slug);
          if (result.agent_uuid) {
            setFreshlyInstalled(result.agent_uuid);
          }
          showInstallToast({
            agentName: listing.name,
            agentId: result.agent_uuid ?? listing.slug,
          });
          // 700ms delay before closing matches the mock spec
          setTimeout(() => close(), 700);
        } else {
          const jwt = await getToken();
          if (!jwt) throw new Error("Not authenticated");
          const result = await checkout({
            listingSlug: listing.slug,
            successUrl: `${window.location.origin}/marketplace/deploy-success?listing_slug=${encodeURIComponent(listing.slug)}`,
            cancelUrl: `${window.location.origin}/chat`,
            jwt,
            email: user?.primaryEmailAddress?.emailAddress,
          });
          window.location.href = result.checkout_url;
        }
      } finally {
        setIsHiring(false);
      }
    },
    [deploy, close, showInstallToast, setFreshlyInstalled, getToken, user],
  );

  return { hire, isHiring };
}
```

- [ ] **Step 4: Run, confirm pass**

```bash
pnpm test -- --run useHire
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/useHire.ts apps/frontend/tests/unit/components/marketplace/drawer/useHire.test.ts
git commit -m "feat(drawer): useHire hook for free + paid install flow"
```

---

## Task 11: MyListingsView — stats + listings list + cash-out

**Files:**
- Replace: `apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx`
- Test: `apps/frontend/tests/unit/components/marketplace/drawer/MyListingsView.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/tests/unit/components/marketplace/drawer/MyListingsView.test.tsx`:

```tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const myListingsFn = vi.fn();
const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("@/lib/marketplace/api", () => ({
  myListings: (opts: unknown) => myListingsFn(opts),
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "u_seller", getToken: vi.fn().mockResolvedValue("tok_abc") }),
}));

const setView = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ setView }),
}));

import { MyListingsView } from "@/components/marketplace/drawer/MyListingsView";

const SAMPLE_LISTINGS = [
  { listing_id: "l1", slug: "atlas", name: "Atlas", status: "published", price_cents: 1200,
    install_count: 847, updated_at: "2026-05-05", description_md: "", seller_id: "u_seller",
    format: "openclaw", tags: [], version: 1, created_at: "2026-05-01", published_at: "2026-05-01" },
  { listing_id: "l2", slug: "tally", name: "Tally", status: "review", price_cents: 0,
    install_count: 0, updated_at: "2026-05-07", description_md: "", seller_id: "u_seller",
    format: "openclaw", tags: [], version: 1, created_at: "2026-05-07", published_at: null },
];

describe("MyListingsView", () => {
  beforeEach(() => {
    myListingsFn.mockReset();
    apiGet.mockReset();
    apiPost.mockReset();
    setView.mockReset();
  });

  it("renders three stats and listings list", async () => {
    myListingsFn.mockResolvedValueOnce({ items: SAMPLE_LISTINGS });
    apiGet.mockResolvedValueOnce({ balance_held_cents: 0, dashboard_url: null });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText("Atlas")).toBeInTheDocument());
    expect(screen.getByText("1")).toBeInTheDocument(); // 1 active (status=published)
    expect(screen.getByText("847")).toBeInTheDocument(); // total installs
    expect(screen.getByText("$0.00")).toBeInTheDocument(); // pending payout
    expect(screen.getByText(/in review/i)).toBeInTheDocument();
    expect(screen.getByText(/^live$/i)).toBeInTheDocument();
  });

  it("shows 'Set up payouts' when dashboard_url is null", async () => {
    myListingsFn.mockResolvedValueOnce({ items: SAMPLE_LISTINGS });
    apiGet.mockResolvedValueOnce({ balance_held_cents: 0, dashboard_url: null });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText(/set up payouts/i)).toBeInTheDocument());
  });

  it("shows 'Cash out' when dashboard_url exists", async () => {
    myListingsFn.mockResolvedValueOnce({ items: SAMPLE_LISTINGS });
    apiGet.mockResolvedValueOnce({
      balance_held_cents: 2430,
      dashboard_url: "https://dashboard.stripe.com/abc",
    });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText(/cash out/i)).toBeInTheDocument());
    expect(screen.getByText("$24.30")).toBeInTheDocument();
  });

  it("'Set up payouts' click triggers /payouts/onboard then redirects", async () => {
    myListingsFn.mockResolvedValueOnce({ items: [] });
    apiGet.mockResolvedValueOnce({ balance_held_cents: 0, dashboard_url: null });
    apiPost.mockResolvedValueOnce({ onboarding_url: "https://connect.stripe.com/setup/xyz" });

    const user = userEvent.setup();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      value: { ...originalLocation, href: "" },
      writable: true,
    });

    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText(/set up payouts/i)).toBeInTheDocument());
    await user.click(screen.getByText(/set up payouts/i));
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/marketplace/payouts/onboard"));
    await waitFor(() => expect(window.location.href).toContain("connect.stripe.com"));
  });

  it("'+ New listing' switches to publish view", async () => {
    myListingsFn.mockResolvedValueOnce({ items: [] });
    apiGet.mockResolvedValueOnce({ balance_held_cents: 0, dashboard_url: null });
    const user = userEvent.setup();
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByRole("button", { name: /new listing/i })).toBeVisible());
    await user.click(screen.getByRole("button", { name: /new listing/i }));
    expect(setView).toHaveBeenCalledWith("publish");
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run MyListingsView
```

Expected: FAIL — placeholder doesn't render any of these.

- [ ] **Step 3: Implement**

Replace `apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { myListings } from "@/lib/marketplace/api";
import { useApi } from "@/lib/api";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import type { Listing } from "@/lib/marketplace/types";

interface PayoutsDashboard {
  balance_held_cents?: number;
  lifetime_earned_cents?: number;
  dashboard_url?: string | null;
}

export function MyListingsView() {
  const { userId, getToken } = useAuth();
  const api = useApi();
  const { setView } = useMarketplaceDrawer();
  const [listings, setListings] = useState<Listing[] | null>(null);
  const [dashboard, setDashboard] = useState<PayoutsDashboard | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!jwt || !userId) return;
      const [r1, r2] = await Promise.all([
        myListings({ sellerId: userId, jwt }),
        api.get("/marketplace/payouts/dashboard"),
      ]);
      if (!alive) return;
      setListings(r1.items);
      setDashboard(r2 as PayoutsDashboard);
    })().catch(() => {
      if (alive) {
        setListings([]);
        setDashboard({});
      }
    });
    return () => {
      alive = false;
    };
  }, [userId, getToken, api]);

  if (!listings || !dashboard) {
    return <p className="font-editorial-serif italic text-[#8a8578]">Loading…</p>;
  }

  const activeCount = listings.filter((l) => l.status === "published").length;
  const totalInstalls = listings.reduce((s, l) => s + ((l as unknown as { install_count?: number }).install_count ?? 0), 0);
  const pendingCents = dashboard.balance_held_cents ?? 0;
  const isOnboarded = !!dashboard.dashboard_url;

  async function handlePayoutAction() {
    setBusy(true);
    try {
      if (isOnboarded) {
        window.open(dashboard.dashboard_url!, "_blank", "noopener,noreferrer");
      } else {
        const r = (await api.post("/marketplace/payouts/onboard")) as { onboarding_url: string };
        window.location.href = r.onboarding_url;
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="flex items-end justify-between gap-3.5 flex-wrap mb-4">
        <div>
          <h3 className="font-editorial-serif text-2xl text-[#1a1a1a]">My listings</h3>
          <div className="flex gap-6 mt-2 text-[13px] text-[#5b5b5b]">
            <Stat label="active listings" value={activeCount.toString()} />
            <Stat label="total installs" value={totalInstalls.toLocaleString()} />
            <Stat
              label={isOnboarded ? "pending payout" : "set up payouts"}
              value={`$${(pendingCents / 100).toFixed(2)}`}
              action={isOnboarded ? "Cash out →" : "Set up payouts →"}
              onAction={handlePayoutAction}
              disabled={busy}
            />
          </div>
        </div>
        <button
          type="button"
          onClick={() => setView("publish")}
          className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-4 py-2 rounded-full text-[13px] font-semibold"
        >
          + New listing
        </button>
      </div>

      {listings.length === 0 ? (
        <div className="text-center py-12 border border-dashed border-[#e0dbd0] rounded-2xl bg-white">
          <h4 className="font-editorial-serif text-xl mb-2">No listings yet.</h4>
          <p className="text-[14px] text-[#5b5b5b] mb-4">
            Open chat to publish your first agent.
          </p>
          <button
            type="button"
            onClick={() => setView("publish")}
            className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full text-[14px] font-semibold"
          >
            + Publish your agent
          </button>
        </div>
      ) : (
        <div className="space-y-2.5">
          {listings.map((l) => (
            <ListingRow key={l.listing_id} listing={l} />
          ))}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  action,
  onAction,
  disabled,
}: {
  label: string;
  value: string;
  action?: string;
  onAction?: () => void;
  disabled?: boolean;
}) {
  return (
    <div>
      <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">{value}</strong>
      <span>{label}</span>
      {action && (
        <button
          type="button"
          onClick={onAction}
          disabled={disabled}
          className="block text-[12px] font-semibold text-[#06402b] hover:underline disabled:opacity-60 mt-1"
        >
          {action}
        </button>
      )}
    </div>
  );
}

function ListingRow({ listing }: { listing: Listing }) {
  const installCount = (listing as unknown as { install_count?: number }).install_count ?? 0;
  return (
    <div className="bg-white border border-[#e0dbd0] rounded-2xl p-5 flex items-center gap-4">
      <div className="flex-1">
        <div className="font-editorial-serif text-lg">{listing.name}</div>
        <div className="flex gap-3 text-[12.5px] text-[#5b5b5b]">
          <span>Updated {new Date(listing.created_at).toLocaleDateString()}</span>
          <span>{installCount} installs</span>
          <span className="font-semibold text-[#06402b]">
            {listing.price_cents === 0 ? "Free" : "Premium tier"}
          </span>
        </div>
      </div>
      <span
        className={`text-[10.5px] tracking-widest uppercase font-bold px-2.5 py-1 rounded-full ${
          listing.status === "published"
            ? "bg-[#e3efe6] text-[#06402b]"
            : listing.status === "review"
              ? "bg-[#fef4e3] text-[#a07520]"
              : "bg-[#f3eee5] text-[#5b5b5b]"
        }`}
      >
        {listing.status === "published"
          ? "Live"
          : listing.status === "review"
            ? "In review"
            : listing.status}
      </span>
    </div>
  );
}
```

- [ ] **Step 4: Run, confirm pass**

```bash
pnpm test -- --run MyListingsView
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx apps/frontend/tests/unit/components/marketplace/drawer/MyListingsView.test.tsx
git commit -m "feat(drawer): MyListingsView with stats, listings, and cash-out branching"
```

---

## Task 12: PublishWizard scaffold

**Files:**
- Replace: `apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx`
- Create: `apps/frontend/src/components/marketplace/drawer/wizardSteps.tsx`
- Test: `apps/frontend/tests/unit/components/marketplace/drawer/PublishWizard.test.tsx`

This task only builds the stepper / footer / state machine. Steps 1-4 content are placeholders filled in by Tasks 13-15.

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/tests/unit/components/marketplace/drawer/PublishWizard.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/components/marketplace/drawer/wizardSteps", () => ({
  PickAgentStep: ({ onSelect, selected }: { onSelect: (id: string) => void; selected?: string }) => (
    <div>
      <span>step1</span>
      <button onClick={() => onSelect("agent_pulse")}>pick</button>
      {selected && <span>selected={selected}</span>}
    </div>
  ),
  DetailsStep: ({ form }: { form: { name: string; tagline: string } }) => (
    <div>step2 — name={form.name} tag={form.tagline}</div>
  ),
  PricingStep: () => <div>step3</div>,
  ReviewStep: () => <div>step4</div>,
  SuccessStep: () => <div>step5</div>,
}));

const setView = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ setView, state: { publishAgentId: undefined } }),
}));

import { PublishWizard } from "@/components/marketplace/drawer/PublishWizard";

describe("PublishWizard", () => {
  it("renders Step 1 by default", () => {
    render(<PublishWizard />);
    expect(screen.getByText("step1")).toBeInTheDocument();
  });

  it("Continue is disabled until Step 1 selects an agent", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    expect(screen.getByRole("button", { name: /continue/i })).toBeDisabled();
    await user.click(screen.getByText("pick"));
    expect(screen.getByRole("button", { name: /continue/i })).toBeEnabled();
  });

  it("Continue advances Step 1 → 2", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i }));
    expect(screen.getByText(/^step2/)).toBeInTheDocument();
  });

  it("Back button goes from Step 2 → 1", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await user.click(screen.getByRole("button", { name: /^back/i }));
    expect(screen.getByText("step1")).toBeInTheDocument();
  });

  it("Cancel returns to prevView", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(setView).toHaveBeenCalledWith("browse");
  });

  it("Step 4 Continue text is 'Submit for review'", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 2
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 3
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 4
    expect(screen.getByRole("button", { name: /submit for review/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run PublishWizard
```

Expected: FAIL — placeholder.

- [ ] **Step 3: Implement scaffold**

Replace `apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx`:

```tsx
"use client";

import { useState } from "react";

import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import {
  PickAgentStep,
  DetailsStep,
  PricingStep,
  ReviewStep,
  SuccessStep,
} from "./wizardSteps";

export interface PublishForm {
  agentId?: string;
  name: string;
  tagline: string;
  description: string;
  category: string;
  tags: string[];
  priceTier: "free" | "paid";
  priceDollars: number;
}

const INITIAL_FORM: PublishForm = {
  name: "",
  tagline: "",
  description: "",
  category: "Operations",
  tags: [],
  priceTier: "free",
  priceDollars: 10,
};

type Step = 1 | 2 | 3 | 4 | 5;

export function PublishWizard() {
  const { setView, state } = useMarketplaceDrawer();
  const [step, setStep] = useState<Step>(1);
  const [form, setForm] = useState<PublishForm>({
    ...INITIAL_FORM,
    agentId: state.publishAgentId,
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const setAgent = (id: string) =>
    setForm((f) => ({ ...f, agentId: id, name: f.name || titleFromId(id) }));

  const updateForm = (patch: Partial<PublishForm>) => setForm((f) => ({ ...f, ...patch }));

  function canContinue() {
    if (step === 1) return !!form.agentId;
    if (step === 2) return form.name.trim().length > 0 && form.tagline.trim().length > 0;
    if (step === 3) return form.priceTier === "free" || form.priceDollars >= 1;
    if (step === 4) return true;
    return false;
  }

  function next() {
    if (step < 4) setStep((s) => (s + 1) as Step);
    else if (step === 4) {
      // Submit chain (Task 14)
      void submit();
    }
  }

  function back() {
    if (step > 1) setStep((s) => (s - 1) as Step);
  }

  function cancel() {
    setView(state.prevView ?? "browse");
  }

  async function submit() {
    // Stubbed — Task 14 implements the 3-call chain with retry/draft-resume.
    setStep(5);
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-1.5 mb-4">
        {[1, 2, 3, 4].map((n) => (
          <span
            key={n}
            className={`w-5 h-1 rounded-full ${n <= step ? "bg-[#06402b]" : "bg-[#e0dbd0]"}`}
          />
        ))}
        <span className="text-[11.5px] text-[#5b5b5b] ml-2">
          {step === 5 ? "Submitted" : `Step ${step} of 4`}
        </span>
      </div>

      <div className="flex-1">
        {step === 1 && <PickAgentStep selected={form.agentId} onSelect={setAgent} />}
        {step === 2 && <DetailsStep form={form} onChange={updateForm} />}
        {step === 3 && <PricingStep form={form} onChange={updateForm} />}
        {step === 4 && <ReviewStep form={form} />}
        {step === 5 && <SuccessStep onViewMine={() => setView("mine")} />}
      </div>

      {step <= 4 && (
        <div className="flex justify-between gap-2 pt-3 mt-4 border-t border-[#e0dbd0]">
          <button
            type="button"
            onClick={back}
            disabled={step === 1 || submitting}
            className="text-[#5b5b5b] hover:bg-[#f3eee5] px-4 py-2 rounded-full text-[14px] disabled:opacity-30"
          >
            ← Back
          </button>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={cancel}
              disabled={submitting}
              className="text-[#5b5b5b] hover:bg-[#f3eee5] px-4 py-2 rounded-full text-[14px]"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={next}
              disabled={!canContinue() || submitting}
              className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full text-[14px] font-semibold disabled:opacity-50"
            >
              {step === 4 ? "Submit for review →" : "Continue →"}
            </button>
          </div>
        </div>
      )}

      {submitError && (
        <p role="alert" className="mt-3 text-[13px] text-[#b32424]">
          {submitError}
        </p>
      )}
    </div>
  );
}

function titleFromId(id: string): string {
  return id.replace(/^agent[_-]/, "").replace(/[_-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
```

Create the placeholder `apps/frontend/src/components/marketplace/drawer/wizardSteps.tsx`:

```tsx
"use client";

import type { PublishForm } from "./PublishWizard";

export function PickAgentStep({ selected, onSelect }: { selected?: string; onSelect: (id: string) => void }) {
  return (
    <div>
      Pick an agent (placeholder).
      <button onClick={() => onSelect("agent_pulse")}>Pick Pulse</button>
      {selected && <span>selected={selected}</span>}
    </div>
  );
}

export function DetailsStep({
  form,
  onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  return <div>Details (placeholder). name=&quot;{form.name}&quot;</div>;
}

export function PricingStep({
  form,
  onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  return <div>Pricing (placeholder). tier={form.priceTier}</div>;
}

export function ReviewStep({ form }: { form: PublishForm }) {
  return <div>Review (placeholder). agent={form.agentId}</div>;
}

export function SuccessStep({ onViewMine }: { onViewMine: () => void }) {
  return (
    <div className="text-center py-8">
      <div className="text-3xl mb-3">✓</div>
      <h3 className="font-editorial-serif text-2xl mb-2">Submitted for review.</h3>
      <p className="text-[14px] text-[#5b5b5b] mb-4">
        You&apos;ll get a notification when it&apos;s live — usually within 24 hours.
      </p>
      <button
        onClick={onViewMine}
        className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full font-semibold"
      >
        View my listings →
      </button>
    </div>
  );
}
```

- [ ] **Step 4: Run, confirm pass**

```bash
pnpm test -- --run PublishWizard
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx apps/frontend/src/components/marketplace/drawer/wizardSteps.tsx apps/frontend/tests/unit/components/marketplace/drawer/PublishWizard.test.tsx
git commit -m "feat(drawer): publish-wizard scaffold (stepper + state machine)"
```

---

## Task 13: PickAgent step + Details step

**Files:**
- Replace: parts of `apps/frontend/src/components/marketplace/drawer/wizardSteps.tsx`
- Test: extend `apps/frontend/tests/unit/components/marketplace/drawer/PublishWizard.test.tsx`

- [ ] **Step 1: Write failing tests**

Append to `apps/frontend/tests/unit/components/marketplace/drawer/PublishWizard.test.tsx`:

```tsx
// (Imports already present at top of file)

import { PickAgentStep } from "@/components/marketplace/drawer/wizardSteps";

const useAgentsMock = vi.fn();
const myAgentsSummaryMock = vi.fn();

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => useAgentsMock(),
  agentDisplayName: (a: { name?: string; identity?: { name?: string } }) =>
    a.identity?.name || a.name || "?",
}));
vi.mock("@/lib/marketplace/api", () => ({
  myAgentsSummary: (opts: unknown) => myAgentsSummaryMock(opts),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("tok_abc") }),
}));

describe("PickAgentStep", () => {
  it("renders agents from useAgents and marks main as disabled", async () => {
    useAgentsMock.mockReturnValue({
      agents: [
        { id: "agent_main", name: "main", identity: { name: "main" } },
        { id: "agent_pulse", name: "pulse", identity: { name: "Pulse" } },
      ],
      defaultId: "agent_main",
    });
    myAgentsSummaryMock.mockResolvedValueOnce({
      agents: [
        { agent_id: "agent_main", name: "main", skill_count: 0, channel_count: 0, cron_count: 0 },
        { agent_id: "agent_pulse", name: "Pulse", skill_count: 3, channel_count: 1, cron_count: 1 },
      ],
    });
    const onSelect = vi.fn();
    render(<PickAgentStep onSelect={onSelect} />);
    await waitFor(() => expect(screen.getByText("Pulse")).toBeInTheDocument());
    expect(screen.getByText(/3 skills/)).toBeInTheDocument();
    expect(screen.getByText(/1 cron/)).toBeInTheDocument();

    const mainCard = screen.getByText("main").closest("div");
    expect(mainCard?.parentElement).toHaveAttribute("aria-disabled", "true");
  });

  it("clicking a card calls onSelect with agent id", async () => {
    useAgentsMock.mockReturnValue({
      agents: [{ id: "agent_pulse", name: "pulse", identity: { name: "Pulse" } }],
      defaultId: "agent_main",
    });
    myAgentsSummaryMock.mockResolvedValueOnce({ agents: [] });
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<PickAgentStep onSelect={onSelect} />);
    await user.click(screen.getByText("Pulse"));
    expect(onSelect).toHaveBeenCalledWith("agent_pulse");
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run PickAgentStep
```

Expected: FAIL.

- [ ] **Step 3: Implement PickAgentStep + DetailsStep**

Replace `apps/frontend/src/components/marketplace/drawer/wizardSteps.tsx` (in-place updates to PickAgentStep and DetailsStep, leave other steps as-is for now):

```tsx
"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { useAgents, agentDisplayName, type Agent } from "@/hooks/useAgents";
import { myAgentsSummary, type AgentSummary } from "@/lib/marketplace/api";
import type { PublishForm } from "./PublishWizard";

export function PickAgentStep({
  selected,
  onSelect,
}: {
  selected?: string;
  onSelect: (id: string) => void;
}) {
  const { agents, defaultId } = useAgents() as unknown as {
    agents: Agent[];
    defaultId?: string;
  };
  const { getToken } = useAuth();
  const [summary, setSummary] = useState<AgentSummary[] | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!jwt) return;
      const r = await myAgentsSummary({ jwt });
      if (alive) setSummary(r.agents);
    })().catch(() => {
      if (alive) setSummary([]);
    });
    return () => {
      alive = false;
    };
  }, [getToken]);

  const summaryById: Record<string, AgentSummary> = (summary ?? []).reduce(
    (acc, a) => ({ ...acc, [a.agent_id]: a }),
    {},
  );

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Pick which agent in your workspace you&apos;d like to publish. We&apos;ll bundle its config,
        skills, and cron jobs into a deployable artifact.
      </p>
      <div className="grid grid-cols-2 gap-2.5">
        {agents.map((agent) => {
          const isMain = agent.id === defaultId || agent.identity?.name === "main";
          const isSelected = agent.id === selected;
          const sub = summaryById[agent.id];
          return (
            <div
              key={agent.id}
              role="button"
              aria-disabled={isMain}
              tabIndex={isMain ? -1 : 0}
              onClick={() => !isMain && onSelect(agent.id)}
              className={`flex items-center gap-3 p-3 rounded-2xl border bg-white cursor-pointer transition ${
                isMain
                  ? "opacity-45 cursor-not-allowed bg-[#f3eee5]"
                  : isSelected
                    ? "border-[#06402b] bg-[#f4f8f4] shadow-[0_0_0_3px_rgba(6,64,43,0.08)]"
                    : "border-[#e0dbd0] hover:border-[#cfc7b8] hover:-translate-y-px"
              }`}
              title={isMain ? "main agent can't be published" : undefined}
            >
              <div className="w-9 h-9 rounded-[10px] bg-[#dad4f0] text-[#4f458b] font-editorial-serif font-semibold text-sm flex items-center justify-center">
                {(agent.identity?.name ?? agent.name ?? "?").slice(0, 2)}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[14px] font-medium">{agentDisplayName(agent)}</div>
                <div className="text-[11px] text-[#5b5b5b]">
                  {isMain
                    ? "core agent · not publishable"
                    : sub
                      ? `${sub.skill_count} skill${sub.skill_count === 1 ? "" : "s"} · ${sub.cron_count} cron${sub.cron_count === 1 ? "" : "s"}`
                      : "…"}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function DetailsStep({
  form,
  onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  const [tagInput, setTagInput] = useState("");

  function addTag(raw: string) {
    const t = raw.trim().toLowerCase();
    if (!t || form.tags.includes(t) || form.tags.length >= 5) return;
    onChange({ tags: [...form.tags, t] });
  }

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Help buyers find your agent. This is what they&apos;ll see in the marketplace.
      </p>
      <Field label="Public name">
        <input
          value={form.name}
          onChange={(e) => onChange({ name: e.target.value })}
          className="border border-[#e0dbd0] bg-white rounded-xl px-3.5 py-2.5 text-sm w-full focus:border-[#06402b] outline-0"
        />
      </Field>
      <Field label="One-line tagline" hint="Max 80 characters.">
        <input
          maxLength={80}
          value={form.tagline}
          onChange={(e) => onChange({ tagline: e.target.value })}
          className="border border-[#e0dbd0] bg-white rounded-xl px-3.5 py-2.5 text-sm w-full focus:border-[#06402b] outline-0"
        />
      </Field>
      <Field label="Description" hint="Markdown allowed.">
        <textarea
          rows={4}
          value={form.description}
          onChange={(e) => onChange({ description: e.target.value })}
          className="border border-[#e0dbd0] bg-white rounded-xl px-3.5 py-2.5 text-sm w-full focus:border-[#06402b] outline-0 min-h-[88px] resize-y"
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Category">
          <select
            value={form.category}
            onChange={(e) => onChange({ category: e.target.value })}
            className="border border-[#e0dbd0] bg-white rounded-xl px-3.5 py-2.5 text-sm w-full focus:border-[#06402b] outline-0"
          >
            {["Operations", "Sales", "Engineering", "Research", "Finance", "Marketing", "Personal"].map(
              (c) => (
                <option key={c}>{c}</option>
              ),
            )}
          </select>
        </Field>
        <Field label="Tags (max 5)">
          <div className="flex gap-1.5 flex-wrap items-center p-2 bg-white border border-[#e0dbd0] rounded-xl min-h-[44px]">
            {form.tags.map((t) => (
              <span
                key={t}
                className="bg-[#f3eee5] text-[#1a1a1a] text-[12px] px-2.5 py-1 rounded-full inline-flex items-center gap-1.5"
              >
                {t}
                <button
                  type="button"
                  onClick={() => onChange({ tags: form.tags.filter((x) => x !== t) })}
                  className="text-[#5b5b5b] hover:text-[#1a1a1a]"
                >
                  ×
                </button>
              </span>
            ))}
            <input
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addTag(tagInput);
                  setTagInput("");
                } else if (e.key === "Backspace" && !tagInput && form.tags.length) {
                  onChange({ tags: form.tags.slice(0, -1) });
                }
              }}
              placeholder="type + Enter"
              className="flex-1 min-w-[80px] outline-0 bg-transparent text-sm"
            />
          </div>
        </Field>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5 mb-3.5">
      <label className="text-[12.5px] font-semibold text-[#1a1a1a]">{label}</label>
      {children}
      {hint && <span className="text-[11.5px] text-[#5b5b5b]">{hint}</span>}
    </div>
  );
}

// Leave PricingStep, ReviewStep, SuccessStep as-is from Task 12 — they're filled in next tasks.
export { PricingStep, ReviewStep, SuccessStep } from "./wizardStepsRest";
```

Move the as-yet-unfinished steps (PricingStep, ReviewStep, SuccessStep) into a new sibling file `apps/frontend/src/components/marketplace/drawer/wizardStepsRest.tsx`:

```tsx
"use client";

import type { PublishForm } from "./PublishWizard";

export function PricingStep({
  form,
  onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  return <div>Pricing (placeholder, Task 14). tier={form.priceTier}</div>;
}

export function ReviewStep({ form }: { form: PublishForm }) {
  return <div>Review (placeholder, Task 14). agent={form.agentId}</div>;
}

export function SuccessStep({ onViewMine }: { onViewMine: () => void }) {
  return (
    <div className="text-center py-8">
      <div className="text-3xl mb-3">✓</div>
      <h3 className="font-editorial-serif text-2xl mb-2">Submitted for review.</h3>
      <p className="text-[14px] text-[#5b5b5b] mb-4">
        You&apos;ll get a notification when it&apos;s live — usually within 24 hours.
      </p>
      <button
        onClick={onViewMine}
        className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full font-semibold"
      >
        View my listings →
      </button>
    </div>
  );
}
```

- [ ] **Step 4: Run, confirm pass**

```bash
pnpm test -- --run PickAgentStep
pnpm test -- --run PublishWizard
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/wizardSteps.tsx apps/frontend/src/components/marketplace/drawer/wizardStepsRest.tsx apps/frontend/tests/unit/components/marketplace/drawer/PublishWizard.test.tsx
git commit -m "feat(wizard): Step 1 PickAgent + Step 2 Details with /me/agents-summary"
```

---

## Task 14: Pricing step + Review step + Submit chain

**Files:**
- Replace: `apps/frontend/src/components/marketplace/drawer/wizardStepsRest.tsx`
- Replace: parts of `apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx` (the `submit()` body)
- Test: extend `apps/frontend/tests/unit/components/marketplace/drawer/PublishWizard.test.tsx`

- [ ] **Step 1: Implement PricingStep**

Replace `wizardStepsRest.tsx` PricingStep:

```tsx
export function PricingStep({
  form,
  onChange,
}: {
  form: PublishForm;
  onChange: (patch: Partial<PublishForm>) => void;
}) {
  const v = form.priceDollars;
  const stripe = +(v * 0.029 + 0.3).toFixed(2);
  const platform = +(v * 0.15).toFixed(2);
  const take = +(v - stripe - platform).toFixed(2);

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Set what your work is worth. Price is hidden from the marketplace browse view —
        buyers only see it at install time.
      </p>

      <div className="bg-[#f3eee5] rounded-2xl p-1 flex mb-4">
        <button
          type="button"
          onClick={() => onChange({ priceTier: "free" })}
          className={`flex-1 px-3.5 py-2.5 rounded-xl text-sm ${
            form.priceTier === "free"
              ? "bg-white text-[#1a1a1a] shadow-sm font-semibold"
              : "text-[#5b5b5b]"
          }`}
        >
          Free
        </button>
        <button
          type="button"
          onClick={() => onChange({ priceTier: "paid" })}
          className={`flex-1 px-3.5 py-2.5 rounded-xl text-sm ${
            form.priceTier === "paid"
              ? "bg-white text-[#1a1a1a] shadow-sm font-semibold"
              : "text-[#5b5b5b]"
          }`}
        >
          Premium tier
        </button>
      </div>

      {form.priceTier === "free" ? (
        <div className="bg-white border border-[#e0dbd0] rounded-xl p-4 text-[#5b5b5b] text-[13.5px] leading-snug">
          <strong className="text-[#1a1a1a]">Free agents install instantly.</strong>
          <br />
          No payout setup needed. You can switch this listing to a premium tier anytime.
        </div>
      ) : (
        <>
          <label className="block text-[12.5px] font-semibold text-[#1a1a1a] mb-1.5">
            Price per install (USD)
          </label>
          <div className="flex items-center bg-white border border-[#e0dbd0] rounded-xl px-3.5 py-3 gap-2 mb-2">
            <span className="font-editorial-serif text-2xl text-[#5b5b5b]">$</span>
            <input
              type="number"
              min={1}
              max={20}
              step={1}
              value={v}
              onChange={(e) => onChange({ priceDollars: Math.max(1, Math.min(20, +e.target.value || 0)) })}
              className="font-editorial-serif text-2xl flex-1 outline-0 bg-transparent"
            />
            <span className="text-[12px] text-[#5b5b5b]">USD · one-time, perpetual license</span>
          </div>
          <p className="text-[11.5px] text-[#5b5b5b] mb-3">
            Suggested range: $5–$20. Listings over $20 require Isol8 approval.
          </p>
          <div className="bg-white border border-[#e0dbd0] rounded-xl px-4 py-3.5 text-[13px]">
            <Row label="List price" value={`$${v.toFixed(2)}`} />
            <Row label="Stripe processing fee (2.9% + $0.30)" value={`−$${stripe.toFixed(2)}`} />
            <Row label="Isol8 platform fee (15%)" value={`−$${platform.toFixed(2)}`} />
            <div className="flex justify-between border-t border-[#e0dbd0] mt-1.5 pt-2.5 text-[#1a1a1a] font-semibold">
              <span>Your take-home per install</span>
              <span className="font-editorial-serif text-[#06402b] text-base">${take.toFixed(2)}</span>
            </div>
          </div>
          <p className="text-[12px] italic text-[#5b5b5b] mt-3">
            Earnings accrue to your seller balance. Cash out anytime from My Listings.
          </p>
        </>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between text-[#5b5b5b] py-0.5">
      <span>{label}</span>
      <span>{value}</span>
    </div>
  );
}
```

- [ ] **Step 2: Implement ReviewStep**

In `wizardStepsRest.tsx`, replace ReviewStep:

```tsx
import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { myAgentsSummary, type AgentSummary } from "@/lib/marketplace/api";

export function ReviewStep({ form }: { form: PublishForm }) {
  const { getToken } = useAuth();
  const [summary, setSummary] = useState<AgentSummary | null>(null);

  useEffect(() => {
    if (!form.agentId) return;
    (async () => {
      const jwt = await getToken();
      if (!jwt) return;
      const r = await myAgentsSummary({ jwt });
      const match = r.agents.find((a) => a.agent_id === form.agentId);
      if (match) setSummary(match);
    })();
  }, [form.agentId, getToken]);

  return (
    <div>
      <p className="text-[14px] text-[#5b5b5b] leading-snug mb-4">
        Here&apos;s how your listing will appear. Submit when ready — listings go live after
        admin moderation (usually within 24h).
      </p>
      <div className="bg-white border border-[#e0dbd0] rounded-2xl p-5">
        <div className="font-editorial-serif text-xl">{form.name || "Untitled"}</div>
        <div className="text-[12px] text-[#5b5b5b] italic mt-0.5">by you</div>
        <p className="text-[13.5px] text-[#5b5b5b] leading-snug mt-2">
          {form.tagline || "—"}
        </p>
        {form.tags.length > 0 && (
          <div className="flex gap-1.5 flex-wrap mt-3">
            {form.tags.map((t) => (
              <span key={t} className="bg-[#f3eee5] text-[#5b5b5b] text-[11px] px-2 py-0.5 rounded-full">
                {t}
              </span>
            ))}
          </div>
        )}
        <div className="flex justify-between text-[12.5px] text-[#5b5b5b] mt-3 pt-3 border-t border-dashed border-[#e0dbd0]">
          <span>
            {summary
              ? `Bundled: ${summary.skill_count} skill${summary.skill_count === 1 ? "" : "s"} · ${summary.cron_count} cron${summary.cron_count === 1 ? "" : "s"} · ${summary.channel_count} channel${summary.channel_count === 1 ? "" : "s"}`
              : "Bundled: …"}
          </span>
          <span className="font-semibold text-[#06402b]">
            {form.priceTier === "free" ? "Free" : "Premium tier"}
          </span>
        </div>
      </div>
      <p className="text-[12px] text-[#5b5b5b] leading-snug mt-3">
        By publishing, you agree to the <a href="#" className="text-[#06402b] underline">Marketplace Terms</a> and
        confirm this agent is yours to publish. Isol8 retains the right to remove listings that violate the{" "}
        <a href="#" className="text-[#06402b] underline">Acceptable Use Policy</a>.
      </p>
    </div>
  );
}
```

- [ ] **Step 3: Implement the submit chain in `PublishWizard.tsx`**

Replace the stubbed `submit()` function:

```tsx
import { useApi } from "@/lib/api";

// inside PublishWizard:
const api = useApi();
const [draftListingId, setDraftListingId] = useState<string | null>(null);

async function submit() {
  setSubmitting(true);
  setSubmitError(null);
  try {
    let listingId = draftListingId;
    if (!listingId) {
      const slug = slugify(form.name);
      const draft = (await api.post("/marketplace/listings", {
        slug,
        name: form.name.trim(),
        description_md: form.description || form.tagline,
        format: "openclaw",
        price_cents: form.priceTier === "free" ? 0 : Math.round(form.priceDollars * 100),
        tags: form.tags,
      })) as { listing_id: string };
      listingId = draft.listing_id;
      setDraftListingId(listingId);
    }
    await api.post(`/marketplace/listings/${listingId}/artifact-from-agent`, {
      agent_id: form.agentId,
    });
    await api.post(`/marketplace/listings/${listingId}/submit`, {});
    setStep(5);
  } catch (err) {
    const e = err as Error & { status?: number; detail?: string };
    if (e.status === 409 && (e.detail || "").includes("slug")) {
      setSubmitError("That slug is already taken — change the name and try again.");
      setDraftListingId(null);
      setStep(2);
    } else {
      setSubmitError(e.detail || e.message || "Failed to submit listing");
    }
  } finally {
    setSubmitting(false);
  }
}

function slugify(input: string): string {
  return input
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64) || "untitled";
}
```

- [ ] **Step 4: Run targeted tests, then full wizard suite**

```bash
pnpm test -- --run wizardStepsRest
pnpm test -- --run PublishWizard
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/wizardStepsRest.tsx apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx apps/frontend/tests/unit/components/marketplace/drawer/PublishWizard.test.tsx
git commit -m "feat(wizard): Steps 3-5 with 3-call submit chain and draft-resume"
```

---

## Task 15: MarketplaceToast component

**Files:**
- Create: `apps/frontend/src/components/marketplace/drawer/MarketplaceToast.tsx`
- Modify: `apps/frontend/src/app/chat/page.tsx` (mount the toast next to the drawer)
- Test: `apps/frontend/tests/unit/components/marketplace/drawer/MarketplaceToast.test.tsx`

- [ ] **Step 1: Write the failing test**

Create the test:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";

vi.useFakeTimers();

const dismissToast = vi.fn();
const stateRef = { current: { toast: { agentName: "Pulse", agentId: "ag_42" } as { agentName: string; agentId: string } | null } };

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    state: stateRef.current,
    dismissToast,
  }),
}));

import { MarketplaceToast } from "@/components/marketplace/drawer/MarketplaceToast";

describe("MarketplaceToast", () => {
  it("renders toast when state.toast is set", () => {
    stateRef.current = { toast: { agentName: "Pulse", agentId: "ag_42" } };
    render(<MarketplaceToast />);
    expect(screen.getByText(/pulse installed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /open in chat/i })).toBeVisible();
  });

  it("auto-dismisses after 5500ms", () => {
    stateRef.current = { toast: { agentName: "Pulse", agentId: "ag_42" } };
    render(<MarketplaceToast />);
    act(() => {
      vi.advanceTimersByTime(5500);
    });
    expect(dismissToast).toHaveBeenCalled();
  });

  it("renders nothing when state.toast is null", () => {
    stateRef.current = { toast: null };
    const { container } = render(<MarketplaceToast />);
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 2: Run, confirm fail**

```bash
pnpm test -- --run MarketplaceToast
```

Expected: FAIL.

- [ ] **Step 3: Implement**

Create `apps/frontend/src/components/marketplace/drawer/MarketplaceToast.tsx`:

```tsx
"use client";

import { useEffect } from "react";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";

export function MarketplaceToast() {
  const { state, dismissToast } = useMarketplaceDrawer();
  const toast = state.toast;

  useEffect(() => {
    if (!toast) return;
    const handle = setTimeout(() => dismissToast(), 5500);
    return () => clearTimeout(handle);
  }, [toast, dismissToast]);

  if (!toast) return null;

  function handleOpen() {
    window.dispatchEvent(new CustomEvent("selectAgent", { detail: { agentId: toast!.agentId } }));
    dismissToast();
  }

  return (
    <div className="fixed bottom-8 left-1/2 -translate-x-1/2 z-[100] bg-[#06402b] text-white rounded-2xl px-5 py-3.5 flex items-center gap-3.5 text-sm shadow-xl">
      <span>
        ✓ <strong>{toast.agentName}</strong> installed in your workspace.
      </span>
      <button
        type="button"
        onClick={handleOpen}
        className="bg-white/20 hover:bg-white/30 px-3 py-1.5 rounded-full text-[12.5px] font-semibold"
      >
        Open in chat →
      </button>
    </div>
  );
}
```

- [ ] **Step 4: Mount in /chat/page.tsx**

In `apps/frontend/src/app/chat/page.tsx`, add to the JSX (next to `<MarketplaceDrawer />`):

```tsx
import { MarketplaceToast } from "@/components/marketplace/drawer/MarketplaceToast";

// inside the provider:
<MarketplaceDrawer />
<MarketplaceToast />
```

- [ ] **Step 5: Run, confirm pass**

```bash
pnpm test -- --run MarketplaceToast
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/MarketplaceToast.tsx apps/frontend/src/app/chat/page.tsx apps/frontend/tests/unit/components/marketplace/drawer/MarketplaceToast.test.tsx
git commit -m "feat(drawer): MarketplaceToast bottom-center post-install confirmation"
```

---

## Task 16: Sidebar slide-in animation + new-arrival wiring

**Files:**
- Modify: `apps/frontend/src/app/globals.css` (or `apps/frontend/src/components/chat/ChatLayout.css`)
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx`

- [ ] **Step 1: Add the keyframe**

Append to `apps/frontend/src/app/globals.css`:

```css
@keyframes slideIn {
  from {
    opacity: 0;
    transform: translateY(-8px) scale(0.96);
    background: #dde9d6;
  }
  to {
    opacity: 1;
    transform: none;
  }
}
.agent-item.new-arrival {
  animation: slideIn 0.5s ease both;
}
```

- [ ] **Step 2: Wire the class in ChatLayout**

In `apps/frontend/src/components/chat/ChatLayout.tsx`:

Add the import:
```tsx
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
```

Inside the component:
```tsx
const { state: drawerState, setFreshlyInstalled } = useMarketplaceDrawer();
```

When the agents list refreshes after `freshlyInstalledAgentId` changes, clear the marker after 600ms so the class only applies once:

```tsx
useEffect(() => {
  if (!drawerState.freshlyInstalledAgentId) return;
  const handle = setTimeout(() => setFreshlyInstalled(null), 600);
  return () => clearTimeout(handle);
}, [drawerState.freshlyInstalledAgentId, setFreshlyInstalled]);
```

In the agent map (around line 302):

```tsx
{agents.map((agent) => {
  const isNewArrival = drawerState.freshlyInstalledAgentId === agent.id;
  return (
    <div
      key={agent.id}
      className={`agent-item${currentAgentId === agent.id ? " active" : ""}${isNewArrival ? " new-arrival" : ""}`}
      onClick={...}
    >
      ...
    </div>
  );
})}
```

- [ ] **Step 3: Smoke**

```bash
pnpm dev
```

Hire a free curator listing from the drawer. Confirm: drawer auto-closes, the new agent slides into the sidebar with a green flash. Toast appears bottom-center.

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/app/globals.css apps/frontend/src/components/chat/ChatLayout.tsx
git commit -m "feat(sidebar): new-arrival slideIn animation for freshly-installed agents"
```

---

## Task 17: Sidebar gold pub-dot for published agents

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx`
- Test: extend the ChatLayout test if one exists; otherwise smoke

- [ ] **Step 1: Add a hook reading the seller's listings to surface published agent ids**

In `apps/frontend/src/components/chat/ChatLayout.tsx`, add an effect that fetches the seller's listings (`myListings`) and derives the set of published agent ids:

```tsx
import { myListings } from "@/lib/marketplace/api";
// ... inside ChatLayout:
const [publishedAgentIds, setPublishedAgentIds] = useState<Set<string>>(new Set());
useEffect(() => {
  let alive = true;
  (async () => {
    if (!user?.id) return;
    const jwt = await getToken();
    if (!jwt) return;
    try {
      const r = await myListings({ sellerId: user.id, jwt });
      if (!alive) return;
      const ids = new Set<string>();
      for (const listing of r.items) {
        const sourceAgentId = (listing as unknown as { manifest_json?: { agent_id?: string } })
          .manifest_json?.agent_id;
        if (sourceAgentId) ids.add(sourceAgentId);
      }
      setPublishedAgentIds(ids);
    } catch {
      // best effort — drop silently
    }
  })();
  return () => {
    alive = false;
  };
}, [user?.id, getToken]);
```

The `getToken` here comes from Clerk's `useAuth()` — it should already be imported. If not, add `import { useAuth } from "@clerk/nextjs"` and `const { getToken } = useAuth()`.

- [ ] **Step 2: Add the dot to agent items**

In the agent map JSX, add the `.pub-dot` indicator next to each agent's avatar/monogram for agents whose id is in `publishedAgentIds`:

```tsx
{publishedAgentIds.has(agent.id) && (
  <span className="absolute top-0 right-0 w-2 h-2 rounded-full bg-[#c2a378] border-2 border-[#faf7f2]" />
)}
```

The exact positioning depends on the existing avatar wrapper — make it a relative-positioned absolute child.

- [ ] **Step 3: Smoke check**

Run dev, publish an agent via the drawer, verify the gold dot appears on its sidebar row after the listing's `manifest_json.agent_id` matches.

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/components/chat/ChatLayout.tsx
git commit -m "feat(sidebar): gold pub-dot for agents the user has published to marketplace"
```

---

## Task 18: Cleanup deletes (PublishAgentModal + AgentDetailPanel + GalleryItemRow + AgentsPanel)

**Files:**
- Delete: `apps/frontend/src/components/marketplace/PublishAgentModal.tsx`
- Delete: `apps/frontend/src/components/chat/AgentDetailPanel.tsx`
- Delete: `apps/frontend/src/components/chat/GalleryItemRow.tsx`
- Delete: `apps/frontend/tests/unit/components/marketplace/PublishAgentModal.test.tsx`
- Modify: `apps/frontend/src/components/control/panels/AgentsPanel.tsx` (5 distinct removal points)
- Modify: `apps/frontend/src/hooks/useCatalog.ts` (comment update)

- [ ] **Step 1: Remove all 5 AgentsPanel removal points**

Edit `apps/frontend/src/components/control/panels/AgentsPanel.tsx`:

1. Line 12 — drop `Upload` from the lucide-react import block
2. Line 18 — drop `import { PublishAgentModal } from "@/components/marketplace/PublishAgentModal"`
3. Lines 38-40 — drop the `publishingAgent` state + comment
4. Lines 187-194 — drop the `<Button onClick={() => setPublishingAgent(selected)}>` element
5. Lines 215-225 — drop the `{publishingAgent && <PublishAgentModal ... />}` mount

After edits, run:
```bash
grep -n "PublishAgentModal\|publishingAgent\|setPublishingAgent\|Upload" apps/frontend/src/components/control/panels/AgentsPanel.tsx
```

Expected: no results.

- [ ] **Step 2: Delete the four files**

```bash
rm apps/frontend/src/components/marketplace/PublishAgentModal.tsx
rm apps/frontend/src/components/chat/AgentDetailPanel.tsx
rm apps/frontend/src/components/chat/GalleryItemRow.tsx
rm apps/frontend/tests/unit/components/marketplace/PublishAgentModal.test.tsx
```

- [ ] **Step 3: Update useCatalog comment**

In `apps/frontend/src/hooks/useCatalog.ts`, replace the docstring header (around lines 6-15) so it no longer references the deleted GallerySection list and AgentDetailPanel — the hook is now used only for `installedSlugs` derivation and free `deploy(slug)` calls from the drawer.

- [ ] **Step 4: Run all marketplace tests + frontend tests**

```bash
pnpm test -- --run marketplace
pnpm test -- --run drawer
pnpm test -- --run GallerySection
pnpm test -- --run AgentsPanel
```

Expected: all green. There should be no test referencing the deleted files; if any do, delete those tests in this commit.

- [ ] **Step 5: Lint**

```bash
pnpm lint 2>&1 | grep -E "marketplace|drawer|chat" | head
```

Expected: 0 errors related to our area.

- [ ] **Step 6: Commit**

```bash
git add -A apps/frontend/
git commit -m "refactor: delete PublishAgentModal, chat-side AgentDetailPanel, GalleryItemRow; gut AgentsPanel publish flow"
```

---

## Task 19: Lint, build, manual smoke, push, PR

**Files:** none (verification + ship).

- [ ] **Step 1: Full frontend test suite**

```bash
cd apps/frontend
pnpm lint && pnpm test -- --run
```

Expected: all green, 0 lint errors related to changed files.

- [ ] **Step 2: Frontend build**

```bash
pnpm build
```

Expected: TypeScript check clean. (Prerender failures unrelated to /chat are pre-existing — see PR #566 history; not a blocker.)

- [ ] **Step 3: Backend test sweep**

```bash
cd ../backend
uv run pytest tests/unit/routers/test_marketplace_listings.py tests/unit/routers/test_marketplace_payouts.py tests/unit/routers/test_marketplace_purchases.py tests/unit/services/test_marketplace_search.py -q
uv run ruff check routers/ core/services/marketplace_search.py core/auth.py
```

Expected: green.

- [ ] **Step 4: Manual smoke (`pnpm dev`)**

Walk through the spec's manual smoke checklist:
1. Sign in, open `/chat`. Sidebar shows the 2-button Marketplace block.
2. Click `Browse` → drawer slides in from right at ~92vw. Browse view loads with curator listings.
3. Search "atlas" → grid filters.
4. Click a category chip → grid filters.
5. Click a card body → standalone listing detail opens in a new tab.
6. Click `Hire` on a free curator listing → drawer auto-closes, agent appears in sidebar with `slideIn` animation, toast appears bottom-center, "Open in chat →" button works.
7. Click `+ Publish your agent` in the drawer header → wizard Step 1 opens. Pick an agent (subline shows N skills · M crons).
8. Continue through Steps 2-4. Submit. Success step appears.
9. Click `View my listings →` → drawer switches to My Listings tab. New listing shows with "In review" badge. Stats show "1 active listings · 0 total installs · $0.00 pending payout".
10. Click "Set up payouts" if not onboarded → Stripe Connect onboarding URL opens.
11. Open Agents control panel — confirm there's no "Publish to marketplace" button anymore.

- [ ] **Step 5: Push branch**

```bash
git push -u origin feat/marketplace-drawer
```

- [ ] **Step 6: Open PR**

```bash
gh pr create --title "feat(chat): in-chat marketplace drawer (Browse + My Listings + Publish wizard)" \
  --body "$(cat <<'EOF'
## Summary

Right-side `<aside>` drawer in `/chat` with three views — Browse, My Listings, Publish wizard — that replaces the existing `GallerySection` curated rail and the `PublishAgentModal`. Preserves chat session; reuses every existing publish + checkout backend; adds two small backend endpoints.

Spec: `docs/superpowers/specs/2026-05-07-marketplace-drawer-design.md`
Plan: `docs/superpowers/plans/2026-05-07-marketplace-drawer.md`

## Key changes

- **Backend**: extend `/marketplace/listings/search` with seller-scoped filtering (`seller_id` + `status_in` query params, auth-gated when `seller_id == auth.user_id`); new `GET /marketplace/me/agents-summary` endpoint surfaces per-agent skill/channel/cron counts to the wizard
- **Frontend new**: `MarketplaceDrawer`, `MarketplaceDrawerProvider`, `BrowseView`, `MyListingsView`, `PublishWizard` (4 steps + success), `MarketplaceToast`, `useHire`
- **Frontend deletions**: `PublishAgentModal.tsx` (-472), chat-side `AgentDetailPanel.tsx` (-141), `GalleryItemRow.tsx` (-~80); 5 distinct removals from `AgentsPanel.tsx`
- **Sidebar**: `GallerySection` shrinks from a curated-agent rail to a 2-button Browse + Publish block; published agents render with a gold `.pub-dot`; freshly-installed agents play a `slideIn` keyframe (NEW)

## Test plan

- [x] Backend: 6 new tests in `test_marketplace_listings.py` (seller-scoped search + agents-summary), 2 in `test_marketplace_search.py` (include_unpublished flag)
- [x] Frontend: provider, drawer, browse, my-listings, wizard, hire-flow, toast tests
- [x] Lint clean, TypeScript clean
- [ ] Manual smoke walkthrough (per plan Task 19 Step 4)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: Watch CI**

```bash
gh pr view --repo Isol8AI/isol8 --json reviews,statusCheckRollup
```

Address any Codex findings inline (per the `feedback_codex_fix_loop_push` workflow).

---

## Done when

- All 19 tasks complete and committed.
- Frontend test suite green; backend test suite green.
- PR opens with green CI; Codex either approves (👍 reaction) or finds nothing P1/P2.
- Manual smoke walks all flows in the spec without regressions.
