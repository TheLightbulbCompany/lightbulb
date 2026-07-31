# Marketplace Agent Hiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Friday discover real marketplace listings and surface them as one-click, approval-gated hires — in chat and in Teams.

**Architecture:** A read-only baked OpenClaw plugin (`marketplace_search`, `recommend_agents`) calls the Isol8 backend over HTTP with the container's existing `OPENCLAW_HOOK_TOKEN`. Its typed result rides the existing `tool_end.result` path to the chat UI, which renders a hire card whose Deploy button hits the *existing* authenticated deploy route. Headless runs instead create a Paperclip `hire_agent` approval that lands in the Teams Inbox. Nothing agent-side mutates the roster.

**Tech Stack:** Python 3.12 / FastAPI / pytest (backend), TypeScript / React 19 / vitest (frontend), TypeScript + `defineToolPlugin` + typebox (plugin), Docker (image bake).

## Global Constraints

- **The plugin is read-only.** No tool may mutate the roster, EFS, or `openclaw.json`. `tools.exec.mode = "full"` fleet-wide, so a write tool gated on an OpenClaw approval would hang 120s and be re-dispatched by Paperclip forever.
- **Every deploy runs on a human click** carrying that human's Clerk session. No new privileged write path.
- **Plugin stays dumb** — a typed shell over HTTP. Ranking, projection, and policy live in the backend so they iterate without an image rebuild.
- **Paperclip approval payloads MUST set `adapterType: "openclaw_gateway"`** and `adapterConfig.url = wss://ws-<env>.isol8.co`. Paperclip defaults to `String(payload.adapterType ?? "process")`; a `"process"` agent is unreachable on Isol8 and fails silently at first wake.
- **`payload.agentId` is omitted** when creating a hire approval — nothing exists in the roster before the human says yes.
- Plugin pins must match the `FROM` tag's release line, exactly as every other baked plugin does (currently `2026.7.2-beta.3`).
- Existing default (no `view` param) search response shape is **unchanged** — the frontend already consumes it.

---

### Task 1: Skill + playbook copy — make Friday reach for the marketplace

Highest value-per-line change in the plan and independent of every other task. `friday_identity.py` ships on a backend deploy; the SKILL.md change ships on the next image bake.

**Files:**
- Modify: `apps/infra/openclaw/skills/isol8/SKILL.md` (marketplace section, ~line 151)
- Modify: `apps/backend/core/services/friday_identity.py` (`_AGENTS_TEMPLATE` step 3, ~line 441)
- Test: `apps/backend/tests/unit/services/test_friday_identity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by later tasks. Task 3's tool names (`marketplace_search`, `recommend_agents`) are referenced in the copy — keep them identical.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_friday_identity.py
def test_agents_template_checks_marketplace_before_atlas():
    """The playbook must lead with the marketplace, not Atlas: a marketplace
    hire costs $0 inference, Atlas is a full agent-building run."""
    from core.services.friday_identity import _AGENTS_TEMPLATE

    body = _AGENTS_TEMPLATE
    assert "marketplace_search" in body, "playbook must name the discovery tool"
    marketplace_pos = body.index("marketplace_search")
    atlas_pos = body.index("Atlas", marketplace_pos - 2000)
    assert marketplace_pos < atlas_pos, (
        "marketplace must be presented before Atlas in the hire step"
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py::test_agents_template_checks_marketplace_before_atlas -v`
Expected: FAIL — `marketplace_search` is not yet in the template.

- [ ] **Step 3: Rewrite `_AGENTS_TEMPLATE` step 3**

Replace the existing step 3 body (currently *"Recommend **Atlas** … Or recommend the **marketplace** for a pre-built one."*) with:

```
3. **Identify or hire.** For each piece, pick the right agent. If no agent
   is qualified, check the marketplace FIRST:
   - Run `marketplace_search` with what the work needs. Deploying a
     published agent costs NOTHING in inference and it is ready
     immediately — always the cheaper option.
   - If something fits, call `recommend_agents` with its slug and say in
     your own words why it fits. {owner_display} gets a one-click hire.
   - Only if nothing in the marketplace fits, hand **Atlas** a role spec
     — what the new hire must do, never the work itself. Atlas builds
     agents; it does not do research or prototypes. Building an agent is
     a full run against the owner's budget, so it is the fallback, not
     the default.
   - Present what you found to {owner_display} and let them choose.
```

- [ ] **Step 4: Update the `isol8` skill's marketplace section**

In `apps/infra/openclaw/skills/isol8/SKILL.md`, insert directly under the `## The Marketplace (hiring and publishing agents)` heading, before the existing "**Hiring (buyer side):**" paragraph:

```markdown
**Never recite listings from memory — the catalog changes.** Read it live with
the `marketplace_search` tool, exactly as you read `platform-info` for
integrations. When you find a fit, call `recommend_agents` with its slug: the
owner gets a one-click hire card. Name the agent in your reply too, so the
recommendation still reads on Telegram and email where no card renders.

**Cost:** deploying a published marketplace agent costs NOTHING in inference —
it is a file extract plus a config merge. Asking Atlas to build one costs a
full agent-building run against the owner's budget. On the free tier that
difference is most of a day's cap, so check the marketplace first.

**You cannot hire on your own.** Both tools are read-only; the roster only
changes when the owner clicks Deploy (chat) or approves the hire in Teams.
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/openclaw/skills/isol8/SKILL.md apps/backend/core/services/friday_identity.py apps/backend/tests/unit/services/test_friday_identity.py
git commit -m "feat(marketplace): teach Friday to check the marketplace before Atlas"
```

