# Isol8 Platform Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every agent on Isol8 uniform, correct platform knowledge via a baked `isol8` skill, a refreshable per-owner facts file, and a reconciled Paperclip roster (titles/capabilities/reports-to for the seed team).

**Architecture:** A bundled skill in the extended OpenClaw image (`/app/skills/isol8`, same mechanism as the `paperclip` skill) carries all platform-invariant knowledge with progressive disclosure. A backend-written `isol8-facts.json` at the EFS user root carries per-owner dynamic state (plan/provider). `provision_org` registers Friday/Atlas with real titles, capabilities, and org placement so the roster — Friday's delegation source of truth — stops calling Atlas a generalist. A backfill script patches existing companies.

**Tech Stack:** Python (FastAPI backend, pytest), Dockerfile (extended image), Markdown (skill).

**Spec:** `docs/superpowers/specs/2026-07-15-isol8-platform-skill-design.md`

## Global Constraints

- Never modify production directly; everything ships via PR + CI (CLAUDE.md).
- No `sleep`/`tail` in bash; use `gh run watch` / Read tool.
- Work happens on a worktree branch, never main. Rebase on a fresh main before PR.
- No PR/bug references in code comments.
- Backend tests: `cd apps/backend && uv run pytest tests/ -v` (targeted per task, full suite at the end).
- Fleet rollout stays behind `openclaw-version.json` tag promotion (flip dev, validate, then prod). The image build workflow already path-triggers on `apps/infra/openclaw/skills/**` — no workflow change needed.
- Verified fork facts used throughout (do not re-derive from the vanilla `paperclip/` clone): `openclaw_gateway` adapter has `supportsInstructionsBundle: false` (fork `server/src/adapters/registry.ts:388-393`), so Paperclip's role-based instruction bundles are never delivered to Isol8 agents; roster list returns full agent rows including `title`/`capabilities` (fork `routes/agents.ts:1770-1787`); `chainOfCommand` walks `reportsTo` (fork `services/agents.ts:774`); `role === "ceo"` gates `canCreateAgents` (fork `services/agent-permissions.ts:7`) and company ops.

---

### Task 1: `owner_facts` service

**Files:**
- Create: `apps/backend/core/services/owner_facts.py`
- Test: `apps/backend/tests/unit/services/test_owner_facts.py`

**Interfaces:**
- Produces: `format_provider_label(provider_choice, byo_provider) -> str` (moved verbatim from `friday_identity._format_provider_label`), `build_owner_facts(owner_id, billing_account) -> dict` (pure), `async write_owner_facts(owner_id) -> None` (best-effort, never raises), `FACTS_FILENAME = "isol8-facts.json"`.
- Consumes: `core.repositories.billing_repo.get_by_owner_id`, `core.containers.workspace.get_workspace().write_file(user_id, path, content)`, `core.auth.is_org_owner_id`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for core.services.owner_facts."""

import json

import pytest

from core.services import owner_facts


def test_build_owner_facts_trial_local():
    facts = owner_facts.build_owner_facts(
        "user_abc",
        {"subscription_status": "trial_local", "provider_choice": "bedrock_claude"},
    )
    assert facts["owner_kind"] == "personal"
    assert facts["subscription_status"] == "trial_local"
    assert "free" in facts["plan"].lower()
    assert "generated_at" in facts


def test_build_owner_facts_paid_byo_org():
    facts = owner_facts.build_owner_facts(
        "org_123",
        {
            "subscription_status": "active",
            "provider_choice": "byo_key",
            "byo_provider": "anthropic",
        },
    )
    assert facts["owner_kind"] == "org"
    assert facts["plan"].startswith("Starter")
    assert facts["provider"] == "BYO Anthropic key"


def test_build_owner_facts_no_account():
    facts = owner_facts.build_owner_facts("user_abc", None)
    assert facts["subscription_status"] is None
    assert "no billing" in facts["plan"].lower()


@pytest.mark.asyncio
async def test_write_owner_facts_writes_json(monkeypatch):
    written = {}

    class FakeWs:
        def write_file(self, user_id, path, content):
            written["user_id"] = user_id
            written["path"] = path
            written["content"] = content

    async def fake_get(owner_id):
        return {"subscription_status": "active", "provider_choice": "bedrock_claude"}

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_get)
    monkeypatch.setattr(owner_facts, "get_workspace", lambda: FakeWs())

    await owner_facts.write_owner_facts("user_abc")

    assert written["path"] == "isol8-facts.json"
    facts = json.loads(written["content"])
    assert facts["provider_choice"] == "bedrock_claude"


@pytest.mark.asyncio
async def test_write_owner_facts_never_raises(monkeypatch):
    async def boom(owner_id):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", boom)
    await owner_facts.write_owner_facts("user_abc")  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_owner_facts.py -v`
Expected: FAIL / collection error — `core.services.owner_facts` does not exist.

- [ ] **Step 3: Implement `owner_facts.py`**

```python
"""Owner-facts file: per-owner dynamic platform state on EFS.

