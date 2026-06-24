# OAuth reprovision/update stale-token fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop reprovisions and config-updates from resurrecting a stale ChatGPT OAuth token by clobbering the container's live (rotated) credential.

**Architecture:** The container's per-agent SQLite auth store is the source of truth for the live, rotating token; DDB `oauth-tokens` is a bootstrap seed only. The destructive force-reimport (`clear_default_agent_auth_profile_row`) is removed from the ops re-stage helper and fired only on genuine fresh-token events: reconnect (`/poll`) and provider switch (`_hot_swap_provider`, already correct). Staging stays everywhere (idempotent; the container's store-row-absent import gate makes it inert on a populated store).

**Tech Stack:** Python 3 / FastAPI backend, DynamoDB, pytest (`uv run pytest`), ruff.

## Global Constraints

- Backend test runner: `cd apps/backend && uv run pytest <path> -v`. Lint: `cd apps/backend && uv run ruff check <path>` and `uv run ruff format <path>`.
- Best-effort container-auth helpers NEVER raise (mirror `revoke_provider_auth_in_container`).
- `clear_default_agent_auth_profile_row` may fire ONLY on a genuine fresh-token event (reconnect, provider switch) — NEVER on provision/reprovision/recover/config-rewrite.
- One ChatGPT account ↔ one container. Token rows are read with `bound_to=<container owner_id>`; staged under the container owner (`org_id` for orgs), token read under the connector (`clerk_user_id`).
- Commit after every task. Worktree branch `fix/oauth-stale-seed-reprovision` only; never main.

---

### Task 1: Remove the destructive `clear` from the ops re-stage helper

Makes every ops path (provision/recover at `ecs_manager.py:1527`, `write_user_configs` at `:2318`) stop clobbering the container's live token, in one edit to the shared helper.

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py` — `_pre_stage_oauth_for_user` (1435–1483)
- Test: `apps/backend/tests/unit/containers/test_ecs_manager.py`

**Interfaces:**
- `_pre_stage_oauth_for_user(self, user_id, *, token_owner_id=None) -> None` — unchanged signature; after this task it stages but does NOT clear.

- [ ] **Step 1: Write the failing test** — add to `test_ecs_manager.py`:

```python
@pytest.mark.asyncio
async def test_pre_stage_oauth_does_not_clear_default_agent_row(manager):
    """Ops re-stage (provision/reprovision) must STAGE the seed but never
    force-reimport: clearing the live container token resurrects a stale
    DDB seed (the refresh_token_reused incident, 2026-06-23)."""
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()
        ) as stage,
        patch(
            "core.containers.workspace.clear_default_agent_auth_profile_row",
            new=AsyncMock(),
        ) as clear,
    ):
        await manager._pre_stage_oauth_for_user("user_test_123")

    stage.assert_awaited_once()
    clear.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_ecs_manager.py::test_pre_stage_oauth_does_not_clear_default_agent_row -v`
Expected: FAIL — `clear` is awaited once (current code calls it at line 1483).

- [ ] **Step 3: Make the change** — in `ecs_manager.py:_pre_stage_oauth_for_user`, drop the `clear` import and call. Replace lines 1471–1483:

```python
        from core.containers.workspace import pre_stage_auth_profile_store

        # Stage the container-global oauth.json seed. We do NOT clear the
        # default agent's auth row here: the container's auth store is the
        # source of truth for the LIVE (rotating) token, and this DDB-sourced
        # seed is a stale snapshot the instant the container's first refresh
        # rotates it. The container's store-row-absent import gate already
        # consumes this seed ONLY for a container with no credential yet (fresh
        # provision), and ignores it on a populated store — so re-staging on a
        # reprovision/recover never clobbers a live token. Force-reimport
        # (clear_default_agent_auth_profile_row) happens ONLY on a genuine
        # fresh-token event: reconnect (routers/oauth.py /poll) and provider
        # switch (_hot_swap_provider). See
        # docs/superpowers/specs/2026-06-24-oauth-reprovision-stale-token-design.md.
        await pre_stage_auth_profile_store(user_id=user_id, oauth_tokens=tokens)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_ecs_manager.py -v`
Expected: PASS — the new test passes; the existing pre-stage ordering test (`...pre_stage_auth_profile_store must run before create_service`, ~line 2195) still passes (it asserts staging order only, not clearing).

- [ ] **Step 5: Lint + commit**

```bash
cd apps/backend && uv run ruff check core/containers/ecs_manager.py && uv run ruff format core/containers/ecs_manager.py tests/unit/containers/test_ecs_manager.py
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_ecs_manager.py
git commit -m "fix(oauth): ops re-stage must not clear the container's live token row

