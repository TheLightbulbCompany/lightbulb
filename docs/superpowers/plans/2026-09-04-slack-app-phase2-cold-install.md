# Slack App Phase 2 — Cold Install (No Account → Friday in Slack)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Slack workspace admin with NO Lightbulb account clicks Add to Slack, and a few minutes later Friday DMs them in Slack — a Clerk org and a free container were minted from the OAuth callback alone.

**Architecture:** The Phase 1 rail is unchanged (verify → route → re-sign → forward). Phase 2 adds four things in front of and around it: (1) identity minting from the installer's Slack email (`users:read.email`) — Clerk user + org, the org named after the workspace; (2) a Slack-origin `trial_local` row that the org billing gate admits; (3) the installs table becoming a source for config generation, so a container provisioned AFTER the install picks up its Slack account on first boot; (4) wake-on-inbound and a templated "upgrade" reply for capped/expired owners. Decision 3 holds: Slack peers are free, never in Clerk.

**Tech Stack:** FastAPI + httpx, Clerk Backend API, DynamoDB/moto, the existing provisioning executor (`scripts/provision_runner.py`), `@openclaw/slack` http mode.

**Spec:** `docs/superpowers/specs/2026-09-04-slack-marketplace-app-design.md` §2 (decisions 1–3), §3.5 steps 4–7, §3.6 (org resolution), §3.7 (free tier + wake). Phase 1 must be merged first — this plan imports `slack_install_service`, `slack_installs_repo`, `routers/slack_app.py` from it.

## Global Constraints

- Same tooling/commit/branch rules as Phase 1's Global Constraints (pytest via `uv run`, ruff, no push, commit trailers, worktree `~/dev/wt-slack-spec`).
- **The seven autonomy gate sites do not move.** Only the org *provisioning* gate learns to admit a Slack-origin trial; routines, webhooks, nightly CEO, Meta, Paperclip triggers, hooks proxy and Stripe Connect keep refusing `trial_local`.
- The `trial_local` machinery is keyed on `subscription_status == "trial_local"`, not on owner type. Any place that assumes trial ⇒ personal is a bug to fix in the task that finds it, and to name in the ledger.
- Slack-origin rows carry `origin: "slack"` on the billing row and `origin: "slack"` in the Clerk org's `public_metadata` (`{"source": "slack", "team_id": ...}`).
- Templated blocked reply is sent at most once per `(team_id, channel)` per UTC day, via `webhook_dedup` key `slack:blocked:{team}:{channel}:{YYYY-MM-DD}` with a 24 h TTL.
- Never wake a container for an owner whose `trial_block_reason` is not None (same rule as `agent_mail_delivery`).
- `users:read.email` is used for exactly one call: `users.info` on `authed_user.id` at the callback. Nothing else may call it; the scope justification says so.
- The "which team?" picker is NOT built. Ruling: an installer who admins ≥1 org **without** an active Slack install gets the most recently created such org; otherwise a new org is created. `# ponytail: no picker page; add when an installer with >1 candidate org complains.`
- Spot skip on first provision is NOT built (Phase 4 backlog).

## File map

| file | responsibility |
|---|---|
| `apps/backend/core/services/clerk_admin.py` | `create_user`, `create_organization` |
| `apps/backend/core/repositories/billing_repo.py` | `create_trial_local_if_absent(..., origin=None)` writes `origin` |
| `apps/backend/core/services/provision_gate.py` | org gate admits a Slack-origin `trial_local` row |
| `apps/backend/core/containers/ecs_manager.py` | `_provision_channel_accounts` merges installs-table-derived Slack accounts + bindings |
| `apps/backend/core/services/slack_install_service.py` | `BOT_SCOPES` += `users:read.email`; `record_pending_install`, `bind_pending_installs`, `notify_installer` |
| `apps/backend/core/services/slack_cold_install.py` | the callback's cold path: resolve/mint identity, org, trial row, dispatch provision |
| `apps/backend/routers/slack_app.py` | callback branches on `state.owner_id`; events route wakes and answers blocked owners |
| `apps/backend/scripts/provision_runner.py` | post-step-0 hook binds pending installs to Friday; post-step-1 hook DMs the installer |
| `apps/backend/scripts/slack-app-manifest.yaml` | `users:read.email` |

---

### Task 1: `clerk_admin.create_user` + `create_organization`

**Files:**
- Modify: `apps/backend/core/services/clerk_admin.py` (append after `create_organization_invitation`)
- Test: `apps/backend/tests/unit/services/test_clerk_admin_create.py`

