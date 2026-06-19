# Google BYO-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Google (gog) a bring-your-own-OAuth-app integration available to **every** owner, and delete the now-unused allowlist (`internal`) + Bedrock (`requires_bedrock`) + managed-shared-app machinery it replaces.

**Architecture:** Today `google` is gated four ways (`internal` allowlist, `requires_bedrock` Bedrock-lock, the list-visibility filter, the OAuth-callback re-check) and connecting falls back to a shared, unverified Isol8 Google OAuth app. Because Google's scopes (`gmail.modify`, `drive`) are Google *restricted* scopes, that shared unverified app can never serve non-test users anyway — so the only path that works at GA is BYO: each owner registers their own Google Cloud OAuth app and carries their own Limited-Use compliance. This plan collapses `allow_byo_app` into a single `byo_required` flag, makes BYO the mandatory connect path for Google (no managed fallback), and removes every gate + helper + config + infra knob + frontend branch that only existed to serve the old managed/gated model. Google is the sole provider touching any of this machinery, so removal is total, not partial.

**Tech Stack:** FastAPI + Pydantic (backend), pytest, Next.js 16 + React 19 + vitest (frontend), AWS CDK (infra), AWS Secrets Manager (per-owner BYO app config).

## Global Constraints

- The per-owner BYO app config lives in Secrets Manager at `isol8/<env>/integrations/<owner_id>/oauth-app/<provider>` (the `integrations/*` RW-granted prefix). Do NOT change this path — it is already IAM-granted (`oauth_app_store.py`).
- The redirect URI is server-derived and must stay byte-for-byte identical in the authorize + callback legs and the frontend hint: `{PUBLIC_API_BASE}/api/v1/oauth/{provider}/callback` (backend) / `${BACKEND_URL}/oauth/{provider}/callback` (frontend, where `BACKEND_URL` already includes `/api/v1`).
- Google is `refresh_owner: actor` — the in-container `gog` CLI self-refreshes from its EFS keyring; the backend retains no Google token. Do not add a backend refresh path.
- Comments explain WHY, never reference PR numbers / bug IDs / review rounds.
- Frontend tests: run ONLY the single changed test file with vitest, foreground. NEVER run the full `pnpm test` (it consumes ~17.7 GB RAM).
- No new "dead" code: this plan REMOVES the `internal` / `requires_bedrock` / managed-shared-app machinery entirely. Do not leave the fields, helpers, config keys, env vars, or frontend branches behind "just in case."
- This work is layered on the existing `feat/byo-oauth-google` branch, which already added `oauth_app_store.py`, the BYO `_oauth_client` resolution, the `put/delete byo-app` endpoints, and the `ConnectButton` BYO panel. Those are the starting point — this plan revises them, it does not re-add them.

---

### Task 1: Backend — Google becomes BYO-required; strip the allowlist + Bedrock gates

**Files:**
- Modify: `apps/backend/core/services/credentials/registry.py` (Provider model: add `byo_required`, drop `allow_byo_app`; KEEP `internal`/`requires_bedrock` for now — Task 2 removes them after the classifier stops reading them)
- Modify: `apps/backend/core/services/credentials/providers.yaml` (google block)
- Modify: `apps/backend/core/services/credentials/oauth_dance.py` (`_oauth_client` BYO-only; delete `_isol8_oauth_client`)
- Modify: `apps/backend/routers/connections.py` (delete `_require_grantable_provider`, `_owner_on_bedrock`, `_require_bedrock_for`, `_bedrock_lock`, `_BEDROCK_PROVIDER_CHOICE`, `_SWITCH_PROVIDER_HREF`; rewrite list/authorize/connect_paste/callback; rename `allow_byo_app`→`byo_required` usages)
- Modify: `apps/backend/core/config.py` (delete `INTERNAL_INTEGRATION_OWNERS` + `internal_integration_owners`)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (delete the `INTERNAL_INTEGRATION_OWNERS` env var, ~lines 904-913)
- Test: `apps/backend/tests/unit/services/credentials/test_oauth_dance.py`, `apps/backend/tests/unit/routers/test_connections.py`, `apps/backend/tests/credentials/test_registry.py`