Reprovision/recover/config-rewrite all flow through
_pre_stage_oauth_for_user, which force-imported a stale DDB seed over the
container's live rotated token (refresh_token_reused). Drop the clear
from the ops path; force-reimport now fires only on genuine fresh-token
events (reconnect, switch).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add `push_oauth_credential_to_container` helper

A best-effort helper that stages the connector's CURRENT token + forces the running container to re-import it. Used by the genuine fresh-token events.

**Files:**
- Modify: `apps/backend/core/services/container_auth.py` (append after `revoke_provider_auth_in_container`, line 91)
- Test: `apps/backend/tests/unit/services/test_container_auth.py`

**Interfaces:**
- Produces: `async def push_oauth_credential_to_container(*, owner_id: str, connector_id: str) -> bool` — stages + clears (best-effort). Returns `True` if a credential was staged, `False` if no active tokens or the stage/clear failed (no container/workspace yet). NEVER raises.

- [ ] **Step 1: Write the failing tests** — add to `test_container_auth.py`:

```python
import core.services.container_auth as container_auth


@pytest.mark.asyncio
async def test_push_credential_stages_and_clears_when_tokens_present():
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()) as stage,
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
    ):
        result = await container_auth.push_oauth_credential_to_container(
            owner_id="org_1", connector_id="user_c"
        )
    assert result is True
    stage.assert_awaited_once_with(user_id="org_1", oauth_tokens={"access_token": "a", "refresh_token": "r"})
    clear.assert_awaited_once_with(user_id="org_1")


@pytest.mark.asyncio
async def test_push_credential_noop_when_no_tokens():
    with (
        patch("core.services.oauth_service.get_decrypted_tokens", new=AsyncMock(return_value=None)),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()) as stage,
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
    ):
        result = await container_auth.push_oauth_credential_to_container(
            owner_id="org_1", connector_id="user_c"
        )
    assert result is False
    stage.assert_not_called()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_push_credential_best_effort_when_stage_raises():
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store",
            new=AsyncMock(side_effect=RuntimeError("no workspace yet")),
        ),
        patch("core.containers.workspace.clear_default_agent_auth_profile_row", new=AsyncMock()) as clear,
    ):
        result = await container_auth.push_oauth_credential_to_container(
            owner_id="org_1", connector_id="user_c"
        )
    assert result is False  # never raises
    clear.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_container_auth.py -k push_credential -v`
Expected: FAIL — `AttributeError: ... has no attribute 'push_oauth_credential_to_container'`.

- [ ] **Step 3: Implement the helper** — append to `container_auth.py`:

```python
async def push_oauth_credential_to_container(*, owner_id: str, connector_id: str) -> bool:
    """Stage the connector's CURRENT ChatGPT token onto ``owner_id``'s container
    and force the running container to re-import it (clear the default agent's
    auth row so the store-row-absent import gate re-fires).

    Call ONLY on a genuine credential-change event — reconnect
    (``routers/oauth.py`` ``/poll``) or provider switch — where the token is
    freshly minted/unspent. NEVER call from reprovision / config-rewrite: those
    must not overwrite the container's live, rotated token (see
    ``_pre_stage_oauth_for_user``).

    Best-effort: returns ``True`` if a credential was staged, ``False`` if there
    are no active tokens or the owner has no container/workspace yet. NEVER
    raises.
    """
    from core.containers.workspace import (
        clear_default_agent_auth_profile_row,
        pre_stage_auth_profile_store,
    )
    from core.services import oauth_service

    try:
        tokens = await oauth_service.get_decrypted_tokens(user_id=connector_id, bound_to=owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("push_oauth_credential: token read failed for owner=%s: %s", owner_id, exc)
        return False
    if not tokens:
        return False
    try:
        await pre_stage_auth_profile_store(user_id=owner_id, oauth_tokens=tokens)
        await clear_default_agent_auth_profile_row(user_id=owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort (no workspace/container yet)
        logger.warning("push_oauth_credential: stage/clear failed for owner=%s: %s", owner_id, exc)
        return False
    logger.info("push_oauth_credential: staged fresh ChatGPT token", extra={"owner_id": owner_id})
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_container_auth.py -v`
Expected: PASS (all, including existing revoke tests).