**Interfaces:**
- `async create_user(*, email: str, external_id: str, public_metadata: dict | None = None) -> dict` — Clerk `POST /v1/users` with `email_address=[email]`, `skip_password_requirement=True`, `external_id`, `public_metadata`. Returns Clerk's user JSON (`id` is `user_…`). Raises `HTTPException(503)` on transport error, `HTTPException(<status>)` with Clerk's body on ≥400. Stubs `{"id": f"user_stub_{email}", "stubbed": True}` when `CLERK_SECRET_KEY` is unset.
- `async create_organization(*, name: str, created_by: str, slug: str | None = None, public_metadata: dict | None = None) -> dict` — `POST /v1/organizations`; `created_by` becomes admin. Same error contract; stub `{"id": f"org_stub_{name}", "stubbed": True}`.

- [ ] **Step 1: Write the failing tests**

```python
import os
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import clerk_admin  # noqa: E402


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, status=200, body=None):
        self.status, self.body, self.seen = status, body or {}, []

    async def handle_async_request(self, request):
        self.seen.append(request)
        return httpx.Response(self.status, json=self.body)


def _client(t):
    return lambda *a, **k: httpx.AsyncClient(transport=t)


@pytest.mark.asyncio
async def test_create_user_posts_email_and_external_id(monkeypatch):
    monkeypatch.setattr(clerk_admin.settings, "CLERK_SECRET_KEY", "sk_test")
    t = _Transport(200, {"id": "user_1"})
    with patch.object(clerk_admin.httpx, "AsyncClient", _client(t)):
        out = await clerk_admin.create_user(email="a@b.co", external_id="slack:T1:U1", public_metadata={"source": "slack"})
    assert out["id"] == "user_1"
    req = t.seen[0]
    assert req.url.path == "/v1/users" and req.headers["authorization"] == "Bearer sk_test"
    import json

    body = json.loads(req.content)
    assert body == {
        "email_address": ["a@b.co"],
        "skip_password_requirement": True,
        "external_id": "slack:T1:U1",
        "public_metadata": {"source": "slack"},
    }


@pytest.mark.asyncio
async def test_create_organization_sets_created_by(monkeypatch):
    monkeypatch.setattr(clerk_admin.settings, "CLERK_SECRET_KEY", "sk_test")
    t = _Transport(200, {"id": "org_1"})
    with patch.object(clerk_admin.httpx, "AsyncClient", _client(t)):
        out = await clerk_admin.create_organization(name="Acme", created_by="user_1", public_metadata={"source": "slack", "team_id": "T1"})
    assert out["id"] == "org_1"
    import json

    body = json.loads(t.seen[0].content)
    assert body == {"name": "Acme", "created_by": "user_1", "public_metadata": {"source": "slack", "team_id": "T1"}}
    assert t.seen[0].url.path == "/v1/organizations"


@pytest.mark.asyncio
async def test_clerk_error_surfaces_status(monkeypatch):
    monkeypatch.setattr(clerk_admin.settings, "CLERK_SECRET_KEY", "sk_test")
    t = _Transport(422, {"errors": [{"message": "email taken"}]})
    with patch.object(clerk_admin.httpx, "AsyncClient", _client(t)):
        with pytest.raises(HTTPException) as ei:
            await clerk_admin.create_user(email="a@b.co", external_id="x")
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_stubbed_without_secret(monkeypatch):
    monkeypatch.setattr(clerk_admin.settings, "CLERK_SECRET_KEY", "")
    assert (await clerk_admin.create_user(email="a@b.co", external_id="x"))["stubbed"] is True
    assert (await clerk_admin.create_organization(name="Acme", created_by="user_1"))["stubbed"] is True
```

- [ ] **Step 2: Run to verify they fail** — `cd apps/backend && uv run pytest tests/unit/services/test_clerk_admin_create.py -v` → `AttributeError: create_user`.

- [ ] **Step 3: Implement** — append to `clerk_admin.py`, mirroring `create_organization_invitation`'s error handling exactly (transport error → 503, ≥400 → Clerk's status + body):

```python
async def _post_json(path: str, body: dict, *, what: str) -> dict:
    """POST a JSON body to Clerk; surface Clerk's own status on failure."""
    from fastapi import HTTPException

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(f"{_CLERK_API_BASE}{path}", headers=headers, json=body)
    except httpx.TransportError as e:
        logger.warning("clerk_admin.%s transport error err=%s", what, e)
        raise HTTPException(status_code=503, detail="Clerk API unavailable") from e
    if response.status_code >= 400:
        logger.warning("clerk_admin.%s HTTP %s body=%s", what, response.status_code, response.text)
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


async def create_user(*, email: str, external_id: str, public_metadata: dict | None = None) -> dict:
    """Mint a Clerk user for an identity we learned elsewhere (the Slack
    installer). ``skip_password_requirement`` — they sign in via a ticket
    link, never a password. Clerk creates the address VERIFIED by default."""
    if not settings.CLERK_SECRET_KEY:
        return {"id": f"user_stub_{email}", "stubbed": True}
    body: dict = {"email_address": [email], "skip_password_requirement": True, "external_id": external_id}
    if public_metadata:
        body["public_metadata"] = public_metadata
    return await _post_json("/users", body, what="create_user")


async def create_organization(
    *, name: str, created_by: str, slug: str | None = None, public_metadata: dict | None = None
) -> dict:
    """Mint a Clerk org; ``created_by`` becomes its admin."""
    if not settings.CLERK_SECRET_KEY:
        return {"id": f"org_stub_{name}", "stubbed": True}
    body: dict = {"name": name, "created_by": created_by}
    if slug:
        body["slug"] = slug
    if public_metadata:
        body["public_metadata"] = public_metadata
    return await _post_json("/organizations", body, what="create_organization")
```

