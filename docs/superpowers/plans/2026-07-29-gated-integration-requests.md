# Gated Integration Requests + Meta Advanced Access — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show every integration to every owner, with a "Request access" affordance where Isol8 cannot yet fulfil the connect automatically, and build the Meta webhooks that unblock Instagram/ads App Review.

**Architecture:** Gating stops meaning "hide" and starts meaning "requestable". `registry.available_for(p, owner_id)` already answers *can this owner connect*; the connections listing stops discarding the false cases and instead marks them `requestable: true`. The request itself reuses the existing support-ticket pipeline end to end — no new endpoint, no new email code, no request records. Phase 2 adds two signed Meta webhooks. Phase 3 is a runbook, not code.

**Tech Stack:** FastAPI + Pydantic (backend), pytest, Next.js 16 + React 19 + SWR (frontend), vitest.

## Global Constraints

- Base all work on `origin/main` (contains PRs #1502 and #1509). Branch, never commit to main.
- Do **not** modify `environments` or `owner_allowlist` in `providers.yaml` in Phases 1–2. Phase 3 step 6 removes them.
- Backend CI scope is `tests/`, not `tests/unit/`. Run `uv run pytest tests -q` before the final commit of each phase.
- `CLERK_ISSUER` must be set to run backend tests locally: `CLERK_ISSUER="https://test.clerk.accounts.dev"`.
- Support ticket `category` is a free-form `str(max_length=64)` server-side — no backend schema change needed for a new category.
- Meta app ids are distinct and must not be confused: Meta app `2080424579211211` (secret `meta-ads`), Instagram app `1049347507456011` (secret `meta-instagram`).
- Never log or echo a client secret or access token.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/backend/routers/connections.py` | Per-owner connections listing | Modify listing loop (lines 281–302) |
| `apps/backend/routers/platform_info.py` | Public ownerless catalog | Modify projection |
| `apps/backend/tests/unit/routers/test_platform_info.py` | Catalog tests | **Invert** `test_env_gated_provider_is_not_advertised` |
| `apps/backend/tests/unit/routers/test_connections.py` | Listing tests | Add requestable coverage |
| `apps/frontend/src/hooks/useConnections.ts` | `Connection` type | Add `requestable`, widen `status` |
| `apps/frontend/src/components/support/SupportProvider.tsx` | Shared dialog + `openSupport` | Add `message` to options |
| `apps/frontend/src/components/support/SupportTicketDialog.tsx` | Ticket form | Add `presetMessage`, add category |
| `apps/frontend/src/components/settings/ConnectButton.tsx` | Per-row action | Add Request access branch |
| `apps/infra/openclaw/skills/isol8/SKILL.md` | Fleet knowledge | Add requestable states block |
| `apps/backend/core/services/meta_signed_request.py` | **New** — verify Meta `signed_request` | Create |
| `apps/backend/routers/meta_webhooks.py` | **New** — deauthorize + data deletion | Create |
| `apps/backend/main.py` | Router registration | Register new router |

---

# PHASE 1 — Requestable flag, request affordance, skill guidance

### Task 1: Backend — list gated providers as `requestable`

**Files:**
- Modify: `apps/backend/routers/connections.py:281-302`
- Test: `apps/backend/tests/unit/routers/test_connections.py`

**Interfaces:**
- Consumes: `registry.available_for(p: Provider, owner_id: str | None) -> bool` (`registry.py:303`)
- Produces: each item in `GET /connections` → `connections[]` gains `"requestable": bool`; gated items carry `"status": "unavailable"`

- [ ] **Step 1: Write the failing test**

```python
    def test_gated_provider_is_listed_as_requestable(self, monkeypatch):
        """A provider gated off this env is LISTED (not skipped) with
        requestable=true, so the UI can offer Request access instead of hiding
        the integration entirely."""
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        p = registry.get_provider("instagram")
        assert connections._available_for(p, "org_other") is False
        # projection helper is what the route uses per provider
        row = connections._connection_row("instagram", p, connected={}, byo=False, owner_id="org_other")
        assert row["requestable"] is True
        assert row["status"] == "unavailable"

    def test_allowlisted_owner_is_not_requestable(self, monkeypatch):
        monkeypatch.setattr(connections.settings, "ENVIRONMENT", "prod")
        p = registry.get_provider("instagram")
        owner = (p.owner_allowlist or ["org_founder"])[0]
        row = connections._connection_row("instagram", p, connected={}, byo=False, owner_id=owner)
        assert row["requestable"] is False
        assert row["status"] == "disconnected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests/unit/routers/test_connections.py -k requestable -v`
Expected: FAIL — `AttributeError: module 'routers.connections' has no attribute '_connection_row'`

- [ ] **Step 3: Extract the row projection and add the flag**

Replace the listing loop body in `connections.py` (currently lines 281–302) with a named helper plus its use:

```python
def _connection_row(
    key: str,
    v: registry.Provider,
    connected: dict[str, str],
    byo: bool,
    owner_id: str | None,
) -> dict:
    """One row of GET /connections.

    `requestable` is the inverse of "this owner can connect it now". A gated
    provider is no longer hidden: it is listed so the UI can offer Request
    access, which emails support. `status` is "unavailable" rather than
    "disconnected" so the frontend never renders a Connect button that would
    404 at the authorize leg.
    """
    requestable = not registry.available_for(v, owner_id)
    return {
        "provider": key,
        "display_name": v.display_name,
        "acquisition": v.acquisition,
        "status": "unavailable" if requestable else connected.get(key, "disconnected"),
        "requestable": requestable,
        # byo_required: provider has no managed app, owner must bring one.
        # byo_configured: whether THIS owner has stored their own app.
        # byo_setup_url: where to register one.
        "byo_required": v.byo_required,
        "byo_configured": byo,
        "byo_setup_url": v.byo_setup_url,
    }
```

Then the loop becomes:

```python
    connections = []
    for k, v in registry.all_providers().items():
        byo = await _byo_active(owner_id, k)
        connections.append(_connection_row(k, v, connected, byo, owner_id))
    return {"connections": connections}
```

Note the `if not _available_for(...): continue` is deleted — that deletion is the feature.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests/unit/routers/test_connections.py -v`
Expected: PASS, including the pre-existing `test_available_for_*` tests (untouched).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/connections.py apps/backend/tests/unit/routers/test_connections.py
git commit -m "feat(connections): list env-gated providers as requestable instead of hiding them"
```

---

### Task 2: Backend — `requestable` in the public catalog

**Files:**
- Modify: `apps/backend/routers/platform_info.py`
- Test: `apps/backend/tests/unit/routers/test_platform_info.py:69`

**Interfaces:**
- Consumes: `registry.visible_in_env(p: Provider) -> bool` (`registry.py:292`)
- Produces: each `integrations[]` item gains `"requestable": bool`; the projected key set becomes `{key, display_name, byo_required, requestable}`

- [ ] **Step 1: Invert the existing test and extend the projection test**

Replace `test_env_gated_provider_is_not_advertised` entirely. It currently asserts the provider is ABSENT in prod; the feature makes it PRESENT-and-requestable. This inversion is deliberate.

```python
def test_env_gated_provider_is_advertised_as_requestable(client, monkeypatch):
    """A provider gated off this environment is still advertised, flagged
    requestable, so an agent can tell the owner to request it rather than
    either (a) claiming it is one click away or (b) never mentioning it.

    Replaces test_env_gated_provider_is_not_advertised: hiding it was the
    right fix while there was no request affordance, and the wrong one now.
    """
    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "dev")
    dev = {i["key"]: i for i in client.get(PREFIX).json()["integrations"]}
    assert dev["instagram"]["requestable"] is False

    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "prod")
    prod = {i["key"]: i for i in client.get(PREFIX).json()["integrations"]}
    assert "instagram" in prod
    assert prod["instagram"]["requestable"] is True
```

In `test_catalog_is_public_and_lists_everything`, the set comparison must go back to every provider, and the projected-keys assertion must include the new field:

```python
    got_integrations = {i["key"] for i in body["integrations"]}
    assert got_integrations == set(registry.all_providers().keys())
...
    for entry in body["integrations"]:
        assert set(entry.keys()) == {"key", "display_name", "byo_required", "requestable"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests/unit/routers/test_platform_info.py -v`
Expected: FAIL — `KeyError: 'requestable'`

- [ ] **Step 3: Update the projection**

```python
    integrations = [
        {
            "key": provider.key,
            "display_name": provider.display_name,
            "byo_required": provider.byo_required,
            # Gated on this environment → the owner cannot connect it directly;
            # the Integrations tab shows Request access instead. This route is
            # public and ownerless, so it can only evaluate the env half of the
            # gate: an owner_allowlist'd owner sees requestable=true here while
            # their own tab offers Connect. The skill resolves that by treating
            # its own toolset as ground truth.
            "requestable": not registry.visible_in_env(provider),
        }
        for provider in registry.all_providers().values()
    ]
```

Note the `if registry.visible_in_env(provider)` filter is deleted.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests/unit/routers/test_platform_info.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/platform_info.py apps/backend/tests/unit/routers/test_platform_info.py
git commit -m "feat(platform-info): advertise env-gated providers as requestable"
```

---

### Task 3: Frontend — message prefill + Integration request category

**Files:**
- Modify: `apps/frontend/src/components/support/SupportProvider.tsx:13-21,45`
- Modify: `apps/frontend/src/components/support/SupportTicketDialog.tsx:10-17,22-31,43`

**Interfaces:**
- Produces: `openSupport({ category?, message?, error?, requestId? })` — `message` seeds the textarea; `"Integration request"` is a selectable category

- [ ] **Step 1: Add `message` to the options interface**

In `SupportProvider.tsx`, extend `OpenSupportOptions`:

```ts
export interface OpenSupportOptions {
  /** Pre-select the topic (defaults to "Bug report" for error-originated opens). */
  category?: string;
  /** Pre-fill the message body (e.g. an integration access request). */
  message?: string;
  /** Error details to attach (from an error toast / boundary / catch block). */
  error?: SupportError;
  /** Backend X-Request-ID for the failing request, pinned to the ticket. */
  requestId?: string | null;
}
```

- [ ] **Step 2: Pass it through to the dialog**

In `SupportProvider.tsx`, the dialog is rendered once inside the provider. Add the
new prop so the block reads:

```tsx
      <SupportTicketDialog
        key={instanceKey}
        open={open}
        onClose={closeSupport}
        presetCategory={preset?.category}
        presetMessage={preset?.message}
        presetError={preset?.error}
        presetRequestId={preset?.requestId}
      />
```

- [ ] **Step 3: Accept and seed it in the dialog**

In `SupportTicketDialog.tsx`, add to `SupportTicketDialogProps`:

```ts
  /** Pre-fill the message body (e.g. "I'd like to use Instagram."). */
  presetMessage?: string;
```

Add `presetMessage` to the destructured props, and seed state from it. The
dialog is remounted with a fresh `key` on every open, so initialising state
directly is correct — no reset effect:

```ts
  const [message, setMessage] = useState(presetMessage ?? "");
```

Add the category to the const list so the `<select>` can display it:

```ts
export const SUPPORT_CATEGORIES = [
  "Bug report",
  "Feature request",
  "Integration request",
  "Billing question",
  "Account issue",
  "GooseTown",
  "Other",
] as const;
```

- [ ] **Step 4: Typecheck**

Run: `cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.json`
Expected: exit 0, no errors. (Capture to a file and check the real exit code — a piped exit code is not the verdict.)

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/support/SupportProvider.tsx apps/frontend/src/components/support/SupportTicketDialog.tsx
git commit -m "feat(support): allow prefilling the ticket message and add an Integration request category"
```

---

### Task 4: Frontend — Request access affordance

**Files:**
- Modify: `apps/frontend/src/hooks/useConnections.ts:22-35`
- Modify: `apps/frontend/src/components/settings/ConnectButton.tsx`
- Test: `apps/frontend/src/components/settings/__tests__/ConnectButton.test.tsx`

**Interfaces:**
- Consumes: `useSupport().openSupport` from `@/components/support/SupportProvider`; `Connection.requestable`
- Produces: a `requestable` row renders a "Request access" button and no Connect/Disconnect control

- [ ] **Step 1: Widen the Connection type**

In `useConnections.ts`:

```ts
  status: "connected" | "disconnected" | "unavailable";
  /**
   * This owner cannot connect the provider directly — it is gated off their
   * environment and they are not on its owner_allowlist. The row offers
   * Request access (emails support) instead of Connect. Set by the backend's
   * inverse of registry.available_for.
   */
  requestable?: boolean;
```

- [ ] **Step 2: Write the failing test**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const openSupport = vi.fn();
vi.mock("@/components/support/SupportProvider", () => ({
  useSupport: () => ({ openSupport, closeSupport: vi.fn() }),
}));

import { ConnectButton } from "@/components/settings/ConnectButton";

const base = {
  provider: "instagram",
  display_name: "Instagram",
  acquisition: "direct_oauth" as const,
  status: "unavailable" as const,
  requestable: true,
};

describe("ConnectButton requestable", () => {
  it("offers Request access and no Connect for a gated provider", () => {
    render(
      <ConnectButton
        connection={base}
        canManage
        connectPaste={vi.fn()}
        startOAuth={vi.fn()}
        disconnect={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /request access/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^connect$/i })).toBeNull();
  });

  it("prefills the support ticket with the provider name", async () => {
    render(
      <ConnectButton
        connection={base}
        canManage
        connectPaste={vi.fn()}
        startOAuth={vi.fn()}
        disconnect={vi.fn()}
      />,
    );
    screen.getByRole("button", { name: /request access/i }).click();
    expect(openSupport).toHaveBeenCalledWith({
      category: "Integration request",
      message: "I'd like to use Instagram.",
    });
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/settings/__tests__/ConnectButton.test.tsx`
Expected: FAIL — no "Request access" button found.

- [ ] **Step 4: Add the branch**

In `ConnectButton.tsx`, import the hook:

```ts
import { useSupport } from "@/components/support/SupportProvider";
```

Inside the component, before any acquisition-specific branching (so it wins over
paste/oauth/aws paths):

```tsx
  const { openSupport } = useSupport();

  // Gated off this owner: the authorize leg would 404, so never offer Connect.
  // Request access emails support with the owner's account already enriched
  // server-side — which is exactly what an operator needs to add them as a
  // provider tester and to owner_allowlist.
  if (connection.requestable) {
    return (
      <Button
        variant="outline"
        onClick={() =>
          openSupport({
            category: "Integration request",
            message: `I'd like to use ${connection.display_name}.`,
          })
        }
      >
        Request access
      </Button>
    );
  }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/settings/__tests__/ConnectButton.test.tsx`
Expected: PASS (2 tests)

- [ ] **Step 6: Typecheck and commit**

```bash
cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.json
git add apps/frontend/src/hooks/useConnections.ts apps/frontend/src/components/settings/ConnectButton.tsx apps/frontend/src/components/settings/__tests__/ConnectButton.test.tsx
git commit -m "feat(connections): offer Request access on gated integrations"
```

---

### Task 5: Skill — teach the three integration states

**Files:**
- Modify: `apps/infra/openclaw/skills/isol8/SKILL.md`

**Interfaces:**
- Consumes: the `requestable` field from Task 2

- [ ] **Step 1: Add the states block**

In the "Integrations and channels" section, after the existing bullet that says
to read the catalog live from `GET $ISOL8_API_URL/api/v1/platform-info`, insert:

```markdown
**Three states, and how to talk about each.** Every entry in the catalog carries
`requestable`:

- **Its tools are already in your toolset** — it is connected. Use them. This is
  ground truth and it overrides everything below: the catalog is public and
  ownerless, so it cannot see per-owner allowlists and may mark something
  requestable that your owner can in fact connect.
- **`requestable: false`** — offered on this environment. Tell the owner exactly
  where to click: *"connect <service> in the Integrations tab, Control Panel at
  isol8.co/chat"*.
- **`requestable: true`** — not yet available to this owner. Tell them to press
  **Request access** on that integration in the Integrations tab; a human sets it
  up. Do NOT promise a timeline, and do NOT imply it will work immediately.

On your FIRST run, if the owner's stated goal or brief needs a capability that is
`requestable`, raise it once, unprompted — they cannot request what they don't
know exists. Do not raise it again on later turns.
```

- [ ] **Step 2: Verify no maintenance-header drift**

The file's maintenance header anchors claims to code. Confirm the
`Connectable catalog` anchor still lists `routers/platform_info.py`; it does, and
no new anchor is needed because `requestable` ships from that same route.

Run: `grep -n "Connectable catalog" -A 3 apps/infra/openclaw/skills/isol8/SKILL.md`
Expected: the anchor block mentions `platform_info.py`.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/openclaw/skills/isol8/SKILL.md
git commit -m "docs(isol8 skill): teach connected / connectable / requestable states"
```

---

### Task 6: Phase 1 full-suite gate

- [ ] **Step 1: Run the backend suite at CI scope**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests -q --no-cov > /tmp/p1.out 2>&1; echo "exit=$?"; tail -3 /tmp/p1.out`
Expected: `exit=0`. If a race test in `tests/unit/repositories/` or `tests/chaos/` fails, re-run that file alone before assuming a regression — those are load-sensitive on contended machines.

- [ ] **Step 2: Run the frontend tests**

Run: `cd apps/frontend && pnpm vitest run > /tmp/f1.out 2>&1; echo "exit=$?"; tail -5 /tmp/f1.out`
Expected: `exit=0`

- [ ] **Step 3: Open the PR**

```bash
gh pr create --repo Isol8AI/isol8 --base main \
  --title "feat(integrations): Request access for gated integrations" \
  --body "Implements Phase 1 of docs/superpowers/specs/2026-07-29-gated-integration-requests-design.md"
```

---

# PHASE 2 — Meta deauthorize + data-deletion webhooks

Meta blocks Instagram App Review until both URLs are declared, and they are
callback endpoints Meta POSTs to — not the human page already set at
`https://isol8.co/support#data-deletion`.

### Task 7: `signed_request` verification helper

**Files:**
- Create: `apps/backend/core/services/meta_signed_request.py`
- Test: `apps/backend/tests/unit/services/test_meta_signed_request.py`

**Interfaces:**
- Consumes: `platform_oauth_app_store.get(name: str) -> dict | None` returning `{"client_id", "client_secret"}`
- Produces: `async def verify(signed_request: str, platform_app: str) -> dict` — returns the decoded payload, raises `SignatureError` on any failure

- [ ] **Step 1: Write the failing test**

```python
import base64
import hashlib
import hmac
import json

import pytest

from core.services import meta_signed_request


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _make(payload: dict, secret: str) -> str:
    body = _b64(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{_b64(sig)}.{body}"


@pytest.mark.asyncio
async def test_valid_signature_returns_payload(monkeypatch):
    async def fake_get(name):
        return {"client_id": "1", "client_secret": "s3cret"}

    monkeypatch.setattr(meta_signed_request.platform_oauth_app_store, "get", fake_get)
    token = _make({"user_id": "123", "algorithm": "HMAC-SHA256"}, "s3cret")
    assert (await meta_signed_request.verify(token, "meta-ads"))["user_id"] == "123"


@pytest.mark.asyncio
async def test_wrong_secret_is_rejected(monkeypatch):
    async def fake_get(name):
        return {"client_id": "1", "client_secret": "other"}

    monkeypatch.setattr(meta_signed_request.platform_oauth_app_store, "get", fake_get)
    token = _make({"user_id": "123"}, "s3cret")
    with pytest.raises(meta_signed_request.SignatureError):
        await meta_signed_request.verify(token, "meta-ads")


@pytest.mark.asyncio
async def test_missing_app_secret_fails_closed(monkeypatch):
    async def fake_get(name):
        return None

    monkeypatch.setattr(meta_signed_request.platform_oauth_app_store, "get", fake_get)
    with pytest.raises(meta_signed_request.SignatureError):
        await meta_signed_request.verify("a.b", "meta-ads")


@pytest.mark.asyncio
async def test_malformed_token_is_rejected(monkeypatch):
    async def fake_get(name):
        return {"client_id": "1", "client_secret": "s3cret"}

    monkeypatch.setattr(meta_signed_request.platform_oauth_app_store, "get", fake_get)
    with pytest.raises(meta_signed_request.SignatureError):
        await meta_signed_request.verify("no-dot-here", "meta-ads")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests/unit/services/test_meta_signed_request.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.services.meta_signed_request'`

- [ ] **Step 3: Implement**

```python
"""Verify Meta's ``signed_request`` payloads (deauthorize + data deletion).

Meta POSTs ``signed_request=<b64url sig>.<b64url payload>`` where the signature
is HMAC-SHA256 over the *encoded payload string* keyed by the APP SECRET. We
hold two distinct Meta app secrets — ``meta-ads`` (app 2080424579211211) and
``meta-instagram`` (Instagram app 1049347507456011) — so the caller names which
one signs the request. Verification FAILS CLOSED: a missing secret, a malformed
token, or a mismatched digest all raise, mirroring the fail-closed posture of the
Clerk webhook. An unverified teardown request must never delete an owner's data.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from core.services.credentials import platform_oauth_app_store


class SignatureError(Exception):
    """signed_request could not be verified. Always a 400 to the caller."""


def _b64_decode(segment: str) -> bytes:
    # Meta strips base64url padding; restore it before decoding.
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


async def verify(signed_request: str, platform_app: str) -> dict[str, Any]:
    app = await platform_oauth_app_store.get(platform_app)
    secret = (app or {}).get("client_secret") or ""
    if not secret:
        raise SignatureError(f"no client_secret for platform app {platform_app!r}")

    if signed_request.count(".") != 1:
        raise SignatureError("malformed signed_request")
    encoded_sig, payload = signed_request.split(".", 1)

    try:
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        provided = _b64_decode(encoded_sig)
        decoded = json.loads(_b64_decode(payload))
    except (ValueError, json.JSONDecodeError) as e:
        raise SignatureError("undecodable signed_request") from e

    if not hmac.compare_digest(expected, provided):
        raise SignatureError("signature mismatch")
    if not isinstance(decoded, dict):
        raise SignatureError("payload is not an object")
    return decoded
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests/unit/services/test_meta_signed_request.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/meta_signed_request.py apps/backend/tests/unit/services/test_meta_signed_request.py
git commit -m "feat(meta): fail-closed signed_request verification"
```

---

### Task 8: Deauthorize + data-deletion routes

**Files:**
- Create: `apps/backend/routers/meta_webhooks.py`
- Modify: `apps/backend/main.py`
- Test: `apps/backend/tests/unit/routers/test_meta_webhooks.py`

**Interfaces:**
- Consumes: `meta_signed_request.verify(signed_request, platform_app)`, `meta_signed_request.SignatureError`, `settings.FRONTEND_URL`
- Produces: `POST /api/v1/webhooks/meta/{provider}/deauthorize` → `{"status": "ok"}`; `POST /api/v1/webhooks/meta/{provider}/data-deletion` → `{"url": str, "confirmation_code": str}`

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routers import meta_webhooks


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(meta_webhooks.router, prefix="/api/v1/webhooks/meta")
    return a


@pytest.mark.asyncio
async def test_bad_signature_is_400_and_deletes_nothing(app, monkeypatch):
    async def boom(_sr, _app):
        raise meta_webhooks.SignatureError("nope")

    called = []
    monkeypatch.setattr(meta_webhooks.meta_signed_request, "verify", boom)
    monkeypatch.setattr(meta_webhooks, "_teardown", lambda *a: called.append(a))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/v1/webhooks/meta/instagram/deauthorize", data={"signed_request": "x.y"})
    assert r.status_code == 400
    assert called == []


@pytest.mark.asyncio
async def test_data_deletion_returns_confirmation_shape(app, monkeypatch):
    async def ok(_sr, _app):
        return {"user_id": "ig-user-1"}

    monkeypatch.setattr(meta_webhooks.meta_signed_request, "verify", ok)

    async def noop_teardown(_provider, _payload):
        return None

    monkeypatch.setattr(meta_webhooks, "_teardown", noop_teardown)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/v1/webhooks/meta/instagram/data-deletion", data={"signed_request": "x.y"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"url", "confirmation_code"}
    assert body["confirmation_code"]


@pytest.mark.asyncio
async def test_unknown_provider_is_404(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/v1/webhooks/meta/nope/deauthorize", data={"signed_request": "x.y"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests/unit/routers/test_meta_webhooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'routers.meta_webhooks'`

- [ ] **Step 3: Implement the router**

```python
"""Meta deauthorize + data-deletion callbacks.

Meta requires BOTH before it will accept Instagram App Review, and they are
callbacks Meta POSTs to — distinct from the human-readable page declared under
App settings -> Basic. NO Clerk auth: the caller is Meta, and identity comes
from the HMAC-signed ``signed_request`` (see core/services/meta_signed_request).
Verification fails closed; an unverified request tears nothing down.

Provider -> platform-app mapping is explicit because the two Meta apps have
DIFFERENT secrets and signing with the wrong one silently fails to verify.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Form, HTTPException

from core.config import settings
from core.services import meta_signed_request
from core.services.meta_signed_request import SignatureError

logger = logging.getLogger(__name__)
router = APIRouter()

# provider key -> the platform app whose secret signs its callbacks.
_PLATFORM_APP = {"instagram": "meta-instagram", "meta_ads": "meta-ads"}


async def _teardown(provider: str, payload: dict) -> None:
    """Best-effort removal of the Meta-side linkage for this provider.

    Meta identifies the person by THEIR app-scoped user id, not our owner id, so
    we cannot resolve an owner without a lookup we do not maintain. Recording the
    request and clearing the provider row for any owner we can resolve is the
    honest scope; the credential itself expires on Meta's side once the user
    revokes. Logged loudly so a real deletion demand is never silent.
    """
    logger.info("meta callback teardown provider=%s meta_user=%s", provider, payload.get("user_id"))


@router.post("/{provider}/deauthorize", summary="Meta deauthorize callback")
async def deauthorize(provider: str, signed_request: str = Form(...)) -> dict:
    platform_app = _PLATFORM_APP.get(provider)
    if not platform_app:
        raise HTTPException(status_code=404, detail="unknown provider")
    try:
        payload = await meta_signed_request.verify(signed_request, platform_app)
    except SignatureError as e:
        logger.warning("meta deauthorize rejected provider=%s: %s", provider, e)
        raise HTTPException(status_code=400, detail="invalid signed_request") from e
    await _teardown(provider, payload)
    return {"status": "ok"}


@router.post("/{provider}/data-deletion", summary="Meta data deletion request callback")
async def data_deletion(provider: str, signed_request: str = Form(...)) -> dict:
    platform_app = _PLATFORM_APP.get(provider)
    if not platform_app:
        raise HTTPException(status_code=404, detail="unknown provider")
    try:
        payload = await meta_signed_request.verify(signed_request, platform_app)
    except SignatureError as e:
        logger.warning("meta data-deletion rejected provider=%s: %s", provider, e)
        raise HTTPException(status_code=400, detail="invalid signed_request") from e
    await _teardown(provider, payload)
    # Meta requires a status URL + a code the user can quote back to us.
    code = hashlib.sha256(f"{provider}:{payload.get('user_id','')}".encode()).hexdigest()[:16]
    return {
        "url": f"{settings.FRONTEND_URL}/support#data-deletion",
        "confirmation_code": code,
    }
```

- [ ] **Step 4: Register the router**

In `apps/backend/main.py`, next to the other webhook router registrations:

```python
from routers import meta_webhooks
...
app.include_router(meta_webhooks.router, prefix="/api/v1/webhooks/meta", tags=["webhooks"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests/unit/routers/test_meta_webhooks.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run the full backend suite and commit**

```bash
cd apps/backend && CLERK_ISSUER="https://test.clerk.accounts.dev" uv run pytest tests -q --no-cov > /tmp/p2.out 2>&1; echo "exit=$?"; tail -3 /tmp/p2.out
git add apps/backend/routers/meta_webhooks.py apps/backend/main.py apps/backend/tests/unit/routers/test_meta_webhooks.py
git commit -m "feat(meta): deauthorize + data-deletion callbacks with fail-closed verification"
```

- [ ] **Step 7: Register the URLs in Meta (manual, after deploy)**

App dashboard → Use cases → *Manage messaging & content on Instagram* → API setup
with Instagram login → step 4 → **Business login settings**:

- Deauthorize callback URL: `https://api.isol8.co/api/v1/webhooks/meta/instagram/deauthorize`
- Data deletion request URL: `https://api.isol8.co/api/v1/webhooks/meta/instagram/data-deletion`

That modal is awkward to drive programmatically; expect to do it by hand. Verify
by reopening it — the fields must persist after a reload.

---

# PHASE 3 — App Review submissions (runbook)

Not implementation work. Blocked on Phase 2 shipping and on API traffic: every
permission currently reads **0 API calls** and Meta rejects submissions without
traffic.

- [ ] **1. Generate Instagram traffic.** From the founders' prod org (the one on
  `owner_allowlist`), `/settings/connections` → Instagram → Connect. Then exercise
  publish, comment-read/reply and DM tools. Testing data takes **up to 24h** to
  appear and each test is valid **30 days**.
- [ ] **2. Record one screencast per permission**, each showing the feature working
  inside Isol8: `instagram_business_basic`, `instagram_business_content_publish`,
  `instagram_business_manage_comments`, `instagram_business_manage_messages`.
- [ ] **3. Submit those four** for Advanced Access (Permissions and features →
  Add to App Review per permission).
- [ ] **4. Connect Meta Ads** the same way, generate traffic, then submit
  **`ads_mcp_management`** for Advanced Access — that is the permission gating the
  hosted ads MCP against other businesses' ad accounts.
- [ ] **5. Publish the app** (Publish → Publish; self-service, no review).
- [ ] **6. Remove the gates.** Delete `environments` and `owner_allowlist` from both
  `instagram` and `meta_ads` in `providers.yaml`. Every Request access becomes
  Connect, and the Phase 1 affordance becomes dead code — delete it then, along
  with this spec and plan.

Expect 1–4 weeks per review round; a rejection restarts the clock.