**Interfaces:**
- Consumes: `oauth_app_store.get(owner_id, provider) -> dict|None` (has `client_id`/`client_secret`/`acknowledged_at`); `registry.get_provider(key)`, `registry.all_providers()`.
- Produces:
  - `registry.Provider.byo_required: bool` (replaces `allow_byo_app`). `byo_setup_url: str|None` unchanged.
  - `oauth_dance._oauth_client(owner_id, provider_key) -> tuple[str,str]` — returns the owner's BYO client_id/secret, or raises `ValueError` when none is configured (no managed fallback).
  - `connections._byo_active(owner_id, provider_key) -> bool` — True when the provider is `byo_required` and the owner has a stored, acknowledged app.
  - `GET /connections` rows expose `byo_required`, `byo_configured`, `byo_setup_url`; NO `requires_bedrock` or `byo_capable` field; all providers visible (no allowlist filter).
  - `GET /connections/{provider}/authorize` returns 400 when `byo_required` and no app is stored.

- [ ] **Step 1: Write the failing registry test**

In `apps/backend/tests/credentials/test_registry.py`, add (and delete any existing assertion that `google.internal` / `google.requires_bedrock` / `google.allow_byo_app` is True):

```python
def test_google_is_byo_required():
    from core.services.credentials import registry

    g = registry.get_provider("google")
    assert g.byo_required is True
    assert g.byo_setup_url == "https://console.cloud.google.com/apis/credentials"
    assert not hasattr(g, "allow_byo_app")


def test_no_provider_is_byo_required_except_google():
    from core.services.credentials import registry

    byo = [k for k, v in registry.all_providers().items() if v.byo_required]
    assert byo == ["google"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/credentials/test_registry.py::test_google_is_byo_required -v`
Expected: FAIL (`Provider` has no field `byo_required`; still has `allow_byo_app`).

- [ ] **Step 3: Update the Provider model**

In `apps/backend/core/services/credentials/registry.py`, replace the `allow_byo_app` field (lines ~109-112) with `byo_required`. KEEP `internal` and `requires_bedrock` (Task 2 deletes them):

```python
    # internal + requires_bedrock are removed in Task 2 once the marketplace
    # classifier stops reading them. Do not add new readers.
    internal: bool = False
    requires_bedrock: bool = False
    # BYO-required: this provider has no Isol8-managed OAuth app, so the owner
    # MUST connect with their own OAuth app (client_id/secret). It is visible to
    # every owner and gated only by "have you stored your app yet". Today only
    # google: its scopes are Google *restricted* scopes that an unverified shared
    # app cannot serve, and the owner's own app carries its own Limited-Use
    # compliance.
    byo_required: bool = False
    # Help link shown in the BYO setup panel (the provider's OAuth-app console).
    byo_setup_url: str | None = None
```

- [ ] **Step 4: Flip the google provider definition**

In `apps/backend/core/services/credentials/providers.yaml`, the `google:` block — remove the `internal: true`, `requires_bedrock: true`, and `allow_byo_app: true` lines (and their comments), add `byo_required: true`. Result for the gating lines:

```yaml
  # BYO-only: no Isol8-managed Google app. Every owner connects with their own
  # OAuth app and owns its Limited-Use compliance. Visible to all; the connect
  # path requires a stored app.
  byo_required: true
  byo_setup_url: "https://console.cloud.google.com/apis/credentials"
```

- [ ] **Step 5: Run the registry tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/credentials/test_registry.py -v`
Expected: PASS.

- [ ] **Step 6: Write the failing oauth_dance tests**

In `apps/backend/tests/unit/services/credentials/test_oauth_dance.py`, replace the three fallback tests (`test_oauth_client_falls_back_to_isol8`, `test_oauth_client_falls_back_when_byo_row_missing_secret`, `test_oauth_client_falls_back_when_byo_row_missing_client_id`) with no-fallback tests, and keep `test_oauth_client_prefers_owner_byo_app`:

```python
@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_uses_owner_byo_app(mock_byo_get):
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": "owner_sec"}
    cid, sec = await oauth_dance._oauth_client("user_1", "google")
    assert (cid, sec) == ("owner_cid", "owner_sec")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_raises_when_no_byo_app(mock_byo_get):
    mock_byo_get.return_value = None
    with pytest.raises(ValueError):
        await oauth_dance._oauth_client("user_1", "google")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_raises_when_byo_row_incomplete(mock_byo_get):
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": ""}
    with pytest.raises(ValueError):
        await oauth_dance._oauth_client("user_1", "google")