- [ ] **Step 4: Run + ruff** — 4 passed. Also run the existing `tests/unit/services/test_clerk_admin*.py` to confirm nothing regressed.
- [ ] **Step 5: Commit** — `feat(backend): clerk_admin.create_user + create_organization for Slack-minted identities`

---

### Task 2: Slack-origin trial row + the org provisioning gate admits it

**Files:**
- Modify: `apps/backend/core/repositories/billing_repo.py:859` (`create_trial_local_if_absent` gains `origin: str | None = None`, written as `origin` when given)
- Modify: `apps/backend/core/services/provision_gate.py` — the org branch that returns `_team_billing_required_gate` (find it: `grep -n "_team_billing_required_gate(" core/services/provision_gate.py`)
- Test: `apps/backend/tests/unit/services/test_provision_gate.py` (append), `apps/backend/tests/unit/repositories/test_billing_repo.py` (append)

**Interfaces:**
- `provision_gate.is_slack_trial(account: Mapping | None) -> bool` → `subscription_status == "trial_local" and origin == "slack"`.
- The org gate: `if is_org and not is_subscription_active(account) and not is_slack_trial(account): return _team_billing_required_gate(...)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_provision_gate.py — append
def test_is_slack_trial():
    from core.services.provision_gate import is_slack_trial

    assert is_slack_trial({"subscription_status": "trial_local", "origin": "slack"}) is True
    assert is_slack_trial({"subscription_status": "trial_local"}) is False
    assert is_slack_trial({"subscription_status": "active", "origin": "slack"}) is False
    assert is_slack_trial(None) is False


def test_org_slack_trial_is_not_sent_to_team_billing_gate():
    """Mirror the existing org-without-subscription test in this file: same
    AuthContext (org admin), same call, but the billing row is a Slack-origin
    trial_local — the gate must NOT be team_billing_required."""
    # Copy the arrange/act lines from the neighbouring test that asserts
    # code == "team_billing_required", substitute the account dict with
    # {"owner_id": ..., "subscription_status": "trial_local", "origin": "slack"},
    # and assert the returned gate is None OR any code other than
    # "team_billing_required".
```

```python
# tests/unit/repositories/test_billing_repo.py — append (moto table fixture already exists in this file)
@pytest.mark.asyncio
async def test_create_trial_local_writes_origin(billing_table):
    from core.repositories import billing_repo

    assert await billing_repo.create_trial_local_if_absent("org_slack", "org", origin="slack") is True
    row = await billing_repo.get_by_owner_id("org_slack")
    assert row["subscription_status"] == "trial_local" and row["origin"] == "slack"
    assert await billing_repo.create_trial_local_if_absent("org_slack", "org", origin="slack") is False
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** — in `billing_repo.create_trial_local_if_absent`, add the kwarg and `if origin: item["origin"] = origin` before the conditional put. In `provision_gate.py`:

```python
def is_slack_trial(account: Mapping[str, Any] | None) -> bool:
    """A card-less trial minted by a Slack install (spec decision 2). The ONLY
    org shape allowed onto ``trial_local``; the autonomy gates still refuse it."""
    return bool(account) and account.get("subscription_status") == "trial_local" and account.get("origin") == "slack"
```

and add `and not is_slack_trial(account)` to the condition guarding `_team_billing_required_gate`. Read the surrounding function to confirm `account` is the billing row in scope there; if the org branch runs before the row is loaded, load it the same way the personal branch does.

- [ ] **Step 4: Run** the whole `test_provision_gate.py` + `test_billing_repo.py`; ruff.
- [ ] **Step 5: Commit** — `feat(backend): Slack-origin trial_local rows; org provisioning gate admits them`

---

### Task 3: Installs table feeds config generation

Why: on a cold install the container is provisioned AFTER the install row exists, so the first `write_user_configs` finds no `channels.slack` on disk. The table must be a source, not just the file.

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py:152` (`_provision_channel_accounts`)
- Modify: `apps/backend/core/services/slack_install_service.py` — export `account_block` and `route_binding` (already public in Phase 1)
- Test: `apps/backend/tests/unit/containers/test_channel_accounts_carry.py` (append)