``isol8-facts.json`` sits at the EFS user root (in-container:
``~/.openclaw/isol8-facts.json``) so every agent — Friday, Atlas,
scaffolded specialists, marketplace hires — can read the owner's
CURRENT plan/provider state instead of trusting seed files rendered
once at provision time. The backend is the sole writer; writes happen
at provision (``ecs_manager.write_user_configs``) and on provider
switches (``billing_service.change_provider``). Best-effort by design:
a facts write must never block provisioning or a provider switch.

The bundled ``isol8`` platform skill tells agents to read this file
for plan/provider questions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.containers.workspace import get_workspace
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

FACTS_FILENAME = "isol8-facts.json"


def format_provider_label(provider_choice: str | None, byo_provider: str | None) -> str:
    """Map (provider_choice, byo_provider) to a human-readable label."""
    if provider_choice == "chatgpt_oauth":
        return "ChatGPT (their own OpenAI account)"
    if provider_choice == "bedrock_claude":
        return "Bedrock (Claude, Isol8-managed credits)"
    if provider_choice == "bedrock_trial":
        return "Bedrock (Claude Sonnet, free trial — $3/day)"
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return "BYO Anthropic key"
        if byo_provider == "openai":
            return "BYO OpenAI key"
        return "BYO API key (provider unspecified)"
    return f"unknown ({provider_choice})"