---

### Task 2: Compact search projection

Today's search returns ~50KB for 8 listings (dominated by `manifest_json`) — ~15k tokens Friday cannot afford under a $3/day cap.

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py:139-197` (`search_listings`)
- Test: `apps/backend/tests/unit/routers/test_marketplace_search_compact.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `GET /api/v1/marketplace/listings/search?view=compact` → `{"items": [{slug, name, summary, tags, price_cents}]}`. Task 3's plugin calls exactly this.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/routers/test_marketplace_search_compact.py
import pytest
from routers.marketplace_listings import _compact_items

_HEAVY = [
    {
        "slug": "pulse", "name": "Pulse", "price_cents": 0,
        "tags": ["marketing", "content"],
        "description_md": "# Pulse\n\nMarketing intel and content research.\n\nMore prose.",
        "manifest_json": {"file_count": 195, "safety_flags": [{"pattern": "eval"}]},
        "s3_prefix": "listings/abc/v9/", "manifest_sha256": "deadbeef",
        "seller_id": "user_x", "status": "published",
    }
]

def test_compact_drops_heavy_fields():
    [item] = _compact_items(_HEAVY)
    assert set(item) == {"slug", "name", "summary", "tags", "price_cents"}

def test_compact_summary_is_first_prose_line_not_heading():
    [item] = _compact_items(_HEAVY)
    assert item["summary"] == "Marketing intel and content research."

def test_compact_is_small():
    import json
    assert len(json.dumps(_compact_items(_HEAVY * 8))) < 2048

def test_compact_tolerates_missing_fields():
    [item] = _compact_items([{"slug": "x"}])
    assert item == {"slug": "x", "name": "", "summary": "", "tags": [], "price_cents": 0}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_search_compact.py -v`
Expected: FAIL with `ImportError: cannot import name '_compact_items'`.

- [ ] **Step 3: Implement `_compact_items` and wire the query param**

Add above `search_listings` in `apps/backend/routers/marketplace_listings.py`:

```python
_COMPACT_SUMMARY_MAX = 200


def _summary_of(listing: dict) -> str:
    """First prose line of description_md — skips markdown headings and blanks.

    Agent-facing: one line is enough to judge fit, and the full body is a
    listing-detail fetch away.
    """
    for line in (listing.get("description_md") or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_COMPACT_SUMMARY_MAX]
    return ""


def _compact_items(items: list[dict]) -> list[dict]:
    """Agent-shaped projection: enough to judge fit, nothing else.

    Deliberately excludes manifest_json, s3_prefix, manifest_sha256,
    safety_flags and seller fields — ~50KB of the current 8-listing
    response is manifest_json alone.
    """
    return [
        {
            "slug": item.get("slug", ""),
            "name": item.get("name", ""),
            "summary": _summary_of(item),
            "tags": item.get("tags") or [],
            "price_cents": item.get("price_cents", 0) or 0,
        }
        for item in items
    ]
```

Add the query parameter to `search_listings`'s signature (after `category`):

```python
    view: str | None = Query(
        default=None,
        pattern="^compact$",
        description="compact = agent-shaped projection (slug, name, summary, tags, price_cents).",
    ),
```

And replace the final `return {"items": items}` with:

```python
    if view == "compact":
        return {"items": _compact_items(items)}
    return {"items": items}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_search_compact.py -v`
Expected: 4 passed.

- [ ] **Step 5: Verify the default shape is unchanged**

Run: `cd apps/backend && uv run pytest tests/unit/routers/ -k marketplace -q`
Expected: all pass — no existing marketplace router test regresses.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/routers/test_marketplace_search_compact.py
git commit -m "feat(marketplace): agent-shaped compact search projection"
```

---

### Task 3: The `isol8-marketplace` plugin

**Files:**
- Create: `apps/infra/openclaw/plugins/isol8-marketplace/package.json`
- Create: `apps/infra/openclaw/plugins/isol8-marketplace/tsconfig.json`
- Create: `apps/infra/openclaw/plugins/isol8-marketplace/src/index.ts`
- Create: `apps/infra/openclaw/plugins/isol8-marketplace/src/index.test.ts`
- Create: `apps/infra/openclaw/plugins/isol8-marketplace/vitest.config.ts`

**Interfaces:**
- Consumes: `GET {ISOL8_API_URL}/api/v1/marketplace/listings/search?view=compact` (Task 2).
- Produces: tool names `marketplace_search` and `recommend_agents`. `recommend_agents` returns `{kind: "isol8.hire_suggestion", reason: string, listings: [{slug, name, summary, tags, price_cents}]}` — Task 5's card matches on `kind`.

- [ ] **Step 1: Create the package manifest**

```json
{
  "name": "@isol8/marketplace-plugin",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "./dist/index.js",
  "openclaw": { "extensions": ["./dist/index.js"] },
  "scripts": {
    "build": "tsc",
    "test": "vitest run"
  },
  "dependencies": { "typebox": "^0.34.0" },
  "devDependencies": { "typescript": "^5.6.0", "vitest": "^2.1.0" }
}
```