**Interfaces:** `_provision_channel_accounts(owner_id) -> tuple[accounts, bindings]` now unions the on-disk Slack accounts with `{team_id: account_block(team_id)}` for every ACTIVE install row whose `agent_id` is set, and appends `route_binding(team_id, agent_id)` for each. Table wins over file for the same `team_id`.

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_provision_channel_accounts_unions_installs_table():
    on_disk = {"channels": {}, "bindings": []}
    rows = [
        {"team_id": "T1", "status": "active", "agent_id": "agent_f"},
        {"team_id": "T2", "status": "active", "agent_id": ""},  # pending — not bound yet
        {"team_id": "T3", "status": "revoked", "agent_id": "agent_f"},
    ]
    with (
        patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(return_value=on_disk)),
        patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(return_value=rows)),
    ):
        accounts, bindings = await em._provision_channel_accounts("org_1")
    assert set(accounts["slack"]) == {"T1"}
    assert accounts["slack"]["T1"]["mode"] == "http"
    assert bindings == [{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}]
```

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** — inside `_provision_channel_accounts`'s `try`, after the file loop and before `return out, bindings`:

```python
        # The installs table is the source of truth for the platform Slack app:
        # a container provisioned AFTER the install (cold path) has nothing on
        # disk yet. Table wins over file for the same team.
        from core.repositories import slack_installs_repo
        from core.services.slack_install_service import account_block, route_binding

        for row in await slack_installs_repo.list_for_owner(owner_id):
            if row.get("status") != "active" or not row.get("agent_id"):
                continue
            team_id = row["team_id"]
            out.setdefault("slack", {})[team_id] = account_block(team_id)
            bindings = [b for b in bindings if (b.get("match") or {}).get("accountId") != team_id]
            bindings.append(route_binding(team_id, row["agent_id"]))
```

(Local imports avoid a cycle: `slack_install_service` imports `ecs_manager` for `slack_env_var_name`.)

- [ ] **Step 4: Run** `test_channel_accounts_carry.py` (all), ruff.
- [ ] **Step 5: Commit** — `feat(backend): installs table feeds Slack account blocks + bindings at config write`

---

### Task 4: Pending installs, binding to Friday, installer DMs

**Files:**
- Modify: `apps/backend/core/services/slack_install_service.py` (append)
- Test: `apps/backend/tests/unit/services/test_slack_install_service.py` (append)

**Interfaces:**
- `async record_pending_install(*, owner_id, team_id, team_name, bot_token, bot_user_id, app_id, scopes, installer_slack_user_id, installer_member_id) -> dict` — secret + row with `agent_id=""` + status. NO task-def roll, NO config patch (the container does not exist yet; Task 3 makes provisioning pick it up). Raises `SlackInstallConflict` like `apply_install`.
- `async bind_pending_installs(owner_id, agent_id) -> list[str]` — for every active row of `owner_id` with empty `agent_id`, `slack_installs_repo.update_agent(team_id, agent_id)`; returns team ids bound. (Add `update_agent(team_id, agent_id)` to the repo: `SET agent_id = :a, updated_at = :t`.)
- `async notify_installer(team_id, text) -> bool` — `chat.postMessage` to `installer_slack_user_id` using the bot token from `integration_secret_store.get(owner, secret_provider(team_id))`; False on any failure (never raises).

- [ ] **Step 1: Failing tests** (append to the Phase 1 service test file):

```python
@pytest.mark.asyncio
async def test_record_pending_install_writes_secret_and_row_only():
    calls = []

    async def _put_secret(o, p, c):
        calls.append("secret")
        return "arn"

    async def _put_row(**kw):
        calls.append(f"row:{kw['agent_id']!r}")
        return {**kw, "status": "active"}

    with (
        patch("core.services.slack_install_service.integration_secret_store.put", _put_secret),
        patch("core.services.slack_install_service.slack_installs_repo.put", _put_row),
        patch("core.services.slack_install_service._mark_connected", AsyncMock()),
        patch("core.services.slack_install_service.get_ecs_manager") as mgr,
    ):
        row = await svc.record_pending_install(**{k: v for k, v in _install_kwargs().items() if k != "agent_id"})
    assert calls == ["secret", "row:''"]
    mgr.assert_not_called()
    assert row["agent_id"] == ""


@pytest.mark.asyncio
async def test_bind_pending_installs_sets_agent_on_empty_rows_only():
    rows = [
        {"team_id": "T1", "status": "active", "agent_id": ""},
        {"team_id": "T2", "status": "active", "agent_id": "agent_x"},
        {"team_id": "T3", "status": "revoked", "agent_id": ""},
    ]
    with (
        patch("core.services.slack_install_service.slack_installs_repo.list_for_owner", AsyncMock(return_value=rows)),
        patch("core.services.slack_install_service.slack_installs_repo.update_agent", AsyncMock()) as upd,
    ):
        assert await svc.bind_pending_installs("org_1", "agent_f") == ["T1"]
    upd.assert_awaited_once_with("T1", "agent_f")


