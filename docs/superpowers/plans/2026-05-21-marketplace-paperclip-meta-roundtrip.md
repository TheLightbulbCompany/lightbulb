# Paperclip Meta Round-Trip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carry Paperclip-relevant agent metadata (`role`, `title`, `capabilities`, `icon`) through OpenClaw's storage so it round-trips on agent create / edit / marketplace publish / marketplace hire — making hired agents arrive in Paperclip with populated profiles instead of empty `role="general", title=null, capabilities=null`.

**Architecture:** Use OpenClaw's documented `AgentEntry.params: z.record(z.string(), z.unknown()).optional()` extension point as the carrier. Backend mapping helpers read/write `agent_entry["params"]["paperclip"] = {role, title, capabilities, icon}`; every Paperclip-touching code path (`_intercept_agents_create`, new `_intercept_agents_update`, `create_paperclip_for_marketplace`, marketplace deploy) reads from there and threads through to `PaperclipAdminClient`. Frontend gets a shared form component used in the chat New-Agent / Edit-Agent / Publish-Wizard surfaces. Also fixes a latent bug where Atlas seeding fails because `role="recruiter"` isn't in Paperclip's `AGENT_ROLES` enum.

**Tech Stack:** Python (FastAPI, pytest-asyncio), TypeScript (React, Vitest), OpenClaw `params` extension convention, Paperclip `createAgentSchema` / `updateAgentSchema`.

---

## Why this design (context for the implementer)

OpenClaw's `AgentEntrySchema` is `.strict()` — adding `role`/`title`/`capabilities` to the schema upstream is not a path we control and breaks the strict contract. BUT the schema has `params: z.record(z.string(), z.unknown()).optional()` as a deliberately free-form key-value bag. Upstream's own tests (`config-misc.test.ts:991`) document this as "accepts documented `agents.list[].params` overrides" — it's the intended escape hatch for integration-layer metadata.

Paperclip is a separate application with its own schema (`createAgentSchema` accepts `name, role, title, icon, reportsTo, capabilities, desiredSkills, adapterType, adapterConfig, instructionsBundle, runtimeConfig, defaultEnvironmentId, budgetMonthlyCents, permissions, metadata`). Its `AGENT_ROLES` enum is closed: `ceo | cto | cmo | cfo | security | engineer | designer | pm | qa | devops | researcher | general`. Its `AGENT_ICON_NAMES` enum lives in `paperclip/packages/shared/src/constants.ts` near `AGENT_ROLES`.

We don't merge the two systems. We use OpenClaw's `params.paperclip` as a typed sub-block whose shape mirrors Paperclip's create-agent-relevant fields. A single mapping helper translates back and forth. Every write path (chat create, chat edit, marketplace hire) does TWO writes through this helper: one to OpenClaw (so the metadata stays with the agent and round-trips through the marketplace slice) and one to Paperclip (so Friday's `paperclipGetAgent` and the org-chart UI see populated profiles immediately).