- [ ] **Step 2: Create `tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "skipLibCheck": true,
    "declaration": true
  },
  "include": ["src/**/*.ts"],
  "exclude": ["src/**/*.test.ts"]
}
```

- [ ] **Step 3: Create `vitest.config.ts`**

```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { include: ["src/**/*.test.ts"] },
});
```

- [ ] **Step 4: Write the failing test**

```typescript
// src/index.test.ts
import { describe, expect, it } from "vitest";
import plugin from "./index.js";

describe("isol8-marketplace plugin", () => {
  it("exposes exactly the two read-only tools", () => {
    const names = plugin.tools.map((t: { name: string }) => t.name).sort();
    expect(names).toEqual(["marketplace_search", "recommend_agents"]);
  });

  it("declares no tool that mutates state", () => {
    const descriptions = plugin.tools
      .map((t: { description: string }) => t.description.toLowerCase())
      .join(" ");
    expect(descriptions).not.toContain("deploy");
    expect(descriptions).not.toContain("install");
  });
});
```

- [ ] **Step 5: Run it to verify it fails**

Run: `cd apps/infra/openclaw/plugins/isol8-marketplace && npm install && npm test`
Expected: FAIL — `./index.js` does not exist.

- [ ] **Step 6: Implement the plugin**

```typescript
// src/index.ts
import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

const LISTING = Type.Object(
  {
    slug: Type.String(),
    name: Type.String(),
    summary: Type.String(),
    tags: Type.Array(Type.String()),
    price_cents: Type.Number(),
  },
  { additionalProperties: false },
);

/** Backend base URL, owner id, and the container's own hook credential.
 *
 *  A BAKED plugin is byte-identical in every container, so it cannot use the
 *  stage-time placeholder trick Atlas relies on ({{ISOL8_BRIDGE_URL}} in its
 *  per-owner seed files). It must read owner identity from the environment —
 *  hence ISOL8_OWNER_ID, added alongside ISOL8_API_URL in Task 7.
 *  ISOL8_OWNER_ID is an identifier, not a credential; OPENCLAW_HOOK_TOKEN is
 *  what actually authenticates the call. */
function env(): { base: string; ownerId: string; token: string } {
  const base = (process.env.ISOL8_API_URL ?? "").replace(/\/$/, "");
  const ownerId = process.env.ISOL8_OWNER_ID ?? "";
  const token = process.env.OPENCLAW_HOOK_TOKEN ?? "";
  if (!base) throw new Error("ISOL8_API_URL is not set");
  if (!ownerId) throw new Error("ISOL8_OWNER_ID is not set");
  if (!token) throw new Error("OPENCLAW_HOOK_TOKEN is not set");
  return { base, ownerId, token };
}

async function getJson(path: string, signal?: AbortSignal): Promise<any> {
  const { base, token } = env();
  const res = await fetch(`${base}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  if (!res.ok) throw new Error(`Isol8 API ${res.status}: ${await res.text()}`);
  return res.json();
}

export default defineToolPlugin({
  id: "isol8-marketplace",
  name: "Isol8 Marketplace",
  description: "Discover hireable agents in the Isol8 marketplace and recommend them to the owner.",
  tools: (tool) => [
    tool({
      name: "marketplace_search",
      label: "Search the Isol8 marketplace",
      description:
        "Search published Isol8 marketplace agents the owner could hire. Read-only. " +
        "Use whenever work needs a capability nobody on the roster has.",
      parameters: Type.Object({
        query: Type.String({
          description: "What the work needs, e.g. 'content marketing' or 'bookkeeping'.",
        }),
      }),
      outputSchema: Type.Array(LISTING),
      async execute({ query }, _config, context) {
        context.signal?.throwIfAborted();
        const data = await getJson(
          `/api/v1/marketplace/listings/search?view=compact&q=${encodeURIComponent(query)}`,
          context.signal,
        );
        return data.items ?? [];
      },
    }),
    tool({
      name: "recommend_agents",
      label: "Recommend agents to hire",
      description:
        "Recommend one or more marketplace agents to the owner. Renders a one-click hire card in chat. " +
        "Read-only: you cannot hire on your own — the owner decides. " +
        "Pass issue_id when working a Paperclip task so the recommendation reaches them in Teams.",
      parameters: Type.Object({
        slugs: Type.Array(Type.String(), { description: "Listing slugs from marketplace_search." }),
        reason: Type.String({ description: "One line on why these fit the work." }),
        issue_id: Type.Optional(
          Type.String({
            description:
              "The Paperclip issue you are working ($PAPERCLIP_TASK_ID). Omit in webchat.",
          }),
        ),
      }),
      outputSchema: Type.Object(
        {
          kind: Type.Literal("isol8.hire_suggestion"),
          reason: Type.String(),
          listings: Type.Array(LISTING),
        },
        { additionalProperties: false },
      ),
      async execute({ slugs, reason, issue_id }, _config, context) {
        context.signal?.throwIfAborted();
        const { ownerId } = env();
        const qs = new URLSearchParams({ slugs: slugs.join(",") });
        if (issue_id) qs.set("issue_id", issue_id);
        const data = await getJson(
          `/api/v1/marketplace/u/${encodeURIComponent(ownerId)}/recommend?${qs}`,
          context.signal,
        );
        return {
          kind: "isol8.hire_suggestion" as const,
          reason,
          listings: data.listings ?? [],
        };
      },
    }),
  ],
});
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd apps/infra/openclaw/plugins/isol8-marketplace && npm run build && npm test`
Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add apps/infra/openclaw/plugins/isol8-marketplace
git commit -m "feat(marketplace): read-only isol8-marketplace OpenClaw tool plugin"
```