@pytest.mark.asyncio
async def test_notify_installer_posts_dm_with_bot_token():
    seen = []

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

    row = {"team_id": "T1", "owner_id": "org_1", "installer_slack_user_id": "U_ADMIN"}
    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.integration_secret_store.get", AsyncMock(return_value={"bot_token": "xoxb-1"})),
        patch("core.services.slack_install_service._client_factory", lambda: httpx.AsyncClient(transport=T())),
    ):
        assert await svc.notify_installer("T1", "hello") is True
    import json

    assert seen[0].headers["authorization"] == "Bearer xoxb-1"
    assert json.loads(seen[0].content) == {"channel": "U_ADMIN", "text": "hello"}
```

(add `import httpx` to the test file's imports.)

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** — in `slack_install_service.py`:

```python
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(5.0))


async def record_pending_install(
    *, owner_id, team_id, team_name, bot_token, bot_user_id, app_id, scopes, installer_slack_user_id, installer_member_id
) -> dict:
    """Cold path: the container does not exist yet, so only the secret and the
    routing row are written. Provisioning (Task 3) turns the row into the
    account block; ``bind_pending_installs`` fills in the agent once Friday
    is minted."""
    provider = secret_provider(team_id)
    await integration_secret_store.put(owner_id, provider, {"bot_token": bot_token, "bot_user_id": bot_user_id})
    row = await slack_installs_repo.put(
        team_id=team_id, owner_id=owner_id, agent_id="", team_name=team_name, bot_user_id=bot_user_id,
        app_id=app_id, scopes=scopes, installer_slack_user_id=installer_slack_user_id,
        installer_member_id=installer_member_id,
        token_secret_name=integration_secret_store._secret_name(owner_id, provider),
    )
    if row is None:
        await integration_secret_store.delete(owner_id, provider)
        raise SlackInstallConflict(team_id)
    await _mark_connected(owner_id)
    return row


async def bind_pending_installs(owner_id: str, agent_id: str) -> list[str]:
    bound: list[str] = []
    for row in await slack_installs_repo.list_for_owner(owner_id):
        if row.get("status") == slack_installs_repo.STATUS_ACTIVE and not row.get("agent_id"):
            await slack_installs_repo.update_agent(row["team_id"], agent_id)
            bound.append(row["team_id"])
    return bound


async def notify_installer(team_id: str, text: str) -> bool:
    row = await slack_installs_repo.get(team_id)
    if not row or not row.get("installer_slack_user_id"):
        return False
    cred = await integration_secret_store.get(row["owner_id"], secret_provider(team_id))
    token = (cred or {}).get("bot_token")
    if not token:
        return False
    try:
        async with _client_factory() as client:
            resp = await client.post(
                SLACK_POST_MESSAGE_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"channel": row["installer_slack_user_id"], "text": text},
            )
        return bool(resp.json().get("ok"))
    except (httpx.RequestError, ValueError) as exc:
        logger.warning("slack notify_installer failed team=%s err=%s", team_id, exc)
        return False
```

and `import httpx` at the top. In `slack_installs_repo.py`:

```python
async def update_agent(team_id: str, agent_id: str) -> None:
    await run_in_thread(
        _get_table().update_item,
        Key={"team_id": team_id},
        UpdateExpression="SET agent_id = :a, updated_at = :t",
        ExpressionAttributeValues={":a": agent_id, ":t": utc_now_iso()},
    )
