# Paperclip ↔ OpenClaw Agent Pairing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Paperclip agents and OpenClaw agents share a single UUID space so the Teams Inbox (`/teams`) and the chat UI (`/chat`) operate on the same agent population — assigning an issue to a Paperclip agent routes to the matching OpenClaw agent's workspace, not to OpenClaw's literal `"main"` fallback. Each issue gets its own OpenClaw session so contexts don't bleed between issues.

**Architecture:** Paperclip is the source of truth for agent identity. At container provision time, the backend fetches the live Paperclip agent list via the existing `paperclip_admin_client.list_agents` REST call and writes one entry per Paperclip agent into `openclaw.json`'s `agents.list`, using the Paperclip UUID as the OpenClaw agentId. The hardcoded `"main"` entry is removed; the Paperclip agent with `role="ceo"` (or the first one) gets `default: true`. For agents created after the container is alive, the backend intercepts `/chat`'s `agents.create` RPC and `/teams`' POST, calling Paperclip first to mint a UUID and then OpenClaw's `agents.create` RPC with that UUID as the name (followed by `agents.update` to set the human-friendly display name).

The container is keyed by **owner_id** (Clerk org_id for orgs, user_id for personal users). One container per owner; org members share their owner's container, EFS, and Paperclip company. Member-level isolation for `/chat` is preserved by the existing `agent:<agentId>:<member_user_id>` session-key format in `websocket_chat.py`. Paperclip-driven runs use `sessionKeyStrategy="issue"` so each issue gets its own OpenClaw session — different issues never bleed transcripts into each other, while long-term knowledge persists across sessions via OpenClaw's memory plugin.