---

### Task 4: `/marketplace/recommend` resolver endpoint

Resolves slugs server-side so Friday cannot render a card for a listing that does not exist, and creates the Paperclip `hire_agent` approval when `issue_id` is present.

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py` (add `GET /recommend`)
- Test: `apps/backend/tests/unit/routers/test_marketplace_recommend.py`

**Interfaces:**
- Consumes: `_compact_items` (Task 2); `marketplace_search.search`; `marketplace_hire.create_hire_approval` (Task 5).
- Produces: `GET /api/v1/marketplace/u/{owner_id}/recommend?slugs=a,b[&issue_id=…]` → `{"listings": [...], "approval_id": str | None}`. Owner in the PATH, matching the `hooks_proxy` / `agent_builder` convention for hook-token routes.

**Ordering note:** Step 5 imports `marketplace_hire.create_hire_approval`, which Task 5 creates. Implement Task 5 first, or land Step 5 of this task after it — Steps 1-4 stand alone and can be done in either order.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/routers/test_marketplace_recommend.py
import pytest
from routers.marketplace_listings import _resolve_recommended

_PUBLISHED = [
    {"slug": "pulse", "name": "Pulse", "status": "published", "price_cents": 0,
     "tags": ["content"], "description_md": "Marketing intel."},
    {"slug": "ora", "name": "Ora", "status": "published", "price_cents": 0,
     "tags": ["calendar"], "description_md": "Scheduling."},
]

def test_resolves_known_slugs_in_requested_order():
    out = _resolve_recommended(["ora", "pulse"], _PUBLISHED)
    assert [i["slug"] for i in out] == ["ora", "pulse"]

def test_drops_unknown_slugs_rather_than_failing():
    """A hallucinated slug must not render a card and must not 500."""
    out = _resolve_recommended(["pulse", "not-a-real-agent"], _PUBLISHED)
    assert [i["slug"] for i in out] == ["pulse"]

def test_returns_compact_shape():
    [item] = _resolve_recommended(["pulse"], _PUBLISHED)
    assert set(item) == {"slug", "name", "summary", "tags", "price_cents"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_recommend.py -v`
Expected: FAIL with `ImportError: cannot import name '_resolve_recommended'`.

- [ ] **Step 3: Implement the resolver**

Add to `apps/backend/routers/marketplace_listings.py`:

```python
def _resolve_recommended(slugs: list[str], published: list[dict]) -> list[dict]:
    """Compact rows for `slugs`, in the order asked, unknown slugs dropped.

    Dropping rather than erroring is deliberate: a model naming a listing
    that does not exist should produce no card, never a failed tool call
    that costs another turn to recover from.
    """
    by_slug = {row.get("slug"): row for row in published if row.get("status") == "published"}
    return _compact_items([by_slug[s] for s in slugs if s in by_slug])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_recommend.py -v`
Expected: 3 passed.

- [ ] **Step 5: Add the authenticated route**

Reuse the hook-token guard from `routers/agent_builder.py:72` rather than
re-implementing the timing-safe compare. Add these imports at the top of
`marketplace_listings.py`:

```python
from fastapi import Request
from routers.agent_builder import _require_hook_token
from core.services import marketplace_hire
```

Then add the route:

```python
@router.get("/u/{owner_id}/recommend")
async def recommend(
    owner_id: str,
    request: Request,
    slugs: str = Query(description="Comma-separated listing slugs."),
    issue_id: str | None = Query(default=None, description="Paperclip issue id, headless runs only."),
):
    """Resolve slugs for a hire recommendation. Container-authenticated.

    Read-only with respect to the roster: when ``issue_id`` is present this
    creates a PENDING Paperclip approval, which changes nothing until a
    human approves it in Teams.
    """
    await _require_hook_token(owner_id, request)
    wanted = [s.strip() for s in slugs.split(",") if s.strip()]
    published = await marketplace_search.search(query=None, limit=100)
    listings = _resolve_recommended(wanted, published)

    approval_id = None
    if issue_id and listings:
        approval_id = await marketplace_hire.create_hire_approval(
            owner_id=owner_id, issue_id=issue_id, listing=listings[0]
        )
    return {"listings": listings, "approval_id": approval_id}
```

- [ ] **Step 6: Run the router tests**

Run: `cd apps/backend && uv run pytest tests/unit/routers/ -k marketplace -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/routers/test_marketplace_recommend.py
git commit -m "feat(marketplace): container-authenticated recommend resolver"
```

---

### Task 5: Paperclip hire approval + post-approve artifact deploy

**Files:**
- Create: `apps/backend/core/services/marketplace_hire.py`
- Modify: `apps/backend/routers/teams/approvals.py` (`approve`)
- Test: `apps/backend/tests/unit/services/test_marketplace_hire.py`