```

- [ ] **Step 7: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_oauth_dance.py -k oauth_client -v`
Expected: FAIL (`_oauth_client` still falls back instead of raising).

- [ ] **Step 8: Make `_oauth_client` BYO-only and delete `_isol8_oauth_client`**

In `apps/backend/core/services/credentials/oauth_dance.py`, delete the entire `_isol8_oauth_client` function and rewrite `_oauth_client`:

```python
async def _oauth_client(owner_id: str, provider_key: str) -> tuple[str, str]:
    """The owner's OWN OAuth app for this provider. There is no Isol8-managed
    shared app, so a missing or half-populated BYO app is an error — start() and
    exchange() both call this, so a connect attempt without a stored app fails
    fast instead of silently using a shared app."""
    byo = await oauth_app_store.get(owner_id, provider_key)
    if not (byo and byo.get("client_id") and byo.get("client_secret")):
        raise ValueError(f"{provider_key} requires your own OAuth app; none configured for this owner")
    return byo["client_id"], byo["client_secret"]
```

Also delete the now-unused `boto3`/`json`/`settings`/`run_in_thread` imports ONLY if nothing else in the file uses them (the file's `exchange` still uses `httpx`; check before removing each import).

- [ ] **Step 9: Run the oauth_dance tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_oauth_dance.py -v`
Expected: PASS.

- [ ] **Step 10: Write the failing connections tests**

In `apps/backend/tests/unit/routers/test_connections.py`, delete the obsolete gated tests (`test_internal_provider_hidden_when_not_allowlisted`, `test_internal_provider_shown_when_allowlisted`, `test_connected_internal_provider_stays_visible_for_removal`, `test_disconnected_internal_provider_stays_hidden`, `test_authorize_404_when_not_allowlisted`, `test_connect_paste_404_when_not_allowlisted`, `test_catalog_exposes_requires_bedrock_flag`, `test_authorize_403_for_non_bedrock_owner`, `test_connect_paste_403_for_non_bedrock_owner`) and add their replacements. Use the file's existing `async_client` / `mock_repo` fixtures and its monkeypatch pattern for `_byo_active`:

```python
async def test_google_visible_to_every_owner(self, mock_repo, async_client, monkeypatch):
    mock_repo.return_value = {}  # no connected rows
    response = await async_client.get("/api/v1/connections")
    by_key = {c["provider"]: c for c in response.json()["connections"]}
    assert "google" in by_key
    assert by_key["google"]["byo_required"] is True
    assert "requires_bedrock" not in by_key["google"]
    assert "byo_capable" not in by_key["google"]


async def test_authorize_400_when_byo_app_not_configured(self, async_client, monkeypatch):
    monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=False))
    response = await async_client.get("/api/v1/connections/google/authorize")
    assert response.status_code == 400


async def test_authorize_succeeds_when_byo_app_configured(self, mock_dance, async_client, monkeypatch):
    monkeypatch.setattr(connections, "_byo_active", AsyncMock(return_value=True))
    response = await async_client.get("/api/v1/connections/google/authorize")
    assert response.status_code == 200
    assert response.json()["authorize_url"].startswith("https://accounts.google.com")
```

(Match `mock_dance` to how the existing `test_authorize_passes_gate_when_allowlisted` patches `oauth_dance.start`. Keep the import alias the test file already uses for the router module as `connections`.)

- [ ] **Step 11: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -k "google_visible or byo_app" -v`
Expected: FAIL (google hidden / authorize still 404 / `requires_bedrock` still present).

- [ ] **Step 12: Rewrite `routers/connections.py`**

Delete these helpers entirely: `_require_grantable_provider`, `_owner_on_bedrock`, `_require_bedrock_for`, `_bedrock_lock`, and the module constants `_BEDROCK_PROVIDER_CHOICE` and `_SWITCH_PROVIDER_HREF`. Remove the `billing_repo` import if `_owner_on_bedrock` was its only user (grep first).

Update `_byo_active` to key on `byo_required` (it replaced `allow_byo_app`):

```python
async def _byo_active(owner_id: str, provider_key: str) -> bool:
    """True when the owner has a stored, acknowledged OWN OAuth app for a
    byo-required provider — the prerequisite for connecting it."""
    prov = registry.all_providers().get(provider_key)
    if not prov or not prov.byo_required:
        return False
    app = await oauth_app_store.get(owner_id, provider_key)
    return bool(app and app.get("acknowledged_at"))
```