No DDB schema changes — Aurora (via Paperclip's API) is the master.

**Tech Stack:** Python 3.13 (FastAPI, uv), httpx (Paperclip admin client), websockets (OpenClaw gateway RPC), DynamoDB (read-only for this work, via existing repos), pytest + pytest-asyncio.

---

## File Structure

**Modified files:**
- `apps/backend/core/services/paperclip_adapter_config.py` — change `sessionKeyStrategy` from `"fixed"` to `"issue"` so per-issue session isolation kicks in.
- `apps/backend/core/services/paperclip_provisioning.py` — rename the seeded CEO agent from "Main Agent" to "Friday" (one-line change at `:265`).
- `apps/backend/core/containers/config.py` — `build_openclaw_config_dict` gains a `paperclip_agents` parameter; `write_openclaw_config` fetches it before calling.
- `apps/backend/core/containers/ecs_manager.py` — `write_user_configs` fetches Paperclip agents (or accepts `None` for offline tests) and threads them through. Keying is owner_id, not user_id.
- `apps/backend/core/services/paperclip_admin_client.py` — confirm `list_agents` shape; add an admin-cookie variant if missing.
- `apps/backend/routers/teams/agents.py` — extend `create_agent` route to call OpenClaw `agents.create` after Paperclip create, when the container is reachable.
- `apps/backend/routers/websocket_chat.py` — intercept `agents.create` RPC: do Paperclip-first, then forward to gateway with name=UUID, then `agents.update` for display.

**New files:**
- `apps/backend/core/services/paperclip_openclaw_pairing.py` — single home for the pairing helpers (`fetch_paperclip_agents_for_owner`, `pair_with_openclaw`, `_resolve_company_id_for_owner`). Centralizes the policy so `routers/teams/agents.py`, `routers/websocket_chat.py`, and `ecs_manager.py` all share one implementation.
- `apps/backend/scripts/migrate_paperclip_session_strategy.py` — one-shot migration that flips existing Paperclip agents from `sessionKeyStrategy="fixed"` to `"issue"`. Run once per environment after deploy.
- `apps/backend/tests/unit/services/test_paperclip_adapter_config.py` — unit tests for the strategy change.
- `apps/backend/tests/unit/containers/test_config_paperclip_agents.py` — unit tests for the builder.
- `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py` — unit tests for the pairing helpers.
- `apps/backend/tests/unit/scripts/test_migrate_paperclip_session_strategy.py` — unit tests for the migration script.
- `apps/backend/tests/integration/test_pairing_flow.py` — integration test covering provision-time and forward-going creates.

**Out of scope (deferred to follow-up):**
- Backfill of any existing OpenClaw-only agents. Once this lands, a one-shot script can iterate over each OpenClaw agent in a user's container, create a matching Paperclip agent, and either rename the EFS dir or recreate the agent. Tracked separately.
- Agent deletion sync (Paperclip delete → OpenClaw `agents.delete`). The existing `agents.delete` interception at `routers/websocket_chat.py:543` handles `/chat`-initiated deletes; cross-direction is a separate change.

---

## ⚠️ Important: do NOT relocate `agents/main/agent/auth-profiles.json`

`apps/backend/core/containers/workspace.py:712` writes ChatGPT OAuth credentials to `<EFS>/users/{owner_id}/agents/main/agent/auth-profiles.json`. After this PR replaces the literal `"main"` entry in `agents.list` with paired Paperclip UUIDs, it is tempting to "tidy up" by relocating this file to Friday's agent dir (e.g., `agents/<friday_uuid>/agent/auth-profiles.json`). **Do not do this.**

OpenClaw's auth-profile resolver (`openclaw/src/agents/agent-paths.ts:6-13`) returns `<stateDir>/agents/<DEFAULT_AGENT_ID>/agent/` unconditionally, where `DEFAULT_AGENT_ID = "main"` is hardcoded in `routing/session-key.ts:21`. This is the **container-wide auth-fallback location** — not tied to any agent in `agents.list`.

The fallback test `openclaw/src/agents/auth-profiles/oauth.fallback-to-main-agent.test.ts` confirms that secondary agents (anything not at `agents/main/`) inherit credentials from this location when their own are missing or refresh fails. In Isol8's flat-fee model every container has one provider; relocating the file would break the inheritance chain and force us to write per-paired-agent copies that are otherwise unnecessary.

Two roles, one literal name:
- Pre-PR: `agents/main/` was BOTH the agent's runtime dir AND the auth fallback.
- Post-PR: `agents/main/` is PURELY the auth fallback. Friday (and any other paired agent) runs out of `agents/<paperclip_uuid>/`. No agent runtime lives at `agents/main/` anymore — but the auth file does.

---

## Pre-flight: worktree

- [ ] **Step 0: Create the worktree**

Use the `superpowers:using-git-worktrees` skill to create a worktree branch named `feat/paperclip-openclaw-agent-pairing` off `main`. All work below happens inside that worktree.

---

## Task 0.5: Switch `synthesize_openclaw_adapter` to per-issue session strategy

**Files:**
- Modify: `apps/backend/core/services/paperclip_adapter_config.py:46-69`
- Test: `apps/backend/tests/unit/services/test_paperclip_adapter_config.py` (extend or create)

**Why this comes first:** This change is independent of agent pairing and affects every existing/future Paperclip agent's wire format. Doing it first means subsequent test fixtures use the correct strategy.

- [ ] **Step 0.5.1: Write the failing test**

Create or extend `apps/backend/tests/unit/services/test_paperclip_adapter_config.py`:

```python
"""Unit tests for synthesize_openclaw_adapter."""

from core.services.paperclip_adapter_config import (
    OPENCLAW_GATEWAY_TYPE,
    synthesize_openclaw_adapter,
)


def test_strategy_is_issue_for_per_issue_session_isolation():
    """We want each issue assigned to an agent to be its own OpenClaw
    session so transcripts don't bleed across unrelated issues. The
    sessionKey field becomes the fallback for manual wakes (no issue
    context); the issue-driven path uses paperclip:issue:<id>."""
    cfg = synthesize_openclaw_adapter(
        gateway_url="wss://ws.isol8.co",
        service_token="t" * 100,
        user_id="user_abc",
    )
    assert cfg["sessionKeyStrategy"] == "issue"
    assert cfg["sessionKey"] == "user_abc"
    assert cfg["url"] == "wss://ws.isol8.co"
    assert cfg["authToken"] == "t" * 100
```

- [ ] **Step 0.5.2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_adapter_config.py -v`
Expected: FAIL — current code returns `"fixed"`.

- [ ] **Step 0.5.3: Update `synthesize_openclaw_adapter`**

Edit `apps/backend/core/services/paperclip_adapter_config.py:64-69`. Change `sessionKeyStrategy` from `"fixed"` to `"issue"`:

```python
    return {
        "url": gateway_url,
        "authToken": service_token,
        "sessionKeyStrategy": "issue",
        "sessionKey": user_id,
    }
```

Also update the module docstring at the top of the file to explain the strategy choice. Replace the existing docstring lines 1-20 with:

```python
"""Server-side synthesis of openclaw_gateway adapter config for Paperclip.

Spec §5 security invariant: the BFF NEVER accepts ``adapterType``,
``adapterConfig``, or any nested URL/header field from the client.
Every agent-mutating BFF endpoint calls ``synthesize_openclaw_adapter``
to assemble the upstream body. The URL is allowlisted against the
env-derived gateway hostnames; an out-of-allowlist URL is an operator
bug (the URL is read from our own infrastructure), not user input,
so we raise rather than 4xx.

Field shape per ``paperclip/packages/adapters/openclaw-gateway/src/index.ts:21``:
{ url, authToken, sessionKeyStrategy, sessionKey }. Note ``authToken``,
not ``token``.

Adapter type is ``"openclaw_gateway"`` with an underscore — canonical
per ``paperclip/packages/shared/src/constants.ts:40``.

Session key strategy is ``"issue"`` so each Paperclip issue assigned
to an agent gets its own OpenClaw session
(``agent:<agent_id>:paperclip:issue:<issue_id>``). This isolates
transcripts across unrelated issues; long-term knowledge still
persists across sessions via OpenClaw's memory plugin (MEMORY.md).
The ``sessionKey`` field is the fallback for wakes without an issueId
(manual ``Run Heartbeat``, sibling-agent wake, etc.) and is set to
the agent creator's user_id so manual wakes route to a stable
per-creator session.
"""
```

- [ ] **Step 0.5.4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_adapter_config.py -v`
Expected: PASS.

- [ ] **Step 0.5.5: Run any existing tests that exercise this module to confirm no regression**

Run: `cd apps/backend && uv run pytest tests/ -k "adapter_config" -v`
Expected: PASS for all matched tests.

- [ ] **Step 0.5.6: Commit**

```bash
git add apps/backend/core/services/paperclip_adapter_config.py apps/backend/tests/unit/services/test_paperclip_adapter_config.py
git commit -m "feat(paperclip): switch adapter sessionKeyStrategy to 'issue'

Each Paperclip issue assigned to an agent now gets its own OpenClaw
session, preventing transcript bleed between unrelated issues. The
sessionKey field becomes a fallback for manual wakes (no issueId).
Long-term cross-session knowledge still persists via OpenClaw's
memory plugin."
```

---

## Task summary

| # | What | Order |
|---|---|---|
| 0 | Worktree | pre-flight |
| 0.5 | `synthesize_openclaw_adapter` strategy → `"issue"` | first (independent of pairing) |
| 0.6 | Rename seeded CEO agent to "Friday" in `provision_org` | first (independent of pairing) |
| 1 | `build_openclaw_config_dict` accepts `paperclip_agents` | core builder |
| 2 | `write_openclaw_config` threads it through | core builder |
| 3 | `paperclip_openclaw_pairing` helper module — `fetch_paperclip_agents_for_owner` | helper |
| 4 | Same module — `pair_with_openclaw` | helper |
| 5 | `write_user_configs` calls the fetcher (provision-time) | wiring |
| 6 | `/teams` create route calls `pair_with_openclaw` | forward-going |
| 7 | `/chat` `agents.create` RPC interception | forward-going |
| 8 | End-to-end integration test | verification |
| 9 | Docs (CLAUDE.md + architecture note) | docs |
| 10 | PR + smoke test on dev | release |
| 11 | One-shot migration script for existing agents (sessionKeyStrategy) | post-deploy |

---

## Task 0.6: Rename the seeded agent from "Main Agent" to "Friday"

**Files:**
- Modify: `apps/backend/core/services/paperclip_provisioning.py:265`
- Test: `apps/backend/tests/unit/services/test_paperclip_provisioning.py` (extend or create)

**Why:** Branding — the platform's default-seeded agent is "Friday", not "Main Agent". One-line change. Existing accounts will be wiped on dev (via the CLAUDE.md "Clean-Slate Dev Reset" procedure) and on prod (admin@isol8.co only at the time of writing); no rename migration needed.

- [ ] **Step 0.6.1: Write a test that the seed name is "Friday"**

Append to (or create) `apps/backend/tests/unit/services/test_paperclip_provisioning.py`:

```python
"""Test the agent name used when seeding a new org's Main Agent."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_provision_org_seeds_agent_named_friday():
    """The auto-seeded CEO agent for a new org is named 'Friday'."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = MagicMock()
    admin.sign_up_user = AsyncMock(
        return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_new"}
    )
    admin.create_company = AsyncMock(return_value={"id": "co_xyz"})
    admin.create_invite = AsyncMock(return_value={"token": "invtok"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_1"})
    admin.approve_join_request = AsyncMock()
    admin.create_agent = AsyncMock()

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.put = AsyncMock()

    # Stub admin-session helper imported lazily inside provision_org.
    import core.services.paperclip_provisioning as mod
    mod.get_admin_session_cookie = AsyncMock(return_value="admin_cookie")
    mod.invalidate_admin_session = MagicMock()

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        org_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    admin.create_agent.assert_awaited_once()
    assert admin.create_agent.await_args.kwargs["name"] == "Friday"
    assert admin.create_agent.await_args.kwargs["role"] == "ceo"
```

- [ ] **Step 0.6.2: Run the test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py::test_provision_org_seeds_agent_named_friday -v`
Expected: FAIL — current code passes `name="Main Agent"`.

- [ ] **Step 0.6.3: Make the change**

Edit `apps/backend/core/services/paperclip_provisioning.py:265`. Change:

```python
await self._admin.create_agent(
    company_id=company_id,
    name="Main Agent",
    role="ceo",
    ...
)
```

to:

```python
await self._admin.create_agent(
    company_id=company_id,
    name="Friday",
    role="ceo",
    ...
)
```

- [ ] **Step 0.6.4: Run the test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py::test_provision_org_seeds_agent_named_friday -v`
Expected: PASS.

- [ ] **Step 0.6.5: Commit**

```bash
git add apps/backend/core/services/paperclip_provisioning.py apps/backend/tests/unit/services/test_paperclip_provisioning.py
git commit -m "feat(paperclip): rename seeded CEO agent to Friday

Branding — Isol8's default-seeded agent is 'Friday', not the generic
'Main Agent'. One-line change in provision_org. Existing accounts
will be wiped, no rename migration."
```

---

## Task 1: Add `paperclip_agents` parameter to `build_openclaw_config_dict`

**Files:**
- Modify: `apps/backend/core/containers/config.py:438-668`
- Test: `apps/backend/tests/unit/containers/test_config_paperclip_agents.py` (new)

- [ ] **Step 1.1: Write the failing test for empty paperclip_agents**

Create `apps/backend/tests/unit/containers/test_config_paperclip_agents.py`:

```python
"""Unit tests for paperclip_agents handling in build_openclaw_config_dict."""

from core.containers.config import build_openclaw_config_dict


def test_empty_paperclip_agents_falls_back_to_main():
    """When paperclip_agents=[] (provision before any Paperclip agents seeded),
    keep the legacy 'main' entry as the default so /chat still works."""
    cfg = build_openclaw_config_dict(
        user_id="user_abc",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[],
    )
    agents = cfg["agents"]["list"]
    assert len(agents) == 1
    assert agents[0]["id"] == "main"
    assert agents[0]["default"] is True


def test_single_paperclip_agent_replaces_main():
    """One Paperclip agent → one entry, marked default. No 'main' entry."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    cfg = build_openclaw_config_dict(
        user_id="user_abc",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[
            {"id": pc_uuid, "name": "Main Agent", "role": "ceo"},
        ],
    )
    agents = cfg["agents"]["list"]
    assert len(agents) == 1
    assert agents[0]["id"] == pc_uuid
    assert agents[0]["default"] is True
    assert agents[0]["workspace"] == f"/home/node/.openclaw/workspaces/{pc_uuid}"
    assert agents[0]["reasoningDefault"] == "stream"


def test_multiple_paperclip_agents_ceo_is_default():
    """When multiple Paperclip agents exist, the one with role='ceo' is the
    default. Others are listed but not default."""
    ceo_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    other_uuid = "11111111-1111-1111-1111-111111111111"
    cfg = build_openclaw_config_dict(
        user_id="user_abc",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[
            {"id": other_uuid, "name": "Code Reviewer", "role": "engineer"},
            {"id": ceo_uuid, "name": "Main Agent", "role": "ceo"},
        ],
    )
    agents = cfg["agents"]["list"]
    assert len(agents) == 2
    by_id = {a["id"]: a for a in agents}
    assert by_id[ceo_uuid]["default"] is True
    assert "default" not in by_id[other_uuid] or by_id[other_uuid]["default"] is False


def test_multiple_agents_no_ceo_first_is_default():
    """If no Paperclip agent has role='ceo' (edge case — seed failed), the
    first one in the list becomes the default."""
    a_uuid = "11111111-1111-1111-1111-111111111111"
    b_uuid = "22222222-2222-2222-2222-222222222222"
    cfg = build_openclaw_config_dict(
        user_id="user_abc",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[
            {"id": a_uuid, "name": "First", "role": "engineer"},
            {"id": b_uuid, "name": "Second", "role": "engineer"},
        ],
    )
    by_id = {a["id"]: a for a in cfg["agents"]["list"]}
    assert by_id[a_uuid]["default"] is True
    assert "default" not in by_id[b_uuid] or by_id[b_uuid]["default"] is False
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_config_paperclip_agents.py -v`
Expected: FAIL — `TypeError: build_openclaw_config_dict() got an unexpected keyword argument 'paperclip_agents'`.

- [ ] **Step 1.3: Update the builder signature and replace the hardcoded `agents.list`**

Edit `apps/backend/core/containers/config.py`. Add the parameter to `build_openclaw_config_dict` (line 438) and replace the hardcoded `agents.list` block (lines 544-554).

Change the signature from:
```python
def build_openclaw_config_dict(
    *,
    user_id: str,
    gateway_token: str,
    provider_choice: str,
    byo_provider: str | None = None,
) -> dict:
```

to:
```python
def build_openclaw_config_dict(
    *,
    user_id: str,
    gateway_token: str,
    provider_choice: str,
    byo_provider: str | None = None,
    paperclip_agents: list[dict] | None = None,
) -> dict:
```

Then replace the hardcoded list at lines 544-554:
```python
            "list": [
                {
                    "id": "main",
                    "default": True,
                    "reasoningDefault": "stream",
                    "workspace": "/home/node/.openclaw/workspaces/main",
                },
            ],
```

with:
```python
            "list": _build_agents_list(paperclip_agents or []),
```

Add the helper above `build_openclaw_config_dict` (after `_provider_block` ends, around line 437):

```python
def _build_agents_list(paperclip_agents: list[dict]) -> list[dict]:
    """Build the openclaw.json agents.list entries from Paperclip agents.

    If paperclip_agents is empty, fall back to the legacy hardcoded 'main'
    agent so a freshly-provisioned container still works (e.g. when
    Paperclip provisioning failed silently and the user opens /chat
    before the next reprovision).

    Otherwise emit one entry per Paperclip agent. The agent with
    role='ceo' is marked default; if no agent has that role, the first
    one in the list wins. Each agent's workspace is keyed by its
    Paperclip UUID so OpenClaw's session-key routing
    (agent:<uuid>:<rest>) lands in the right directory on EFS.
    """
    if not paperclip_agents:
        return [
            {
                "id": "main",
                "default": True,
                "reasoningDefault": "stream",
                "workspace": "/home/node/.openclaw/workspaces/main",
            },
        ]

    default_index = next(
        (i for i, a in enumerate(paperclip_agents) if a.get("role") == "ceo"),
        0,
    )

    entries: list[dict] = []
    for i, agent in enumerate(paperclip_agents):
        agent_id = agent["id"]
        entry: dict = {
            "id": agent_id,
            "reasoningDefault": "stream",
            "workspace": f"/home/node/.openclaw/workspaces/{agent_id}",
        }
        if i == default_index:
            entry["default"] = True
        entries.append(entry)
    return entries
```

- [ ] **Step 1.4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_config_paperclip_agents.py -v`
Expected: PASS — all four tests.

- [ ] **Step 1.5: Run the existing config drift test to make sure nothing else broke**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_config.py -v`
Expected: PASS (the existing test exercises the legacy code path; our change is backward-compatible because `paperclip_agents=None` keeps the old `main` entry).

- [ ] **Step 1.6: Commit**

```bash
git add apps/backend/core/containers/config.py apps/backend/tests/unit/containers/test_config_paperclip_agents.py
git commit -m "feat(containers): accept paperclip_agents in build_openclaw_config_dict

Replaces the hardcoded 'main' agent in openclaw.json's agents.list with
one entry per live Paperclip agent, keyed by the Paperclip UUID. The
agent with role='ceo' is marked default. Empty paperclip_agents keeps
the legacy 'main' fallback so unprovisioned containers still work."
```

---

## Task 2: Thread `paperclip_agents` through `write_openclaw_config`

**Files:**
- Modify: `apps/backend/core/containers/config.py:668-712`
- Test: `apps/backend/tests/unit/containers/test_config_paperclip_agents.py` (extend)

- [ ] **Step 2.1: Add a test that paperclip_agents flows through write_openclaw_config**

Append to `apps/backend/tests/unit/containers/test_config_paperclip_agents.py`:

```python
import json
import tempfile
from pathlib import Path

import pytest

from core.containers.config import write_openclaw_config


@pytest.mark.asyncio
async def test_write_openclaw_config_persists_paperclip_agents():
    """write_openclaw_config writes agents.list with the supplied Paperclip
    agents, not the hardcoded 'main' entry."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "openclaw.json"
        await write_openclaw_config(
            config_path=path,
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            user_id="user_abc",
            paperclip_agents=[
                {"id": pc_uuid, "name": "Main Agent", "role": "ceo"},
            ],
        )
        cfg = json.loads(path.read_text())
        assert cfg["agents"]["list"][0]["id"] == pc_uuid
        assert cfg["agents"]["list"][0]["default"] is True


@pytest.mark.asyncio
async def test_write_openclaw_config_omitted_paperclip_agents_keeps_main():
    """write_openclaw_config without paperclip_agents arg falls back to
    the legacy 'main' entry. Backward compatible for callers we haven't
    updated yet."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "openclaw.json"
        await write_openclaw_config(
            config_path=path,
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            user_id="user_abc",
        )
        cfg = json.loads(path.read_text())
        assert cfg["agents"]["list"][0]["id"] == "main"
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_config_paperclip_agents.py -v`
Expected: the two new tests FAIL — `write_openclaw_config` doesn't accept `paperclip_agents` yet.

- [ ] **Step 2.3: Update `write_openclaw_config`**

Edit `apps/backend/core/containers/config.py:668-705`. Add the parameter and pass it through:

```python
async def write_openclaw_config(
    *,
    config_path: Path,
    gateway_token: str,
    provider_choice: str,
    user_id: str,
    byo_provider: str | None = None,
    paperclip_agents: list[dict] | None = None,
) -> None:
    """Write the user's openclaw.json with the full container config.

    See ``build_openclaw_config_dict`` for ``paperclip_agents`` semantics.
    Omitting the kwarg keeps the legacy hardcoded 'main' agent — used by
    test fixtures and migration paths that don't have Paperclip access.
    """
    config = build_openclaw_config_dict(
        user_id=user_id,
        gateway_token=gateway_token,
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        paperclip_agents=paperclip_agents,
    )

    def _write() -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2))

    await asyncio.to_thread(_write)
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_config_paperclip_agents.py -v`
Expected: PASS — all six tests.

- [ ] **Step 2.5: Commit**

```bash
git add apps/backend/core/containers/config.py apps/backend/tests/unit/containers/test_config_paperclip_agents.py
git commit -m "feat(containers): pass paperclip_agents through write_openclaw_config

Forwarded to build_openclaw_config_dict. Default-None preserves the
hardcoded 'main' agent for callers that haven't been updated yet."
```

---

## Task 3: Pairing helper module

**Files:**
- Create: `apps/backend/core/services/paperclip_openclaw_pairing.py`
- Test: `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py` (new)

**Multi-tenant note:** The container is keyed by `owner_id` (Clerk org_id for orgs, user_id for personal users). Org members share the owner's container, EFS, and Paperclip company. So this helper looks up agents by `owner_id`, not `user_id` — it returns the **whole company's** agent list, which is correct because all org members share the runtime. Member-level isolation for `/chat` is handled separately by `websocket_chat.py`'s `agent:<agentId>:<member_user_id>` session-key format. Issue-driven runs use `paperclip:issue:<issue_id>` (after Task 0.5).

- [ ] **Step 3.1: Write tests for `fetch_paperclip_agents_for_owner`**

Create `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py`:

```python
"""Unit tests for the paperclip ↔ openclaw pairing helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.paperclip_openclaw_pairing import (
    fetch_paperclip_agents_for_owner,
)


@pytest.mark.asyncio
async def test_fetch_returns_empty_list_for_personal_user_with_no_paperclip_row():
    """Personal user (owner_id starts with 'user_') has no PaperclipCompany
    row → return []. Container provision falls back to 'main'."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    admin_client = MagicMock()

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_for_personal_user_returns_their_company_agents():
    """Personal user happy path: repo.get(user_id) → company_id →
    list_agents → return list."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": pc_uuid, "name": "Main Agent", "role": "ceo"}]
    )

    import core.services.paperclip_openclaw_pairing as mod
    mod.get_admin_session_cookie = AsyncMock(return_value="cookie123")

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == [{"id": pc_uuid, "name": "Main Agent", "role": "ceo"}]
    admin_client.list_agents.assert_called_once_with(
        session_cookie="cookie123",
        company_id="co_xyz",
    )


@pytest.mark.asyncio
async def test_fetch_for_org_uses_get_org_company_id():
    """Org owner (owner_id starts with 'org_') resolves company via
    repo.get_org_company_id, NOT repo.get."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    repo = MagicMock()
    repo.get = AsyncMock()  # should NOT be called
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": pc_uuid, "name": "Main Agent", "role": "ceo"}]
    )

    import core.services.paperclip_openclaw_pairing as mod
    mod.get_admin_session_cookie = AsyncMock(return_value="cookie123")

    result = await fetch_paperclip_agents_for_owner(
        owner_id="org_xyz",
        repo=repo,
        admin_client=admin_client,
    )

    assert len(result) == 1
    repo.get.assert_not_awaited()
    repo.get_org_company_id.assert_awaited_once_with("org_xyz")


@pytest.mark.asyncio
async def test_fetch_for_org_with_no_company_returns_empty():
    """Org never had Paperclip provisioned → return []. Container provision
    falls back to 'main'."""
    repo = MagicMock()
    repo.get_org_company_id = AsyncMock(return_value=None)
    admin_client = MagicMock()

    result = await fetch_paperclip_agents_for_owner(
        owner_id="org_xyz",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_paperclip_api_error():
    """If Paperclip is down or returns 5xx, don't fail container provision —
    fall back to legacy 'main' agent. Log the error for ops visibility."""
    from core.services.paperclip_admin_client import PaperclipApiError

    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(
        side_effect=PaperclipApiError("503", status_code=503, body={})
    )

    import core.services.paperclip_openclaw_pairing as mod
    mod.get_admin_session_cookie = AsyncMock(return_value="cookie123")

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_openclaw_pairing.py -v`
Expected: FAIL — module doesn't exist yet (`ModuleNotFoundError`).

- [ ] **Step 3.3: Create the helper module**

Create `apps/backend/core/services/paperclip_openclaw_pairing.py`:

```python
"""Paperclip ↔ OpenClaw agent pairing helpers.

Single source of truth for the policy that:

1. Fetches the live Paperclip agent list for an OWNER (used at
   container provision time to populate openclaw.json's agents.list).

2. Pairs a single Paperclip agent with the OpenClaw container by
   calling agents.create + agents.update RPCs over the gateway.

The container is the runtime; Paperclip is the master record. Owner
keying matches the rest of the platform: org_id for orgs, user_id
for personal users. One container per owner, shared across all org
members.

Anything that creates a Paperclip agent should also call
``pair_with_openclaw`` when the container is reachable. Anything
that provisions a container should call
``fetch_paperclip_agents_for_owner`` first and pass the result into
``write_openclaw_config``.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)


def _is_org_owner_id(owner_id: str) -> bool:
    """Clerk org IDs are prefixed 'org_'; user IDs are prefixed 'user_'.

    Stable convention from Clerk; if either prefix changes upstream,
    update this and the tests together.
    """
    return owner_id.startswith("org_")


async def _resolve_company_id_for_owner(
    *, owner_id: str, repo: PaperclipRepo
) -> str | None:
    """Look up the Paperclip company_id for this owner.

    For personal users (owner_id = user_id): repo.get(user_id).company_id
    For orgs (owner_id = org_id): repo.get_org_company_id(org_id)

    Returns None if no company exists yet (provisioning hasn't run).
    """
    if _is_org_owner_id(owner_id):
        return await repo.get_org_company_id(owner_id)
    row = await repo.get(owner_id)
    return row.company_id if row else None


async def fetch_paperclip_agents_for_owner(
    *,
    owner_id: str,
    repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
) -> list[dict[str, Any]]:
    """Return the live Paperclip agent list for ``owner_id``, or [] if
    none can be fetched.

    Returns [] (not raises) when:
      - The owner has no Paperclip company yet (provisioning didn't run).
      - Paperclip's API is unreachable or returns an error.

    Container provision is the primary caller. We refuse to fail
    provision on Paperclip availability — empty list flows through to
    the legacy 'main' agent fallback in build_openclaw_config_dict.
    """
    company_id = await _resolve_company_id_for_owner(
        owner_id=owner_id, repo=repo
    )
    if company_id is None:
        return []

    try:
        cookie = await get_admin_session_cookie(admin_client._http)
        agents = await admin_client.list_agents(
            session_cookie=cookie,
            company_id=company_id,
        )
    except PaperclipApiError as e:
        logger.warning(
            "paperclip_openclaw_pairing: list_agents failed for owner=%s "
            "company=%s status=%s — falling back to empty list",
            owner_id,
            company_id,
            e.status_code,
        )
        return []

    if not isinstance(agents, list):
        logger.warning(
            "paperclip_openclaw_pairing: list_agents returned non-list for "
            "owner=%s — got %s",
            owner_id,
            type(agents).__name__,
        )
        return []

    return agents
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_openclaw_pairing.py -v`
Expected: PASS — all five tests.

- [ ] **Step 3.5: Commit**

```bash
git add apps/backend/core/services/paperclip_openclaw_pairing.py apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py
git commit -m "feat(paperclip): add paperclip_openclaw_pairing service

Centralizes the policy for fetching the live Paperclip agent list at
container provision time. Keyed by owner_id (org_id for orgs,
user_id for personal users) — matches the platform's container key.
Returns [] on any error so provision flows fall back to the legacy
'main' agent."
```

---

## Task 4: Add `pair_with_openclaw` to the helper

**Files:**
- Modify: `apps/backend/core/services/paperclip_openclaw_pairing.py`
- Test: `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py` (extend)

**Owner_id note:** `gateway_pool.send_rpc` accepts a `user_id` kwarg, but in practice the gateway pool is keyed by **owner_id** (the container key). Pass owner_id wherever the existing API takes user_id — name your local variable accordingly to avoid confusion.

- [ ] **Step 4.1: Write tests for `pair_with_openclaw`**

Append to `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py`:

```python
from core.services.paperclip_openclaw_pairing import pair_with_openclaw


@pytest.mark.asyncio
async def test_pair_calls_agents_create_with_uuid_then_update_for_display_name():
    """pair_with_openclaw sends two RPCs:
    1. agents.create with name=<paperclip_uuid> (so OpenClaw uses the
       UUID as the on-disk agentId).
    2. agents.update with name=<friendly_name> to fix the display."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True, "agentId": pc_uuid})

    await pair_with_openclaw(
        owner_id="user_abc",
        paperclip_agent={
            "id": pc_uuid,
            "name": "Code Reviewer",
            "role": "engineer",
        },
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
    )

    assert pool.send_rpc.await_count == 2

    create_call = pool.send_rpc.await_args_list[0]
    assert create_call.kwargs["method"] == "agents.create"
    assert create_call.kwargs["params"]["name"] == pc_uuid
    assert create_call.kwargs["params"]["workspace"] == (
        f"/home/node/.openclaw/workspaces/{pc_uuid}"
    )
    # The pool's user_id kwarg is actually owner_id under the hood —
    # the existing gateway pool's signature is what it is.
    assert create_call.kwargs["user_id"] == "user_abc"

    update_call = pool.send_rpc.await_args_list[1]
    assert update_call.kwargs["method"] == "agents.update"
    assert update_call.kwargs["params"]["agentId"] == pc_uuid
    assert update_call.kwargs["params"]["name"] == "Code Reviewer"


@pytest.mark.asyncio
async def test_pair_skips_if_agent_already_exists():
    """If agents.create returns 'already exists' (idempotent reprovision
    path), skip the update — the agent is already paired correctly."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pool = MagicMock()
    # OpenClaw returns INVALID_REQUEST with "agent already exists" in message
    pool.send_rpc = AsyncMock(
        side_effect=RuntimeError(
            '{"code":-32602,"message":"agent \\"' + pc_uuid + '\\" already exists"}'
        )
    )

    # Should not raise — idempotent.
    await pair_with_openclaw(
        owner_id="org_xyz",
        paperclip_agent={"id": pc_uuid, "name": "Main Agent", "role": "ceo"},
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
    )

    # Only the create attempt; update was skipped because create raised.
    assert pool.send_rpc.await_count == 1
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_openclaw_pairing.py -v`
Expected: FAIL — `pair_with_openclaw` doesn't exist yet.

- [ ] **Step 4.3: Implement `pair_with_openclaw`**

Append to `apps/backend/core/services/paperclip_openclaw_pairing.py`:

```python
async def pair_with_openclaw(
    *,
    owner_id: str,
    paperclip_agent: dict[str, Any],
    gateway_pool: Any,
    ip: str,
    token: str,
) -> None:
    """Pair one Paperclip agent with the owner's OpenClaw container.

    Calls OpenClaw's ``agents.create`` RPC with ``name=<paperclip_uuid>``
    (so OpenClaw derives ``agentId`` from the UUID via
    ``normalizeAgentId``), then ``agents.update`` to set the human-
    friendly display name.

    Idempotent: an "agent already exists" error from agents.create is
    swallowed (the agent was paired by a previous run or by the
    provision-time openclaw.json write).

    Other errors propagate so the caller (an HTTP handler or RPC
    interceptor) can return them to the user. Container reachability
    is the caller's responsibility — this helper assumes the gateway
    pool can reach the owner's container.

    Note: ``gateway_pool.send_rpc`` takes a ``user_id`` kwarg by name,
    but the gateway pool is keyed by container owner. Always pass the
    owner_id there.
    """
    pc_uuid = paperclip_agent["id"]
    friendly_name = paperclip_agent.get("name") or pc_uuid

    create_params = {
        "name": pc_uuid,
        "workspace": f"/home/node/.openclaw/workspaces/{pc_uuid}",
    }
    if paperclip_agent.get("model"):
        create_params["model"] = paperclip_agent["model"]

    try:
        await gateway_pool.send_rpc(
            user_id=owner_id,  # gateway pool is keyed by owner_id
            req_id=f"pair-create-{secrets.token_hex(8)}",
            method="agents.create",
            params=create_params,
            ip=ip,
            token=token,
        )
    except RuntimeError as e:
        # OpenClaw returns INVALID_REQUEST with "already exists" when the
        # agent is registered. Treat as idempotent success.
        if "already exists" in str(e):
            logger.info(
                "pair_with_openclaw: agent %s already exists in container "
                "for owner %s — skipping update",
                pc_uuid,
                owner_id,
            )
            return
        raise

    await gateway_pool.send_rpc(
        user_id=owner_id,  # gateway pool is keyed by owner_id
        req_id=f"pair-update-{secrets.token_hex(8)}",
        method="agents.update",
        params={
            "agentId": pc_uuid,
            "name": friendly_name,
        },
        ip=ip,
        token=token,
    )
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_openclaw_pairing.py -v`
Expected: PASS — five tests.

- [ ] **Step 4.5: Commit**

```bash
git add apps/backend/core/services/paperclip_openclaw_pairing.py apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py
git commit -m "feat(paperclip): add pair_with_openclaw helper

Sends agents.create with name=<paperclip_uuid> then agents.update with
the friendly display name. Idempotent: 'already exists' from
agents.create is treated as success."
```

---

## Task 5: Wire `fetch_paperclip_agents_for_user` into `write_user_configs`

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py:1502-1549`

- [ ] **Step 5.1: Read the current `write_user_configs` and identify its callers**

Read `apps/backend/core/containers/ecs_manager.py:1502-1549` and run:

```bash
grep -n "write_user_configs" apps/backend/core/containers/ecs_manager.py
```

Expected callers: `provision_user_container` (line 1129 and 1316). Both are paths that need the new behavior.

- [ ] **Step 5.2: Add an integration test that provision writes Paperclip-paired agents.list**

Create `apps/backend/tests/integration/test_pairing_flow.py`:

```python
"""Integration test for the paperclip ↔ openclaw pairing flow.

Exercises the boundary where ECS-manager fetches Paperclip's live agent
list and writes it into openclaw.json. Uses tempfile-backed EFS, so no
ECS / AWS calls.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.config import write_openclaw_config


@pytest.mark.asyncio
async def test_provision_writes_paperclip_agents_into_config():
    """End-to-end: write_user_configs (via write_openclaw_config) emits
    openclaw.json with agents.list keyed by Paperclip UUIDs."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    paperclip_agents = [{"id": pc_uuid, "name": "Main Agent", "role": "ceo"}]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "openclaw.json"
        await write_openclaw_config(
            config_path=path,
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            user_id="user_abc",
            paperclip_agents=paperclip_agents,
        )
        cfg = json.loads(path.read_text())
        agents = cfg["agents"]["list"]
        assert agents[0]["id"] == pc_uuid
        assert agents[0]["default"] is True
        assert agents[0]["workspace"] == f"/home/node/.openclaw/workspaces/{pc_uuid}"
        # Sanity: the literal "main" agent is gone.
        assert all(a["id"] != "main" for a in agents)
```

- [ ] **Step 5.3: Run the integration test (it should already pass since Task 2 wired the param through)**

Run: `cd apps/backend && uv run pytest tests/integration/test_pairing_flow.py -v`
Expected: PASS.

- [ ] **Step 5.4: Update `write_user_configs` to fetch Paperclip agents**

Edit `apps/backend/core/containers/ecs_manager.py:1502-1549`. The signature stays as `user_id` (existing callers pass owner_id under that name — preserve the API surface), but treat it as owner_id internally:

```python
    async def write_user_configs(
        self,
        user_id: str,
        gateway_token: str,
        *,
        provider_choice: str,
        byo_provider: str | None = None,
    ) -> None:
        """Write OpenClaw config files + pre-paired device trust store to EFS.

        Now also reads the owner's live Paperclip agent list and writes
        one entry per agent into openclaw.json's agents.list (keyed by
        Paperclip UUID). If the owner has no Paperclip company yet or
        Paperclip is unreachable, falls back to the legacy 'main' agent
        so provision still succeeds.

        ``user_id`` parameter is the container OWNER (org_id for orgs,
        user_id for personal users) — preserved name from the existing
        API surface; pre-flat-fee containers were per-user, but post-
        cutover everything is per-owner.
        """
        # Treat the inbound parameter as owner_id internally. Naming
        # mismatch with the API surface is intentional — preserves
        # callers but makes the multi-tenant intent clear.
        owner_id = user_id

        # Lazy imports to avoid pulling these into module load (they
        # transitively touch httpx + DDB clients we don't want at import
        # time for unit tests).
        from core.repositories.paperclip_repo import PaperclipRepo
        from core.services.paperclip_admin_client import PaperclipAdminClient
        from core.services.paperclip_openclaw_pairing import (
            fetch_paperclip_agents_for_owner,
        )

        repo = PaperclipRepo()
        admin_client = PaperclipAdminClient()
        try:
            paperclip_agents = await fetch_paperclip_agents_for_owner(
                owner_id=owner_id,
                repo=repo,
                admin_client=admin_client,
            )
        finally:
            await admin_client.aclose()

        config_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "openclaw.json"
        await write_openclaw_config(
            config_path=config_path,
            gateway_token=gateway_token,
            provider_choice=provider_choice,
            user_id=owner_id,
            byo_provider=byo_provider,
            paperclip_agents=paperclip_agents,
        )
        workspace = get_workspace()
        workspace.write_file(owner_id, ".mcporter/mcporter.json", write_mcporter_config())
        await self.ensure_device_identities(owner_id, gateway_token)
```

> **Note for the engineer:** verify that `PaperclipAdminClient()` constructor + `aclose()` exist as shown. If the existing module uses a context manager pattern instead, adapt the call. Read `apps/backend/core/services/paperclip_admin_client.py` to confirm.

- [ ] **Step 5.5: Run the existing ecs_manager tests to confirm nothing broke**

Run: `cd apps/backend && uv run pytest tests/unit/containers/ -v`
Expected: PASS for all existing tests. New tests added in Task 1+2 also pass.

- [ ] **Step 5.6: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/integration/test_pairing_flow.py
git commit -m "feat(containers): fetch Paperclip agent list at provision time

write_user_configs now calls fetch_paperclip_agents_for_user and
threads the result into write_openclaw_config. openclaw.json's
agents.list is keyed by Paperclip UUIDs so /chat and /teams operate on
the same agent population. Empty list (Paperclip down or unprovisioned)
falls back to the legacy 'main' agent."
```

---

## Task 6: Forward-going `/teams` agent creation calls `pair_with_openclaw`

**Files:**
- Modify: `apps/backend/routers/teams/agents.py:233-259`

- [ ] **Step 6.1: Read the existing `/teams` create handler**

Read `apps/backend/routers/teams/agents.py:233-259`. Note how it calls Paperclip's `create_agent` and what it returns.

- [ ] **Step 6.2: Write a test for the new behavior**

Create `apps/backend/tests/unit/routers/test_teams_agents_pairing.py`:

```python
"""Tests for /teams agent create → openclaw pairing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_teams_create_agent_pairs_with_openclaw_when_container_up():
    """After Paperclip create_agent succeeds, the route calls
    pair_with_openclaw with the new agent's UUID."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    paperclip_agent = {
        "id": pc_uuid,
        "name": "Code Reviewer",
        "role": "engineer",
    }

    with patch(
        "routers.teams.agents._admin"
    ) as admin, patch(
        "routers.teams.agents.pair_with_openclaw",
        new=AsyncMock(),
    ) as pair, patch(
        "routers.teams.agents._container_is_reachable",
        new=AsyncMock(return_value=(True, "10.0.0.5", "gw_token")),
    ):
        admin.return_value.create_agent = AsyncMock(return_value=paperclip_agent)

        # Driver: invoke the route's create_agent function with stub ctx.
        # Exact wiring depends on the route's TeamsContext shape — engineer
        # adapts to match the existing test harness in this file's directory.
        from routers.teams.agents import create_agent, CreateAgentBody

        ctx = MagicMock()
        ctx.auth.user_id = "user_abc"
        ctx.auth.org_id = "org_xyz"
        ctx.company_id = "co_xyz"

        body = CreateAgentBody(name="Code Reviewer", role="engineer")
        result = await create_agent(body, ctx)

        pair.assert_awaited_once()
        kwargs = pair.await_args.kwargs
        assert kwargs["paperclip_agent"]["id"] == pc_uuid


@pytest.mark.asyncio
async def test_teams_create_agent_skips_pairing_when_container_down():
    """If the container isn't reachable, /teams create still succeeds —
    pairing is deferred to the next container provision (which will
    pick up the new agent via list_agents)."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    with patch(
        "routers.teams.agents._admin"
    ) as admin, patch(
        "routers.teams.agents.pair_with_openclaw",
        new=AsyncMock(),
    ) as pair, patch(
        "routers.teams.agents._container_is_reachable",
        new=AsyncMock(return_value=(False, None, None)),
    ):
        admin.return_value.create_agent = AsyncMock(
            return_value={"id": pc_uuid, "name": "Code Reviewer", "role": "engineer"}
        )

        from routers.teams.agents import create_agent, CreateAgentBody

        ctx = MagicMock()
        ctx.auth.user_id = "user_abc"
        body = CreateAgentBody(name="Code Reviewer", role="engineer")
        await create_agent(body, ctx)

        pair.assert_not_awaited()
```

- [ ] **Step 6.3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_teams_agents_pairing.py -v`
Expected: FAIL — `_container_is_reachable` and `pair_with_openclaw` aren't imported in the route yet.

- [ ] **Step 6.4: Add a `_container_is_reachable` helper to `routers/teams/agents.py` and call `pair_with_openclaw`**

Edit `apps/backend/routers/teams/agents.py`. Add imports near the top:

```python
from core.services.paperclip_openclaw_pairing import pair_with_openclaw
```

Add the helper above the `create_agent` route (around line 220). Note that the container is keyed by **owner_id** (org_id for orgs, user_id for personal users) — for an org member, the owner_id is `ctx.auth.org_id`, not their own user_id:

```python
def _resolve_owner_id(ctx: TeamsContext) -> str:
    """Container owner: org_id when the user is in an org, else user_id."""
    return getattr(ctx.auth, "org_id", None) or ctx.auth.user_id


async def _container_is_reachable(owner_id: str) -> tuple[bool, str | None, str | None]:
    """Return (reachable, ip, gateway_token) for the owner's container.

    Reachable means: container row exists, has an IP, and has a gateway
    token. We don't ping the container — provisioning may still be in
    progress, in which case pair_with_openclaw will fail gracefully and
    the next reprovision will pick up the agent via list_agents.
    """
    from core.repositories.container_repo import get_by_owner_id

    row = await get_by_owner_id(owner_id)
    if row is None:
        return False, None, None
    ip = row.get("private_ip")
    token = row.get("gateway_token")
    if not ip or not token:
        return False, None, None
    return True, ip, token
```

Modify the `create_agent` route (line 233 onward):

```python
@router.post("/agents", response_model=AgentResponse)
async def create_agent(body: CreateAgentBody, ctx: TeamsContext = Depends(_ctx)):
    """Create a Paperclip agent + pair with the owner's OpenClaw container.

    Spec §5 security invariant: the ``adapter_type`` + ``adapter_config``
    are SYNTHESIZED server-side from the authenticated user's identity
    using ``synthesize_openclaw_adapter``. The client body never carries
    them.

    The Paperclip agent's adapterConfig.sessionKey is the CREATOR's
    user_id (post-Task 0.5, this is the fallback for wakes without an
    issueId). Issue-driven wakes use paperclip:issue:<id> as the
    session key automatically.

    After Paperclip create succeeds, we call pair_with_openclaw to
    register the new agent in the OWNER's running container (which may
    be the org's container if the creator is a member of an org) so
    /chat sees the same agent. If the container isn't reachable,
    pairing is deferred — the next container provision picks the
    agent up via list_agents.
    """
    owner_id = _resolve_owner_id(ctx)

    # adapterConfig.sessionKey = creator's user_id, NOT owner_id. This
    # is the per-creator fallback for manual wakes; per-issue runs use
    # paperclip:issue:<id> automatically.
    adapter_config = synthesize_openclaw_adapter(
        gateway_url=_ws_gateway_url(),
        service_token=await _service_token_for(ctx),
        user_id=ctx.auth.user_id,
    )
    paperclip_agent = await _admin().create_agent(
        company_id=ctx.company_id,
        name=body.name,
        role=body.role,
        adapter_type=OPENCLAW_GATEWAY_TYPE,
        adapter_config=adapter_config,
        session_cookie=await _user_session(ctx),
        idempotency_key=f"{ctx.auth.user_id}:agent:{body.name}",
    )

    reachable, ip, token = await _container_is_reachable(owner_id)
    if reachable and ip and token:
        try:
            await pair_with_openclaw(
                owner_id=owner_id,
                paperclip_agent=paperclip_agent,
                gateway_pool=get_gateway_pool(),
                ip=ip,
                token=token,
            )
        except Exception as exc:
            # Pairing failure is non-fatal: the agent exists in
            # Paperclip and will be picked up by next reprovision.
            logger.warning(
                "teams.create_agent: pair_with_openclaw failed for "
                "owner=%s agent=%s — deferring to next provision: %s",
                owner_id,
                paperclip_agent["id"],
                exc,
            )

    return _redact(paperclip_agent)
```

> **Note for the engineer:** the existing `create_agent` may already have a different body. Read it and apply the diff carefully — only the `pair_with_openclaw` block and the `owner_id` computation are new. Preserve any redaction or validation that's already in place.

- [ ] **Step 6.5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_teams_agents_pairing.py -v`
Expected: PASS.

- [ ] **Step 6.6: Commit**

```bash
git add apps/backend/routers/teams/agents.py apps/backend/tests/unit/routers/test_teams_agents_pairing.py
git commit -m "feat(teams): pair new Paperclip agents with OpenClaw container

After /teams agent create succeeds, call pair_with_openclaw if the
container is reachable. Failure is non-fatal — next reprovision picks
up the agent via list_agents."
```

---

## Task 7: Forward-going `/chat` agents.create RPC interception

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py:540-557` (the `agents.delete` interception zone — extend with `agents.create`)

- [ ] **Step 7.1: Locate the RPC interception block and read its surrounding code**

Read `apps/backend/routers/websocket_chat.py` around line 540 to understand the existing `agents.delete` interception. The new `agents.create` interception will live alongside it — but BEFORE the gateway forwards the RPC, not after.

- [ ] **Step 7.2: Write a test for `/chat` create interception**

Create `apps/backend/tests/unit/routers/test_websocket_chat_pairing.py`:

```python
"""Tests for /chat agents.create → Paperclip-first interception."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_chat_agents_create_calls_paperclip_first_then_pairs():
    """When the frontend sends agents.create over WS, the backend
    1. creates a Paperclip agent for the authenticated user (member)
    2. pairs it with the OWNER's container (org's container if the
       member is in an org, else the personal container)
    3. agents.create + agents.update flow through pair_with_openclaw

    user_id = the authenticated member; owner_id = container key.
    They differ when the user is an org member."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    with patch(
        "routers.websocket_chat._paperclip_create_for_user",
        new=AsyncMock(return_value={"id": pc_uuid, "name": "Code Reviewer"}),
    ) as pc_create, patch(
        "routers.websocket_chat.pair_with_openclaw",
        new=AsyncMock(),
    ) as pair:
        from routers.websocket_chat import _intercept_agents_create

        forwarded_method, forwarded_params = await _intercept_agents_create(
            user_id="user_member_b",     # authenticated member
            owner_id="org_xyz",           # org's container
            params={"name": "Code Reviewer", "role": "engineer"},
            ip="10.0.0.5",
            token="gw_token",
        )

        pc_create.assert_awaited_once()
        # Pairing targets the OWNER's container, not the member's user_id.
        pair.assert_awaited_once()
        assert pair.await_args.kwargs["owner_id"] == "org_xyz"
        # The original agents.create RPC is consumed by the interceptor.
        assert forwarded_method is None