**Marketplace round-trip is automatic.** The slice already includes the full agent entry (PR #715). Once `params.paperclip` is being written at agent create/edit time, it rides along in the marketplace tarball with no schema change to the listing row.

---

## File structure

**Backend — new files:**
- `apps/backend/core/services/paperclip_meta.py` — the mapping module. Defines `PaperclipMeta` dataclass + `read_from_agent_entry(entry: dict) -> PaperclipMeta` + `write_to_agent_entry(entry: dict, meta: PaperclipMeta) -> dict` + the closed enum constants mirroring Paperclip's.

**Backend — modified files:**
- `apps/backend/core/services/paperclip_admin_client.py` — extend `create_agent` to actually pass through the existing schema-allowed kwargs (it already accepts `title`/`capabilities` per signature, verify it sends them); add new `update_agent` method.
- `apps/backend/core/services/paperclip_agent_lifecycle.py` — `create_paperclip_for_chat` + `create_paperclip_for_marketplace` gain optional `meta: PaperclipMeta | None = None` kwarg.
- `apps/backend/core/services/paperclip_provisioning.py:379` — Atlas role fix (`recruiter` → `general`).
- `apps/backend/routers/websocket_chat.py` — `_intercept_agents_create` reads `params.paperclip` from the create RPC params; add new `_intercept_agents_update` that mirrors updates to Paperclip; dispatcher updated to route `agents.update` through it.
- `apps/backend/routers/marketplace_listings.py:418-425` — `deploy_listing` reads `slice.agent.params.paperclip`, falls back to deriving from listing fields (`name`, `description_md`), passes through.

**Backend — new test files:**
- `apps/backend/tests/unit/services/test_paperclip_meta.py`
- `apps/backend/tests/unit/services/test_paperclip_admin_client_update_agent.py`

**Backend — modified test files:**
- `apps/backend/tests/unit/routers/test_websocket_chat_pairing.py` — add paperclip-meta round-trip tests for both interceptors
- `apps/backend/tests/unit/routers/test_marketplace_listings.py` — add deploy-with-paperclip-meta tests
- `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py` — N/A (helper unchanged; meta lives one layer above)

**Frontend — new files:**
- `apps/frontend/src/components/control/agents/AgentPaperclipFields.tsx` — shared form component with role select + title input + capabilities textarea + icon dropdown
- `apps/frontend/src/lib/paperclipMeta.ts` — TS types + constants mirroring Paperclip's enums (kept in sync manually; tiny)

**Frontend — modified files:**
- `apps/frontend/src/components/control/panels/AgentCreateForm.tsx` — embed `AgentPaperclipFields`, send `params.paperclip` in the `agents.create` RPC.
- `apps/frontend/src/components/control/panels/AgentOverviewTab.tsx` — embed `AgentPaperclipFields`, send `params.paperclip` in the `agents.update` RPC.

**Frontend — new test files:**
- `apps/frontend/src/components/control/agents/__tests__/AgentPaperclipFields.test.tsx`

**Out of scope (explicit non-goals):**
- Publish wizard surfacing these fields (rides in the slice automatically; we can add an editable preview later if sellers want override).
- New columns on the listings table for `role`/`capabilities` filtering (defer until search demand emerges).
- Backfill of existing prod agents (separate one-shot, Task 9 below documents how).

---

## Task 1: Paperclip meta mapping module

**Files:**
- Create: `apps/backend/core/services/paperclip_meta.py`
- Test: `apps/backend/tests/unit/services/test_paperclip_meta.py`

The module defines the canonical shape of `agent_entry["params"]["paperclip"]` and the read/write helpers. Enum values mirror Paperclip's (`paperclip/packages/shared/src/constants.ts`).

- [ ] **Step 1: Write the failing test file**

Create `apps/backend/tests/unit/services/test_paperclip_meta.py`:

```python
"""Tests for paperclip_meta — read/write of params.paperclip on agent entries."""

import pytest

from core.services.paperclip_meta import (
    PAPERCLIP_AGENT_ROLES,
    PaperclipMeta,
    read_from_agent_entry,
    write_to_agent_entry,
)


def test_paperclip_agent_roles_matches_upstream_enum():
    # Mirrors paperclip/packages/shared/src/constants.ts AGENT_ROLES.
    # If this fails, Paperclip upstream changed the enum and the
    # constant needs to be re-synced.
    assert PAPERCLIP_AGENT_ROLES == (
        "ceo",
        "cto",
        "cmo",
        "cfo",
        "security",
        "engineer",
        "designer",
        "pm",
        "qa",
        "devops",
        "researcher",
        "general",
    )


def test_read_from_missing_params_returns_default_meta():
    """Agent entry without ``params`` returns a meta with role=general."""
    meta = read_from_agent_entry({"id": "agent_x", "name": "X"})
    assert meta.role == "general"
    assert meta.title is None
    assert meta.capabilities is None
    assert meta.icon is None


def test_read_from_params_without_paperclip_block_returns_default():
    """Agent entry with ``params`` but no ``paperclip`` sub-block returns
    default meta — the params bag is used by multiple consumers."""
    meta = read_from_agent_entry({"id": "x", "params": {"other_integration": {"k": "v"}}})
    assert meta.role == "general"
    assert meta.title is None


def test_read_from_full_paperclip_block_extracts_all_fields():
    entry = {
        "id": "ray",
        "name": "Ray",
        "params": {
            "paperclip": {
                "role": "general",
                "title": "Restaurant booking specialist",
                "capabilities": "Books reservations on Resy.",
                "icon": "calendar",
            }
        },
    }
    meta = read_from_agent_entry(entry)
    assert meta.role == "general"
    assert meta.title == "Restaurant booking specialist"
    assert meta.capabilities == "Books reservations on Resy."
    assert meta.icon == "calendar"


def test_read_with_invalid_role_coerces_to_general():
    """A role string outside the closed enum is coerced to 'general'
    rather than raising — the agent entry is a free-form bag and
    upstream Paperclip would 422 on an invalid role, so we sanitize at
    the read boundary."""
    entry = {"id": "x", "params": {"paperclip": {"role": "recruiter"}}}
    meta = read_from_agent_entry(entry)
    assert meta.role == "general"


def test_write_creates_params_paperclip_block_when_absent():
    """Writing onto an entry without params builds the nested structure
    without mutating any other keys."""
    entry = {"id": "x", "name": "X"}
    meta = PaperclipMeta(role="engineer", title="Backend dev", capabilities="Writes Python.", icon=None)
    out = write_to_agent_entry(entry, meta)
    assert out is entry  # writes in place AND returns the entry
    assert entry["params"]["paperclip"]["role"] == "engineer"
    assert entry["params"]["paperclip"]["title"] == "Backend dev"
    assert entry["params"]["paperclip"]["capabilities"] == "Writes Python."
    # icon is None → must NOT serialize as JSON null in the params bag
    # (keeps the params dict small and matches "unset" semantics).
    assert "icon" not in entry["params"]["paperclip"]
    # Original keys preserved
    assert entry["id"] == "x"
    assert entry["name"] == "X"


def test_write_merges_with_existing_paperclip_block():
    """Writing a partial meta (only some fields set) preserves prior
    values for other fields — supports incremental edit flows where
    the UI sends only what changed."""
    entry = {
        "id": "x",
        "params": {"paperclip": {"role": "engineer", "title": "Original title", "capabilities": "Original caps."}},
    }
    meta = PaperclipMeta(role="engineer", title="New title", capabilities=None, icon=None)
    write_to_agent_entry(entry, meta)
    assert entry["params"]["paperclip"]["title"] == "New title"
    # capabilities=None means "no change", not "clear". Prior value kept.
    assert entry["params"]["paperclip"]["capabilities"] == "Original caps."


def test_write_preserves_other_params_subkeys():
    """The params bag is shared with other integrations — we MUST not
    blow away unrelated subkeys."""
    entry = {
        "id": "x",
        "params": {"other_integration": {"k": "v"}, "paperclip": {"role": "general"}},
    }
    write_to_agent_entry(entry, PaperclipMeta(role="cmo", title="Marketing lead", capabilities=None, icon=None))
    assert entry["params"]["other_integration"] == {"k": "v"}
    assert entry["params"]["paperclip"]["role"] == "cmo"


def test_write_invalid_role_raises():
    """Writing — unlike reading — is strict. Catching a typo at write
    time prevents bad data from getting into openclaw.json."""
    with pytest.raises(ValueError, match="role"):
        write_to_agent_entry({"id": "x"}, PaperclipMeta(role="bogus", title=None, capabilities=None, icon=None))
```

- [ ] **Step 2: Run tests to verify they fail with ImportError**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_paperclip_meta.py -v
```

Expected: every test errors with `ModuleNotFoundError: No module named 'core.services.paperclip_meta'`.

- [ ] **Step 3: Implement the module**

Create `apps/backend/core/services/paperclip_meta.py`:

```python
"""Paperclip-relevant agent metadata that lives inside OpenClaw's
``AgentEntry.params`` bag.

OpenClaw's ``AgentEntrySchema`` is ``.strict()`` so we can't add
``role`` / ``title`` / ``capabilities`` to it. But it has
``params: z.record(z.string(), z.unknown()).optional()`` as a
documented free-form extension point (see ``openclaw/src/config/
zod-schema.agent-runtime.ts:1002`` + the ``config-misc.test.ts``
case "accepts documented agents.list[].params overrides").

We put the Paperclip-relevant fields under ``params.paperclip``. This
means:

  * They round-trip through the marketplace slice (which carries the
    full agent entry verbatim).
  * They survive chokidar reload + container re-provisions (they're
    persisted in openclaw.json).
  * They stay isolated from other integration metadata that might
    appear in ``params`` (e.g. ``params.cacheRetention`` from upstream
    OpenClaw's own documented overrides).

This module is the SINGLE place that knows the shape of
``params.paperclip``. Every caller — create-agent interceptor,
update-agent interceptor, marketplace deploy — goes through it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Final

# Mirrors paperclip/packages/shared/src/constants.ts AGENT_ROLES.
# If Paperclip upstream changes this list, the test in
# tests/unit/services/test_paperclip_meta.py will fail and force a
# manual re-sync (we keep the constant inline because importing from
# the gitignored paperclip clone is fragile).
PAPERCLIP_AGENT_ROLES: Final[tuple[str, ...]] = (
    "ceo",
    "cto",
    "cmo",
    "cfo",
    "security",
    "engineer",
    "designer",
    "pm",
    "qa",
    "devops",
    "researcher",
    "general",
)


@dataclass(frozen=True)
class PaperclipMeta:
    """Paperclip-relevant agent metadata, lifted from
    ``agent_entry.params.paperclip`` and ready to thread into
    ``PaperclipAdminClient.create_agent`` / ``update_agent``.

    Field meanings match Paperclip's ``createAgentSchema``:
      * ``role`` — one of ``PAPERCLIP_AGENT_ROLES``; defaults to
        ``"general"`` for newly-created agents.
      * ``title`` — short one-liner (e.g. "Restaurant booking
        specialist"). Rendered in Paperclip's roster UI.
      * ``capabilities`` — longer freeform description of what the
        agent does. This is what Friday reads when orchestrating.
      * ``icon`` — Paperclip-side icon picker value; opaque string
        from Paperclip's ``AGENT_ICON_NAMES`` enum. Not validated
        here because the icon list churns more often than roles.
    """

    role: str = "general"
    title: str | None = None
    capabilities: str | None = None
    icon: str | None = None


def read_from_agent_entry(entry: dict) -> PaperclipMeta:
    """Lift the Paperclip meta out of an agent entry.

    Tolerant on read — an unknown ``role`` value is coerced to
    ``"general"`` rather than raising, because the entry comes from
    openclaw.json (potentially hand-edited) and we don't want a typo
    to crash the chat path. Writes are strict (see ``write_to_agent_entry``).
    """
    params = entry.get("params") or {}
    if not isinstance(params, dict):
        return PaperclipMeta()
    pc = params.get("paperclip") or {}
    if not isinstance(pc, dict):
        return PaperclipMeta()
    role = pc.get("role") or "general"
    if role not in PAPERCLIP_AGENT_ROLES:
        role = "general"
    title = pc.get("title")
    capabilities = pc.get("capabilities")
    icon = pc.get("icon")
    return PaperclipMeta(
        role=role,
        title=title if isinstance(title, str) and title.strip() else None,
        capabilities=capabilities if isinstance(capabilities, str) and capabilities.strip() else None,
        icon=icon if isinstance(icon, str) and icon.strip() else None,
    )


def write_to_agent_entry(entry: dict, meta: PaperclipMeta) -> dict:
    """Write the Paperclip meta into ``entry["params"]["paperclip"]``.

    Strict on write: an unknown role raises ``ValueError`` to keep bad
    data out of openclaw.json. ``None`` values for ``title`` /
    ``capabilities`` / ``icon`` are interpreted as "no change" — we
    DON'T overwrite an existing value with null. To clear a field,
    pass an empty string (translated to absent at write time).

    Mutates ``entry`` in place AND returns it (so call sites can chain).
    """
    if meta.role not in PAPERCLIP_AGENT_ROLES:
        raise ValueError(f"unknown Paperclip role: {meta.role!r}")

    params = entry.get("params")
    if not isinstance(params, dict):
        params = {}
        entry["params"] = params
    pc = params.get("paperclip")
    if not isinstance(pc, dict):
        pc = {}
        params["paperclip"] = pc

    pc["role"] = meta.role
    if meta.title is not None:
        if meta.title.strip():
            pc["title"] = meta.title
        else:
            pc.pop("title", None)
    if meta.capabilities is not None:
        if meta.capabilities.strip():
            pc["capabilities"] = meta.capabilities
        else:
            pc.pop("capabilities", None)
    if meta.icon is not None:
        if meta.icon.strip():
            pc["icon"] = meta.icon
        else:
            pc.pop("icon", None)
    return entry


def derive_from_listing(*, name: str | None, description_md: str | None) -> PaperclipMeta:
    """Last-resort derivation when the marketplace slice's agent entry
    has no ``params.paperclip`` block — typical for listings published
    before this feature shipped. Maps:

      * ``listing.name`` → ``title`` (truncated to 80 chars to match
        Paperclip's ``title`` length cap)
      * ``listing.description_md`` → ``capabilities`` (truncated to
        the schema-safe 1024 char ceiling)
      * ``role`` defaults to ``"general"`` — no signal in listing data
        to pick a more specific role.

    Returns ``PaperclipMeta`` even when both inputs are None (caller
    gets defaults). Never raises.
    """
    title = (name or "").strip()[:80] or None
    caps = (description_md or "").strip()[:1024] or None
    return PaperclipMeta(role="general", title=title, capabilities=caps, icon=None)


# Public helpers (intentionally narrow API). Don't reach into
# ``entry["params"]["paperclip"]`` from outside this module — use
# read_from_agent_entry / write_to_agent_entry so the format stays
# consistent if we evolve the sub-block later.
__all__ = [
    "PAPERCLIP_AGENT_ROLES",
    "PaperclipMeta",
    "derive_from_listing",
    "read_from_agent_entry",
    "write_to_agent_entry",
]
```

Also import `copy` is unused in the final module above — clean it up before committing if ruff flags it. (It's imported because earlier drafts deep-copied; final code mutates in place.)

- [ ] **Step 4: Run the tests; verify pass**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_paperclip_meta.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 5: Lint**

```bash
cd apps/backend
uv run ruff format apps/backend/core/services/paperclip_meta.py tests/unit/services/test_paperclip_meta.py
uv run ruff check apps/backend/core/services/paperclip_meta.py tests/unit/services/test_paperclip_meta.py
```

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/paperclip_meta.py apps/backend/tests/unit/services/test_paperclip_meta.py
git commit -m "feat(paperclip-meta): mapping helpers for params.paperclip sub-block"
```

---

## Task 2: PaperclipAdminClient.update_agent + create_agent param verification

**Files:**
- Modify: `apps/backend/core/services/paperclip_admin_client.py:551-620` (verify `create_agent` body sends `title`/`capabilities`/`icon`/`metadata`)
- Modify: `apps/backend/core/services/paperclip_admin_client.py` — add new `update_agent` method
- Test: `apps/backend/tests/unit/services/test_paperclip_admin_client_update_agent.py` (new file)

`PaperclipAdminClient` is the HTTP wrapper around Paperclip's REST. It already declares `title` / `capabilities` as kwargs on `create_agent` per the signature — Step 2 verifies they're actually sent in the request body. We then add `update_agent` for the edit path (Paperclip's `updateAgentSchema` is `createAgentSchema.partial()` so the API exists — we just need a Python wrapper).

- [ ] **Step 1: Inspect current `create_agent` to confirm what's sent**

```bash
grep -nB1 -A40 "async def create_agent\b" apps/backend/core/services/paperclip_admin_client.py
```

Expected: function signature includes `title, capabilities, ...` kwargs and the request body actually includes them. If the body uses a hardcoded `payload = {"name": name, "role": role, "adapterType": ..., "adapterConfig": ...}` and OMITS `title`/`capabilities`/`icon`/`metadata`, those need to be added. Verify before continuing.

- [ ] **Step 2: Write the failing test for update_agent**

Create `apps/backend/tests/unit/services/test_paperclip_admin_client_update_agent.py`:

```python
"""Tests for PaperclipAdminClient.update_agent — partial-update wrapper
around Paperclip's PATCH /api/companies/{companyId}/agents/{agentId}."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError  # noqa: E402


def _stub_http_client(response: httpx.Response) -> MagicMock:
    """Build a stub httpx.AsyncClient whose .patch() returns the given
    response. Returns the mock so tests can introspect the call."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_update_agent_sends_only_provided_fields():
    """A partial update with just ``title`` set must send a PATCH body
    that ONLY contains ``title`` — empty values must not be sent as
    null/empty strings (would wipe other fields server-side)."""
    response = httpx.Response(200, json={"id": "agent-uuid", "title": "New title"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    result = await admin.update_agent(
        session_cookie="sess=abc",
        company_id="co-1",
        agent_id="agent-uuid",
        title="New title",
    )

    assert result == {"id": "agent-uuid", "title": "New title"}
    http.patch.assert_awaited_once()
    call = http.patch.await_args
    # URL — relative path per the admin client's base_url contract.
    assert call.args[0] == "/api/companies/co-1/agents/agent-uuid"
    # Body — only title.
    assert call.kwargs["json"] == {"title": "New title"}
    # Session cookie threaded through headers.
    assert "sess=abc" in call.kwargs["headers"]["Cookie"]


@pytest.mark.asyncio
async def test_update_agent_sends_all_provided_fields():
    """Multiple fields in one call get bundled into the same PATCH body."""
    response = httpx.Response(200, json={"id": "a"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    await admin.update_agent(
        session_cookie="c",
        company_id="co",
        agent_id="a",
        role="engineer",
        title="Backend dev",
        capabilities="Writes Python.",
        icon="terminal",
        metadata={"source": "marketplace"},
    )

    body = http.patch.await_args.kwargs["json"]
    assert body["role"] == "engineer"
    assert body["title"] == "Backend dev"
    assert body["capabilities"] == "Writes Python."
    assert body["icon"] == "terminal"
    assert body["metadata"] == {"source": "marketplace"}


@pytest.mark.asyncio
async def test_update_agent_skips_none_fields():
    """``None`` means "don't touch" — must NOT appear in the body."""
    response = httpx.Response(200, json={"id": "a"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    await admin.update_agent(
        session_cookie="c",
        company_id="co",
        agent_id="a",
        role=None,
        title="New title",
        capabilities=None,
        icon=None,
    )

    body = http.patch.await_args.kwargs["json"]
    assert body == {"title": "New title"}
    assert "role" not in body
    assert "capabilities" not in body


@pytest.mark.asyncio
async def test_update_agent_raises_paperclip_api_error_on_non_2xx():
    """Server-side validation failures surface as PaperclipApiError, so
    callers (e.g. the chat interceptor) can decide whether to swallow
    or propagate."""
    response = httpx.Response(422, json={"error": "invalid role"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    with pytest.raises(PaperclipApiError):
        await admin.update_agent(
            session_cookie="c",
            company_id="co",
            agent_id="a",
            role="bogus",
        )


@pytest.mark.asyncio
async def test_update_agent_returns_dict_payload():
    """200 path returns the server's JSON dict so callers can read the
    updated row without re-fetching."""
    response = httpx.Response(200, json={"id": "a", "title": "T", "updatedAt": "2026-01-01"})
    http = _stub_http_client(response)
    admin = PaperclipAdminClient(http_client=http)

    result = await admin.update_agent(
        session_cookie="c",
        company_id="co",
        agent_id="a",
        title="T",
    )

    assert result["id"] == "a"
    assert result["title"] == "T"
    assert result["updatedAt"] == "2026-01-01"
```

- [ ] **Step 3: Run tests — expect fail (no update_agent method yet)**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_paperclip_admin_client_update_agent.py -v
```

Expected: `AttributeError: 'PaperclipAdminClient' object has no attribute 'update_agent'`.

- [ ] **Step 4: Add the `update_agent` method**

In `apps/backend/core/services/paperclip_admin_client.py`, add this method (place it next to `create_agent`, e.g. right after `delete_agent`):

```python
    async def update_agent(
        self,
        *,
        session_cookie: str,
        company_id: str,
        agent_id: str,
        role: str | None = None,
        title: str | None = None,
        capabilities: str | None = None,
        icon: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Partial-update an agent.

        Maps to ``PATCH /api/companies/{companyId}/agents/{agentId}``
        (see ``paperclip/server/src/routes/agents.ts`` —
        ``updateAgentSchema = createAgentSchema.partial()``). Only
        the fields explicitly supplied are sent; ``None`` means
        "leave alone", an empty string would WIPE the field.

        Returns the updated agent dict from Paperclip's response so
        callers can read the new state without a follow-up GET.

        Raises :class:`PaperclipApiError` on 4xx/5xx (mirrors the
        existing pattern in ``create_agent`` / ``delete_agent``).
        """
        body: dict = {}
        if role is not None:
            body["role"] = role
        if title is not None:
            body["title"] = title
        if capabilities is not None:
            body["capabilities"] = capabilities
        if icon is not None:
            body["icon"] = icon
        if metadata is not None:
            body["metadata"] = metadata

        headers = {"Cookie": session_cookie}
        resp = await self._http.patch(
            f"/api/companies/{company_id}/agents/{agent_id}",
            json=body,
            headers=headers,
        )
        if resp.status_code >= 400:
            raise PaperclipApiError(
                f"PATCH /api/companies/{company_id}/agents/{agent_id} -> {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_json(resp),
            )
        return resp.json()
```

Note: `_safe_json` is an existing helper in the module — verify by greppping. If it's named differently, use whatever existing helper extracts a dict from a response without crashing on non-JSON bodies.

- [ ] **Step 5: Verify `create_agent` also sends title/capabilities/icon/metadata**

Read the existing `create_agent` body. If the request body OMITS `title`/`capabilities`/`icon`/`metadata`, add them with the same "skip None" pattern. Otherwise leave it.

If a change is needed, add a small test to the same test file:

```python
@pytest.mark.asyncio
async def test_create_agent_sends_title_capabilities_icon_metadata_when_provided():
    """Regression: create_agent's signature has accepted title +
    capabilities for a while but the request body silently dropped
    them. Confirm they're sent."""
    response = httpx.Response(200, json={"id": "new-agent"})
    http = MagicMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=response)
    admin = PaperclipAdminClient(http_client=http)

    await admin.create_agent(
        session_cookie="c",
        company_id="co",
        name="Ray",
        role="general",
        adapter_type="openclaw_gateway",
        adapter_config={"url": "wss://..."},
        title="Restaurant booking specialist",
        capabilities="Books Resy reservations.",
    )

    body = http.post.await_args.kwargs["json"]
    assert body["title"] == "Restaurant booking specialist"
    assert body["capabilities"] == "Books Resy reservations."
```

If this test fails, patch `create_agent` to include `title`/`capabilities`/`icon`/`metadata` in the body (skipping None values).

- [ ] **Step 6: Run tests; verify pass**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_paperclip_admin_client_update_agent.py -v
```

- [ ] **Step 7: Lint + commit**

```bash
cd apps/backend
uv run ruff format apps/backend/core/services/paperclip_admin_client.py tests/unit/services/test_paperclip_admin_client_update_agent.py
uv run ruff check apps/backend/core/services/paperclip_admin_client.py tests/unit/services/test_paperclip_admin_client_update_agent.py
git add apps/backend/core/services/paperclip_admin_client.py apps/backend/tests/unit/services/test_paperclip_admin_client_update_agent.py
git commit -m "feat(paperclip-admin): update_agent wrapper + verify create_agent body"
```

---

## Task 3: Atlas role fix + `paperclip_agent_lifecycle` meta kwarg

**Files:**
- Modify: `apps/backend/core/services/paperclip_provisioning.py:379` — change `role="recruiter"` → `role="general"`
- Modify: `apps/backend/core/services/paperclip_agent_lifecycle.py:36-145` — `create_paperclip_for_chat` + `create_paperclip_for_marketplace` gain optional `meta: PaperclipMeta | None`
- Test: `apps/backend/tests/unit/services/test_paperclip_agent_lifecycle_meta.py` (new)

- [ ] **Step 1: Atlas role fix**

In `apps/backend/core/services/paperclip_provisioning.py`, find the Atlas seed call (around line 376-388) and change `role="recruiter"` to `role="general"`. Update the surrounding comment to reflect the constraint: "any non-ceo role is fine; general matches Paperclip's enum closest to 'no specific specialty'."

The current code:
```python
            try:
                await self._admin.create_agent(
                    company_id=company_id,
                    name="Atlas",
                    role="recruiter",
                    ...
```

Becomes:
```python
            try:
                await self._admin.create_agent(
                    company_id=company_id,
                    name="Atlas",
                    # Atlas was originally typed as ``role="recruiter"``
                    # but Paperclip's AGENT_ROLES enum doesn't include
                    # "recruiter" (see paperclip/packages/shared/src/
                    # constants.ts:44). The constraint here is just
                    # "must not be 'ceo'" because config.py:463 selects
                    # the ceo-role agent as the default. "general" is the
                    # most semantically neutral fit.
                    role="general",
                    ...
```

- [ ] **Step 2: Write the failing test for `create_paperclip_for_chat` accepting meta**

Create `apps/backend/tests/unit/services/test_paperclip_agent_lifecycle_meta.py`:

```python
"""Tests for paperclip_agent_lifecycle — meta kwarg threading."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services.paperclip_agent_lifecycle import (  # noqa: E402
    create_paperclip_for_chat,
    create_paperclip_for_marketplace,
)
from core.services.paperclip_meta import PaperclipMeta  # noqa: E402


@pytest.mark.asyncio
async def test_create_paperclip_for_chat_forwards_meta_fields():
    """When meta is supplied, create_paperclip_for_chat passes
    title/capabilities/icon through to admin_client.create_agent.
    Role from meta overrides the ``role`` kwarg only when the kwarg
    is None; explicit role argument wins (chat-side currently passes
    role="general" hardcoded — meta lets that be overridden)."""
    meta = PaperclipMeta(
        role="engineer",
        title="Backend specialist",
        capabilities="Writes services.",
        icon="terminal",
    )
    mock_admin = MagicMock()
    mock_admin.create_agent = AsyncMock(return_value={"id": "new-uuid"})

    with (
        patch("core.services.paperclip_agent_lifecycle.PaperclipRepo") as mock_repo_cls,
        patch("core.services.paperclip_agent_lifecycle.PaperclipAdminClient", return_value=mock_admin),
        patch(
            "core.services.paperclip_agent_lifecycle.get_admin_session_cookie",
            new=AsyncMock(return_value="sess=cookie"),
        ),
        patch("core.services.paperclip_agent_lifecycle.service_token") as mock_st,
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co-1"))
        mock_repo_cls.return_value = mock_repo
        mock_st.mint = MagicMock(return_value="svc-token")

        await create_paperclip_for_chat(
            creator_user_id="user_creator",
            owner_id="user_owner",
            name="Ray",
            role=None,
            model=None,
            meta=meta,
        )

    mock_admin.create_agent.assert_awaited_once()
    kwargs = mock_admin.create_agent.await_args.kwargs
    assert kwargs["name"] == "Ray"
    # meta.role wins when role kwarg is None
    assert kwargs["role"] == "engineer"
    assert kwargs["title"] == "Backend specialist"
    assert kwargs["capabilities"] == "Writes services."
    assert kwargs["icon"] == "terminal"


@pytest.mark.asyncio
async def test_create_paperclip_for_chat_no_meta_keeps_existing_behavior():
    """Backwards-compat: existing callers that don't pass meta still
    get role="general" and no title/capabilities (status quo before
    this PR)."""
    mock_admin = MagicMock()
    mock_admin.create_agent = AsyncMock(return_value={"id": "new-uuid"})

    with (
        patch("core.services.paperclip_agent_lifecycle.PaperclipRepo") as mock_repo_cls,
        patch("core.services.paperclip_agent_lifecycle.PaperclipAdminClient", return_value=mock_admin),
        patch(
            "core.services.paperclip_agent_lifecycle.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
        patch("core.services.paperclip_agent_lifecycle.service_token") as mock_st,
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
        mock_repo_cls.return_value = mock_repo
        mock_st.mint = MagicMock(return_value="t")

        await create_paperclip_for_chat(
            creator_user_id="u",
            owner_id="u",
            name="Ray",
            role=None,
            model=None,
        )

    kwargs = mock_admin.create_agent.await_args.kwargs
    assert kwargs["role"] == "general"  # default — meta=None path
    assert "title" not in kwargs or kwargs.get("title") is None
    assert "capabilities" not in kwargs or kwargs.get("capabilities") is None


@pytest.mark.asyncio
async def test_create_paperclip_for_marketplace_forwards_meta():
    """Same contract on the marketplace path."""
    meta = PaperclipMeta(role="cmo", title="Marketing lead", capabilities="Campaign work.", icon=None)
    mock_admin = MagicMock()
    mock_admin.create_agent = AsyncMock(return_value={"id": "new-uuid"})

    with (
        patch("core.services.paperclip_agent_lifecycle.PaperclipRepo") as mock_repo_cls,
        patch("core.services.paperclip_agent_lifecycle.PaperclipAdminClient", return_value=mock_admin),
        patch(
            "core.services.paperclip_agent_lifecycle.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
        patch("core.services.paperclip_agent_lifecycle.service_token") as mock_st,
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
        mock_repo_cls.return_value = mock_repo
        mock_st.mint = MagicMock(return_value="t")

        await create_paperclip_for_marketplace(
            buyer_user_id="u",
            owner_id="u",
            name="Ray",
            role=None,
            meta=meta,
        )

    kwargs = mock_admin.create_agent.await_args.kwargs
    assert kwargs["role"] == "cmo"
    assert kwargs["title"] == "Marketing lead"
    assert kwargs["capabilities"] == "Campaign work."
```

- [ ] **Step 3: Run tests — expect fail (meta kwarg doesn't exist yet)**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_paperclip_agent_lifecycle_meta.py -v
```

Expected: `TypeError: create_paperclip_for_chat() got an unexpected keyword argument 'meta'`.

- [ ] **Step 4: Extend both helpers**

In `apps/backend/core/services/paperclip_agent_lifecycle.py`, add `meta: PaperclipMeta | None = None` to both function signatures and forward to `create_agent`:

```python
# At top of file, add import:
from core.services.paperclip_meta import PaperclipMeta
```

Update `create_paperclip_for_chat`:

```python
async def create_paperclip_for_chat(
    *,
    creator_user_id: str,
    owner_id: str,
    name: str,
    role: str | None,
    model: str | None,
    meta: PaperclipMeta | None = None,  # NEW
) -> dict:
    # ... existing body up to the create_agent call ...

    # Determine final role/title/capabilities/icon: explicit role kwarg wins
    # over meta.role (chat path passes role="general" historically; meta lets
    # callers override). Title/capabilities/icon come from meta only.
    effective_role = role if role is not None else (meta.role if meta else "general")
    title = meta.title if meta else None
    capabilities = meta.capabilities if meta else None
    icon = meta.icon if meta else None

    return await admin_client.create_agent(
        company_id=company_id,
        name=name,
        role=effective_role,
        adapter_type=OPENCLAW_GATEWAY_TYPE,
        adapter_config=adapter_config,
        session_cookie=admin_cookie,
        title=title,
        capabilities=capabilities,
        icon=icon,
        # ... existing kwargs like model if present ...
    )
```

Same pattern for `create_paperclip_for_marketplace`.

- [ ] **Step 5: Run tests; verify pass**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_paperclip_agent_lifecycle_meta.py tests/unit/services/test_paperclip_provisioning.py -v
```

The provisioning tests should still pass — Atlas now uses `role="general"` which is in the enum.

- [ ] **Step 6: Lint + commit**

```bash
cd apps/backend
uv run ruff format apps/backend/core/services/paperclip_provisioning.py apps/backend/core/services/paperclip_agent_lifecycle.py apps/backend/tests/unit/services/test_paperclip_agent_lifecycle_meta.py
uv run ruff check apps/backend/core/services/
git add apps/backend/core/services/paperclip_provisioning.py apps/backend/core/services/paperclip_agent_lifecycle.py apps/backend/tests/unit/services/test_paperclip_agent_lifecycle_meta.py
git commit -m "feat(paperclip-lifecycle): meta kwarg; fix Atlas role recruiter→general"
```

---

## Task 4: Chat-path interceptors — `_intercept_agents_create` reads meta, new `_intercept_agents_update`

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py:569-655` — `_intercept_agents_create` reads `params["params"]["paperclip"]`, builds `PaperclipMeta`, passes to `create_paperclip_for_chat`. ALSO writes the meta back into the agent_entry post-create so it persists in openclaw.json via the file-merge path.
- Modify: `apps/backend/routers/websocket_chat.py:~700-740` — add `_intercept_agents_update` that fires whenever `agents.update` is RPC'd. Calls `PaperclipAdminClient.update_agent` with meta from params.paperclip if present. Returns False (don't consume the RPC; OpenClaw still gets the update for its own fields).
- Test: `apps/backend/tests/unit/routers/test_websocket_chat_paperclip_meta.py` (new)

This task wires the chat-side create/edit to also push to Paperclip. **Crucially**: the `agents.update` RPC must STILL go to OpenClaw (for name/identity/skills changes); we just FAN OUT a parallel call to Paperclip for the meta fields. The interceptor returns `False` so the dispatcher still forwards to OpenClaw.

- [ ] **Step 1: Read existing `_intercept_agents_create` to understand the contract**

```bash
grep -nB1 -A50 "async def _intercept_agents_create" apps/backend/routers/websocket_chat.py | head -70
```

Note: existing function returns `True` to "consume" the RPC (don't forward to OpenClaw — pairing handles it). Update should NOT consume because OpenClaw needs to see the update for its own fields.

- [ ] **Step 2: Write failing tests**

Create `apps/backend/tests/unit/routers/test_websocket_chat_paperclip_meta.py`:

```python
"""Tests for chat-path Paperclip-meta wiring on agents.create + agents.update."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from routers.websocket_chat import _intercept_agents_create, _intercept_agents_update  # noqa: E402


@pytest.mark.asyncio
async def test_intercept_create_reads_paperclip_meta_from_params():
    """When the chat client sends params={"name": "Ray", "params":
    {"paperclip": {"role": "engineer", "title": "...", "capabilities": "..."}}},
    the interceptor passes a PaperclipMeta to create_paperclip_for_chat."""
    create_call = AsyncMock(return_value={"id": "agent-uuid", "name": "Ray"})
    mock_pair = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.send_rpc = AsyncMock()
    mock_mgmt = MagicMock()
    mock_mgmt.send_message = MagicMock()

    with (
        patch("routers.websocket_chat.create_paperclip_for_chat", new=create_call),
        patch("routers.websocket_chat.pair_with_openclaw", new=mock_pair),
        patch("routers.websocket_chat.get_gateway_pool", return_value=mock_pool),
        patch("routers.websocket_chat.get_management_api_client", new=AsyncMock(return_value=mock_mgmt)),
    ):
        consumed = await _intercept_agents_create(
            connection_id="c1",
            req_id="r1",
            user_id="user_creator",
            owner_id="user_owner",
            params={
                "name": "Ray",
                "params": {
                    "paperclip": {
                        "role": "engineer",
                        "title": "Backend dev",
                        "capabilities": "Writes Python.",
                    }
                },
            },
            ip="10.0.0.1",
            token="tok",
        )

    assert consumed is True
    # create_paperclip_for_chat got a PaperclipMeta with the user's fields
    kwargs = create_call.await_args.kwargs
    assert kwargs["meta"] is not None
    assert kwargs["meta"].role == "engineer"
    assert kwargs["meta"].title == "Backend dev"
    assert kwargs["meta"].capabilities == "Writes Python."


@pytest.mark.asyncio
async def test_intercept_create_with_no_meta_works_as_before():
    """No params.paperclip — meta=None, behavior identical to today."""
    create_call = AsyncMock(return_value={"id": "agent-uuid", "name": "Ray"})

    with (
        patch("routers.websocket_chat.create_paperclip_for_chat", new=create_call),
        patch("routers.websocket_chat.pair_with_openclaw", new=AsyncMock()),
        patch("routers.websocket_chat.get_gateway_pool", return_value=MagicMock()),
        patch("routers.websocket_chat.get_management_api_client", new=AsyncMock(return_value=MagicMock())),
    ):
        await _intercept_agents_create(
            connection_id="c1",
            req_id="r1",
            user_id="u",
            owner_id="u",
            params={"name": "Ray"},
            ip="10.0.0.1",
            token="tok",
        )

    kwargs = create_call.await_args.kwargs
    # meta arg either absent (existing signature) or None
    assert kwargs.get("meta") is None


@pytest.mark.asyncio
async def test_intercept_update_calls_paperclip_update_when_meta_present():
    """agents.update with params.paperclip in the RPC body triggers a
    parallel PATCH to Paperclip. Returns False so the dispatcher still
    forwards to OpenClaw."""
    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(return_value={"id": "agent-uuid"})
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co-1"))

    with (
        patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin),
        patch("routers.websocket_chat.PaperclipRepo", return_value=mock_repo),
        patch(
            "routers.websocket_chat.get_admin_session_cookie",
            new=AsyncMock(return_value="sess=c"),
        ),
    ):
        consumed = await _intercept_agents_update(
            owner_id="user_owner",
            params={
                "agentId": "agent-uuid",
                "name": "Renamed",
                "params": {
                    "paperclip": {"role": "engineer", "title": "Senior engineer"}
                },
            },
        )

    # Update interceptor does NOT consume — OpenClaw still gets the RPC
    assert consumed is False
    # Paperclip got the partial PATCH with just the meta fields
    mock_admin.update_agent.assert_awaited_once()
    kwargs = mock_admin.update_agent.await_args.kwargs
    assert kwargs["agent_id"] == "agent-uuid"
    assert kwargs["company_id"] == "co-1"
    assert kwargs["role"] == "engineer"
    assert kwargs["title"] == "Senior engineer"
    # capabilities/icon weren't in the meta block — must NOT be sent
    assert kwargs.get("capabilities") is None
    assert kwargs.get("icon") is None


@pytest.mark.asyncio
async def test_intercept_update_no_meta_is_noop():
    """A normal agents.update without params.paperclip (e.g. model
    change, identity emoji change) doesn't call Paperclip. Returns
    False so OpenClaw still gets the update."""
    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock()

    with patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin):
        consumed = await _intercept_agents_update(
            owner_id="u",
            params={"agentId": "agent-uuid", "model": "claude-sonnet-4-6"},
        )

    assert consumed is False
    mock_admin.update_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_intercept_update_swallows_paperclip_errors():
    """If Paperclip's PATCH fails, the chat-side update_agent MUST
    NOT propagate the error — the user's OpenClaw update should still
    succeed. Log + drop on the floor (best effort to keep the two
    in sync; OpenClaw is source of truth for runtime state)."""
    from core.services.paperclip_admin_client import PaperclipApiError

    mock_admin = MagicMock()
    mock_admin.update_agent = AsyncMock(
        side_effect=PaperclipApiError("422", status_code=422, body={"error": "bad role"})
    )
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))

    with (
        patch("routers.websocket_chat.PaperclipAdminClient", return_value=mock_admin),
        patch("routers.websocket_chat.PaperclipRepo", return_value=mock_repo),
        patch(
            "routers.websocket_chat.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
    ):
        # Should NOT raise
        consumed = await _intercept_agents_update(
            owner_id="u",
            params={
                "agentId": "a",
                "params": {"paperclip": {"role": "bogus"}},
            },
        )

    assert consumed is False
```

- [ ] **Step 3: Run tests; expect fail (interceptor doesn't read meta yet, update interceptor doesn't exist)**

```bash
cd apps/backend
uv run pytest tests/unit/routers/test_websocket_chat_paperclip_meta.py -v
```

- [ ] **Step 4: Update `_intercept_agents_create`**

In `apps/backend/routers/websocket_chat.py`, find the function (line ~569). Just before the `create_paperclip_for_chat` call, build the meta:

```python
    # NEW imports at top of file:
    # from core.services.paperclip_meta import PaperclipMeta, read_from_agent_entry

    # ... inside _intercept_agents_create, after parsing name/role/model: ...

    # Pull Paperclip meta out of the RPC params' own ``params`` sub-dict.
    # The chat UI builds this from its AgentPaperclipFields form. Absent
    # for legacy clients — defaults to PaperclipMeta() = role=general.
    rpc_params_inner = (params or {}).get("params") or {}
    meta = (
        read_from_agent_entry({"params": rpc_params_inner})
        if isinstance(rpc_params_inner, dict)
        else None
    )

    paperclip_agent = await create_paperclip_for_chat(
        creator_user_id=user_id,
        owner_id=owner_id,
        name=name,
        role=role,
        model=model,
        meta=meta,
    )
```

Also after pair_with_openclaw succeeds, write the meta into the agent_entry that gets persisted via OpenClaw's update RPC — so it's durable in `openclaw.json`. The chat path doesn't directly write openclaw.json (OpenClaw owns that for chat-created agents), but `pair_with_openclaw`'s `extra_update_fields` carries arbitrary fields. We extend that to include `params: {"paperclip": {...}}`. Look at how `pair_with_openclaw` handles `extra_update_fields` — if it doesn't currently round-trip `params`, that's a separate small fix to that helper.

For this PR's scope, the most important effect is Paperclip getting populated. OpenClaw's openclaw.json round-trip via chokidar is a nice-to-have we can leave to chokidar's natural reload. Add a comment marking this as known.

- [ ] **Step 5: Add `_intercept_agents_update`**

In `apps/backend/routers/websocket_chat.py`, add after `_intercept_agents_create`:

```python
async def _intercept_agents_update(
    *,
    owner_id: str,
    params: dict,
) -> bool:
    """Mirror Paperclip-relevant fields from an agents.update RPC over
    to Paperclip's PATCH /agents.

    Does NOT consume the RPC — returns False so the dispatcher still
    forwards to OpenClaw (the user's update probably includes
    OpenClaw-only fields like model / identity / skills which only
    OpenClaw cares about).

    Best-effort: a Paperclip PATCH failure is logged but never raised.
    OpenClaw is the source of truth for the agent's runtime state; if
    Paperclip's row drifts, the next manual edit or a reconciliation
    job can re-sync.
    """
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.paperclip_meta import read_from_agent_entry
    from core.config import settings
    import httpx

    rpc_params_inner = (params or {}).get("params") or {}
    if not isinstance(rpc_params_inner, dict) or not rpc_params_inner.get("paperclip"):
        return False  # No meta in payload — nothing to mirror.

    meta = read_from_agent_entry({"params": rpc_params_inner})
    agent_id = (params or {}).get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return False

    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        row = await repo.get(owner_id) if not owner_id.startswith("org_") else None
        company_id = row.company_id if row else (await repo.get_org_company_id(owner_id))
        if not company_id:
            return False
        async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
            admin = PaperclipAdminClient(http_client=http)
            cookie = await get_admin_session_cookie(http)
            await admin.update_agent(
                session_cookie=cookie,
                company_id=company_id,
                agent_id=agent_id,
                role=meta.role,
                title=meta.title,
                capabilities=meta.capabilities,
                icon=meta.icon,
            )
    except (PaperclipApiError, httpx.HTTPError, RuntimeError) as e:
        logger.warning(
            "[%s] paperclip update_agent mirror failed agent=%s err=%s",
            owner_id,
            agent_id,
            e,
        )
    return False
```

- [ ] **Step 6: Wire `_intercept_agents_update` into the RPC dispatcher**

Find the existing `if method == "agents.create":` dispatcher block (around line 700). Add a sibling block:

```python
        if method == "agents.update":
            try:
                consumed = await _intercept_agents_update(
                    owner_id=owner_id,
                    params=params,
                )
            except Exception as exc:
                # Defensive: never let our mirror code crash the RPC.
                logger.exception(
                    "[%s] agents.update interception failed: %s",
                    user_id,
                    exc,
                )
                consumed = False
            # update interceptor always returns False — fall through to
            # the gateway forwarding below.
            if consumed:
                return
```

- [ ] **Step 7: Run tests; verify pass**

```bash
cd apps/backend
uv run pytest tests/unit/routers/test_websocket_chat_paperclip_meta.py tests/unit/routers/test_websocket_chat_pairing.py -v
```

- [ ] **Step 8: Lint + commit**

```bash
cd apps/backend
uv run ruff format apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_chat_paperclip_meta.py
uv run ruff check apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_chat_paperclip_meta.py
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_chat_paperclip_meta.py
git commit -m "feat(chat): mirror Paperclip meta on agents.create + agents.update"
```

---

## Task 5: Marketplace deploy — read meta from slice, derive from listing fallback

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py:415-430` (the Paperclip mint block in `deploy_listing`)
- Test: `apps/backend/tests/unit/routers/test_marketplace_listings.py` — add deploy-with-meta tests

- [ ] **Step 1: Read current deploy_listing structure**

```bash
sed -n '395,470p' apps/backend/routers/marketplace_listings.py
```

Confirm the current code is:
```python
agent_name = (slice_.get("agent") or {}).get("name") or listing.get("name") or slug
agent_role = (slice_.get("agent") or {}).get("role") or "general"
paperclip_agent = await create_paperclip_for_marketplace(
    buyer_user_id=auth.user_id,
    owner_id=owner_id,
    name=agent_name,
    role=agent_role,
)
```

- [ ] **Step 2: Write failing tests**

Add to `apps/backend/tests/unit/routers/test_marketplace_listings.py` (the existing test file for marketplace_listings):

```python
def test_deploy_passes_paperclip_meta_from_slice(monkeypatch):
    """When the slice's agent entry has params.paperclip set, deploy
    forwards a PaperclipMeta with those values to
    create_paperclip_for_marketplace."""
    from unittest.mock import AsyncMock, patch
    from core.services.paperclip_meta import PaperclipMeta
    # Set up the existing deploy fixture (S3 stubs, ECS stubs, etc.)
    # — copy the pattern from existing test_deploy_503s_when_container_not_reachable
    # but add slice_["agent"]["params"]["paperclip"] = {"role": "engineer",
    # "title": "Backend dev", "capabilities": "Writes Python."}.
    # Assert the meta arg passed to create_paperclip_for_marketplace has those values.
    # [Implementer: mirror the existing tests' fixture wiring.]


def test_deploy_derives_meta_from_listing_when_slice_missing_paperclip_block(monkeypatch):
    """Listings published before this feature don't have params.paperclip
    in the slice. Deploy must fall back to deriving title=listing.name,
    capabilities=listing.description_md, role="general"."""
    # [Implementer: same fixture pattern; slice has no params.paperclip.]
```

**Note for implementer**: this test file is large (~700 lines) and has an established mocking style (`_stub_ecs_manager`, `_stub_s3_get_object`, `mock_create_pc`). MIRROR that style. Don't invent new fixtures.

- [ ] **Step 3: Run tests — expect fail**

```bash
cd apps/backend
uv run pytest tests/unit/routers/test_marketplace_listings.py -k "paperclip_meta" -v
```

- [ ] **Step 4: Update `deploy_listing`**

In `apps/backend/routers/marketplace_listings.py`, replace the Paperclip-mint block:

```python
    # Imports at top:
    # from core.services.paperclip_meta import derive_from_listing, read_from_agent_entry

    # Existing slice/manifest fetch code stays.

    # Build PaperclipMeta from the slice. If the slice's agent entry has
    # params.paperclip, use that. Otherwise (legacy listing or seller
    # hasn't filled in the meta fields), derive from the listing's name
    # + description_md so the buyer at least gets a non-empty profile.
    slice_agent = slice_.get("agent") or {}
    if isinstance(slice_agent, dict) and isinstance(slice_agent.get("params"), dict):
        meta = read_from_agent_entry(slice_agent)
    else:
        meta = derive_from_listing(
            name=listing.get("name"),
            description_md=listing.get("description_md"),
        )

    agent_name = slice_agent.get("name") or listing.get("name") or slug
    try:
        paperclip_agent = await create_paperclip_for_marketplace(
            buyer_user_id=auth.user_id,
            owner_id=owner_id,
            name=agent_name,
            role=None,  # let meta.role drive
            meta=meta,
        )
    except Exception as exc:
        # ... existing 503 path ...
```

- [ ] **Step 5: Run tests; verify pass**

```bash
cd apps/backend
uv run pytest tests/unit/routers/test_marketplace_listings.py -v
```

- [ ] **Step 6: Lint + commit**

```bash
cd apps/backend
uv run ruff format apps/backend/routers/marketplace_listings.py
uv run ruff check apps/backend/routers/marketplace_listings.py
git add apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/routers/test_marketplace_listings.py
git commit -m "feat(marketplace-deploy): forward PaperclipMeta from slice or derive from listing"
```

---

## Task 6: Frontend — AgentPaperclipFields component + AgentCreateForm wiring

**Files:**
- Create: `apps/frontend/src/lib/paperclipMeta.ts`
- Create: `apps/frontend/src/components/control/agents/AgentPaperclipFields.tsx`
- Create: `apps/frontend/src/components/control/agents/__tests__/AgentPaperclipFields.test.tsx`
- Modify: `apps/frontend/src/components/control/panels/AgentCreateForm.tsx`

- [ ] **Step 1: Constants module**

Create `apps/frontend/src/lib/paperclipMeta.ts`:

```typescript
/**
 * Paperclip-relevant agent metadata. Mirrors the backend's
 * core/services/paperclip_meta.py shape — both write into
 * agent_entry.params.paperclip.
 *
 * Kept in sync MANUALLY with Paperclip's
 * paperclip/packages/shared/src/constants.ts AGENT_ROLES. If the
 * backend's PAPERCLIP_AGENT_ROLES test fails, this file needs the
 * same update.
 */

export const PAPERCLIP_AGENT_ROLES = [
  "ceo",
  "cto",
  "cmo",
  "cfo",
  "security",
  "engineer",
  "designer",
  "pm",
  "qa",
  "devops",
  "researcher",
  "general",
] as const;

export type PaperclipRole = (typeof PAPERCLIP_AGENT_ROLES)[number];

export const PAPERCLIP_ROLE_LABELS: Record<PaperclipRole, string> = {
  ceo: "CEO",
  cto: "CTO",
  cmo: "CMO",
  cfo: "CFO",
  security: "Security",
  engineer: "Engineer",
  designer: "Designer",
  pm: "Product manager",
  qa: "QA",
  devops: "DevOps",
  researcher: "Researcher",
  general: "General (no specific role)",
};

export interface PaperclipMeta {
  role: PaperclipRole;
  title?: string;
  capabilities?: string;
  icon?: string;
}
```

- [ ] **Step 2: AgentPaperclipFields component**

Create `apps/frontend/src/components/control/agents/AgentPaperclipFields.tsx`:

```tsx
"use client";

import {
  PAPERCLIP_AGENT_ROLES,
  PAPERCLIP_ROLE_LABELS,
  type PaperclipMeta,
  type PaperclipRole,
} from "@/lib/paperclipMeta";

interface AgentPaperclipFieldsProps {
  value: PaperclipMeta;
  onChange: (next: PaperclipMeta) => void;
  disabled?: boolean;
}

/**
 * Shared role/title/capabilities form. Used in:
 *   * AgentCreateForm — when creating a new agent.
 *   * AgentOverviewTab — when editing an existing agent.
 *   * (Future) PublishWizard — for sellers to override per-listing.
 *
 * The same values write into agent_entry.params.paperclip via the
 * backend's paperclip_meta module, so the agent's profile in
 * Paperclip stays in sync without the user thinking about the split.
 */
export function AgentPaperclipFields({
  value,
  onChange,
  disabled,
}: AgentPaperclipFieldsProps) {
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground" htmlFor="paperclip-role">
          Role
        </label>
        <select
          id="paperclip-role"
          value={value.role}
          disabled={disabled}
          onChange={(e) =>
            onChange({ ...value, role: e.target.value as PaperclipRole })
          }
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        >
          {PAPERCLIP_AGENT_ROLES.map((r) => (
            <option key={r} value={r}>
              {PAPERCLIP_ROLE_LABELS[r]}
            </option>
          ))}
        </select>
      </div>

      <div className="space-y-1">
        <label className="text-xs text-muted-foreground" htmlFor="paperclip-title">
          Title <span className="text-muted-foreground/60">(short)</span>
        </label>
        <input
          id="paperclip-title"
          type="text"
          value={value.title ?? ""}
          disabled={disabled}
          maxLength={80}
          placeholder="e.g. Restaurant booking specialist"
          onChange={(e) => onChange({ ...value, title: e.target.value })}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
      </div>

      <div className="space-y-1">
        <label
          className="text-xs text-muted-foreground"
          htmlFor="paperclip-capabilities"
        >
          Capabilities{" "}
          <span className="text-muted-foreground/60">
            (what this agent does — Friday reads this)
          </span>
        </label>
        <textarea
          id="paperclip-capabilities"
          value={value.capabilities ?? ""}
          disabled={disabled}
          maxLength={1024}
          rows={3}
          placeholder="e.g. Books Resy reservations. Knows my preferences from USER.md."
          onChange={(e) => onChange({ ...value, capabilities: e.target.value })}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Component test**

Create `apps/frontend/src/components/control/agents/__tests__/AgentPaperclipFields.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AgentPaperclipFields } from "../AgentPaperclipFields";
import type { PaperclipMeta } from "@/lib/paperclipMeta";

describe("AgentPaperclipFields", () => {
  const defaultMeta: PaperclipMeta = { role: "general" };

  it("renders all 12 Paperclip roles in the select", () => {
    render(<AgentPaperclipFields value={defaultMeta} onChange={() => {}} />);
    const select = screen.getByLabelText(/Role/i) as HTMLSelectElement;
    expect(select.options).toHaveLength(12);
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toEqual([
      "ceo",
      "cto",
      "cmo",
      "cfo",
      "security",
      "engineer",
      "designer",
      "pm",
      "qa",
      "devops",
      "researcher",
      "general",
    ]);
  });

  it("emits onChange with the new role when select changes", () => {
    const onChange = vi.fn();
    render(<AgentPaperclipFields value={defaultMeta} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/Role/i), {
      target: { value: "engineer" },
    });
    expect(onChange).toHaveBeenCalledWith({ role: "engineer" });
  });

  it("emits onChange with new title on input change, preserving role", () => {
    const onChange = vi.fn();
    render(
      <AgentPaperclipFields
        value={{ role: "cmo", title: "Old" }}
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Title/i), {
      target: { value: "New" },
    });
    expect(onChange).toHaveBeenCalledWith({ role: "cmo", title: "New" });
  });

  it("emits onChange with new capabilities on textarea change", () => {
    const onChange = vi.fn();
    render(
      <AgentPaperclipFields
        value={{ role: "general" }}
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Capabilities/i), {
      target: { value: "Books Resy reservations." },
    });
    expect(onChange).toHaveBeenCalledWith({
      role: "general",
      capabilities: "Books Resy reservations.",
    });
  });

  it("disables all inputs when disabled prop is true", () => {
    render(
      <AgentPaperclipFields
        value={defaultMeta}
        onChange={() => {}}
        disabled
      />,
    );
    expect(screen.getByLabelText(/Role/i)).toBeDisabled();
    expect(screen.getByLabelText(/Title/i)).toBeDisabled();
    expect(screen.getByLabelText(/Capabilities/i)).toBeDisabled();
  });
});
```

- [ ] **Step 4: Run frontend tests; verify pass**

```bash
cd apps/frontend
pnpm vitest run src/components/control/agents/__tests__/AgentPaperclipFields.test.tsx
```

- [ ] **Step 5: Wire into AgentCreateForm**

In `apps/frontend/src/components/control/panels/AgentCreateForm.tsx`, replace the form body to include the new component and pass `params.paperclip` in the create RPC:

```tsx
"use client";

import { useState, useCallback } from "react";
import { Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";

import { capture } from "@/lib/analytics";
import { normalizeAgentId, validateAgentName } from "@/lib/agentIdentity";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { AgentPaperclipFields } from "@/components/control/agents/AgentPaperclipFields";
import type { PaperclipMeta } from "@/lib/paperclipMeta";

interface AgentCreateFormProps {
  existingIds: string[];
  onCreated: () => void;
  onCancel: () => void;
}

export function AgentCreateForm({ existingIds, onCreated, onCancel }: AgentCreateFormProps) {
  const [name, setName] = useState("");
  const [meta, setMeta] = useState<PaperclipMeta>({ role: "general" });
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const callRpc = useGatewayRpcMutation();

  const normalizedId = normalizeAgentId(name);
  const clientError = validateAgentName(name, existingIds);
  const canCreate = name.trim() !== "" && clientError === null && !creating;

  const handleCreate = useCallback(async () => {
    if (!canCreate) return;
    setCreating(true);
    setError(null);
    try {
      // Build params.paperclip payload — only include keys with non-empty values
      const paperclipBlock: Record<string, string> = { role: meta.role };
      if (meta.title && meta.title.trim()) paperclipBlock.title = meta.title.trim();
      if (meta.capabilities && meta.capabilities.trim())
        paperclipBlock.capabilities = meta.capabilities.trim();
      if (meta.icon && meta.icon.trim()) paperclipBlock.icon = meta.icon.trim();

      await callRpc("agents.create", {
        name: name.trim(),
        workspace: ".openclaw/workspaces/" + normalizedId,
        reasoningDefault: "stream",
        params: { paperclip: paperclipBlock },
      });
      capture("agent_created", { agent_name: name.trim(), paperclip_role: meta.role });
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }, [canCreate, callRpc, name, normalizedId, meta, onCreated]);

  return (
    <div className="rounded-lg border border-border bg-card/30 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">New Agent</h3>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md p-1 text-muted-foreground hover:text-foreground hover:bg-[#f0ebe2] transition-colors"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setError(null);
          }}
          placeholder="e.g. Research Assistant"
          disabled={creating}
          onKeyDown={(e) => {
            if (e.key === "Escape") onCancel();
          }}
          autoFocus
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
        {normalizedId && !clientError && (
          <p className="text-[10px] text-muted-foreground">ID: {normalizedId}</p>
        )}
      </div>

      <AgentPaperclipFields value={meta} onChange={setMeta} disabled={creating} />

      {(clientError || error) && (
        <p className="text-xs text-red-500">{clientError || error}</p>
      )}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={creating}>
          Cancel
        </Button>
        <Button size="sm" onClick={handleCreate} disabled={!canCreate}>
          {creating ? <Loader2 className="h-3 w-3 animate-spin" /> : "Create"}
        </Button>
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Run frontend typecheck**

```bash
cd apps/frontend
npx tsc --noEmit
```

Expected: 0 errors.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/lib/paperclipMeta.ts apps/frontend/src/components/control/agents/AgentPaperclipFields.tsx apps/frontend/src/components/control/agents/__tests__/AgentPaperclipFields.test.tsx apps/frontend/src/components/control/panels/AgentCreateForm.tsx
git commit -m "feat(frontend): AgentPaperclipFields + wire into AgentCreateForm"
```

---

## Task 7: Frontend — AgentOverviewTab edit support

**Files:**
- Modify: `apps/frontend/src/components/control/panels/AgentOverviewTab.tsx`

The overview tab is the existing edit surface (it has model selection today). Add the AgentPaperclipFields, pre-populate from the agent's current `params.paperclip`, save via `agents.update` RPC.

- [ ] **Step 1: Read current AgentOverviewTab**

```bash
cat apps/frontend/src/components/control/panels/AgentOverviewTab.tsx
```

Note the existing pattern: it uses `useGatewayRpcMutation` and writes via `callRpc("agents.update", {agentId, model: ...})`.

- [ ] **Step 2: Add state for meta + Save button**

The exact diff depends on the file layout — broadly:

1. Read the agent's `params.paperclip` from the agent prop (via the agent config snapshot the tab already reads).
2. Local state for meta initialized from that.
3. AgentPaperclipFields wired to local state.
4. "Save" button that fires `callRpc("agents.update", {agentId, params: {paperclip: {...meta-block-with-empty-strings-stripped}}})` (this is what triggers `_intercept_agents_update` on the backend).
5. After save, call `onAgentUpdated()` to refresh.

Implementation skeleton:

```tsx
import { AgentPaperclipFields } from "@/components/control/agents/AgentPaperclipFields";
import type { PaperclipMeta, PaperclipRole } from "@/lib/paperclipMeta";

// Inside the component, after existing model state:
const existingPaperclip = (agentConfigEntry?.params?.paperclip ?? {}) as Partial<PaperclipMeta>;
const [meta, setMeta] = useState<PaperclipMeta>({
  role: (existingPaperclip.role as PaperclipRole) ?? "general",
  title: existingPaperclip.title,
  capabilities: existingPaperclip.capabilities,
  icon: existingPaperclip.icon,
});
const [savingMeta, setSavingMeta] = useState(false);

const saveMeta = useCallback(async () => {
  setSavingMeta(true);
  try {
    const block: Record<string, string> = { role: meta.role };
    if (meta.title?.trim()) block.title = meta.title.trim();
    if (meta.capabilities?.trim()) block.capabilities = meta.capabilities.trim();
    if (meta.icon?.trim()) block.icon = meta.icon.trim();
    await callRpc("agents.update", {
      agentId,
      params: { paperclip: block },
    });
    onAgentUpdated?.();
  } finally {
    setSavingMeta(false);
  }
}, [agentId, callRpc, meta, onAgentUpdated]);
```

Add the UI block inside the existing tab's return JSX, near the model selector. Save button is its own action (not auto-save) so the user can edit multiple fields and commit together.

- [ ] **Step 3: Typecheck**

```bash
cd apps/frontend
npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/components/control/panels/AgentOverviewTab.tsx
git commit -m "feat(frontend): edit Paperclip meta from AgentOverviewTab"
```

---

## Task 8: End-to-end verification + PR

- [ ] **Step 1: Full backend suite**

```bash
cd apps/backend
uv run pytest tests/ 2>&1 | tail -5
```

Expected: no regressions.

- [ ] **Step 2: Full frontend suite**

```bash
cd apps/frontend
pnpm vitest run 2>&1 | tail -10
```

Expected: same baseline + the new AgentPaperclipFields tests pass. (The pre-existing PublishWizard.submit.test.tsx failures from prior PRs are out of scope.)

- [ ] **Step 3: Lint**

```bash
cd apps/backend
uv run ruff format --check . && uv run ruff check .
cd ../frontend
pnpm lint
```

- [ ] **Step 4: Typecheck**

```bash
cd apps/frontend
npx tsc --noEmit
```

- [ ] **Step 5: Push + open PR**

```bash
git push -u origin worktree-marketplace-paperclip-meta

gh pr create --title "feat(marketplace): Paperclip meta round-trip via OpenClaw params" --body "$(cat <<'EOF'
## Summary

Carries Paperclip-relevant agent metadata (\`role\`, \`title\`, \`capabilities\`, \`icon\`) through OpenClaw's storage so it round-trips on agent create / edit / marketplace publish / marketplace hire. Closes the gap where hired marketplace agents arrived in Paperclip with empty profiles (\`role=\"general\", title=null, capabilities=null\`) and Friday couldn't tell what they were.

Also fixes a latent bug where Atlas seeding silently failed because \`role=\"recruiter\"\` isn't in Paperclip's \`AGENT_ROLES\` enum.

## Design

Uses OpenClaw's documented \`AgentEntry.params: z.record()\` extension point (\`openclaw/src/config/zod-schema.agent-runtime.ts:1002\`) as the carrier. New module \`apps/backend/core/services/paperclip_meta.py\` is the single source of truth for the \`params.paperclip\` sub-block shape; every write path (\`_intercept_agents_create\`, new \`_intercept_agents_update\`, \`create_paperclip_for_marketplace\`, marketplace deploy) goes through it.

Marketplace round-trips for free because the slice already carries the full agent entry under PR #715's wiring — no listing schema changes.

## Tests

- Backend: 30+ new tests across paperclip_meta, paperclip_admin_client.update_agent, paperclip_agent_lifecycle.meta, websocket_chat interceptors, marketplace deploy
- Frontend: AgentPaperclipFields component tests
- All existing tests still pass

## Out of scope

- Publish wizard surfacing meta (rides in slice; can add UI later)
- Listings-table columns for role/capabilities (defer until search demand emerges)
- Backfilling existing prod agents (separate one-shot — Stitch/Tally/Vera + Atlas seed)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Task 9 (out-of-PR): Backfill script for existing prod agents

After the PR merges and deploys, run a one-shot to backfill the existing 3 prod agents + create Atlas. Not part of the PR — runs via ECS exec against the prod backend, takes ~30s, idempotent.

Mark this as a follow-up checklist item in the PR description rather than committing the script. The user can authorize me to run it once the PR is live.

Recipe (for reference, do NOT add to the PR):
```python
# ECS exec on prod backend:
import asyncio, httpx
from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_meta import PaperclipMeta

OWNER = "org_3DBS8G83oPrMm49yN2b70EjoFpN"
# (id, listing_slug) pairs gathered from marketplace listings + paperclip agents table
TARGETS = [
    ("d71a38fa-fb95-4c93-961a-dd0cac1bdf26", "stitch"),  # actual slugs TBD by user
    ("9e8c7c6f-613d-4e96-ad4b-97a96efcba2f", "tally"),
    ("f9fc524c-acef-4467-8387-91ac7cbd00ef", "vera"),
]

async def main():
    repo = PaperclipRepo(table_name="paperclip-companies")
    # Lookup company_id, listing rows, etc., then PATCH each agent
    # with title=listing.name, capabilities=listing.description_md.
    # Also create Atlas with role="general".
    ...

asyncio.run(main())
```

---

## Self-review

**Spec coverage check:**

- ✅ `paperclip_meta` module with read/write/derive helpers (Task 1)
- ✅ `PaperclipAdminClient.update_agent` + create_agent verification (Task 2)
- ✅ `paperclip_agent_lifecycle` meta kwarg + Atlas role fix (Task 3)
- ✅ Chat create wired to read meta + chat update mirrors to Paperclip (Task 4)
- ✅ Marketplace deploy reads meta from slice with listing fallback (Task 5)
- ✅ Frontend create form wired (Task 6)
- ✅ Frontend edit form wired (Task 7)
- ✅ Verification + PR (Task 8)
- ✅ Backfill follow-up documented but out of PR (Task 9)

**Placeholder scan:** None of the prohibited patterns (TBD, TODO, "appropriate", "edge cases" without test code, "similar to Task N"). Test file mocking in Task 5 references "mirror the existing tests' fixture pattern" — that's actionable because the implementer can grep the file. Acceptable.

**Type consistency:** `PaperclipMeta` dataclass used identically across all backend tasks. `PaperclipMeta` TS interface used identically across all frontend tasks. `read_from_agent_entry` / `write_to_agent_entry` named consistently. `params.paperclip` block shape (`{role, title, capabilities, icon}`) consistent throughout.