- [ ] **Step 5: Lint + commit**

```bash
cd apps/backend && uv run ruff check core/services/container_auth.py && uv run ruff format core/services/container_auth.py tests/unit/services/test_container_auth.py
git add apps/backend/core/services/container_auth.py apps/backend/tests/unit/services/test_container_auth.py
git commit -m "feat(oauth): add push_oauth_credential_to_container helper

Best-effort stage+clear used by genuine fresh-token events (reconnect /
switch) to push an unspent token into the running container. NEVER raises;
no-ops when there are no active tokens or no container yet.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire the push helper into `/poll` (reconnect heal + C2 gap)

A reconnect of the *same* provider never hits `_hot_swap_provider` (provider-change only) and never reprovisions, so today the fresh token sits in DDB and never reaches the running container. Push it on `/poll` completion.

**Files:**
- Modify: `apps/backend/routers/oauth.py` — import (line 27 area) + `poll` handler (146–174)
- Test: `apps/backend/tests/unit/routers/test_oauth.py`

**Interfaces:**
- Consumes: `container_auth.push_oauth_credential_to_container(owner_id=..., connector_id=...)` from Task 2.

- [ ] **Step 1: Write the failing test** — add to `test_oauth.py` (mirror an existing `test_poll_*` for the `ctx` object and `DevicePollResult(...)` construction used in this file):

```python
@pytest.mark.asyncio
async def test_poll_completion_pushes_fresh_credential_to_container():
    """On a completed device-code flow, the fresh token must be pushed into the
    running container (reconnect heal) — staged under the container owner,
    token read under the connector."""
    from routers import oauth as oauth_router

    result = DevicePollResult(bound_owner_id="org_1", account_id="acct_9")  # mirror existing poll-success test fields
    ctx = _make_ctx(user_id="user_c")  # reuse this file's ctx helper/fixture

    with (
        patch.object(oauth_router, "poll_device_code", new=AsyncMock(return_value=result)),
        patch.object(oauth_router.billing_repo, "claim_oauth_connector", new=AsyncMock()),
        patch.object(oauth_router, "push_oauth_credential_to_container", new=AsyncMock(return_value=True)) as push,
    ):
        resp = await oauth_router.poll(ctx=ctx)

    assert resp["status"] == "completed"
    push.assert_awaited_once_with(owner_id="org_1", connector_id="user_c")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_oauth.py -k pushes_fresh_credential -v`
Expected: FAIL — `push_oauth_credential_to_container` is not imported/called by `poll`.

- [ ] **Step 3: Implement** — in `oauth.py`, add the import next to the existing container_auth import (line 27):

```python
from core.services.container_auth import (
    push_oauth_credential_to_container,
    revoke_provider_auth_in_container,
)
```

Then, in `poll`, replace the final `return` (lines 161–174) so the push runs for BOTH personal and org owners, after the org-claim block:

```python
    if is_org_owner_id(result.bound_owner_id):
        # ... unchanged claim_oauth_connector try/except block ...
        try:
            await billing_repo.claim_oauth_connector(result.bound_owner_id, connector_user_id=ctx.user_id)
        except Exception:
            logger.exception("Failed to claim oauth connector for owner %s", result.bound_owner_id)
    # Push the freshly minted token into the running container. A same-provider
    # reconnect never reprovisions and never hits _hot_swap_provider, so without
    # this the container keeps its old (often spent) token while DDB holds the
    # fresh one. Best-effort: no-op if the owner has no container yet (first
    # connect during onboarding) — provisioning stages it then.
    await push_oauth_credential_to_container(
        owner_id=result.bound_owner_id, connector_id=ctx.user_id
    )
    return {"status": "completed", "account_id": result.account_id}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_oauth.py -v`
Expected: PASS (new test + existing poll tests).

- [ ] **Step 5: Lint + commit**

```bash
cd apps/backend && uv run ruff check routers/oauth.py && uv run ruff format routers/oauth.py tests/unit/routers/test_oauth.py
git add apps/backend/routers/oauth.py apps/backend/tests/unit/routers/test_oauth.py
git commit -m "fix(oauth): push fresh credential to the running container on /poll