```

- [ ] **Step 7.3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat_pairing.py -v`
Expected: FAIL — `_intercept_agents_create` doesn't exist.

- [ ] **Step 7.4: Add the interception function and wire it into the dispatcher**

Edit `apps/backend/routers/websocket_chat.py`. Add imports near the top (with the other `from core.services` imports):

```python
from core.services.paperclip_openclaw_pairing import pair_with_openclaw
```

Add the interception helper above the existing `agents.delete` block (around line 540). Note: `_paperclip_create_for_user` looks up the company by **owner_id**, since members of an org share the company with the org. Use the same `_resolve_company_id_for_owner` policy as the pairing helper:

```python
async def _paperclip_create_for_chat(
    *,
    creator_user_id: str,
    owner_id: str,
    name: str,
    role: str | None,
    model: str | None,
) -> dict:
    """Create a Paperclip agent invoked from /chat WS RPC interception.

    creator_user_id: the authenticated member triggering the create;
        baked into adapterConfig.sessionKey as the manual-wake fallback.
    owner_id: container owner (org_id for orgs, user_id for personal);
        used to look up the shared Paperclip company.
    """
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.paperclip_adapter_config import (
        OPENCLAW_GATEWAY_TYPE,
        synthesize_openclaw_adapter,
    )
    from core.services.paperclip_openclaw_pairing import (
        _resolve_company_id_for_owner,
    )
    from core.services import service_token

    repo = PaperclipRepo()
    company_id = await _resolve_company_id_for_owner(
        owner_id=owner_id, repo=repo
    )
    if company_id is None:
        raise RuntimeError(
            f"owner {owner_id} has no Paperclip company — agent create "
            "blocked. Re-run org provisioning."
        )

    admin_client = PaperclipAdminClient()
    try:
        admin_cookie = await get_admin_session_cookie(admin_client._http)
        # Reuse the existing gateway-URL resolver from
        # paperclip_provisioning.py to keep the env-derived hostname
        # consistent across all callers.
        from core.services.paperclip_provisioning import _ws_gateway_url
        from core.config import settings as _settings

        adapter_config = synthesize_openclaw_adapter(
            gateway_url=_ws_gateway_url(_settings.ENVIRONMENT or ""),
            service_token=service_token.mint(creator_user_id),
            user_id=creator_user_id,
        )
        return await admin_client.create_agent(
            company_id=company_id,
            name=name,
            role=role or "general",
            adapter_type=OPENCLAW_GATEWAY_TYPE,
            adapter_config=adapter_config,
            session_cookie=admin_cookie,
            idempotency_key=f"{creator_user_id}:chat-agent:{name}",
        )
    finally:
        await admin_client.aclose()


async def _intercept_agents_create(
    *,
    user_id: str,
    owner_id: str,
    params: dict,
    ip: str,
    token: str,
) -> tuple[str | None, dict | None]:
    """Intercept /chat's agents.create RPC.

    Performs the Paperclip-first pairing flow:
    1. Create a Paperclip agent (under the owner's company), getting
       a UUID.
    2. Call pair_with_openclaw to register it in the OWNER's container.

    Returns (None, None) to signal the dispatcher that the original
    RPC has been fully handled and should NOT be forwarded.
    Returns (method, params) tuple if forwarding is still needed
    (e.g. the create call had no name — let OpenClaw reject).

    user_id is the authenticated member (creator); owner_id is the
    container owner — these differ for org members.
    """
    name = (params or {}).get("name", "").strip()
    role = (params or {}).get("role")
    model = (params or {}).get("model")
    if not name:
        # Let OpenClaw's validator reject — we don't duplicate validation.
        return "agents.create", params

    paperclip_agent = await _paperclip_create_for_chat(
        creator_user_id=user_id,
        owner_id=owner_id,
        name=name,
        role=role,
        model=model,
    )

    # pair_with_openclaw issues both agents.create and agents.update RPCs
    # to the gateway, registering the agent under its Paperclip UUID
    # with the friendly display name.
    from core.containers import get_gateway_pool
    await pair_with_openclaw(
        owner_id=owner_id,
        paperclip_agent=paperclip_agent,
        gateway_pool=get_gateway_pool(),
        ip=ip,
        token=token,
    )
    return None, None
```