Rewrite the list endpoint body (the `for k, v in registry.all_providers().items()` loop) to drop the allowlist filter + `requires_bedrock` field and expose `byo_required`:

```python
    connections = []
    for k, v in registry.all_providers().items():
        byo = await _byo_active(owner_id, k)
        connections.append(
            {
                "provider": k,
                "display_name": v.display_name,
                "acquisition": v.acquisition,
                "status": connected.get(k, "disconnected"),
                # byo_required: provider has no managed app, owner must bring one.
                # byo_configured: whether THIS owner has stored + acknowledged it.
                # byo_setup_url: where to register one.
                "byo_required": v.byo_required,
                "byo_configured": byo,
                "byo_setup_url": v.byo_setup_url,
            }
        )
    return {"connections": connections}
```

Rewrite `connect_paste` to drop the gate calls:

```python
@router.post("/{provider}")
async def connect_paste(provider: str, body: PasteBody, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    if p.acquisition != "paste_token":
        raise HTTPException(status_code=400, detail=f"{provider} is not a paste-key integration")
    # ...rest unchanged (validate, store, apply)...
```

Rewrite `authorize` to drop the gate calls and add the BYO-required gate:

```python
@router.get("/{provider}/authorize")
async def authorize(provider: str, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    if p.acquisition != "direct_oauth":
        raise HTTPException(status_code=400, detail=f"{provider} is not an OAuth integration")
    if p.byo_required and not await _byo_active(owner_id, provider):
        raise HTTPException(status_code=400, detail=f"Set up your own {p.display_name} app before connecting")
    url = await oauth_dance.start(owner_id, provider, redirect_uri=_redirect_uri(provider))
    return {"authorize_url": url}
```

In `oauth_callback`, delete the `requires_bedrock` re-check block (the `if p.requires_bedrock and not await _byo_active(...) and not await _owner_on_bedrock(...)` redirect). The `p = registry.get_provider(prov)` line stays only if still used below (it is — `_retains_secret(p)` and `_remove_materialized_artifacts(owner_id, p, ...)`), so keep it.

Update the `put_byo_app` / `delete_byo_app` guards from `if not p.allow_byo_app` to `if not p.byo_required`, and their docstrings to say "the prerequisite for connecting a BYO-required provider" instead of "lifting the internal + requires_bedrock gates".

- [ ] **Step 13: Delete the allowlist config**

In `apps/backend/core/config.py`, delete the `INTERNAL_INTEGRATION_OWNERS: str = ""` field (line ~47) and the `internal_integration_owners` property (lines ~55-57).

- [ ] **Step 14: Delete the allowlist env var from infra**

In `apps/infra/lib/stacks/service-stack.ts`, delete the `INTERNAL_INTEGRATION_OWNERS: env === "prod" ? ... : ...` env entry (~lines 904-913) and its preceding comment. This is an env-var-only change to the backend task definition — it does NOT add/remove resources or cross-stack exports, so it does not trigger the cdk-pipelines deploy.yml drift check. (The reviewer should still confirm `cd apps/infra && pnpm exec cdk synth -q` succeeds.)