Closes the reconnect-same-provider gap: a reconnect now stages+clears so
the unspent token re-imports into the live container instead of stranding
it in DDB while the container keeps a spent token.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Gate hygiene — `has_active_oauth` (no decrypt)

Decouple "is ChatGPT connected?" from the secret at the gate read site: check `state == "active"` without decrypting the token blob.

**Files:**
- Modify: `apps/backend/core/services/oauth_service.py` (add `has_active_oauth` next to `get_decrypted_tokens`, ~line 413)
- Modify: `apps/backend/core/services/provision_gate.py` — `_has_oauth_tokens` (138–157)
- Test: `apps/backend/tests/unit/services/test_oauth_service.py`, `apps/backend/tests/unit/services/test_provision_gate.py`

**Interfaces:**
- Produces: `async def has_active_oauth(*, user_id: str, bound_to: str | None = None) -> bool` — True iff an active row exists and belongs to `bound_to`, WITHOUT decrypting.

- [ ] **Step 1: Write the failing tests** — add to `test_oauth_service.py`:

```python
@pytest.mark.asyncio
async def test_has_active_oauth_true_without_decrypting(oauth_table):
    from core.services.oauth_service import has_active_oauth

    client = boto3.client("dynamodb", region_name="us-east-1")
    client.put_item(
        TableName="test-oauth-tokens",
        Item={
            "user_id": {"S": "u_1"},
            "state": {"S": "active"},
            "bound_owner_id": {"S": "u_1"},
            "encrypted_tokens": {"B": b"undecryptable-garbage"},  # proves no decrypt
        },
    )
    assert await has_active_oauth(user_id="u_1", bound_to="u_1") is True


@pytest.mark.asyncio
async def test_has_active_oauth_false_when_bound_elsewhere(oauth_table):
    from core.services.oauth_service import has_active_oauth

    client = boto3.client("dynamodb", region_name="us-east-1")
    client.put_item(
        TableName="test-oauth-tokens",
        Item={"user_id": {"S": "u_1"}, "state": {"S": "active"}, "bound_owner_id": {"S": "org_other"}},
    )
    assert await has_active_oauth(user_id="u_1", bound_to="u_1") is False


@pytest.mark.asyncio
async def test_has_active_oauth_false_when_pending(oauth_table):
    from core.services.oauth_service import has_active_oauth

    client = boto3.client("dynamodb", region_name="us-east-1")
    client.put_item(
        TableName="test-oauth-tokens",
        Item={"user_id": {"S": "u_1"}, "state": {"S": "pending"}},
    )
    assert await has_active_oauth(user_id="u_1", bound_to="u_1") is False
```

And add to `test_provision_gate.py`:

```python
@pytest.mark.asyncio
async def test_has_oauth_tokens_uses_state_check_not_decrypt():
    from core.services import provision_gate

    with (
        patch("core.services.oauth_service.has_active_oauth", new=AsyncMock(return_value=True)) as active,
        patch("core.services.oauth_service.get_decrypted_tokens", new=AsyncMock()) as decrypt,
    ):
        result = await provision_gate._has_oauth_tokens("user_c", "org_1")
    assert result is True
    active.assert_awaited_once_with(user_id="user_c", bound_to="org_1")
    decrypt.assert_not_called()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_oauth_service.py -k has_active_oauth tests/unit/services/test_provision_gate.py -k uses_state_check -v`
Expected: FAIL — `has_active_oauth` undefined; `_has_oauth_tokens` still calls `get_decrypted_tokens`.

- [ ] **Step 3: Implement** — in `oauth_service.py`, add after `get_decrypted_tokens` (line 412):