Then modify the existing dispatcher (the block around line 543) to call the interception BEFORE forwarding. The dispatcher already has both `user_id` (member) and `owner_id` (container key) in scope. Add an `agents.create` branch ahead of where the RPC is forwarded:

```python
if method == "agents.create":
    forward_method, forward_params = await _intercept_agents_create(
        user_id=user_id,
        owner_id=owner_id,
        params=params,
        ip=ip,
        token=token,
    )
    if forward_method is None:
        # Interception handled the call end-to-end; respond to the
        # frontend with the new agent's metadata and skip gateway forward.
        management_api.send_message(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": True,
                "result": {"agentId": params.get("name")},
            },
        )
        return
    # Else fall through with the (possibly-rewritten) method/params
    method, params = forward_method, forward_params
```

> **Note for the engineer:** the exact placement depends on the existing dispatcher's shape. Look for where `gateway_pool.send_rpc(...)` is called — the interception must happen BEFORE that line. Read 30 lines above and below `if method == "agents.delete":` for the right anchor. Confirm both `user_id` and `owner_id` are in scope at that point; the existing handler already computes `owner_id` for the `last_handshake` lookup.

- [ ] **Step 7.5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat_pairing.py -v`
Expected: PASS.

- [ ] **Step 7.6: Run all router tests to confirm nothing else broke**

Run: `cd apps/backend && uv run pytest tests/unit/routers/ -v`
Expected: PASS.

- [ ] **Step 7.7: Commit**

```bash
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_chat_pairing.py
git commit -m "feat(chat): intercept agents.create RPC for Paperclip-first pairing