**Interfaces:**
- Consumes: `_compact_items` output shape (Task 2).
- Produces: `marketplace_hire.create_hire_approval(owner_id, issue_id, listing) -> str` and `marketplace_hire.deploy_for_approval(approval, ctx) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_marketplace_hire.py
import pytest
from core.services.marketplace_hire import build_hire_approval_payload

_LISTING = {"slug": "pulse", "name": "Pulse", "summary": "Marketing intel.",
            "tags": ["content"], "price_cents": 0}

def test_payload_pins_openclaw_gateway_adapter():
    """Paperclip defaults adapterType to 'process'; a process agent is
    unreachable on Isol8 and fails silently at first wake."""
    payload = build_hire_approval_payload(listing=_LISTING, env="prod")
    assert payload["adapterType"] == "openclaw_gateway"
    assert payload["adapterConfig"]["url"] == "wss://ws-prod.isol8.co"

def test_payload_omits_agent_id():
    """Nothing may exist in the roster before the human approves."""
    assert "agentId" not in build_hire_approval_payload(listing=_LISTING, env="prod")

def test_payload_carries_listing_slug_for_the_deploy_step():
    payload = build_hire_approval_payload(listing=_LISTING, env="prod")
    assert payload["metadata"]["isol8_listing_slug"] == "pulse"

def test_payload_names_the_agent_from_the_listing():
    assert build_hire_approval_payload(listing=_LISTING, env="prod")["name"] == "Pulse"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_hire.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the payload builder**

```python
# apps/backend/core/services/marketplace_hire.py
"""Marketplace hires that route through Paperclip's approval surface.

A headless Friday cannot hire. She can only ask. This module turns that ask
into a Paperclip ``hire_agent`` approval, which lands in the Teams Inbox
"Needs Attention" band; the roster changes only when the owner approves.
"""

from __future__ import annotations


def _gateway_url(env: str) -> str:
    return f"wss://ws-{env}.isol8.co"