```

with a one-line moto test in `test_slack_installs_repo.py` (`put` with `agent_id=""` → `update_agent` → `get` shows the id).

- [ ] **Step 4: Run** both test files; ruff.
- [ ] **Step 5: Commit** — `feat(backend): pending Slack installs — record, bind to Friday, DM the installer`

---

### Task 5: Cold-install service — identity, org, trial, dispatch

**Files:**
- Create: `apps/backend/core/services/slack_cold_install.py`
- Test: `apps/backend/tests/unit/services/test_slack_cold_install.py`

**Interfaces:**
- `async resolve_or_mint_owner(*, email: str, team_id: str, team_name: str, slack_user_id: str) -> tuple[str, str]` → `(org_id, clerk_user_id)`. Rules (spec §3.6 + this plan's picker ruling): `find_user_by_email` → else `create_user`; orgs the user admins (`list_user_organizations`) that have NO active Slack install (`slack_installs_repo.list_for_owner(org_id)` all revoked/empty) → newest by `created_at` → else `create_organization(name=team_name, created_by=user_id, public_metadata={"source": "slack", "team_id": team_id})`.
- `async ensure_trial_and_provision(org_id: str, user_id: str) -> None` — `user_repo` row for the user if missing (`grep -n "^async def put\|^async def upsert" core/repositories/user_repo.py` — use the writer that takes `user_id` + `email`); `billing_repo.create_trial_local_if_absent(org_id, "org", origin="slack")`; if created, emit `trial_started` the way `ensure_personal_trial_default` does (import and reuse its helper; do not add a second emitter shape — set `source: "slack"` in properties); then `ensure_user_ready(AuthContext(user_id=user_id, org_id=org_id, org_role="org:admin"))`.
- `async cold_install(*, token: dict, installer_email: str) -> tuple[str, str]` — orchestrates: owner → `record_pending_install` → `ensure_trial_and_provision` → `notify_installer(team_id, SETUP_TEXT)`; returns `(org_id, team_id)`.
- `SETUP_TEXT = "Hi — I'm Friday. I'm setting up your workspace now; I'll message you here in a couple of minutes when I'm ready."`

- [ ] **Step 1: Failing tests** — one per rule: (a) unknown email → user created + org created named after the team; (b) known email with one org lacking an install → that org reused, no create; (c) known email whose only org already has an active install → new org; (d) `cold_install` calls `record_pending_install` with `agent_id` absent, then `ensure_trial_and_provision`, then `notify_installer` with `SETUP_TEXT`, and returns the ids. Patch `clerk_admin.find_user_by_email/create_user/list_user_organizations/create_organization`, `slack_installs_repo.list_for_owner`, `billing_repo.create_trial_local_if_absent`, `user_provision.ensure_user_ready`, `slack_install_service.record_pending_install/notify_installer`. Assert call args, not just call counts.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the module per the interfaces; keep it under ~120 lines; every Clerk call goes through `clerk_admin`.
- [ ] **Step 4: Run + ruff.**
- [ ] **Step 5: Commit** — `feat(backend): Slack cold install — mint Clerk user/org, Slack-origin trial, dispatch provisioning`

---

### Task 6: Callback branches on cold vs signed-in; scope + manifest

**Files:**
- Modify: `apps/backend/routers/slack_app.py` (`slack_oauth_callback`, `slack_oauth_start`)
- Modify: `apps/backend/core/services/slack_install_service.py` (`BOT_SCOPES` += `"users:read.email"`)
- Modify: `apps/backend/scripts/slack-app-manifest.yaml` (same scope)
- Test: `apps/backend/tests/unit/routers/test_slack_app_oauth.py` (append)

**Interfaces:**
- `GET /slack/oauth/start` **without auth** is now allowed when `?cold=1`: mints a state row with `owner_id=""`, `provider="slack"`, `extra={"cold": True}`. The Marketplace "Add to Slack" button points at `https://api[-env].flickmeon.ai/api/v1/slack/oauth/start?cold=1`. (Signed-in path unchanged.)
- Callback: after the token exchange, if `rec.get("cold")`: call Slack `users.info?user={authed_user.id}` with the bot token to read `user.profile.email`; then `slack_cold_install.cold_install(token=token, installer_email=email)`; redirect to `{FRONTEND_URL}/slack/welcome?team={team_name}` (a static page: "Friday is setting up — check your Slack DMs"). Missing email → redirect `slack=error:email_unavailable`.
- `SLACK_USERS_INFO_URL = "https://slack.com/api/users.info"`.

- [ ] **Step 1: Failing tests** — (a) `start?cold=1` with no auth → 200 `{url}` and the state row has `owner_id == ""` and `cold` extra; (b) cold callback → `users.info` called with the bot token; `cold_install` called with the email; 302 to `/slack/welcome?team=Acme`; (c) `users.info` returns no email → `slack=error:email_unavailable`; (d) the manifest test now expects `users:read.email` in both lists.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** In `slack_oauth_start`, make `auth` optional: `auth: AuthContext | None = Depends(optional_current_user)` — check `core/auth.py` for an existing optional dependency (`grep -n "def optional\|def get_optional" core/auth.py`); if none exists, add `get_current_user_optional` there returning `None` when no bearer is present. Cold requests must not require a session; signed-in requests keep `require_org_admin`. In the callback, the cold branch:

```python
    if rec.get("cold"):
        email = await _installer_email(token["access_token"], (token.get("authed_user") or {}).get("id") or "")
        if not email:
            return RedirectResponse(_frontend_return("", "error:email_unavailable"), status_code=302)
        try:
            await slack_cold_install.cold_install(token=token, installer_email=email)
        except slack_install_service.SlackInstallConflict:
            return RedirectResponse(_frontend_return("", "error:workspace_already_connected"), status_code=302)
        return RedirectResponse(f"{settings.FRONTEND_URL.rstrip('/')}/slack/welcome?team={quote(token['team'].get('name') or '')}", status_code=302)
```