When /chat sends agents.create over WS, the backend now creates a
Paperclip agent first to mint a UUID, then pairs it with the
container via pair_with_openclaw. The original RPC is consumed —
the frontend receives the new agentId via the management API
response."
```

---

## Task 8: End-to-end verification

**Files:**
- Modify: `apps/backend/tests/integration/test_pairing_flow.py` (extend)

- [ ] **Step 8.1: Add an end-to-end test that simulates the full flow**

Append to `apps/backend/tests/integration/test_pairing_flow.py`:

```python
@pytest.mark.asyncio
async def test_provision_then_chat_create_yields_single_uuid_per_agent():
    """End-to-end: after provisioning with one Paperclip Main Agent and
    creating a second agent via /chat, the openclaw.json reflects both
    agents keyed by Paperclip UUIDs — not OpenClaw-internal UUIDs."""
    main_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    second_uuid = "11111111-1111-1111-1111-111111111111"

    # Phase 1: provision-time write picks up Main Agent.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "openclaw.json"
        await write_openclaw_config(
            config_path=path,
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            user_id="user_abc",
            paperclip_agents=[
                {"id": main_uuid, "name": "Main Agent", "role": "ceo"},
            ],
        )
        cfg1 = json.loads(path.read_text())
        assert [a["id"] for a in cfg1["agents"]["list"]] == [main_uuid]

        # Phase 2: simulating that the second agent was created via
        # /chat and registered in the container via agents.create RPC.
        # On next reprovision, list_agents would now return both.
        await write_openclaw_config(
            config_path=path,
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            user_id="user_abc",
            paperclip_agents=[
                {"id": main_uuid, "name": "Main Agent", "role": "ceo"},
                {"id": second_uuid, "name": "Code Reviewer", "role": "engineer"},
            ],
        )
        cfg2 = json.loads(path.read_text())
        assert sorted(a["id"] for a in cfg2["agents"]["list"]) == sorted(
            [main_uuid, second_uuid]
        )
        # Default still on the CEO.
        defaults = [a for a in cfg2["agents"]["list"] if a.get("default")]
        assert len(defaults) == 1
        assert defaults[0]["id"] == main_uuid