def build_hire_approval_payload(*, listing: dict, env: str) -> dict:
    """Payload for a Paperclip ``hire_agent`` approval.

    ``adapterType`` is set EXPLICITLY. Paperclip's approve path does
    ``String(payload.adapterType ?? "process")`` — an agent created as
    "process" is unreachable over the openclaw gateway and fails silently
    at first wake, long after the approval looked successful.

    ``agentId`` is deliberately absent so Paperclip CREATES the agent on
    approve rather than activating a pre-created one: nothing should exist
    in the roster before the human says yes.
    """
    return {
        "name": listing["name"],
        "role": "general",
        "title": listing.get("summary", "")[:120],
        "capabilities": ", ".join(listing.get("tags") or []),
        "adapterType": "openclaw_gateway",
        "adapterConfig": {"url": _gateway_url(env)},
        "budgetMonthlyCents": 0,
        "metadata": {"isol8_listing_slug": listing["slug"]},
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_hire.py -v`
Expected: 4 passed.

- [ ] **Step 4b: Add `create_approval` to the Paperclip admin client**

`core/services/paperclip_admin_client.py` has list/approve/reject/get but no
create. Add it next to `list_approvals` (~line 1084), mapping to the upstream
route verified at `server/src/routes/approvals.ts:86`:

```python
    async def create_approval(
        self,
        *,
        session_cookie: str,
        company_id: str,
        body: dict,
    ) -> dict:
        """Create a pending approval.

        Maps to ``POST /api/companies/{companyId}/approvals``. ``body``
        carries ``type``, ``payload``, and optionally ``issueIds`` — upstream
        links those to the approval via ``issueApprovalsSvc.linkManyForApproval``,
        which is what makes a hire request show up ON the issue that
        prompted it. For ``type: "hire_agent"`` upstream additionally runs
        ``normalizeHireApprovalPayloadForPersistence`` over the payload.
        Returns 201 with the created (payload-redacted) approval.
        """
        return await self._post(
            f"/api/companies/{company_id}/approvals",
            session_cookie=session_cookie,
            json=body,
        )
```

- [ ] **Step 4c: Implement `create_hire_approval` and `deploy_for_approval`**

Append to `core/services/marketplace_hire.py`:

```python
import logging

from core.config import settings

logger = logging.getLogger(__name__)

_LISTING_SLUG_KEY = "isol8_listing_slug"


async def create_hire_approval(*, owner_id: str, issue_id: str, listing: dict) -> str | None:
    """Create a PENDING Paperclip ``hire_agent`` approval linked to ``issue_id``.

    Creating it changes nothing: the roster moves only when a human approves
    in the Teams Inbox. ``issueIds`` is what makes the request visible ON the
    issue that prompted it rather than only in a global list.

    Returns the approval id, or None if the owner has no Paperclip company
    (a degraded state a recommendation must not raise on).
    """
    from core.services import paperclip_admin_session
    from core.services.paperclip_provisioning import get_company_for_owner

    company = await get_company_for_owner(owner_id)
    if not company:
        logger.info("create_hire_approval: no paperclip company for owner=%s", owner_id)
        return None

    admin, cookie = await paperclip_admin_session.session_for(owner_id)
    created = await admin.create_approval(
        session_cookie=cookie,
        company_id=company["company_id"],
        body={
            "type": "hire_agent",
            "payload": build_hire_approval_payload(
                listing=listing, env=settings.ENVIRONMENT
            ),
            "issueIds": [issue_id],
        },
    )
    return created.get("id")


async def deploy_for_approval(approval: dict, ctx) -> None:
    """Deploy the marketplace artifact for a just-approved hire.

    Paperclip created the ROSTER entry; the container artifact is ours. Runs
    synchronously on the approving user's own authenticated request — no
    event plumbing, no polling, no privileged write path.

    A no-op for every approval that is not a marketplace hire, which is how
    this stays invisible to the existing approve flow.

    On deploy failure the Paperclip agent is rolled back, so the owner never
    sees a roster entry with no workspace behind it.
    """
    payload = (approval or {}).get("payload") or {}
    slug = (payload.get("metadata") or {}).get(_LISTING_SLUG_KEY)
    agent_id = approval.get("agentId") or payload.get("agentId")
    if not slug or not agent_id:
        return

    from core.services import marketplace_service
    from core.services.catalog_service import get_catalog_service
    from core.services.paperclip_provisioning import delete_paperclip_for_marketplace

    listing = await marketplace_service.get_published_by_slug(slug=slug)
    if not listing:
        logger.warning("deploy_for_approval: listing %s is no longer published", slug)
        raise HTTPException(status_code=409, detail=f"listing {slug} is no longer available")

    try:
        await get_catalog_service().deploy_from_artifact(
            owner_id=ctx.owner_id,
            slug=slug,
            manifest=listing["manifest_json"],
            slice_=await marketplace_service.fetch_slice(listing),
            tar_bytes=await marketplace_service.fetch_tarball(listing),
            new_agent_id=agent_id,
            container_ip=ctx.container_ip,
            container_token=ctx.container_token,
        )
    except Exception:
        logger.exception("deploy_for_approval: deploy failed for %s — rolling back %s", slug, agent_id)
        try:
            await delete_paperclip_for_marketplace(owner_id=ctx.owner_id, agent_id=agent_id)
        except Exception:
            logger.exception("deploy_for_approval: rollback failed for agent %s", agent_id)
        raise
```

**Before writing this, read the real signatures** — `paperclip_admin_session`,
`get_company_for_owner`, `marketplace_service.get_published_by_slug`, and the
S3 fetch helpers used by `deploy_listing` — and match them exactly. The shapes
above follow `marketplace_listings.deploy_listing` and
`agent_builder.register_built_agent`; reuse their artifact-fetch code rather
than duplicating it, and extract the shared half into one helper both call.

- [ ] **Step 5: Hook the deploy into `approvals.py::approve`**

In `apps/backend/routers/teams/approvals.py`, after the upstream approve call returns, deploy the artifact when the approval carries `isol8_listing_slug`. Keep the existing proxy behaviour otherwise:

```python
    result = await _agents._admin().approve_approval(
        approval_id=approval_id,
        note=body.note,
        session_cookie=ctx.session_cookie,
    )
    # Paperclip created the ROSTER entry; the container artifact is ours.
    # Runs on the approving user's own authenticated request — no
    # privileged write path, no event plumbing.
    await marketplace_hire.deploy_for_approval(result, ctx)
    return result
```

- [ ] **Step 6: Run the Teams router tests**

Run: `cd apps/backend && uv run pytest tests/unit/routers/ -k teams -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/marketplace_hire.py apps/backend/routers/teams/approvals.py apps/backend/tests/unit/services/test_marketplace_hire.py
git commit -m "feat(marketplace): Paperclip hire approval + post-approve artifact deploy"
```

---

### Task 6: Hire card in chat

**Files:**
- Create: `apps/frontend/src/components/chat/HireCard.tsx`
- Create: `apps/frontend/src/components/chat/__tests__/HireCard.test.tsx`
- Modify: `apps/frontend/src/components/chat/MessageList.tsx` (~line 567, where tool pairs render)

**Interfaces:**
- Consumes: `tool_end.result` whose `details` matches `{kind: "isol8.hire_suggestion", reason, listings}` (Task 3).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/chat/__tests__/HireCard.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HireCard, parseHireSuggestion } from "../HireCard";

const SUGGESTION = {
  kind: "isol8.hire_suggestion",
  reason: "Nobody on the team does content.",
  listings: [
    { slug: "pulse", name: "Pulse", summary: "Marketing intel.", tags: ["content"], price_cents: 0 },
  ],
};

describe("parseHireSuggestion", () => {
  it("accepts a well-formed suggestion", () => {
    expect(parseHireSuggestion(SUGGESTION)?.listings).toHaveLength(1);
  });

  it("rejects anything else so ordinary tools fall through to the pill", () => {
    expect(parseHireSuggestion({ kind: "something.else" })).toBeNull();
    expect(parseHireSuggestion(null)).toBeNull();
    expect(parseHireSuggestion("a string")).toBeNull();
  });
});

describe("HireCard", () => {
  it("renders the listing and a deploy affordance", () => {
    render(<HireCard suggestion={SUGGESTION} />);
    expect(screen.getByText("Pulse")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /deploy/i })).toBeInTheDocument();
  });

  it("labels a zero-price listing as free", () => {
    render(<HireCard suggestion={SUGGESTION} />);
    expect(screen.getByText(/free/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/HireCard.test.tsx`
Expected: FAIL — cannot resolve `../HireCard`.

- [ ] **Step 3: Implement `HireCard.tsx`**

```tsx
"use client";

import { useState } from "react";
import { useApi } from "@/lib/api";
import { Button } from "@/components/ui/button";

export interface HireListing {
  slug: string;
  name: string;
  summary: string;
  tags: string[];
  price_cents: number;
}

export interface HireSuggestion {
  kind: "isol8.hire_suggestion";
  reason: string;
  listings: HireListing[];
}

/** Type guard over an untrusted tool result.
 *
 *  Returns null for ANYTHING that is not a well-formed hire suggestion, so
 *  every other tool falls through to the ordinary pill unchanged. The tool
 *  result crosses a process boundary, so nothing here may be assumed. */
export function parseHireSuggestion(value: unknown): HireSuggestion | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (v.kind !== "isol8.hire_suggestion") return null;
  if (!Array.isArray(v.listings)) return null;
  const listings = v.listings.filter(
    (l): l is HireListing =>
      !!l && typeof l === "object" && typeof (l as HireListing).slug === "string",
  );
  if (listings.length === 0) return null;
  return {
    kind: "isol8.hire_suggestion",
    reason: typeof v.reason === "string" ? v.reason : "",
    listings,
  };
}

function priceLabel(cents: number): string {
  return cents === 0 ? "Free" : `$${(cents / 100).toFixed(2)}`;
}

function ListingRow({ listing }: { listing: HireListing }) {
  const api = useApi();
  const [state, setState] = useState<"idle" | "deploying" | "done">("idle");
  const [error, setError] = useState<string | null>(null);

  async function deploy() {
    setState("deploying");
    setError(null);
    try {
      await api.post(`/marketplace/listings/${listing.slug}/deploy`, {});
      setState("done");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Deploy failed");
      setState("idle");
    }
  }

  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium">{listing.name}</div>
          <div className="text-sm text-muted-foreground">{listing.summary}</div>
          {listing.tags.length > 0 && (
            <div className="mt-1 text-xs text-muted-foreground">
              {listing.tags.join(" · ")}
            </div>
          )}
        </div>
        <span className="shrink-0 text-xs text-muted-foreground">
          {priceLabel(listing.price_cents)}
        </span>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" onClick={deploy} disabled={state !== "idle"}>
          {state === "deploying"
            ? "Deploying…"
            : state === "done"
              ? "Added to your team"
              : "Deploy to my team"}
        </Button>
        {error && <span className="text-xs text-destructive">{error}</span>}
      </div>
    </div>
  );
}

export function HireCard({ suggestion }: { suggestion: HireSuggestion }) {
  return (
    <div className="my-2 space-y-2">
      {suggestion.reason && (
        <div className="text-sm text-muted-foreground">{suggestion.reason}</div>
      )}
      {suggestion.listings.map((l) => (
        <ListingRow key={l.slug} listing={l} />
      ))}
    </div>
  );
}
```

Check `components/ui/button` exists and that `useApi` exposes `post` with this
signature before writing — match the real ones if they differ.

- [ ] **Step 4: Wire it into `MessageList.tsx`**

At the tool-pair render site (~line 567), try the hire suggestion **before**
building the `ToolPill`, and fall through untouched when it does not match:

```tsx
        const hire = parseHireSuggestion(
          (resultBlock?.content ?? resultBlock?.result) as unknown,
        );
        if (hire) {
          return <HireCard key={pair.key} suggestion={hire} />;
        }
```

Add the import at the top of the file:

```tsx
import { HireCard, parseHireSuggestion } from "./HireCard";
```

Use whatever the surrounding loop's existing React key expression is in place
of `pair.key` — read the block before editing.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/HireCard.test.tsx`
Expected: 4 passed.

- [ ] **Step 6: Typecheck**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: no errors. (CI typechecks test files too.)

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/chat/HireCard.tsx apps/frontend/src/components/chat/__tests__/HireCard.test.tsx apps/frontend/src/components/chat/MessageList.tsx
git commit -m "feat(marketplace): hire card in chat from recommend_agents tool result"
```

---

### Task 7: Bake and enable the plugin

Last task deliberately: nothing ships to a container until the pieces it calls exist.

**Files:**
- Modify: `apps/infra/openclaw/Dockerfile` (new layer after 12b, ~line 340)
- Modify: `apps/backend/core/containers/config.py:1578` (`plugins.entries`)
- Modify: `apps/backend/core/containers/ecs_manager.py:2132` (add `ISOL8_OWNER_ID`)
- Test: `apps/backend/tests/unit/containers/test_config_plugins.py`

**Interfaces:**
- Consumes: the built plugin from Task 3.
- Produces: `plugins.entries["isol8-marketplace"] = {"enabled": True}` and the `ISOL8_OWNER_ID` env var in every per-user task definition.

- [ ] **Step 0: Add `ISOL8_OWNER_ID` to the container environment**

The plugin is byte-identical in every container, so unlike Atlas's per-owner
seed files it cannot get its owner id from a stage-time placeholder. Add it
next to `ISOL8_API_URL` in `ecs_manager.py` (~line 2132):

```python
        # The baked marketplace plugin needs the owner id to call the
        # hook-token-authenticated recommend route. Unlike Atlas's per-owner
        # seed files (which substitute {{ISOL8_BRIDGE_URL}} at stage time), a
        # baked plugin is identical in every container and must read identity
        # from the environment. This is an identifier, NOT a credential —
        # OPENCLAW_HOOK_TOKEN is what authenticates the call.
        environment_for_task.append({"name": "ISOL8_OWNER_ID", "value": user_id})
```

Confirm `user_id` is the in-scope variable name at that point in the function
before writing (the surrounding code uses `user_id` for the owner).

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/containers/test_config_plugins.py
def test_marketplace_plugin_enabled_on_every_container():
    from core.containers.config import build_openclaw_config

    config = build_openclaw_config(user_id="user_test", provider_choice="bedrock_claude")
    assert config["plugins"]["entries"]["isol8-marketplace"] == {"enabled": True}

def test_marketplace_plugin_enabled_on_the_free_tier():
    """The free tier is exactly where a $0-inference hire matters most."""
    from core.containers.config import build_openclaw_config

    config = build_openclaw_config(user_id="user_test", provider_choice="bedrock_trial")
    assert config["plugins"]["entries"]["isol8-marketplace"] == {"enabled": True}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_config_plugins.py -v`
Expected: FAIL with `KeyError: 'isol8-marketplace'`. (Adjust `build_openclaw_config`'s call signature to match the real one if it differs — read it first.)

- [ ] **Step 3: Add the plugin entry**

In `apps/backend/core/containers/config.py`, inside `plugins.entries` (after the `"parallel"` entry, ~line 1599):

```python
                # First-party read-only marketplace tools (marketplace_search,
                # recommend_agents), baked at /app/dist/extensions/isol8-marketplace.
                # Enabled on EVERY provider path including the free tier — a
                # marketplace hire costs $0 inference, which is precisely the
                # tier where that matters. Its tools are non-optional, so the
                # existing tools.allow ["*"] surfaces them.
                "isol8-marketplace": {"enabled": True},
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_config_plugins.py -v`
Expected: 2 passed.

- [ ] **Step 5: Add the Dockerfile bake layer**

After Layer 12b, mirroring its root-owned bake (the loader rejects plugin dirs not owned by uid 0). This one builds from in-repo source rather than npm:

```dockerfile
# ---- Layer 12c: first-party Isol8 marketplace tool plugin ----
# Read-only agent tools (marketplace_search, recommend_agents) so Friday can
# name real listings instead of waving at a URL. Built from in-repo source
# rather than npm — it is first-party and versioned with this repo. Enabled
# via plugins.entries["isol8-marketplace"] in core/containers/config.py.
# Root-owned like every other baked plugin.
COPY openclaw/plugins/isol8-marketplace /opt/stage-isol8-marketplace
RUN set -eux; \
    cd /opt/stage-isol8-marketplace; \
    npm install --no-audit --no-fund; \
    npm run build; \
    mkdir -p /app/dist/extensions/isol8-marketplace; \
    cp -R /opt/stage-isol8-marketplace/dist /app/dist/extensions/isol8-marketplace/; \
    cp /opt/stage-isol8-marketplace/package.json /app/dist/extensions/isol8-marketplace/; \
    mkdir -p /app/dist/extensions/isol8-marketplace/node_modules; \
    cp -R /opt/stage-isol8-marketplace/node_modules/. /app/dist/extensions/isol8-marketplace/node_modules/; \
    rm -rf /opt/stage-isol8-marketplace; \
    chown -R root:root /app/dist/extensions/isol8-marketplace
```

- [ ] **Step 6: Verify the Dockerfile parses**

Run: `cd apps/infra/openclaw && docker build --check . 2>&1 | tail -20`
Expected: no syntax errors. (A full build needs GHCR auth for the private fork base; syntax check is the gate here.)

- [ ] **Step 7: Commit**

```bash
git add apps/infra/openclaw/Dockerfile apps/backend/core/containers/config.py apps/backend/tests/unit/containers/test_config_plugins.py
git commit -m "feat(marketplace): bake and enable the isol8-marketplace plugin"
```

---

## Final verification

- [ ] Backend suite: `cd apps/backend && uv run pytest tests/ -q` — full suite, matching CI scope (`tests/`, not `tests/unit/`).
- [ ] Frontend: `cd apps/frontend && pnpm test` and `pnpm exec tsc --noEmit`.
- [ ] Plugin: `cd apps/infra/openclaw/plugins/isol8-marketplace && npm test`.
- [ ] Confirm no new `require_org_admin`-free write path was introduced: `git diff origin/main --stat` should show no new POST/PATCH/DELETE route outside `routers/teams/approvals.py`.

## Deployment notes (not code)

The plugin and the SKILL.md copy only reach containers on an image bake plus a
`dev.tag` → validate → `prod.tag` promotion. `friday_identity.py`, the compact
projection, the recommend endpoint, and the approvals hook ship on a normal
backend deploy and are inert until the plugin lands. Before the prod flip,
verify on a dev **trial-tier** container (`is_bedrock_trial`, where `toolSearch`
is deliberately off) that the plugin loads and both tools appear in Friday's
toolset.