- [ ] **Step 15: Run the backend tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py tests/unit/services/credentials/test_oauth_dance.py tests/credentials/test_registry.py -v`
Expected: PASS. Then grep for stragglers: `grep -rn "allow_byo_app\|internal_integration_owners\|INTERNAL_INTEGRATION_OWNERS\|_isol8_oauth_client\|_require_grantable_provider\|_require_bedrock_for" apps/backend` should return only `registry.py`/`providers.yaml` comments (none in code) — fix any code hit.

- [ ] **Step 16: Commit**

```bash
git add apps/backend/core/services/credentials/registry.py apps/backend/core/services/credentials/providers.yaml apps/backend/core/services/credentials/oauth_dance.py apps/backend/routers/connections.py apps/backend/core/config.py apps/infra/lib/stacks/service-stack.ts apps/backend/tests/unit/routers/test_connections.py apps/backend/tests/unit/services/credentials/test_oauth_dance.py apps/backend/tests/credentials/test_registry.py
git commit -m "feat(connections): make Google BYO-only; remove allowlist + Bedrock gates"
```

---

### Task 2: Marketplace — classify byo-required providers as BYO; delete the gated-provider machinery

**Files:**
- Modify: `apps/backend/core/services/marketplace/requirements_classify.py`
- Modify: `apps/backend/core/services/credentials/registry.py` (NOW remove `internal` + `requires_bedrock` — no readers remain after this task)
- Test: `apps/backend/tests/unit/services/marketplace/test_requirements_classify.py`

**Interfaces:**
- Consumes: `registry.Provider.byo_required` (from Task 1).
- Produces: `gog` / `google` (and any future byo-required provider) classify as `Disposition.BYO` with reason "requires your own Google OAuth app"; the `GATED_PROVIDERS` set, `_GATED_REASON`, and all `internal`/`requires_bedrock` reads are gone.

- [ ] **Step 1: Write the failing classifier tests**

In `apps/backend/tests/unit/services/marketplace/test_requirements_classify.py`, replace `test_gog_is_unavailable_gated` and `test_gated_provider_tool_is_unavailable`:

```python
def test_gog_tool_is_byo():
    reqs = _classify([Hit("tool", "gog", "p")])
    assert reqs["google"].disposition == Disposition.BYO
    assert "own" in (reqs["google"].reason or "").lower()


def test_google_tool_is_byo():
    """A bare `--tool google` resolves to the byo-required google provider → BYO."""
    reqs = _classify([Hit("tool", "google", "p")])
    assert reqs["google"].disposition == Disposition.BYO
```

(Note the key is now `"google"` for both, because `_provider_for_tool("gog")` resolves the gog binary to the google provider; the old `GATED_PROVIDERS` branch keyed the row under the raw tool name `"gog"`. Update any other test that asserted a `reqs["gog"]` key.)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/test_requirements_classify.py -k "gog or google" -v`
Expected: FAIL (gog/google still UNAVAILABLE via `GATED_PROVIDERS`).

- [ ] **Step 3: Rewrite the classifier's gating logic**

In `apps/backend/core/services/marketplace/requirements_classify.py`:

Delete the `GATED_PROVIDERS = frozenset({"gog", "google"})` constant (line ~40) and `_GATED_REASON` (line ~42). Add:

```python
_BYO_APP_REASON = "requires your own Google OAuth app"


def _provider_disposition(prov) -> tuple[Disposition, str | None]:
    """How a registry provider is offered to a buyer: byo-required → BYO (bring
    your own app), otherwise a normal connectable integration."""
    if prov.byo_required:
        return Disposition.BYO, _BYO_APP_REASON
    return Disposition.CONNECT, None
```

Delete the `elif name in GATED_PROVIDERS:` branch (lines ~116-117).

In the tool→provider branch (lines ~119-129), replace the `gated = spec.internal or spec.requires_bedrock` block with:

```python
                prov = _provider_for_tool(name)
                if prov is not None:
                    key, spec = prov
                    disp, reason = _provider_disposition(spec)
                    r = _ensure(key, spec.display_name, _kind_for_provider(spec), disp, reason=reason)
```

In the env/host branch (lines ~163-171), replace the `not (prov.internal or prov.requires_bedrock)` ladder with:

```python
            if prov is not None:
                disp, reason = _provider_disposition(prov)
                r = _ensure(key, display, "external_api", disp, env=env, reason=reason)
            else:
                r = _ensure(key, display, "external_api", Disposition.BYO, env=env)
```

In the bundled-MCP branch (lines ~184-190), replace the `gated = prov.internal or prov.requires_bedrock` block with:

```python
        disp, reason = _provider_disposition(prov)
        r = _ensure(name, prov.display_name, _kind_for_provider(prov), disp, reason=reason)
```

Update the module docstring (lines ~6-12): change the `gog / Google (internal + Bedrock-only) -> UNAVAILABLE (gated)` line to `byo-required provider (Google) -> BYO (your own OAuth app)`.

- [ ] **Step 4: Remove the now-unread Provider fields**

In `apps/backend/core/services/credentials/registry.py`, delete the `internal: bool = False` and `requires_bedrock: bool = False` fields and their comment block (kept in Task 1). Verify nothing else reads them, INCLUDING test files (a stale `provider.requires_bedrock` assertion will fail at collection):