```

- [ ] **Step 8.2: Run the integration suite**

Run: `cd apps/backend && uv run pytest tests/integration/test_pairing_flow.py -v`
Expected: PASS.

- [ ] **Step 8.3: Run the full backend test suite**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: PASS — all unit + integration tests, including ones we didn't touch.

- [ ] **Step 8.4: Commit**

```bash
git add apps/backend/tests/integration/test_pairing_flow.py
git commit -m "test(pairing): end-to-end provision + reprovision flow

Asserts openclaw.json reflects the live Paperclip agent list with
exactly one default (the CEO) across both initial provision and
reprovision after a /chat-initiated create."
```

---

## Task 9: Documentation update

**Files:**
- Modify: `CLAUDE.md`
- Create: `docs/architecture/2026-05-07-paperclip-openclaw-pairing.md`

- [ ] **Step 9.1: Add a CLAUDE.md section under the existing `## Backend` block**

Find the section in `CLAUDE.md` describing the Teams BFF and Paperclip integration. Add a new subsection:

```markdown
### Paperclip ↔ OpenClaw agent pairing

Paperclip is the source of truth for agent identity. Every Paperclip agent
has a UUID; that UUID is also the OpenClaw `agentId` in the user's
container — `agents.list[].id`, the workspace dir, the agent dir, and the
session-key prefix all share the same string.

Provisioning order:

1. Sign-up → `provision_org` creates the Paperclip company + seeds a
   "Main Agent" (role=ceo). Paperclip auto-generates the UUID.
2. User opens `/chat` → container provisions → `write_user_configs` calls
   `fetch_paperclip_agents_for_user` → live agent list from Paperclip's
   REST API → written into `openclaw.json`'s `agents.list`.
3. Container boots with all known Paperclip agents pre-registered.

Forward-going creates:

- `/teams` POST `/agents`: Paperclip create_agent first, then
  `pair_with_openclaw` if the container is reachable.
- `/chat` `agents.create` RPC: backend interception calls Paperclip
  first, then forwards `agents.create` (with name=UUID) and
  `agents.update` (with friendly name) to the container.

Reprovision (`PATCH /debug/provision`) re-fetches the live Paperclip list
and rewrites `agents.list`. Stale workspaces on EFS are not GC'd by this
flow — handled separately.
```