def build_owner_facts(owner_id: str, billing_account: dict | None) -> dict:
    """Render the facts dict from a billing row. Pure — no I/O."""
    from core.auth import is_org_owner_id

    account = billing_account or {}
    status = account.get("subscription_status")
    if status == "trial_local":
        plan = (
            "Free tier (no card): Bedrock Claude Sonnet only, $3/day cap, "
            "container sleeps when idle"
        )
    elif status:
        plan = "Starter ($50/mo flat fee)"
    else:
        plan = "No billing account yet"
    return {
        "plan": plan,
        "subscription_status": status,
        "provider": format_provider_label(
            account.get("provider_choice"), account.get("byo_provider")
        ),
        "provider_choice": account.get("provider_choice"),
        "owner_kind": "org" if is_org_owner_id(owner_id) else "personal",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def write_owner_facts(owner_id: str) -> None:
    """Render + write the facts file. Best-effort: never raises."""
    try:
        account = await billing_repo.get_by_owner_id(owner_id)
        facts = build_owner_facts(owner_id, account)
        get_workspace().write_file(
            owner_id, FACTS_FILENAME, json.dumps(facts, indent=2) + "\n"
        )
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("write_owner_facts: skipped for %s: %s", owner_id, e)
```

Note: verify `billing_repo` import style matches `billing_service.py` (module import vs function import) and adjust; if `billing_repo` is imported as functions there, expose the module handle the tests monkeypatch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_owner_facts.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/owner_facts.py apps/backend/tests/unit/services/test_owner_facts.py
git commit -m "feat(platform-context): owner-facts file service"
```

---

### Task 2: wire facts writes into provision + provider switch

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py` (end of `write_user_configs`, ~line 2510s, after the Friday-seed/paperclip-key staging block)
- Modify: `apps/backend/core/services/billing_service.py` (`change_provider`, immediately after the "Commit the new provider state" billing-row write, BEFORE the hot/cold swap split so both paths are covered)
- Test: extend `apps/backend/tests/unit/services/test_billing_service.py` and `apps/backend/tests/unit/services/test_container_provision.py`

**Interfaces:**
- Consumes: `core.services.owner_facts.write_owner_facts(owner_id)` from Task 1.

- [ ] **Step 1: Write the failing tests**

In `test_billing_service.py`, find the existing `change_provider` success-path test (a switch that returns `{"status": "ok"}`) and add alongside it:

```python
@pytest.mark.asyncio
async def test_change_provider_refreshes_owner_facts(monkeypatch):
    # Arrange exactly like the nearest existing change_provider success test
    # (same monkeypatched billing_repo/oauth/ecs fixtures), plus:
    calls = []

    async def fake_write_owner_facts(owner_id):
        calls.append(owner_id)

    monkeypatch.setattr(
        "core.services.owner_facts.write_owner_facts", fake_write_owner_facts
    )
    # ... invoke change_provider with the same args the sibling test uses ...
    assert calls == ["<owner id used by the sibling test>"]
```

In `test_container_provision.py` (or the test file that exercises `write_user_configs` — follow where `friday_identity.pre_stage_friday_files` is asserted), add the symmetric assertion that `write_owner_facts` is awaited with the user id.

Copy the arrange/act sections from the sibling tests verbatim — the delta is only the monkeypatch + assertion.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py -k owner_facts tests/unit/services/test_container_provision.py -k owner_facts -v`
Expected: FAIL — `write_owner_facts` never called.

- [ ] **Step 3: Implement the two call sites**

In `ecs_manager.write_user_configs`, at the end (same best-effort posture as the Friday seeding block that precedes it):

```python
# Refresh the owner-facts file (plan/provider snapshot read by agents via
# the isol8 skill). write_owner_facts is internally best-effort.
from core.services.owner_facts import write_owner_facts

await write_owner_facts(user_id)
```

In `billing_service.change_provider`, right after the billing-row provider commit:

```python
# Keep the agents' view of plan/provider current — the facts file is
# what the isol8 skill points them at. Best-effort inside.
from core.services.owner_facts import write_owner_facts

await write_owner_facts(owner_id)
```

- [ ] **Step 4: Run the touched test files in full**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py tests/unit/services/test_container_provision.py tests/unit/services/test_owner_facts.py -v`
Expected: all PASS (no regressions in sibling tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/core/services/billing_service.py apps/backend/tests/unit/services/test_billing_service.py apps/backend/tests/unit/services/test_container_provision.py
git commit -m "feat(platform-context): write owner facts at provision + provider switch"
```

---

### Task 3: roster enrichment in `provision_org`

**Files:**
- Modify: `apps/backend/core/services/paperclip_provisioning.py` (Friday mint ~line 385, Atlas mint ~line 414)
- Test: extend `apps/backend/tests/unit/services/test_paperclip_provisioning.py::test_provision_org_seeds_friday_and_atlas`

**Interfaces:**
- Consumes: `PaperclipAdminClient.create_agent(..., title=..., capabilities=..., reports_to=...)` (already supported — `paperclip_admin_client.py:596-641`; `reports_to` maps to body key `reportsTo`).
- Produces: module constants `FRIDAY_TITLE`, `FRIDAY_CAPABILITIES`, `ATLAS_TITLE`, `ATLAS_CAPABILITIES` (imported by the Task 6 backfill script).

- [ ] **Step 1: Write the failing test assertions**

Extend `test_provision_org_seeds_friday_and_atlas` (it already captures `create_agent` calls via a mock): assert the Friday call includes `title="CEO"` and a non-empty `capabilities`; assert the Atlas call includes `title="Agent Recruiter"`, a `capabilities` string containing `"not"` (the does-NOT clause), and `reports_to=<the id returned by the mocked Friday create>`.

Add one new test: when the Friday mint raises (`PaperclipApiError`), the Atlas mint is still attempted with `reports_to=None` (mirror the arrange of `test_provision_org_atlas_failure_does_not_block_friday_seed`, inverted).

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py -k "seeds_friday_and_atlas or friday_failure" -v`
Expected: FAIL on missing kwargs.

- [ ] **Step 3: Implement**

Module-level constants (near the existing seed constants):

```python
# Roster metadata for the seed team. The roster (GET /companies/{id}/agents)
# is Friday's source of truth for delegation — these strings are what stops
# her from assigning Atlas domain work. Also used by
# scripts/backfill_seed_team_roster.py for pre-existing companies.
FRIDAY_TITLE = "CEO"
FRIDAY_CAPABILITIES = (
    "Orchestrator and single point of contact for the owner. Decomposes "
    "goals, delegates to specialist agents, triages what comes back, and "
    "reports up. Does not execute specialist work itself; when no "
    "specialist fits, it hires one via Atlas or recommends the marketplace."
)
ATLAS_TITLE = "Agent Recruiter"
ATLAS_CAPABILITIES = (
    "Builds new specialist agents on request: researches the role, "
    "architects the agent, generates its workspace, and registers it into "
    "the company roster. Assign Atlas a role spec (what the new hire must "
    "do) — not domain work. Atlas does not do research tasks, prototypes, "
    "or general work itself, and never publishes to the marketplace (the "
    "owner publishes via the seller wizard)."
)
```

Friday mint: add `title=FRIDAY_TITLE, capabilities=FRIDAY_CAPABILITIES`; capture `friday_agent_id: str | None` from the response (already read as `friday["id"]`). Atlas mint: add `title=ATLAS_TITLE, capabilities=ATLAS_CAPABILITIES, reports_to=friday_agent_id` (pass `None` when the Friday mint failed — `create_agent` omits the key on `None`).

- [ ] **Step 4: Run the full provisioning test file**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/paperclip_provisioning.py apps/backend/tests/unit/services/test_paperclip_provisioning.py
git commit -m "feat(platform-context): seed Friday/Atlas with titles, capabilities, org placement"
```

---

### Task 4: the bundled `isol8` skill + Dockerfile layer

**Files:**
- Create: `apps/infra/openclaw/skills/isol8/SKILL.md`
- Modify: `apps/infra/openclaw/Dockerfile` (new COPY layer directly after the Layer 15 paperclip-skill COPY at ~line 410)

No unit tests (prose artifact); correctness is enforced by the anchor-verification checklist in the PR description and user review. The image workflow already rebuilds on `apps/infra/openclaw/skills/**`.

- [ ] **Step 1: Write `SKILL.md`** — full content below. Before committing, re-verify the two live-drift-prone claims: the Control Panel tab list against `ControlSidebar.NAV_ITEMS` and the settings/marketplace routes against `apps/frontend/src/app/`.

````markdown
---
name: isol8
description: >
  Authoritative guide to the Isol8 platform you run on — its web and desktop
  surfaces, the Integrations tab, the agent marketplace, plans and billing,
  and how your company (Paperclip) is structured. Use whenever a question
  involves Isol8 itself — connecting services, hiring or publishing agents,
  plans/providers/models, where a UI surface lives, what you can and cannot
  do on the platform — rather than the user's own domain work. Do not guess
  platform mechanics; read this first.
---

<!--
  MAINTENANCE (Isol8 engineers): every claim below is anchored to code.
  This file is the fleet's knowledge of the platform — when these change,
  change this file too:
  - Control Panel tabs ...... apps/frontend/src/components/control/ControlSidebar.tsx (NAV_ITEMS)
  - Integrations wiring ..... apps/backend/routers/integrations.py; core/containers/config.py (integration_mcp_servers merge)
  - Marketplace flows ....... apps/backend/routers/marketplace_listings.py; core/services/{marketplace_service,agent_export,catalog_service}.py
  - Seed team + roster ...... apps/backend/core/services/paperclip_provisioning.py (provision_org); friday_identity.py; atlas_seed/
  - Plans / providers ....... apps/backend/core/services/{billing_service,provision_gate,owner_facts}.py
  - Paperclip org model ..... Isol8AI/paperclip fork: server/src/services/agent-permissions.ts,
                              services/agents.ts (getChainOfCommand), adapters/registry.ts (openclaw_gateway)
-->

# The Isol8 Platform

You are an agent running on **Isol8** (https://isol8.co) — a platform where a
person (or team) subscribes and gets their own private AI company: an
isolated container running the OpenClaw agent runtime, a persistent
workspace, a chat UI, a project-management surface (Teams, powered by
Paperclip), and a marketplace of hireable agents.

**Your runtime, briefly:** you run inside your owner's dedicated container.
Your workspace files persist across sessions. Everything in this container
belongs to one owner (a person, or an organization whose members share it).
On the free tier the container sleeps when idle and wakes on demand.

## The surfaces your owner sees

| Surface | Where | What it is |
|---|---|---|
| Chat | isol8.co/chat | Talk to any agent; upload files; browse the workspace |
| Control Panel | isol8.co/chat (right-hand panel) | Tabs: Overview, Agents, Skills, Sessions, Channels, Usage (org admins only), Model & Credits, **Integrations**, Dreams, Skill Proposals |
| Teams | isol8.co/teams | The company board: agents, tasks (issues), threads — where work is assigned and tracked |
| Marketplace | isol8.co/marketplace | Browse, buy, and deploy agents built by others; sellers publish here |
| Settings | isol8.co/settings | Account, billing, connected channels |
| Desktop app | Isol8 for macOS | Same UI, plus: pairs the owner's Chrome for your browser tool, and shows exec-approval prompts natively |
| Channels | Settings / Channels tab | Telegram, Discord, Slack, WhatsApp — chat with agents from outside the web app |

When you tell the owner where to click, name the surface and the tab:
*"the Integrations tab in the Control Panel at isol8.co/chat"*.

## Integrations (connecting external services)

The **Integrations tab** (Control Panel, isol8.co/chat) is where the owner
connects external services — each connection is an MCP server that Isol8
wires into this container's config automatically.

- **What you observe:** once the owner connects a service, its tools simply
  appear in your toolset after the container picks up the config change (no
  restart needed). If a tool you expect is missing, the service isn't
  connected (or was disconnected).
- **What you cannot do:** you cannot connect, authorize, or disconnect a
  service yourself — connecting requires the owner to authenticate in the
  browser. When a task needs an unconnected service, say exactly what's
  needed: *"To do X, connect <service> in the Integrations tab
  (Control Panel at isol8.co/chat)."*
- Some capabilities ship as **click-to-connect skills** that sit disabled in
  the container config until the owner connects the backing service — same
  redirect applies.

## The Marketplace (hiring and publishing agents)

**Hiring (buyer side):** the owner browses isol8.co/marketplace and deploys
an agent into this container. A deployed hire gets its own workspace and a
company roster entry (optionally reporting to whoever the owner picks at
hire time). Some listings are paid (one-time purchase via Stripe). You
cannot deploy a listing yourself — recommend one and point the owner at the
marketplace.

**Publishing (seller side):** the OWNER publishes agents from this container
to the marketplace via the seller wizard, which snapshots the agent, scans
it for secrets/PII, and submits it for Isol8 review (draft → review →
published). Key rules:

- **Atlas builds agents but never publishes them** — its job ends when the
  new agent is registered in the roster. Publishing is always the owner's
  action.
- **Friday cannot be published.** The platform blocks it.
- Runtime state (sessions, memory) is stripped from published snapshots.

## Your company: the org model

Every Isol8 company starts with two seeded agents:

- **Friday** — the CEO (`role: ceo`). The owner's single point of contact.
  Friday decomposes goals, delegates to specialists, triages results, and
  reports back. Friday does not execute specialist work.
- **Atlas** — the Agent Recruiter (`role: general`, reports to Friday).
  Atlas builds new specialist agents from a role spec: researches the role,
  architects the agent, generates its workspace, registers it in the
  roster. **Atlas is not a general worker** — do not assign it research
  tasks, prototypes, or domain work. The right input to Atlas is "we need
  an agent that does X"; the wrong input is "do X".

**How the org works (Paperclip semantics):**

- The roster (`GET /api/companies/{companyId}/agents` via the paperclip
  skill) is the source of truth for who exists. Each entry carries `role`,
  `title`, and `capabilities` — read `capabilities` before delegating.
- `role: ceo` is load-bearing: only the CEO (or agents explicitly granted
  permission) can create agents through Teams or assign work broadly.
  Other roles (`engineer`, `designer`, `pm`, `general`, …) are labels for
  humans and routing — they grant no special powers.
- `chainOfCommand` is the walk up `reportsTo` links; the owner can reparent
  agents in Teams. Keep your own `capabilities` text accurate — it is how
  the rest of the company decides what to send you.
- Agents can have per-agent monthly budgets set by the owner in Teams.
- New agents enter the company three ways: the owner creates one in Teams,
  Atlas builds one from a role spec, or the owner deploys one from the
  marketplace.
- Paperclip ships built-in role instruction bundles, but they are **not
  delivered** to Isol8 agents (the openclaw adapter doesn't support them).
  Your workspace files (SOUL.md, AGENTS.md, …) and bundled skills are your
  entire operating context — if those bundles are ever mentioned, ignore
  them.

## Plans, providers, and this owner's current state

Isol8 has one paid plan — **Starter, $50/month flat fee** (always-on
container) — and a **free tier** (no card: Bedrock Claude Sonnet only,
$3/day usage cap, container sleeps when idle). Inference runs on one of
three provider paths chosen by the owner: **Bedrock managed** (Isol8-billed
credits), **ChatGPT OAuth** (the owner's own OpenAI account; personal
accounts only), or **BYO API key** (owner's Anthropic/OpenAI key).

**Never state the owner's plan or provider from memory.** Read
`~/.openclaw/isol8-facts.json` — Isol8 keeps it current:

```json
{
  "plan": "Starter ($50/mo flat fee)",
  "subscription_status": "active",
  "provider": "Bedrock (Claude, Isol8-managed credits)",
  "provider_choice": "bedrock_claude",
  "owner_kind": "personal",
  "generated_at": "…"
}
```

If the file is missing or the owner says something that contradicts it,
trust the owner and suggest they check isol8.co/settings. Plan and provider
changes happen in Settings / the Model & Credits tab — you cannot change
them.

## What you cannot do (and what to say instead)

| You cannot | Instead say |
|---|---|
| Connect/disconnect an integration | "Connect <service> in the Integrations tab (Control Panel, isol8.co/chat)" |
| Deploy a marketplace listing | "Open isol8.co/marketplace and deploy <listing> — I can take it from there" |
| Publish an agent to the marketplace | "Use the seller wizard at isol8.co/marketplace — publishing is your action" |
| Change plan, provider, or billing | "That's in isol8.co/settings (billing) or the Model & Credits tab" |
| Override an exec approval | Wait for the owner to approve; explain what the command does |
| See or manage other owners' containers | Nothing — every container is single-owner and isolated |

## Quick answers

- **"How do I connect <service>?"** → Integrations tab, Control Panel,
  isol8.co/chat. Its tools appear to you automatically once connected.
- **"Can you build me an agent for X?"** → Yes: Friday routes a role spec
  to Atlas, or the owner deploys a pre-built one from isol8.co/marketplace.
- **"How do I sell my agent?"** → Seller wizard on isol8.co/marketplace;
  Isol8 reviews before publishing. Atlas can build it; only you can publish.
- **"What plan am I on?" / "Which model are you?"** → Read
  `~/.openclaw/isol8-facts.json`, answer from it.
- **"Why can't you do X?"** → Check the boundaries table above before
  improvising an explanation.
````

- [ ] **Step 2: Add the Dockerfile layer** (directly after the paperclip COPY):

```dockerfile
# ---- Layer 16: bundled Isol8 platform skill ----
# The `isol8` skill is every agent's authoritative reference for the
# platform itself — UI surfaces (incl. the Integrations tab), marketplace
# mechanics, plans/providers, the Paperclip org model, and hard boundaries.
# Baked (not seeded) so the WHOLE fleet — Friday, Atlas, scaffolded
# specialists, marketplace hires — gets identical, current platform
# knowledge on every image promotion, with no per-agent seeding and no
# reprovision. Dynamic per-owner state deliberately lives OUTSIDE the image
# in ~/.openclaw/isol8-facts.json (written by the backend — see
# core/services/owner_facts.py); the skill points agents at it.
# Authored in-repo (not vendored). Keep in lockstep with the anchors listed
# in the SKILL.md maintenance header.
COPY skills/isol8 /app/skills/isol8
```

- [ ] **Step 3: Sanity-check the build context** — `docker build apps/infra/openclaw/` locally is optional (base image is private GHCR); minimum bar: `ls apps/infra/openclaw/skills/isol8/SKILL.md` exists and the COPY path matches.

- [ ] **Step 4: Commit**

```bash
git add apps/infra/openclaw/skills/isol8/SKILL.md apps/infra/openclaw/Dockerfile
git commit -m "feat(platform-context): bundled isol8 platform skill"
```

---

### Task 5: seed pointer + dedupe

**Files:**
- Modify: `apps/backend/core/services/friday_identity.py` (SOUL, AGENTS, USER templates; drop `_format_provider_label`)
- Modify: `apps/backend/core/services/atlas_seed/files/AGENTS.md` (line ~69 phrasing + pointer)
- Test: update `apps/backend/tests/unit/services/test_friday_identity.py`

- [ ] **Step 1: Update tests first** — in `test_friday_identity.py`: any assertion on USER.md containing "Isol8 plan" or provider labels changes to assert the facts-file pointer line; add assertions that SOUL.md and AGENTS.md contain `skill isol8`. Run to see them fail.

- [ ] **Step 2: Edit `friday_identity.py` templates**

`_SOUL_TEMPLATE` — append to the "## Where You Live" section:

```
If {owner_display} asks how Isol8 itself works — connecting integrations,
the marketplace, billing, what the UI can do — run `skill isol8` for the
authoritative answers instead of improvising.
```

`_AGENTS_TEMPLATE` — in step 3 ("Identify or hire"), replace the Atlas bullet with:

```
   - Recommend **Atlas** (the agent recruiter) to scaffold a new
     specialist. Hand Atlas a role spec — what the new hire must do —
     never the work itself; Atlas builds agents, it does not do research
     or prototypes.
```

and add beneath the intro paragraph:

```
For platform mechanics — the Integrations tab, marketplace flows, billing
surfaces, what agents can and cannot do on Isol8 — run `skill isol8`.
```

`_USER_TEMPLATE_PERSONAL` and `_USER_TEMPLATE_ORG` — replace the two lines
`- **Isol8 plan:** …` and `- **Provider:** {provider_label}` with:

```
- **Plan & provider:** read `~/.openclaw/isol8-facts.json` — Isol8 keeps it
  current; trust it over anything remembered.
```

Then remove `provider_label` from `_render_templates` and the profile
builders, delete `_format_provider_label` (now `owner_facts.format_provider_label`),
and update any remaining internal callers/imports.

- [ ] **Step 3: Edit `atlas_seed/files/AGENTS.md`** — in the
"Missing Integrations — Click-to-Connect" section, change the quoted user
instruction to: *"To do <X>, connect <service> in the **Integrations tab** of
the Isol8 Control Panel (isol8.co/chat). Supported: <opt1>, <opt2>, <opt3>."*
and append the sentence: `For any other platform-mechanics question, run
\`skill isol8\`.`

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_friday_identity.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/friday_identity.py apps/backend/core/services/atlas_seed/files/AGENTS.md apps/backend/tests/unit/services/test_friday_identity.py
git commit -m "feat(platform-context): point seeds at the isol8 skill; facts file replaces static plan/provider"
```

---

### Task 6: backfill script for existing companies

**Files:**
- Create: `apps/backend/scripts/backfill_seed_team_roster.py`
- Test: `apps/backend/tests/unit/scripts/test_backfill_seed_team_roster.py`

**Interfaces:**
- Consumes: `paperclip_provisioning.{FRIDAY_TITLE,FRIDAY_CAPABILITIES,ATLAS_TITLE,ATLAS_CAPABILITIES}` (Task 3); `PaperclipAdminClient.list_agents(company_id=…, session_cookie=…)` (`paperclip_admin_client.py:681`) and `patch_agent(agent_id=…, body=…, session_cookie=…)` (`paperclip_admin_client.py:711`); `get_admin_session_cookie(http)` (`paperclip_admin_session.py:107`); containers-table scan + `PaperclipRepo.get_org_company_id` — copy both patterns from `scripts/backfill_paperclip_for_legacy_users.py`; http-client + admin-client construction — copy from `paperclip_provisioning.py:70-85`.

Behavior (dry-run by default, `--apply` to write; `--env dev|prod` matching the sibling script):

1. Scan `containers` for owner ids; resolve each to a Paperclip company id (skip if none).
2. `list_agents` per company. Friday = first `role == "ceo"`; Atlas = first `name == "Atlas"` (warn+skip a company whose Atlas was renamed or missing).
3. Compute a patch per agent, **filling only empty fields** (never clobber user-customized titles/capabilities): Friday ← title/capabilities; Atlas ← title/capabilities and `reportsTo` ← Friday's id when Atlas's `reportsTo` is null.
4. Print `owner_id company_id agent: fields-to-set` lines; with `--apply`, call `patch_agent` per agent needing changes. Idempotent by construction (empty-field guard).

- [ ] **Step 1: Write the failing tests** — mirror `test_backfill_paperclip_agent_run_timeout.py` structure: fake admin client capturing `patch_agent` bodies; cases: (a) dry-run patches nothing; (b) apply sets exactly `{"title", "capabilities"}` on a bare Friday and `{"title", "capabilities", "reportsTo"}` on a bare Atlas; (c) an Atlas with an existing custom `capabilities` gets only the missing fields; (d) renamed/missing Atlas → skip line, no patch, no crash.

- [ ] **Step 2: Run to verify failure** — `cd apps/backend && uv run pytest tests/unit/scripts/test_backfill_seed_team_roster.py -v` → collection error.

- [ ] **Step 3: Implement the script** per the behavior spec above (structure copied from the two named sibling scripts; no new abstractions).

- [ ] **Step 4: Run tests** — same command, all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/scripts/backfill_seed_team_roster.py apps/backend/tests/unit/scripts/test_backfill_seed_team_roster.py
git commit -m "feat(platform-context): backfill roster metadata for existing seed teams"
```

---

### Task 7: CLAUDE.md maintenance note + docs

**Files:**
- Modify: `CLAUDE.md` (the paragraph describing the extended OpenClaw image in Project Overview)
- Add (already written, commit here): `docs/superpowers/specs/2026-07-15-isol8-platform-skill-design.md`, `docs/superpowers/plans/2026-07-15-isol8-platform-skill.md`

- [ ] **Step 1: Add one sentence to the extended-image paragraph in CLAUDE.md:**

```
Bundled agent-facing skills live at `apps/infra/openclaw/skills/` (`paperclip`, `isol8`); the `isol8` skill is the fleet's knowledge of the platform itself — when user-facing surfaces, marketplace/billing flows, or Paperclip seeding change, update its SKILL.md (anchors listed in its maintenance header).
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-07-15-isol8-platform-skill-design.md docs/superpowers/plans/2026-07-15-isol8-platform-skill.md
git commit -m "docs(platform-context): spec, plan, and skill-maintenance note"
```

---

### Task 8: full verification, PR, codex review, merge

- [ ] **Step 1: Full backend suite** — `cd apps/backend && uv run pytest tests/ -q`. Expected: green. Any failure — including in untouched files — is presumed caused by this change; fix before proceeding.
- [ ] **Step 2: Self-review the diff** (`/code-review` at medium effort); fix real findings.
- [ ] **Step 3: Rebase on fresh `origin/main`**, re-run the touched test files.
- [ ] **Step 4: Push HEAD; open PR** titled `feat(platform-context): uniform platform knowledge for all agents (isol8 skill + owner facts + roster fix)`. Body: problem (agents' platform blind spots + Atlas roster misdescription), the five components, the anchor-verification checklist for SKILL.md claims, test plan, rollout section (image tag promotion + backfill). Tag codex for review per repo convention (review request comment). Include findings summary in body.
- [ ] **Step 5: Watch CI + codex.** Address findings (dedupe re-flags). Merge only on green CI + codex approval (👍 reaction or "Review Result" comment). Two-step merge + branch delete from the worktree.

---

### Task 9: rollout (post-merge)

- [ ] **Step 1:** `gh run watch` the `build-openclaw-image` run on main; note the new tag (`<upstream>-<shortsha>`).
- [ ] **Step 2:** Run the roster backfill against prod (independent of the image): `cd apps/backend && uv run python -m scripts.backfill_seed_team_roster --env prod` (review dry-run output) then `--apply`. Same for dev.
- [ ] **Step 3:** Dev image flip: PR updating `openclaw-version.json` `dev.tag` to the new tag → merge (same codex gate) → apply to a dev container (admin force-apply lever) → validate: `skill isol8` listed in-container; ask dev Friday "what can Atlas do?" and "how do I connect an integration?" — answers must match the skill.
- [ ] **Step 4:** Prod image flip: same PR flow for `prod.tag` after dev validation. Existing prod containers pick the image up via the update path; facts files materialize as `write_user_configs` re-runs on apply.