Run: `grep -rn "requires_bedrock\|\.internal\b" apps/backend --include='*.py' | grep -v "\.internalization"`
Expected: hits only as comments. Migrate or delete any remaining test/code reader before proceeding.

- [ ] **Step 5: Run the classifier + a broad backend slice to verify**

Run: `cd apps/backend && uv run pytest tests/unit/services/marketplace/ tests/credentials/ -v`
Expected: PASS (the wiring-mismatch post-pass only touches `CONNECT` rows, so a BYO google row is untouched — confirm `test_stripe_*` still pass).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/marketplace/requirements_classify.py apps/backend/core/services/credentials/registry.py apps/backend/tests/unit/services/marketplace/test_requirements_classify.py
git commit -m "feat(marketplace): classify byo-required Google as BYO; drop gated-provider machinery"
```

---

### Task 3: Frontend — BYO panel as the primary connect path; remove the Bedrock lock; add setup instructions

**Files:**
- Modify: `apps/frontend/src/hooks/useConnections.ts` (Connection type: drop `requires_bedrock`, add `byo_required`)
- Modify: `apps/frontend/src/components/settings/ConnectionsList.tsx` (delete the Bedrock-lock + `useBilling` machinery)
- Modify: `apps/frontend/src/components/settings/ConnectButton.tsx` (BYO panel keyed on `byo_capable`, not the lock; delete the Switch-to-Bedrock branch; add setup steps; finalize liability copy)
- Modify: `apps/frontend/src/app/settings/connections/page.tsx` (drop the `requires_bedrock` error case)
- Test: `apps/frontend/src/components/settings/__tests__/ConnectButton.byo.test.tsx`, `apps/frontend/src/components/settings/__tests__/ConnectButton.test.tsx`

**Interfaces:**
- Consumes: `GET /connections` rows with `byo_required`, `byo_configured`, `byo_setup_url` and NO `requires_bedrock` / `byo_capable` (from Task 1).
- Produces: a `ConnectButton` whose BYO setup panel renders whenever `byo_required && !byo_configured` for a not-connected row, with inline setup instructions; no Bedrock-lock UI anywhere.

- [ ] **Step 1: Update the Connection type**

In `apps/frontend/src/hooks/useConnections.ts`, in the `Connection` interface: delete `requires_bedrock?: boolean` and its doc comment; in the BYO doc block, replace the `byo_capable` line wording and add `byo_required`:

```typescript
  /**
   * BYO ("bring your own") OAuth app affordance:
   *   - `byo_required`   → this provider has no managed app; the owner MUST
   *                        register their own OAuth app to connect it.
   *   - `byo_configured` → THIS owner already stored + acknowledged one.
   *   - `byo_setup_url`  → where to register one (provider console deep link).
   */
  byo_required?: boolean;
  byo_configured?: boolean;
  byo_setup_url?: string | null;