- [ ] **Step 9.2: Create an architecture doc capturing the contract**

Create `docs/architecture/2026-05-07-paperclip-openclaw-pairing.md` with the design rationale (this can largely be the design discussion from the planning conversation, condensed). Aim for ~200 lines covering: provenance of `main` in OpenClaw upstream, why we chose Paperclip-as-ID-authority, the consistency surfaces (agentId, workspace, agent dir, session key), what the runtime flow looks like, and what's deliberately out of scope (backfill, deletion sync).

- [ ] **Step 9.3: Commit**

```bash
git add CLAUDE.md docs/architecture/2026-05-07-paperclip-openclaw-pairing.md
git commit -m "docs: explain paperclip ↔ openclaw agent pairing"
```

---

## Task 10: Manual smoke test on dev

**Files:** None (operational verification).

- [ ] **Step 10.1: Push the branch and open a PR**

```bash
git push -u origin feat/paperclip-openclaw-agent-pairing
gh pr create --title "feat: paperclip ↔ openclaw agent pairing" --body "$(cat <<'EOF'
## Summary
- Replaces hardcoded "main" agent in openclaw.json with one entry per Paperclip agent
- Paperclip UUIDs become the OpenClaw agentId, workspace path, and session-key prefix
- Forward-going /teams and /chat creates do Paperclip-first → OpenClaw pair

## Test plan
- [ ] Backend unit + integration tests pass
- [ ] Dev provision: open /chat as a fresh user, verify openclaw.json on EFS lists the Paperclip Main Agent UUID
- [ ] Assign an issue to "Main Agent" in /teams, verify the run lands in `agents/<paperclip_uuid>/agent/` not `agents/main/agent/`
- [ ] Create a new agent via /chat, verify it appears in /teams Inbox under the same UUID
- [ ] Create a new agent via /teams, open /chat, verify it appears in the agent list

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 10.2: Wait for CI**

Run: `gh run watch <run-id> --repo Isol8AI/isol8 --exit-status`
Expected: All checks pass.

- [ ] **Step 10.3: Smoke test on dev**

After deployment to dev, walk through the test plan above. The critical signal: assigning an issue to a Paperclip agent results in workspace files appearing under `agents/<paperclip_uuid>/` on EFS, not under `agents/main/`.

If the smoke test passes, request review and merge.

---

## Task 11: Migrate existing Paperclip agents to `sessionKeyStrategy="issue"`

**Files:**
- Create: `apps/backend/scripts/migrate_paperclip_session_strategy.py`
- Test: `apps/backend/tests/unit/scripts/test_migrate_paperclip_session_strategy.py` (new)

**Why:** Task 0.5 changes the strategy from `"fixed"` to `"issue"` only for **newly-created** Paperclip agents. Existing agents on dev/prod (the seeded Main Agents and any user-created ones) still have `"fixed"` baked into their `adapterConfig` in Aurora. Until they're migrated, those agents continue to collapse all issues into one session.

This is a one-shot admin script, not part of the request flow. Run it once on dev after deploying Tasks 0.5 + 1-10, verify, then run on prod.

- [ ] **Step 11.1: Write test for the migration script's per-agent logic**

Create `apps/backend/tests/unit/scripts/test_migrate_paperclip_session_strategy.py`:

```python
"""Unit tests for the session-strategy migration script."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.migrate_paperclip_session_strategy import (
    migrate_one_agent,
    needs_migration,
)


def test_needs_migration_true_for_fixed_strategy():
    assert needs_migration(
        {"adapterConfig": {"sessionKeyStrategy": "fixed", "sessionKey": "user_x"}}
    )


def test_needs_migration_false_for_issue_strategy():
    assert not needs_migration(
        {"adapterConfig": {"sessionKeyStrategy": "issue", "sessionKey": "user_x"}}
    )


def test_needs_migration_false_when_adapter_config_missing():
    """Non-openclaw_gateway agents (e.g., upstream openclaw-cli) skip."""
    assert not needs_migration({"adapterConfig": {}})
    assert not needs_migration({})


@pytest.mark.asyncio
async def test_migrate_one_agent_calls_update_with_issue_strategy():
    admin_client = MagicMock()
    admin_client.update_agent = AsyncMock()
    admin_cookie = "cookie"

    agent = {
        "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "companyId": "co_xyz",
        "adapterConfig": {
            "url": "wss://ws.isol8.co",
            "authToken": "t" * 100,
            "sessionKeyStrategy": "fixed",
            "sessionKey": "user_abc",
        },
    }

    await migrate_one_agent(
        admin_client=admin_client,
        admin_cookie=admin_cookie,
        agent=agent,
    )

    admin_client.update_agent.assert_awaited_once()
    kwargs = admin_client.update_agent.await_args.kwargs
    assert kwargs["agent_id"] == agent["id"]
    new_cfg = kwargs["adapter_config"]
    assert new_cfg["sessionKeyStrategy"] == "issue"
    # Other fields preserved verbatim.
    assert new_cfg["sessionKey"] == "user_abc"
    assert new_cfg["url"] == "wss://ws.isol8.co"
    assert new_cfg["authToken"] == "t" * 100
```

- [ ] **Step 11.2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_migrate_paperclip_session_strategy.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 11.3: Implement the migration script**

Create `apps/backend/scripts/migrate_paperclip_session_strategy.py`:

```python
"""One-shot migration: switch Paperclip agents from sessionKeyStrategy
'fixed' to 'issue'.