with

```python
async def _installer_email(bot_token: str, user_id: str) -> str:
    """The ONE use of users:read.email: the installer's address at install time."""
    if not user_id:
        return ""
    try:
        async with _client_factory() as client:
            resp = await client.get(SLACK_USERS_INFO_URL, params={"user": user_id}, headers={"Authorization": f"Bearer {bot_token}"})
        data = resp.json()
    except (httpx.RequestError, ValueError):
        return ""
    return (((data.get("user") or {}).get("profile") or {}).get("email") or "").strip().lower()
```

- [ ] **Step 4: Run** both router suites + service test; ruff.
- [ ] **Step 5: Commit** — `feat(backend): cold Slack install path — installer email, mint, provision, welcome redirect`

---

### Task 7: Provisioning hooks — bind Friday, DM on ready

**Files:**
- Modify: `apps/backend/scripts/provision_runner.py` — after step 0 (`_seed_owner_paperclip`) and after step 1 succeeds
- Test: `apps/backend/tests/unit/services/test_provisioning_workflow.py` or the runner's existing test module (`grep -ln "_seed_owner_paperclip" tests/ | head`)

**Interfaces:**
- After step 0 returns: `friday_id = <the company row's friday_agent_id>` (see `paperclip_repo.py:678` — the first seeded agent id) → `await slack_install_service.bind_pending_installs(owner_id, friday_id)`; best-effort, logged, never fails the provision.
- After step 1's config write completes and the container row is `running`: for each install of the owner that was bound in this run, `await slack_install_service.notify_installer(team_id, READY_TEXT.format(link=...))` where the link is a Clerk sign-in ticket: mint via `POST /v1/sign_in_tokens {"user_id": installer_member_id}` (add `clerk_admin.create_sign_in_token(user_id) -> str` mirroring the `_post_json` helper from Task 1; `routers/desktop_auth.py` shows the same call inline) and the URL is `{FRONTEND_URL}/sign-in?__clerk_ticket={token}` (Clerk's `<SignIn>` consumes `__clerk_ticket` natively).
- `READY_TEXT = "I'm ready. DM me here anytime, or @mention me in a channel you've added me to. Your web dashboard: {link}"`.

- [ ] **Step 1: Failing tests** — (a) the runner calls `bind_pending_installs(owner_id, friday_id)` after step 0 with the id the seed returned; (b) a bind failure is logged and step 1 still runs; (c) after step 1, `notify_installer` is called once per bound team with a text containing `__clerk_ticket=`; (d) no installs → neither hook makes a Slack/Clerk call. Patch the two service functions and `clerk_admin.create_sign_in_token`.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** — keep both hooks in a small `_slack_after_seed(owner_id, friday_id)` / `_slack_after_ready(owner_id, bound)` pair inside the runner, each wrapped in `try/except Exception: logger.exception(...)`. Read how step 0 exposes the seeded ids (it returns a count today — extend it to also return the ids, or read the company row via `paperclip_repo` after it returns; pick whichever the existing tests make cheaper and say which in the report).
- [ ] **Step 4: Run** the runner's tests + `test_provisioning_workflow.py`; ruff.
- [ ] **Step 5: Commit** — `feat(backend): provisioning binds pending Slack installs to Friday and DMs the installer when ready`

---

### Task 8: Events route — wake on inbound, templated blocked reply

**Files:**
- Modify: `apps/backend/routers/slack_app.py` (the entitlement block in `receive_slack_event`)
- Test: `apps/backend/tests/unit/routers/test_slack_app_events.py` (replace `test_unentitled_owner_is_200_no_retry_not_forwarded` with the three below)

**Interfaces:** the entitlement step becomes:

```python
    billing = await billing_repo.get_by_owner_id(owner_id)
    if not has_always_on_entitlement(billing):
        # Free tier: reactive chat only (decision 2). Cap/expiry is the same
        # predicate chat and mail use; a blocked owner gets ONE templated
        # reply per channel per day and the event is dropped without retry.
        blocked = await trial_block_reason(owner_id)
        if blocked is not None:
            await _reply_blocked_once(install, data, blocked)
            return Response(status_code=200, headers={"x-slack-no-retry": "1"})

    ip = ecs_manager.discover_ip(container["service_name"])
    if not ip:
        if not has_always_on_entitlement(billing):
            await ecs_manager.start_user_service(owner_id)  # parked trial box — wake it; Slack retries at +1 min
        return Response(status_code=503, headers={"Retry-After": "30"}, ...)
```