```

- [ ] **Step 2: Write the failing ConnectButton BYO test**

In `apps/frontend/src/components/settings/__tests__/ConnectButton.byo.test.tsx`, update the base mock connection to drop `requires_bedrock`/`byo_capable` and use `byo_required: true, byo_configured: false`, and change the render helper to NOT pass `lockedForBedrock` (the prop is being deleted). Use the file's existing fixture/helper names (the names below are illustrative — match what the file already defines). Replace the "offers 'Use your own Google app' for a byo_capable, locked, not-configured provider" test:

```typescript
it("offers 'Use your own Google app' for a byo_required, not-configured provider (no lock needed)", () => {
  render(<ConnectButton {...baseProps} connection={{ ...baseConnection, byo_required: true, byo_configured: false }} />);
  expect(screen.getByRole("button", { name: /use your own Google Workspace app/i })).toBeInTheDocument();
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd apps/frontend && pnpm exec vitest run src/components/settings/__tests__/ConnectButton.byo.test.tsx`
Expected: FAIL (component still requires `lockedForBedrock` to show the panel; TS error on removed prop).

- [ ] **Step 4: Rewrite `ConnectButton.tsx`**

- Delete the `lockedForBedrock` prop from `ConnectButtonProps` and the function signature.
- Delete `SWITCH_PROVIDER_HREF`, `BEDROCK_LOCK_FALLBACK`, `bedrockLockMessage`, and the entire `if (lockedForBedrock) { ... }` Switch-to-Bedrock branch.
- In `errorMessage`/`run`, delete the `bedrockLockMessage(e)` special-casing (a 403 now only means the non-admin denial).
- Change the BYO branch condition from `if (lockedForBedrock && connection.byo_capable && !connection.byo_configured)` to:

```typescript
  // BYO connect path: a provider that has no managed app, where this owner
  // hasn't stored one yet. This IS the connect action (not an upsell) — storing
  // + acknowledging the app, then the normal OAuth redirect, connects it.
  if (connection.byo_required && !connection.byo_configured) {
```

- Inside the open BYO panel (after the `Register an OAuth app →` link), add inline setup steps before the redirect-URI block:

```tsx
        <ol className="list-decimal pl-4 text-xs text-[#8a8578] space-y-0.5">
          <li>Create an OAuth client ID — application type <strong>Web application</strong>.</li>
          <li>Add the authorized redirect URI shown below.</li>
          <li>On the OAuth consent screen, add your Google account as a <strong>Test user</strong>.</li>
          <li>Paste the Client ID and secret here.</li>
        </ol>
```

- Replace the `BYO_ACK_COPY` placeholder with the finalized liability copy (applies to every owner, Bedrock or not):

```typescript
// Limited-Use acknowledgment shown before an owner stores their own OAuth app.
const BYO_ACK_COPY =
  "I'm connecting with my own Google OAuth app and I'm responsible for its compliance with Google's API Services User Data Policy, including the Limited Use requirements, for any Google data my agents access.";
```

- [ ] **Step 5: Write the failing ConnectionsList behavior (delete Bedrock-lock)**

In `apps/frontend/src/components/settings/__tests__/ConnectButton.test.tsx`, delete the two Bedrock tests (`renders a Switch-to-Bedrock upsell (not Connect) when lockedForBedrock` and `surfaces the structured requires_bedrock 403 message…`), every `lockedForBedrock` usage, and the `requires_bedrock`/`byo_capable` properties from the base mock connection (they no longer exist on `Connection`). (No replacement — the behavior is removed.) The `tsc --noEmit` in Step 8 is the backstop for any straggler.

- [ ] **Step 6: Rewrite `ConnectionsList.tsx`**

- Delete the `useBilling` import + call, `BEDROCK_PROVIDER_CHOICE`, `offBedrock`, the entire `locked` computation, and the `lockedForBedrock={locked}` prop passed to `ConnectButton`.
- Delete the `locked ? "Requires Bedrock credits" : …` branch in the status line — render the plain `{connected ? "Connected" : "Not connected"}{acquisition…}` line unconditionally.

- [ ] **Step 7: Drop the requires_bedrock error case**

In `apps/frontend/src/app/settings/connections/page.tsx`, delete the `: error === "requires_bedrock" ? …` branch from the error-banner ternary (line ~44).

- [ ] **Step 8: Run the frontend tests to verify they pass**

Run: `cd apps/frontend && pnpm exec vitest run src/components/settings/__tests__/ConnectButton.byo.test.tsx src/components/settings/__tests__/ConnectButton.test.tsx`
Expected: PASS. Then: `cd apps/frontend && pnpm exec tsc --noEmit` to confirm no dangling `requires_bedrock` / `lockedForBedrock` references.

- [ ] **Step 9: Commit**

```bash
git add apps/frontend/src/hooks/useConnections.ts apps/frontend/src/components/settings/ConnectionsList.tsx apps/frontend/src/components/settings/ConnectButton.tsx apps/frontend/src/app/settings/connections/page.tsx apps/frontend/src/components/settings/__tests__/ConnectButton.byo.test.tsx apps/frontend/src/components/settings/__tests__/ConnectButton.test.tsx
git commit -m "feat(connections): BYO Google as the primary connect path; remove Bedrock lock"
```

---

## Notes for the executor

- **Liability copy (Step 4, Task 3) is a draft.** The founder will finalize the exact legal wording before this ships; implement the draft as written and flag it in the task report.
- **The dev/prod deploy caveat is out of scope for this plan** but real: dev and prod deploy together with no gate, and removing the allowlist makes Google visible to every prod user on deploy. Do not merge/deploy without the user's explicit go (see the open conversation).
- After Task 3, the `INTEGRATION_RECEIPTS` map and the per-agent Google assignment panel are unchanged — they key on `status === "connected"`, which is unaffected.