Run this once per environment (dev, then prod) after deploying the
agent-pairing PR. Iterates every PaperclipCompany row in DDB,
list_agents per company, and update_agent on those whose
adapterConfig.sessionKeyStrategy is "fixed".

Idempotent: agents already on "issue" are skipped. Safe to re-run.

Usage:
    cd apps/backend
    uv run python -m scripts.migrate_paperclip_session_strategy --dry-run
    uv run python -m scripts.migrate_paperclip_session_strategy --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger("migrate_paperclip_session_strategy")


def needs_migration(agent: dict[str, Any]) -> bool:
    """True if this agent has sessionKeyStrategy='fixed' and needs to
    be flipped to 'issue'. False for already-migrated agents and for
    non-openclaw_gateway adapter types (which don't have this field)."""
    cfg = agent.get("adapterConfig") or {}
    return cfg.get("sessionKeyStrategy") == "fixed"


async def migrate_one_agent(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    agent: dict[str, Any],
) -> None:
    """Apply the strategy flip to a single agent. Preserves all other
    adapterConfig fields (url, authToken, sessionKey)."""
    new_cfg = dict(agent["adapterConfig"])
    new_cfg["sessionKeyStrategy"] = "issue"
    await admin_client.update_agent(
        agent_id=agent["id"],
        adapter_config=new_cfg,
        session_cookie=admin_cookie,
    )


async def _migrate_company(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    company_id: str,
    apply: bool,
) -> tuple[int, int]:
    """Returns (matched, migrated)."""
    agents = await admin_client.list_agents(
        session_cookie=admin_cookie,
        company_id=company_id,
    )
    matched = 0
    migrated = 0
    for agent in agents:
        if not needs_migration(agent):
            continue
        matched += 1
        logger.info(
            "%s %s/%s (%s)",
            "MIGRATE" if apply else "DRY-RUN",
            company_id,
            agent["id"],
            agent.get("name", "?"),
        )
        if apply:
            try:
                await migrate_one_agent(
                    admin_client=admin_client,
                    admin_cookie=admin_cookie,
                    agent=agent,
                )
                migrated += 1
            except PaperclipApiError as e:
                logger.error(
                    "FAILED %s/%s status=%s: %s",
                    company_id,
                    agent["id"],
                    e.status_code,
                    e,
                )
    return matched, migrated


async def main(apply: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = PaperclipRepo()
    admin_client = PaperclipAdminClient()
    try:
        admin_cookie = await get_admin_session_cookie(admin_client._http)
        rows = await repo.scan_all()  # iterator over PaperclipCompany rows

        seen_companies: set[str] = set()
        total_matched = 0
        total_migrated = 0
        async for row in rows:
            if row.company_id in seen_companies:
                continue
            seen_companies.add(row.company_id)
            matched, migrated = await _migrate_company(
                admin_client=admin_client,
                admin_cookie=admin_cookie,
                company_id=row.company_id,
                apply=apply,
            )
            total_matched += matched
            total_migrated += migrated

        logger.info(
            "DONE: companies=%d matched=%d migrated=%d (apply=%s)",
            len(seen_companies),
            total_matched,
            total_migrated,
            apply,
        )
    finally:
        await admin_client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
```

> **Note for the engineer:** verify `PaperclipRepo.scan_all()` exists. If not, use whatever scan helper the repo provides. If `update_agent` on the admin client doesn't exist, add it as a thin wrapper over `PATCH /api/agents/:id` first (mirror the pattern of `create_agent`).

- [ ] **Step 11.4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_migrate_paperclip_session_strategy.py -v`
Expected: PASS — all four tests.

- [ ] **Step 11.5: Dry-run on dev**

After this PR is deployed to dev, run:

```bash
cd apps/backend
uv run python -m scripts.migrate_paperclip_session_strategy --dry-run
```

Inspect the log output. Expected: a list of agents (likely seeded Main Agents for every dev account) with `MIGRATE` lines.

- [ ] **Step 11.6: Apply on dev, verify, then prod**

```bash
# Dev
uv run python -m scripts.migrate_paperclip_session_strategy --apply

# Smoke test: assign two issues to the same agent on dev, verify each
# gets its own session in OpenClaw transcripts.

# Prod (only after dev is green)
uv run python -m scripts.migrate_paperclip_session_strategy --apply
```

- [ ] **Step 11.7: Commit the migration script**

```bash
git add apps/backend/scripts/migrate_paperclip_session_strategy.py apps/backend/tests/unit/scripts/test_migrate_paperclip_session_strategy.py
git commit -m "feat(paperclip): one-shot migration to sessionKeyStrategy='issue'

Iterates every PaperclipCompany in DDB, list_agents per company,
update_agent on those whose adapterConfig.sessionKeyStrategy is 'fixed'.
Idempotent. Run --dry-run first on each environment, then --apply."
```

---

## Out-of-scope follow-ups

These are tracked here so we don't lose them, but are NOT part of this PR:

1. **Backfill of existing OpenClaw-only agents.** Once this lands and is verified on dev, a separate one-shot admin script walks each owner's `agents/<oc_uuid>/` dir, creates a matching Paperclip agent, and either renames the dir or recreates the agent. Decision pending: rename (preserve session history) or recreate (clean slate).
2. **Deletion sync from Paperclip → OpenClaw.** Today, `/chat` agent delete is intercepted at `routers/websocket_chat.py:543`. Paperclip-initiated deletes don't propagate. Symmetric to this PR's create flow.
3. **Adapter config refresh on token rotation.** When an agent's creator's service token rotates, the agent's `adapterConfig.authToken` becomes stale. Existing token-rotation path may already handle it; verify.
4. **Removing the legacy `"main"` agent fallback.** Once every owner has at least one Paperclip agent (Main Agent seeded by `provision_org`), the fallback in `_build_agents_list` becomes dead code. Drop it after a quarter of clean operation.

---