- `_reply_blocked_once(install, data, reason)`: key `slack:blocked:{team}:{channel}:{utc date}` via `webhook_dedup.record_event_or_skip(..., source="slack_blocked", ttl_seconds=86400)`; on RECORDED, `chat.postMessage` to `event.channel` (thread_ts = the event's `ts` when present) with `BLOCKED_TEXT[reason]` — two strings: for `trial_expired`: "Your free trial has ended. Upgrade at {FRONTEND_URL}/settings/billing to keep chatting with Friday." and for the daily cap: "Friday has hit today's free-tier limit. It resets at midnight UTC, or upgrade at {FRONTEND_URL}/settings/billing." Read `trial_block_reason`'s return values (`grep -n "return \"" core/services/trial_cap.py`) and map them; unknown reason → the expired text. Uses the bot token via `integration_secret_store.get`. Never raises.

- [ ] **Step 1: Failing tests** — (a) trial owner, not blocked, container running → forwarded (200, upstream called); (b) trial owner, not blocked, no IP → `start_user_service` awaited + 503; (c) trial owner blocked (`trial_block_reason` → "trial_expired") → one `chat.postMessage` with the expired text, 200 + no-retry, not forwarded; a second identical event the same day (dedup → ALREADY_SEEN) posts nothing; (d) paid owner path unchanged (existing tests still pass).

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** per the interface; `trial_block_reason` is imported from wherever `agent_mail_delivery.py` imports it.
- [ ] **Step 4: Run** the events suite; ruff.
- [ ] **Step 5: Commit** — `feat(backend): Slack events wake parked free-tier containers and answer blocked owners once a day`

---

### Task 9: Welcome page + Marketplace-button entry

**Files:**
- Create: `apps/frontend/src/app/slack/welcome/page.tsx` — static, unauthenticated (add `/slack/welcome` to the public routes in `src/middleware.ts`): "Friday is setting up **{team}**. Check your Slack DMs — I'll message you when I'm ready (usually 2–3 minutes)." + a "Open Lightbulb" link to `/sign-in`.
- Test: `apps/frontend/src/app/slack/__tests__/welcome.test.tsx` — renders the team name from the query and the sign-in link.
- Modify: `apps/backend/scripts/slack-app-manifest.yaml` — nothing (the Marketplace listing's install button is configured on api.slack.com and points at `/api/v1/slack/oauth/start?cold=1`; record that URL in the roadmap's human steps — done).

- [ ] Steps 1–5 as usual; commit `feat(frontend): /slack/welcome landing for cold installs`.

---

### Task 10: Dev walk (human + Claude-in-Chrome)

- [ ] Human: throwaway Slack workspace, admin email not in Clerk; dev app re-installed with `users:read.email`.
- [ ] Visit `https://api-dev.flickmeon.ai/api/v1/slack/oauth/start?cold=1` → authorize → land on `/slack/welcome?team=…`.
- [ ] Within ~30 s: a DM from the bot with `SETUP_TEXT`. Clerk dashboard shows a new user (verified email) and a new org named after the workspace with `public_metadata.source == "slack"`. `billing` row: `trial_local`, `origin: "slack"`. `containers` row: provisioning.
- [ ] Within ~3 min: second DM with `READY_TEXT` and a `__clerk_ticket` link; clicking it signs into the web app as the org admin with no password.
- [ ] DM Friday → streamed reply. `openclaw.json` shows the http account keyed by team id with SecretRefs and a binding to Friday's agent id.
- [ ] Close every browser tab; wait 4 min (trial park); DM Friday → first delivery 503s, container wakes, Slack's +1 min retry lands, reply arrives.
- [ ] Burn the daily cap (or flip `trial_end` in the past on the billing row): DM → one templated "upgrade" reply; a second DM the same day → silence, no retry storm in the events log.
- [ ] Install the same workspace again from a different Lightbulb org admin → `workspace_already_connected`.

---

## Self-review

**Spec coverage:** §3.5 steps 4–7 → Tasks 4–7; §3.6 (every row of the resolution table except the picker, which is ruled out with a named ceiling) → Task 5; §3.7 (trial admission, wake, templated reply) → Tasks 2 and 8; decision 3 (peers not members) → unchanged from Phase 1 (`dmPolicy: open`). Deliberately not here: Spot skip (backlog), picker page (backlog), peer-DM session hiding (backlog).

**Placeholder scan:** the tests in Tasks 2 (org gate) and 5 describe assertions by rule rather than as pasted code because they must mirror fixtures that already exist in those files; each names the exact neighbour to copy and what to assert. Everything else is complete.

**Type consistency:** `record_pending_install` takes every `apply_install` kwarg except `agent_id`; `bind_pending_installs(owner_id, agent_id) -> list[str]` is what Task 7 consumes; `notify_installer(team_id, text) -> bool` is used by Tasks 5 and 7; `slack_installs_repo.update_agent(team_id, agent_id)` is defined in Task 4 and used by it only; `is_slack_trial` (Task 2) is the single predicate the org gate consults.