```python
async def has_active_oauth(*, user_id: str, bound_to: str | None = None) -> bool:
    """Whether an ACTIVE OAuth row exists for ``user_id`` bound to ``bound_to`` —
    a connection-state check that does NOT decrypt the token blob. Callers that
    need the actual tokens use ``get_decrypted_tokens``; the gate only needs to
    know a connection exists. Keeps the connected-flag decoupled from the secret.
    """
    row = _table().get_item(Key={"user_id": user_id}).get("Item")
    if not row or row.get("state") != "active":
        return False
    return _row_belongs_to(row, bound_to)
```

Then in `provision_gate.py:_has_oauth_tokens`, replace the body (lines 154–157):

```python
    from core.services import oauth_service

    return await oauth_service.has_active_oauth(user_id=token_owner, bound_to=owner_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_oauth_service.py tests/unit/services/test_provision_gate.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd apps/backend && uv run ruff check core/services/oauth_service.py core/services/provision_gate.py && uv run ruff format core/services/oauth_service.py core/services/provision_gate.py tests/unit/services/test_oauth_service.py tests/unit/services/test_provision_gate.py
git add apps/backend/core/services/oauth_service.py apps/backend/core/services/provision_gate.py apps/backend/tests/unit/services/test_oauth_service.py apps/backend/tests/unit/services/test_provision_gate.py
git commit -m "refactor(oauth): gate checks active-state without decrypting the token

Decouple the connected-flag from the secret at the gate read site.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Full verification of touched surface

Confirm the whole affected test surface is green and the switch invariant still holds.

**Files:** none (verification only)

- [ ] **Step 1: Confirm `_hot_swap_provider` still stages+clears** (invariant guard — should already pass, no code change)

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_change_provider.py -v`
Expected: PASS — including the existing assertion that `clear_default_agent_auth_profile_row` is called on a switch *into* chatgpt_oauth (`test_billing_change_provider.py:601`).

- [ ] **Step 2: Run the full touched-module test set**

Run:
```bash
cd apps/backend && uv run pytest \
  tests/unit/containers/test_ecs_manager.py \
  tests/unit/containers/test_workspace_codex_auth.py \
  tests/unit/services/test_container_auth.py \
  tests/unit/services/test_oauth_service.py \
  tests/unit/services/test_provision_gate.py \
  tests/unit/routers/test_oauth.py \
  tests/unit/routers/test_billing_change_provider.py \
  tests/unit/services/test_billing_service.py -v
```
Expected: ALL PASS.

- [ ] **Step 3: Lint the full diff**

Run: `cd apps/backend && uv run ruff check core/ routers/ tests/` (expect clean) and confirm `git diff origin/main..HEAD --stat` shows only the intended files.

- [ ] **Step 4: (no commit)** — implementation complete; ready for code review + PR.

---

## Self-Review

**Spec coverage:**
- Spec §"The fix" 1 (remove clear from ops) → Task 1. ✓
- §2 (reconnect stage+clear via /poll) → Task 3. ✓
- §3 (keep _hot_swap) → Task 5 Step 1 (guard, no change). ✓
- §4 (shared helper) → Task 2 (`push_oauth_credential_to_container`); used by /poll. `_hot_swap_provider` intentionally keeps its inline stage+clear (different error contract: it must `raise _HotSwapUnavailableError` to fall back to the cold path; the best-effort helper returns bool). Noted deviation from the spec's "shared for #2/#3" — orthogonality where it helps, not forced. ✓
- §5 (gate state-check, no decrypt) → Task 4. ✓
- Invariant "clear only on fresh-token events" → enforced by Task 1 (removes the only ops clear) + Tasks 2/3 (add it on reconnect) + Task 5 Step 1 (switch retained). ✓

**Placeholder scan:** Test scaffolding in Task 3 (`_make_ctx`, `DevicePollResult(...)` fields) is anchored to "mirror an existing `test_poll_*` in this file" because the `ctx`/result construction is established there; the new assertion (`push.assert_awaited_once_with`) is concrete. No TODO/TBD remain.

**Type consistency:** `push_oauth_credential_to_container(*, owner_id, connector_id) -> bool` is defined in Task 2 and consumed with the same keyword args in Task 3. `has_active_oauth(*, user_id, bound_to=None) -> bool` defined in Task 4 Step 3 and asserted with the same kwargs in Task 4 Step 1. ✓
