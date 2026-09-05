# Slack App Phase 1 — Transport for Existing Paid Owners

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An existing paid owner clicks "Add to Slack" on an agent's Channels tab, authorizes ONE platform-owned Lightbulb Slack app, and that agent answers in their workspace over the Events API — replacing the 15-step "create your own Slack app" wizard as the default path.

**Architecture:** Slack POSTs every workspace's events to one backend URL. `routers/slack_app.py` verifies the app-wide signing secret, dedups, resolves `team_id → owner` via a new `slack-installs` table, re-signs the identical body with the owner's `hook_token`, and forwards to the container's `/slack/events/{team_id}` where `@openclaw/slack` runs in `mode:"http"`. The per-workspace bot token lives in Secrets Manager and reaches the container as an ECS task-def `secrets:` entry referenced by a SecretRef in `openclaw.json` — nothing plaintext on EFS. Synchronous proxy, no queue (ceiling named in the spec).

**Tech Stack:** FastAPI + httpx (backend), boto3/moto (DynamoDB, Secrets Manager), AWS CDK (`database-stack.ts`, `api-stack.ts`), Next.js + vitest (frontend tile), `@openclaw/slack` 2026.8.1 http mode (unchanged).

**Spec:** `docs/superpowers/specs/2026-09-04-slack-marketplace-app-design.md` — read §1 (constraints), §3.1–3.5 (transport, credentials, routing, config, lifecycle) and §3.8 (migration) before starting. Phase 1 = spec §6 step 1 only. Cold install (Clerk minting, trial, wake) is Phase 2 and is NOT in this plan.

## Global Constraints

- Backend Python: `cd apps/backend && uv run pytest tests/unit/... -v`; lint with `uv run ruff check .` before every commit. Let CI run the full suite.
- Infra: any change under `apps/infra/lib/` MUST be followed by `cd apps/infra && npx cdk synth` and the regenerated `.github/workflows/deploy.yml` committed in the same commit (CLAUDE.md, `infra-ci.yml` enforces it).
- DynamoDB tables use the prefix convention: `core.dynamodb.get_table("slack-installs")` → `lightbulb-{env}-slack-installs`. The backend task role already has a wildcard grant over the env-prefixed namespace (`service-stack.ts` "DynamoDbOwnedTables") — verify with `grep -n "allTableArns" apps/infra/lib/stacks/*.ts apps/infra/lib/*.ts` in Task 1; if it enumerates tables instead, add `slack-installs` there.
- Secrets Manager prefix is `lightbulb/{env}/...` (NOT `isol8/` — CLAUDE.md drift). Bot tokens go under `lightbulb/{env}/integrations/{owner}/slack/{team_id}` (the integrations grant already covers it).
- Never call `ecs_manager.resolve_running_container` on the Slack path — it runs a 5 s TCP probe. Use `discover_ip(service_name)` directly.
- Never re-serialize a Slack body. Forward `await request.body()` bytes verbatim; slash and interactive payloads are `application/x-www-form-urlencoded`.
- Slack HMAC: `v0=` + hex(HMAC-SHA256(secret, `v0:{timestamp}:{raw body}`)); constant-time compare; reject `|now − timestamp| > 300 s`.
- The container gateway port is `GATEWAY_PORT = 18789` (`ecs_manager.py:43`). The plugin route is `auth:"plugin"` — send NO Authorization header.
- The events route must return **200** for every per-owner failure (forward timeout, upstream 4xx/5xx, unknown team, unentitled owner). Return **5xx only** for backend-own failures (no IP → 503 so Slack retries; DDB/Secrets errors → 500). Slack disables an app only at >95 % failures over 60 min, but we still never let one tenant's box count against the app.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01W8TUbSwGZ3aC9gBbvk8XtZ
  ```
- Work on branch `design/slack-marketplace-app` in worktree `~/dev/wt-slack-spec` (already checked out from `origin/main`). Do not push without approval.

## Manual setup (operator, once per environment — NOT a coding task)

Done before Task 9 can be verified on dev. Recorded here so the executor knows what "the platform app" is.

1. At api.slack.com/apps → Create New App → From a manifest → paste `apps/backend/scripts/slack-app-manifest.yaml` (written in Task 14; use the dev URL for dev). Install to the Lightbulb dev workspace once so Slack activates the Request URL.
2. Basic Information → copy **Client ID**, **Client Secret**, **Signing Secret**.
3. Seed them: `cd apps/backend && uv run python scripts/set_platform_oauth_app.py --env dev --name slack --client-id <id> --with-signing-secret` (prompts for both secrets — Task 3 adds the flag).
4. OAuth & Permissions → Redirect URLs → add `https://api-dev.flickmeon.ai/api/v1/slack/oauth/callback` (prod: `https://api.flickmeon.ai/...`).
5. Event Subscriptions → Request URL `https://api-dev.flickmeon.ai/api/v1/slack/events` — Slack POSTs `url_verification`; Task 8's route answers it. Interactivity & Shortcuts → same URL. Slash command `/lightbulb` → same URL.
6. Settings → Manage Distribution → Activate Public Distribution (unlisted). Marketplace submission is Phase 3.

## File map

| file | responsibility |
|---|---|
| `apps/infra/lib/stacks/database-stack.ts` | `slack-installs` table (PK `team_id`, GSI `by-owner`) |
| `apps/infra/lib/stacks/api-stack.ts` | explicit `POST /api/v1/slack/events` route with its own throttle bucket |
| `apps/backend/core/repositories/slack_installs_repo.py` | conditional put / get / list_for_owner / set_status / delete_all_for_owner |
| `apps/backend/core/repositories/oauth_state_repo.py` | `extra` kwarg so state can carry `agent_id` + `member_id` |
| `apps/backend/core/services/credentials/platform_oauth_app_store.py` | pass `signing_secret` through |
| `apps/backend/scripts/set_platform_oauth_app.py` | `--with-signing-secret` |
| `apps/backend/core/services/slack_signing.py` | `sign` / `verify` — pure HMAC helpers |
| `apps/backend/core/services/slack_install_service.py` | `apply_install`, `revoke_install`, config-block builders, scope list |
| `apps/backend/core/containers/ecs_manager.py` | `refresh_task_definition(extra_secrets=)`, `_build_slack_secrets_for_user`, `_provision_channel_accounts` carries bindings |
| `apps/backend/core/containers/config.py` | `channel_bindings` param — re-inject carried bindings verbatim |
| `apps/backend/routers/slack_app.py` | `/slack/events`, `/slack/oauth/start`, `/slack/oauth/callback`, `/slack/install` |
| `apps/backend/main.py` | mount the router at `/api/v1/slack` |
| `apps/backend/core/services/account_wipe.py` | one `WipeStep` for installs |
| `apps/frontend/src/components/channels/SlackInstallCard.tsx` | Add-to-Slack / Connected card |
| `apps/frontend/src/components/control/panels/AgentChannelsSection.tsx` | render the card in the Slack slot |
| `apps/backend/scripts/slack-app-manifest.yaml` | the platform app's manifest |

---

### Task 1: `slack-installs` DynamoDB table

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (after the `agentEmailAddressesTable` block, ~line 209)
- Test: `apps/infra/test/database-stack.test.ts`

**Interfaces:**
- Produces: table `lightbulb-{env}-slack-installs`, PK `team_id` (S), GSI `by-owner` on `owner_id` (S), ALL projection.

- [ ] **Step 1: Write the failing test** — append to `apps/infra/test/database-stack.test.ts` inside the existing top-level `describe` that has `template = buildStack("dev")` (or add a new `describe` with the same `beforeAll`):

```ts
describe("DatabaseStack — slack-installs table", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates slack-installs keyed by team_id with a by-owner GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("slack-installs$"),
      KeySchema: [{ AttributeName: "team_id", KeyType: "HASH" }],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-owner",
          KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
          Projection: { ProjectionType: "ALL" },
        }),
      ]),
    });
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/infra && npx vitest run test/database-stack.test.ts -t "slack-installs"`
Expected: FAIL — no resource matches `slack-installs$`.

- [ ] **Step 3: Add the table** — in `database-stack.ts`, declare `public readonly slackInstallsTable: dynamodb.Table;` next to `agentEmailAddressesTable` (line ~36), then after the agent-email GSI block (line ~209):

```ts
    // One Lightbulb Slack app serves every workspace; Slack posts to ONE URL
    // with team_id in the body, so this is the team_id → owner routing table.
    // Same shape as agent-email-addresses (address → owner). The bot token is
    // NOT here — it lives in Secrets Manager under the owner's integrations
    // prefix; this row carries only its secret name.
    this.slackInstallsTable = createTable("SlackInstallsTable", "slack-installs", {
      partitionKey: { name: "team_id", type: dynamodb.AttributeType.STRING },
    });
    this.slackInstallsTable.addGlobalSecondaryIndex({
      indexName: "by-owner",
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });
```

- [ ] **Step 4: Verify the IAM grant covers it** — run `grep -rn "allTableArns" apps/infra/lib/ | head`. If `allTableArns` returns a wildcard over `lightbulb-{env}-*` (the comment in `service-stack.ts` says it does), nothing more. If it enumerates table names, add `"slack-installs"` to that list.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd apps/infra && npx vitest run test/database-stack.test.ts -t "slack-installs"`
Expected: PASS

- [ ] **Step 6: Synth and check deploy.yml drift**

Run: `cd apps/infra && npx cdk synth > /dev/null && git -C ../.. status --short .github/workflows/deploy.yml`
Expected: either no output (unchanged) or the file listed — stage it either way in the next step.

- [ ] **Step 7: Commit**

```bash
git add apps/infra/lib/stacks/database-stack.ts apps/infra/test/database-stack.test.ts .github/workflows/deploy.yml
git commit -m "feat(infra): slack-installs table — team_id → owner routing for the platform Slack app"
```

---

### Task 2: `slack_installs_repo`

**Files:**
- Create: `apps/backend/core/repositories/slack_installs_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_slack_installs_repo.py`

**Interfaces:**
- Produces:
  - `async put(*, team_id, owner_id, agent_id, team_name, bot_user_id, app_id, scopes: list[str], installer_slack_user_id, installer_member_id, token_secret_name) -> dict | None` — conditional; `None` when another owner holds the team and its row is not `revoked`.
  - `async get(team_id) -> dict | None` (strongly consistent)
  - `async list_for_owner(owner_id) -> list[dict]`
  - `async set_status(team_id, status: str) -> None`
  - `async delete_all_for_owner(owner_id) -> list[str]` — returns deleted team ids.
  - Row fields: `team_id, owner_id, agent_id, team_name, bot_user_id, app_id, scopes, installer_slack_user_id, installer_member_id, token_secret_name, status ("active"|"revoked"), installed_at, updated_at`.

- [ ] **Step 1: Write the failing tests**

```python
"""slack_installs_repo: team_id-keyed rows with a by-owner GSI.

Mirrors test_agent_email_repo — moto table + prefix/resource patches.
"""

import os

import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import slack_installs_repo as repo  # noqa: E402


@pytest.fixture
def installs_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-slack-installs",
            KeySchema=[{"AttributeName": "team_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "team_id", "AttributeType": "S"},
                {"AttributeName": "owner_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-owner",
                    "KeySchema": [{"AttributeName": "owner_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-slack-installs")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


def _put(**overrides):
    base = dict(
        team_id="T0123ABC",
        owner_id="org_1",
        agent_id="agent_friday",
        team_name="Acme",
        bot_user_id="U_BOT",
        app_id="A_APP",
        scopes=["chat:write", "app_mentions:read"],
        installer_slack_user_id="U_ADMIN",
        installer_member_id="user_1",
        token_secret_name="lightbulb/dev/integrations/org_1/slack/T0123ABC",
    )
    base.update(overrides)
    return repo.put(**base)


@pytest.mark.asyncio
async def test_put_then_get(installs_table):
    row = await _put()
    assert row["status"] == "active"
    got = await repo.get("T0123ABC")
    assert got["owner_id"] == "org_1"
    assert got["agent_id"] == "agent_friday"
    assert got["scopes"] == ["chat:write", "app_mentions:read"]


@pytest.mark.asyncio
async def test_same_owner_reinstall_overwrites(installs_table):
    await _put()
    row = await _put(bot_user_id="U_BOT2")
    assert row is not None
    assert (await repo.get("T0123ABC"))["bot_user_id"] == "U_BOT2"


@pytest.mark.asyncio
async def test_other_owner_cannot_hijack_active_install(installs_table):
    await _put(owner_id="org_1")
    assert await _put(owner_id="org_2") is None
    assert (await repo.get("T0123ABC"))["owner_id"] == "org_1"


@pytest.mark.asyncio
async def test_other_owner_can_claim_after_revoke(installs_table):
    await _put(owner_id="org_1")
    await repo.set_status("T0123ABC", "revoked")
    row = await _put(owner_id="org_2")
    assert row is not None and row["owner_id"] == "org_2"
    assert (await repo.get("T0123ABC"))["status"] == "active"


@pytest.mark.asyncio
async def test_list_for_owner_and_delete_all(installs_table):
    await _put(team_id="T1", owner_id="org_1")
    await _put(team_id="T2", owner_id="org_1")
    await _put(team_id="T3", owner_id="org_other")
    assert {r["team_id"] for r in await repo.list_for_owner("org_1")} == {"T1", "T2"}
    deleted = await repo.delete_all_for_owner("org_1")
    assert sorted(deleted) == ["T1", "T2"]
    assert await repo.get("T1") is None
    assert await repo.get("T3") is not None


@pytest.mark.asyncio
async def test_get_missing_is_none(installs_table):
    assert await repo.get("T_NOPE") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_slack_installs_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: core.repositories.slack_installs_repo`.

- [ ] **Step 3: Implement the repo**

```python
"""Routing table for the platform Slack app: one row per installed workspace.

Slack posts every workspace's events to ONE Request URL with ``team_id`` in the
body, so ``team_id`` is the partition key; a ``by-owner`` GSI lists an owner's
workspaces. The bot token is NOT stored here — ``token_secret_name`` points at
the Secrets Manager entry (``integration_secret_store``) that holds it.

``status`` is ``"active"`` or ``"revoked"`` (after ``app_uninstalled`` /
``tokens_revoked``). A revoked row keeps its ``team_id`` so a later reinstall —
by the same or a different owner — can claim it; an ACTIVE row can only be
overwritten by its own owner (no workspace hijack).

Prefix-convention table (``core.dynamodb.get_table``), matching
``agent_email_repo``.
"""

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"


def _get_table():
    return get_table("slack-installs")


async def put(
    *,
    team_id: str,
    owner_id: str,
    agent_id: str,
    team_name: str,
    bot_user_id: str,
    app_id: str,
    scopes: list[str],
    installer_slack_user_id: str,
    installer_member_id: str,
    token_secret_name: str,
) -> dict | None:
    """Claim ``team_id`` for ``owner_id``. Conditional put:
    ``attribute_not_exists(team_id) OR owner_id = :owner OR #st = :revoked``.
    Returns the written item, or ``None`` when another owner holds an ACTIVE
    install for this workspace."""
    now = utc_now_iso()
    item = {
        "team_id": team_id,
        "owner_id": owner_id,
        "agent_id": agent_id,
        "team_name": team_name,
        "bot_user_id": bot_user_id,
        "app_id": app_id,
        "scopes": list(scopes),
        "installer_slack_user_id": installer_slack_user_id,
        "installer_member_id": installer_member_id,
        "token_secret_name": token_secret_name,
        "status": STATUS_ACTIVE,
        "installed_at": now,
        "updated_at": now,
    }
    try:
        await run_in_thread(
            _get_table().put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(team_id) OR owner_id = :owner OR #st = :revoked",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":owner": owner_id, ":revoked": STATUS_REVOKED},
        )
        return item
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


async def get(team_id: str) -> dict | None:
    """Strongly consistent — the events path decides routing on it."""
    resp = await run_in_thread(_get_table().get_item, Key={"team_id": team_id}, ConsistentRead=True)
    return resp.get("Item")


async def list_for_owner(owner_id: str) -> list[dict]:
    resp = await run_in_thread(
        _get_table().query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return resp.get("Items", [])


async def set_status(team_id: str, status: str) -> None:
    """No-op for an unknown team — never upserts a partial row (update_item
    creates the item otherwise, and every reader would then KeyError on
    ``install["owner_id"]``)."""
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"team_id": team_id},
            UpdateExpression="SET #st = :s, updated_at = :t",
            ConditionExpression="attribute_exists(team_id)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": status, ":t": utc_now_iso()},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return
        raise


async def delete_all_for_owner(owner_id: str) -> list[str]:
    """Teardown: delete every install row for ``owner_id``. Returns team ids."""
    rows = await list_for_owner(owner_id)
    table = _get_table()
    deleted: list[str] = []
    for row in rows:
        team_id = row.get("team_id")
        if not team_id:
            continue
        await run_in_thread(table.delete_item, Key={"team_id": team_id})
        deleted.append(team_id)
    return deleted
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_slack_installs_repo.py -v && uv run ruff check core/repositories/slack_installs_repo.py`
Expected: 6 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/slack_installs_repo.py apps/backend/tests/unit/repositories/test_slack_installs_repo.py
git commit -m "feat(backend): slack_installs_repo — conditional claim of a workspace by one owner"
```

---

### Task 3: Platform app store carries the signing secret

**Files:**
- Modify: `apps/backend/core/services/credentials/platform_oauth_app_store.py:55-57` (`_get_sync` return)
- Modify: `apps/backend/scripts/set_platform_oauth_app.py` (add `--with-signing-secret`)
- Test: `apps/backend/tests/unit/services/credentials/test_platform_oauth_app_store.py` (new)

**Interfaces:**
- Produces: `platform_oauth_app_store.get("slack")` → `{"client_id", "client_secret", "signing_secret"}` (`signing_secret` is `""` when unseeded, so Meta callers are unaffected).

- [ ] **Step 1: Write the failing test**

```python
import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import platform_oauth_app_store as store  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    store._cache.clear()
    yield
    store._cache.clear()


@pytest.mark.asyncio
@patch("core.services.credentials.platform_oauth_app_store._client")
async def test_get_passes_signing_secret_through(mock_client):
    sm = MagicMock()
    mock_client.return_value = sm
    sm.get_secret_value.return_value = {
        "SecretString": json.dumps({"client_id": "cid", "client_secret": "cs", "signing_secret": "ss"})
    }
    got = await store.get("slack")
    assert got == {"client_id": "cid", "client_secret": "cs", "signing_secret": "ss"}


@pytest.mark.asyncio
@patch("core.services.credentials.platform_oauth_app_store._client")
async def test_get_without_signing_secret_is_empty_string(mock_client):
    sm = MagicMock()
    mock_client.return_value = sm
    sm.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "cid", "client_secret": "cs"})}
    got = await store.get("meta-instagram")
    assert got["signing_secret"] == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_platform_oauth_app_store.py -v`
Expected: FAIL — first test's dict lacks `signing_secret`.

- [ ] **Step 3: Widen `_get_sync`** — replace the final `return` in `platform_oauth_app_store._get_sync`:

```python
    return {
        "client_id": data["client_id"],
        "client_secret": data.get("client_secret") or "",
        # Slack signs webhooks with a separate "signing secret" (Meta reuses
        # the client secret). Empty when the seeded app has none.
        "signing_secret": data.get("signing_secret") or "",
    }
```

and update the module docstring's payload line to `{"client_id", "client_secret", "signing_secret"?}` and `get`'s docstring to mention `signing_secret`.

- [ ] **Step 4: Add the script flag** — in `scripts/set_platform_oauth_app.py`, after `ap.add_argument("--profile", ...)`:

```python
    ap.add_argument(
        "--with-signing-secret",
        action="store_true",
        help="also prompt for a signing secret (Slack signs webhooks with one; Meta does not)",
    )
```

and after `client_secret` is read:

```python
    payload_dict = {"client_id": args.client_id, "client_secret": client_secret}
    if args.with_signing_secret:
        signing_secret = getpass.getpass("Signing secret: ").strip()
        if not signing_secret:
            print("empty signing secret — aborting", file=sys.stderr)
            return 1
        payload_dict["signing_secret"] = signing_secret
    payload = json.dumps(payload_dict)
```

replacing the existing `payload = json.dumps({...})` line. Update the module docstring usage block with a `--name slack --with-signing-secret` example.

- [ ] **Step 5: Run tests + ruff**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_platform_oauth_app_store.py -v && uv run ruff check core/services/credentials/platform_oauth_app_store.py scripts/set_platform_oauth_app.py`
Expected: 2 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/credentials/platform_oauth_app_store.py apps/backend/scripts/set_platform_oauth_app.py apps/backend/tests/unit/services/credentials/test_platform_oauth_app_store.py
git commit -m "feat(backend): platform OAuth app store carries a signing_secret (Slack)"
```

---

### Task 4: Slack signing helpers

**Files:**
- Create: `apps/backend/core/services/slack_signing.py`
- Test: `apps/backend/tests/unit/services/test_slack_signing.py`

**Interfaces:**
- Produces:
  - `sign(secret: str, timestamp: str, body: bytes) -> str` → `"v0=<hex>"`
  - `verify(secret: str, timestamp: str | None, body: bytes, signature: str | None, *, now: float | None = None, tolerance_seconds: int = 300) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
import hashlib
import hmac
import time

from core.services import slack_signing as s

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
BODY = b'{"type":"event_callback","team_id":"T1"}'


def _expected(ts: str) -> str:
    return "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + BODY, hashlib.sha256).hexdigest()


def test_sign_matches_slack_basestring():
    assert s.sign(SECRET, "1531420618", BODY) == _expected("1531420618")


def test_verify_accepts_fresh_valid_signature():
    ts = str(int(time.time()))
    assert s.verify(SECRET, ts, BODY, s.sign(SECRET, ts, BODY)) is True


def test_verify_rejects_wrong_secret():
    ts = str(int(time.time()))
    assert s.verify("other", ts, BODY, s.sign(SECRET, ts, BODY)) is False


def test_verify_rejects_tampered_body():
    ts = str(int(time.time()))
    assert s.verify(SECRET, ts, BODY + b" ", s.sign(SECRET, ts, BODY)) is False


def test_verify_rejects_stale_timestamp():
    ts = str(int(time.time()) - 301)
    assert s.verify(SECRET, ts, BODY, s.sign(SECRET, ts, BODY)) is False


def test_verify_rejects_missing_or_malformed_inputs():
    ts = str(int(time.time()))
    assert s.verify(SECRET, None, BODY, s.sign(SECRET, ts, BODY)) is False
    assert s.verify(SECRET, ts, BODY, None) is False
    assert s.verify(SECRET, "not-a-number", BODY, s.sign(SECRET, ts, BODY)) is False
    assert s.verify(SECRET, ts, BODY, "sha256=abc") is False


def test_verify_honours_injected_now():
    assert s.verify(SECRET, "1531420618", BODY, _expected("1531420618"), now=1531420618 + 10) is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_slack_signing.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Slack request signing (v0) — https://docs.slack.dev/authentication/verifying-requests-from-slack

Two callers: the events route VERIFIES Slack's signature with the platform
app's signing secret, then RE-SIGNS the identical body with the owner's
``hook_token`` so the tenant container's Bolt receiver can verify it without
ever holding the app-wide secret (spec §3.2). Pure functions; no I/O.
"""

from __future__ import annotations

import hashlib
import hmac
import time

VERSION = "v0"


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """``v0=`` + hex(HMAC-SHA256(secret, ``v0:{timestamp}:{body}``))."""
    basestring = f"{VERSION}:{timestamp}:".encode() + body
    return f"{VERSION}=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    *,
    now: float | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Constant-time check of ``signature`` against ``body``/``timestamp``, and a
    replay guard: a timestamp more than ``tolerance_seconds`` from ``now`` is
    refused. Missing or malformed inputs are simply False — the caller is a
    machine, not a person."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_seconds:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))
```

- [ ] **Step 4: Run tests + ruff**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_slack_signing.py -v && uv run ruff check core/services/slack_signing.py`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/slack_signing.py apps/backend/tests/unit/services/test_slack_signing.py
git commit -m "feat(backend): Slack v0 request signing helpers (verify + re-sign)"
```

---

### Task 5: `oauth_state_repo.put` accepts `extra`

**Files:**
- Modify: `apps/backend/core/repositories/oauth_state_repo.py:18-45`
- Test: `apps/backend/tests/unit/repositories/test_oauth_state_repo.py` (create if absent; check with `ls`)

**Interfaces:**
- Produces: `oauth_state_repo.put(state, *, owner_id, provider, verifier, client_id=None, origin=None, extra: dict | None = None)`; `consume(state)` returns the merged item, so `rec["agent_id"]` / `rec["member_id"]` round-trip.

- [ ] **Step 1: Write the failing test** (append or create; use the moto pattern from Task 2 with table `test-oauth-state`, PK `state`):

```python
import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import oauth_state_repo as repo  # noqa: E402


@pytest.fixture
def state_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-oauth-state",
            KeySchema=[{"AttributeName": "state", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "state", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-oauth-state")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


@pytest.mark.asyncio
async def test_extra_fields_round_trip_and_state_is_single_use(state_table):
    await repo.put("st_1", owner_id="org_1", provider="slack", verifier="", extra={"agent_id": "agent_f", "member_id": "user_1"})
    rec = await repo.consume("st_1")
    assert rec["owner_id"] == "org_1"
    assert rec["agent_id"] == "agent_f"
    assert rec["member_id"] == "user_1"
    assert await repo.consume("st_1") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_oauth_state_repo.py -v -k extra`
Expected: FAIL — `TypeError: put() got an unexpected keyword argument 'extra'`.

- [ ] **Step 3: Add the kwarg** — in `put`'s signature add `extra: dict | None = None,` after `origin`, and before `await run_in_thread(...)`:

```python
    # Caller-defined attributes that must survive the redirect (the Slack
    # install carries agent_id + member_id). Reserved keys stay ours.
    if extra:
        for k, v in extra.items():
            if k not in item and v is not None:
                item[k] = v
```

- [ ] **Step 4: Run tests + ruff**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_oauth_state_repo.py -v && uv run ruff check core/repositories/oauth_state_repo.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/oauth_state_repo.py apps/backend/tests/unit/repositories/test_oauth_state_repo.py
git commit -m "feat(backend): oauth_state_repo.put carries caller extras across the redirect"
```

---

### Task 6: ECS task-def secrets for Slack tokens

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py` — `refresh_task_definition` (line ~802) and add `_build_slack_secrets_for_user` beside `_build_byo_secrets_for_user` (line ~2563); the BYOK `secrets_for_task` assembly (line ~2862-2893).
- Test: `apps/backend/tests/unit/containers/test_ecs_manager_slack_secrets.py` (new)

**Interfaces:**
- Consumes: `slack_installs_repo.list_for_owner`, `integration_secret_store` naming (`lightbulb/{env}/integrations/{owner}/slack/{team_id}`).
- Produces:
  - `EcsManager.refresh_task_definition(owner_id, *, extra_secrets: list[dict] | None = None) -> str | None` — `extra_secrets` entries `{"name","valueFrom"}` replace same-named entries on the openclaw container's `secrets` list.
  - `EcsManager._build_slack_secrets_for_user(owner_id) -> list[dict]` — one `{"name": "SLACK_BOT_TOKEN_{TEAM}", "valueFrom": <arn>}` per active install; `[]` on any failure (never blocks provisioning).
  - module-level `slack_env_var_name(team_id: str) -> str` → `f"SLACK_BOT_TOKEN_{team_id.upper()}"`.

- [ ] **Step 1: Write the failing tests**

```python
"""refresh_task_definition(extra_secrets=) + _build_slack_secrets_for_user."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers import ecs_manager as em  # noqa: E402


def _manager():
    m = em.EcsManager.__new__(em.EcsManager)
    m._ecs = MagicMock()
    return m


def _task_def():
    return {
        "taskDefinition": {
            "family": "fam",
            "networkMode": "awsvpc",
            "containerDefinitions": [
                {
                    "name": "openclaw",
                    "environment": [{"name": "OPENCLAW_HOOK_TOKEN", "value": "old"}],
                    "secrets": [{"name": "OPENAI_API_KEY", "valueFrom": "arn:byok"}],
                }
            ],
        }
    }


def test_slack_env_var_name_is_uppercase_team():
    assert em.slack_env_var_name("t0123abc") == "SLACK_BOT_TOKEN_T0123ABC"


@pytest.mark.asyncio
async def test_refresh_merges_extra_secrets_replacing_same_name():
    m = _manager()
    m._ecs.describe_task_definition.return_value = _task_def()
    m._ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:new"}}
    container = {"task_definition_arn": "arn:old", "hook_token": "hk", "service_name": "svc"}
    with (
        patch.object(em.container_repo, "get_by_owner_id", AsyncMock(return_value=container)),
        patch.object(em.container_repo, "update_fields", AsyncMock()),
        patch.object(em.settings, "ECS_CLUSTER_ARN", "arn:cluster"),
    ):
        arn = await m.refresh_task_definition(
            "org_1",
            extra_secrets=[
                {"name": "SLACK_BOT_TOKEN_T1", "valueFrom": "arn:slack1"},
                {"name": "OPENAI_API_KEY", "valueFrom": "arn:byok-new"},
            ],
        )
    assert arn == "arn:new"
    registered = m._ecs.register_task_definition.call_args.kwargs["containerDefinitions"][0]["secrets"]
    assert {s["name"]: s["valueFrom"] for s in registered} == {
        "OPENAI_API_KEY": "arn:byok-new",
        "SLACK_BOT_TOKEN_T1": "arn:slack1",
    }


@pytest.mark.asyncio
async def test_refresh_without_extra_secrets_keeps_existing():
    m = _manager()
    m._ecs.describe_task_definition.return_value = _task_def()
    m._ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:new"}}
    container = {"task_definition_arn": "arn:old", "hook_token": "hk", "service_name": "svc"}
    with (
        patch.object(em.container_repo, "get_by_owner_id", AsyncMock(return_value=container)),
        patch.object(em.container_repo, "update_fields", AsyncMock()),
        patch.object(em.settings, "ECS_CLUSTER_ARN", "arn:cluster"),
    ):
        await m.refresh_task_definition("org_1")
    registered = m._ecs.register_task_definition.call_args.kwargs["containerDefinitions"][0]["secrets"]
    assert registered == [{"name": "OPENAI_API_KEY", "valueFrom": "arn:byok"}]


@pytest.mark.asyncio
async def test_build_slack_secrets_lists_active_installs_only():
    m = _manager()
    rows = [
        {"team_id": "T1", "status": "active", "token_secret_name": "lightbulb/dev/integrations/org_1/slack/T1"},
        {"team_id": "T2", "status": "revoked", "token_secret_name": "lightbulb/dev/integrations/org_1/slack/T2"},
    ]
    with (
        patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(return_value=rows)),
        patch("core.containers.ecs_manager._secret_arn_for_name", AsyncMock(side_effect=lambda n: f"arn:{n}")),
    ):
        out = await m._build_slack_secrets_for_user("org_1")
    assert out == [{"name": "SLACK_BOT_TOKEN_T1", "valueFrom": "arn:lightbulb/dev/integrations/org_1/slack/T1"}]


@pytest.mark.asyncio
async def test_build_slack_secrets_is_best_effort():
    m = _manager()
    with patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await m._build_slack_secrets_for_user("org_1") == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_ecs_manager_slack_secrets.py -v`
Expected: FAIL — `slack_env_var_name` missing / unexpected kwarg.

- [ ] **Step 3: Implement** — in `ecs_manager.py`:

(a) module level, near `_provision_channel_accounts` (line ~152):

```python
def slack_env_var_name(team_id: str) -> str:
    """ECS/SecretRef env var carrying one workspace's bot token. The OpenClaw
    SecretRef ``id`` must match ``^[A-Z][A-Z0-9_]{0,127}$`` — Slack team ids
    (``T0123ABC``) do; agent ids would not, which is one reason the Slack
    account is keyed by team, not agent."""
    return f"SLACK_BOT_TOKEN_{team_id.upper()}"


async def _secret_arn_for_name(secret_name: str) -> str | None:
    """ARN for a Secrets Manager name (task-def ``secrets:`` needs the ARN)."""
    from core.config import settings as _settings

    def _describe() -> str | None:
        sm = boto3.client("secretsmanager", region_name=_settings.AWS_REGION)
        try:
            return sm.describe_secret(SecretId=secret_name)["ARN"]
        except sm.exceptions.ResourceNotFoundException:
            return None

    return await asyncio.to_thread(_describe)
```

(`boto3` and `asyncio` are already imported in this module — verify with `grep -n "^import boto3\|^import asyncio" core/containers/ecs_manager.py`; add if missing.)

(b) `refresh_task_definition` signature → `async def refresh_task_definition(self, owner_id: str, *, extra_secrets: list[dict] | None = None) -> str | None:`; add to its docstring: *"``extra_secrets`` (``{name, valueFrom}``) are merged onto the openclaw container's ``secrets`` list, replacing same-named entries — the Slack install uses this to add ``SLACK_BOT_TOKEN_{TEAM}`` without touching BYOK entries."* Then, inside the `for cd in td["containerDefinitions"]:` loop where `cd["environment"] = new_env` is set:

```python
            if cd["name"] == "openclaw":
                cd["environment"] = new_env
                if extra_secrets:
                    incoming = {s["name"]: s for s in extra_secrets}
                    kept = [s for s in (cd.get("secrets") or []) if s.get("name") not in incoming]
                    cd["secrets"] = kept + list(incoming.values())
```

(c) beside `_build_byo_secrets_for_user`:

```python
    async def _build_slack_secrets_for_user(self, owner_id: str) -> list[dict]:
        """ECS ``secrets:`` entries for every ACTIVE Slack install of ``owner_id``
        — one ``SLACK_BOT_TOKEN_{TEAM}`` per workspace, so a reprovision or
        image roll re-registers them and the SecretRef in openclaw.json keeps
        resolving. Best-effort: ``[]`` on any failure; provisioning must never
        break because the Slack table hiccupped (the owner re-installs)."""
        try:
            from core.repositories import slack_installs_repo

            rows = await slack_installs_repo.list_for_owner(owner_id)
            out: list[dict] = []
            for row in rows:
                if row.get("status") != "active" or not row.get("token_secret_name"):
                    continue
                arn = await _secret_arn_for_name(row["token_secret_name"])
                if arn:
                    out.append({"name": slack_env_var_name(row["team_id"]), "valueFrom": arn})
            return out
        except Exception:  # noqa: BLE001 — best-effort; never fail provision over this.
            logger.exception("building slack secrets failed for %s; continuing", owner_id)
            return []
```

(d) where BYOK `secrets_for_task` is assembled for a provision (line ~2862-2893: `secrets_for_task: list[dict] = []` … `secrets_for_task = await self._build_byo_secrets_for_user(...)`), append right after that block:

```python
        # Slack bot tokens ride the task-def like BYOK keys do, so a fresh
        # provision / reprovision of an owner with installs keeps them.
        secrets_for_task = list(secrets_for_task) + await self._build_slack_secrets_for_user(user_id)
```

Find the exact insertion point with `grep -n "_build_byo_secrets_for_user(user_id, byo_provider)" core/containers/ecs_manager.py` — insert after the `if`/`else` that sets `secrets_for_task`, before it is passed on.

- [ ] **Step 4: Run tests + ruff + the existing ecs_manager suite**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_ecs_manager_slack_secrets.py tests/unit/containers/ -q -k "ecs_manager or refresh_task" && uv run ruff check core/containers/ecs_manager.py`
Expected: new tests pass; no regressions in existing `refresh_task_definition` tests.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_ecs_manager_slack_secrets.py
git commit -m "feat(backend): task-def secrets for Slack bot tokens (refresh extra_secrets + provision-time build)"
```

---

### Task 7: `slack_install_service`

**Files:**
- Create: `apps/backend/core/services/slack_install_service.py`
- Test: `apps/backend/tests/unit/services/test_slack_install_service.py`

**Interfaces:**
- Consumes: `slack_installs_repo` (Task 2), `integration_secret_store.put/delete`, `integrations_repo.put_status` (check signature with `grep -n "def put_status\|def delete_status\|def put\b" core/repositories/integrations_repo.py` — use whatever the status-index writer is named; if it's `put(owner_id, provider, status=...)`, adapt the two calls below), `get_ecs_manager().refresh_task_definition(extra_secrets=)` (Task 6), `slack_env_var_name` (Task 6), `config_patcher.patch_openclaw_config / delete_openclaw_config_path / remove_from_openclaw_config_list`, `channel_link_repo.sweep_by_owner_provider_agent`.
- Produces:
  - `BOT_SCOPES: list[str]` — the manifest's bot scopes (Task 14 must match).
  - `secret_provider(team_id) -> str` → `f"slack/{team_id}"`.
  - `account_block(team_id) -> dict` — the http/SecretRef account.
  - `route_binding(team_id, agent_id) -> dict`.
  - `async apply_install(*, owner_id, agent_id, team_id, team_name, bot_token, bot_user_id, app_id, scopes, installer_slack_user_id, installer_member_id) -> dict` — returns the install row; raises `SlackInstallConflict` when another owner holds the workspace.
  - `async revoke_install(team_id) -> bool` — False when unknown.

- [ ] **Step 1: Write the failing tests**

```python
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import slack_install_service as svc  # noqa: E402


def test_account_block_is_http_with_secret_refs_and_no_plaintext():
    block = svc.account_block("T0123ABC")
    assert block["mode"] == "http"
    assert block["webhookPath"] == "/slack/events/T0123ABC"
    assert block["botToken"] == {"source": "env", "provider": "default", "id": "SLACK_BOT_TOKEN_T0123ABC"}
    assert block["signingSecret"] == {"source": "env", "provider": "default", "id": "OPENCLAW_HOOK_TOKEN"}
    assert "appToken" not in block
    assert block["dmPolicy"] == "open" and block["allowFrom"] == ["*"]
    assert block["slashCommand"] == {"enabled": True, "name": "lightbulb"}


def test_route_binding_targets_agent_by_account():
    assert svc.route_binding("T1", "agent_f") == {
        "type": "route",
        "agentId": "agent_f",
        "match": {"channel": "slack", "accountId": "T1"},
    }


def _install_kwargs(**over):
    base = dict(
        owner_id="org_1",
        agent_id="agent_f",
        team_id="T1",
        team_name="Acme",
        bot_token="xoxb-1",
        bot_user_id="U_BOT",
        app_id="A1",
        scopes=["chat:write"],
        installer_slack_user_id="U_ADMIN",
        installer_member_id="user_1",
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_apply_install_writes_secret_row_taskdef_and_config_in_order():
    calls: list[str] = []

    async def _put_secret(owner, provider, cred):
        calls.append(f"secret:{owner}:{provider}:{cred['bot_token']}")
        return "arn:secret"

    async def _put_row(**kw):
        calls.append("row")
        return {**kw, "status": "active"}

    async def _status(*a, **k):
        calls.append("status")

    async def _refresh(owner, *, extra_secrets):
        calls.append(f"refresh:{extra_secrets[0]['name']}:{extra_secrets[0]['valueFrom']}")
        return "arn:td"

    async def _patch(owner, patch):
        calls.append("patch")
        assert patch["channels"]["slack"]["accounts"]["T1"]["mode"] == "http"
        assert patch["channels"]["slack"]["enabled"] is True
        assert patch["channels"]["slack"]["streaming"] == {"mode": "progress"}

    async def _append(owner, path, entries):
        calls.append(f"bindings:{entries[0]['agentId']}")

    mgr = MagicMock()
    mgr.refresh_task_definition = _refresh
    with (
        patch("core.services.slack_install_service.integration_secret_store.put", _put_secret),
        patch("core.services.slack_install_service.slack_installs_repo.put", _put_row),
        patch("core.services.slack_install_service._mark_connected", _status),
        patch("core.services.slack_install_service.get_ecs_manager", lambda: mgr),
        patch("core.services.slack_install_service.patch_openclaw_config", _patch),
        patch("core.services.slack_install_service.append_to_openclaw_config_list", _append),
    ):
        row = await svc.apply_install(**_install_kwargs())
    assert row["status"] == "active"
    assert calls == [
        "secret:org_1:slack/T1:xoxb-1",
        "row",
        "status",
        "refresh:SLACK_BOT_TOKEN_T1:arn:secret",
        "patch",
        "bindings:agent_f",
    ]


@pytest.mark.asyncio
async def test_apply_install_conflict_raises_and_deletes_secret():
    deleted = []

    async def _delete(owner, provider):
        deleted.append(provider)

    with (
        patch("core.services.slack_install_service.integration_secret_store.put", AsyncMock(return_value="arn")),
        patch("core.services.slack_install_service.integration_secret_store.delete", _delete),
        patch("core.services.slack_install_service.slack_installs_repo.put", AsyncMock(return_value=None)),
    ):
        with pytest.raises(svc.SlackInstallConflict):
            await svc.apply_install(**_install_kwargs())
    assert deleted == ["slack/T1"]


@pytest.mark.asyncio
async def test_revoke_install_tears_down_everything():
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}
    removed = {}

    async def _remove(owner, path, predicate):
        removed["pred"] = predicate

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()) as set_status,
        patch("core.services.slack_install_service.integration_secret_store.delete", AsyncMock()) as del_secret,
        patch("core.services.slack_install_service.delete_openclaw_config_path", AsyncMock()) as del_path,
        patch("core.services.slack_install_service.remove_from_openclaw_config_list", _remove),
        patch("core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent", AsyncMock(return_value=2)),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()),
    ):
        assert await svc.revoke_install("T1") is True
    set_status.assert_awaited_once_with("T1", "revoked")
    del_secret.assert_awaited_once_with("org_1", "slack/T1")
    del_path.assert_awaited_once_with("org_1", ["channels", "slack", "accounts", "T1"])
    assert removed["pred"]({"type": "route", "agentId": "x", "match": {"channel": "slack", "accountId": "T1"}}) is True
    assert removed["pred"]({"type": "route", "agentId": "x", "match": {"channel": "slack", "accountId": "T9"}}) is False


@pytest.mark.asyncio
async def test_revoke_unknown_team_is_false():
    with patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=None)):
        assert await svc.revoke_install("T_NOPE") is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_slack_install_service.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Install/uninstall of the platform Slack app for one owner (spec §3.2–3.5).

Order on install matters: secret → routing row → task-def roll → config. The
container's SecretRef resolves ``process.env.SLACK_BOT_TOKEN_{TEAM}``; writing
the account block before the task-def carries the env var just leaves the
account ``configured_unavailable`` on the OLD task until the roll lands, which
is harmless — but rolling first means the NEW task boots with everything.
"""

from __future__ import annotations

import logging

from core.containers import get_ecs_manager
from core.containers.ecs_manager import slack_env_var_name
from core.repositories import channel_link_repo, integrations_repo, slack_installs_repo
from core.services.config_patcher import (
    append_to_openclaw_config_list,
    delete_openclaw_config_path,
    patch_openclaw_config,
    remove_from_openclaw_config_list,
)
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

PROVIDER = "slack"

# The platform app's bot scopes. scripts/slack-app-manifest.yaml MUST list the
# same set — Slack refuses an OAuth request for a scope the app doesn't declare.
BOT_SCOPES: list[str] = [
    "app_mentions:read",
    "assistant:write",
    "channels:history",
    "channels:read",
    "chat:write",
    "commands",
    "files:read",
    "files:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "mpim:write",
    "reactions:read",
    "reactions:write",
    "users:read",
]


class SlackInstallConflict(Exception):
    """Another owner holds an active install for this workspace."""


def secret_provider(team_id: str) -> str:
    """``integration_secret_store`` provider segment — nested under ``slack/`` so
    the bare ``slack`` key is never claimed (the integrations rail's rule)."""
    return f"{PROVIDER}/{team_id}"


def account_block(team_id: str) -> dict:
    """``channels.slack.accounts.<team_id>`` for the platform app: http mode, one
    webhook path per account (the plugin's route registry is first-writer-
    wins), and SecretRefs — the bot token from its task-def env var, the
    signing secret from ``OPENCLAW_HOOK_TOKEN`` because the backend re-signs
    every forwarded request with the owner's hook_token (spec §3.2)."""
    return {
        "mode": "http",
        "webhookPath": f"/slack/events/{team_id}",
        "botToken": {"source": "env", "provider": "default", "id": slack_env_var_name(team_id)},
        "signingSecret": {"source": "env", "provider": "default", "id": "OPENCLAW_HOOK_TOKEN"},
        # Decision 3: Slack peers, not members — anyone in the workspace may DM.
        "dmPolicy": "open",
        "allowFrom": ["*"],
        # Upstream default is disabled; the manifest declares /lightbulb.
        "slashCommand": {"enabled": True, "name": "lightbulb"},
    }


def route_binding(team_id: str, agent_id: str) -> dict:
    return {"type": "route", "agentId": agent_id, "match": {"channel": PROVIDER, "accountId": team_id}}


async def _mark_connected(owner_id: str) -> None:
    await integrations_repo.put_status(owner_id, PROVIDER, "connected")


async def _mark_disconnected(owner_id: str) -> None:
    await integrations_repo.delete_status(owner_id, PROVIDER)


async def apply_install(
    *,
    owner_id: str,
    agent_id: str,
    team_id: str,
    team_name: str,
    bot_token: str,
    bot_user_id: str,
    app_id: str,
    scopes: list[str],
    installer_slack_user_id: str,
    installer_member_id: str,
) -> dict:
    provider = secret_provider(team_id)
    # put() returns the secret ARN — that is what the task-def needs.
    arn = await integration_secret_store.put(owner_id, provider, {"bot_token": bot_token, "bot_user_id": bot_user_id})
    secret_name = integration_secret_store._secret_name(owner_id, provider)
    row = await slack_installs_repo.put(
        team_id=team_id,
        owner_id=owner_id,
        agent_id=agent_id,
        team_name=team_name,
        bot_user_id=bot_user_id,
        app_id=app_id,
        scopes=scopes,
        installer_slack_user_id=installer_slack_user_id,
        installer_member_id=installer_member_id,
        token_secret_name=secret_name,
    )
    if row is None:
        # Somebody else's workspace. Don't leave our copy of their token behind.
        await integration_secret_store.delete(owner_id, provider)
        raise SlackInstallConflict(team_id)
    await _mark_connected(owner_id)

    # ONE locked write carries the account block AND its binding: the patcher
    # takes the account→agent mapping, so there is never a window where the
    # account exists without a route, or with the legacy agentId==accountId
    # auto-binding pointing at an agent that does not exist.
    await get_ecs_manager().refresh_task_definition(
        owner_id, extra_secrets=[{"name": slack_env_var_name(team_id), "valueFrom": arn}]
    )
    await patch_openclaw_config(
        owner_id,
        {
            "channels": {
                PROVIDER: {
                    "enabled": True,
                    "streaming": {"mode": "progress"},
                    "accounts": {team_id: account_block(team_id)},
                }
            }
        },
        agent_for_account={team_id: agent_id},
    )
    logger.info("slack install applied owner=%s team=%s agent=%s", owner_id, team_id, agent_id)
    return row


async def revoke_install(team_id: str) -> bool:
    """``app_uninstalled`` / ``tokens_revoked`` / owner Disconnect: mark the row,
    drop the secret, remove the account + binding, sweep pairing rows. The
    container keeps running; Clerk and billing are untouched."""
    row = await slack_installs_repo.get(team_id)
    if not row:
        return False
    owner_id, agent_id = row["owner_id"], row.get("agent_id") or ""
    await slack_installs_repo.set_status(team_id, slack_installs_repo.STATUS_REVOKED)
    await integration_secret_store.delete(owner_id, secret_provider(team_id))
    await delete_openclaw_config_path(owner_id, ["channels", PROVIDER, "accounts", team_id])
    await remove_from_openclaw_config_list(
        owner_id,
        ["bindings"],
        predicate=lambda b: isinstance(b, dict)
        and b.get("match", {}).get("channel") == PROVIDER
        and b.get("match", {}).get("accountId") == team_id,
    )
    if agent_id:
        await channel_link_repo.sweep_by_owner_provider_agent(owner_id=owner_id, provider=PROVIDER, agent_id=agent_id)
    await _mark_disconnected(owner_id)
    logger.info("slack install revoked owner=%s team=%s", owner_id, team_id)
    return True
```

Two things to resolve while implementing, both by reading the neighbour once:

1. `integrations_repo` — the test patches `_mark_connected`/`_mark_disconnected`, so the exact repo function names only need to be right at runtime. Run `grep -n "^async def " core/repositories/integrations_repo.py` and use the status-index writer/deleter it exposes (the brief names the row shape `{owner_id, provider, status, connected_at, updated_at}`).
2. `patch_openclaw_config` gains a keyword-only `agent_for_account: dict[str, str] | None = None` that flows into `_ensure_channel_bindings_for_patch(cfg, patch, agent_for_account)`, which uses `agent_for_account.get(account_id, account_id)` as the binding's `agentId` (absent → the legacy `agentId == accountId` the BYO wizard relies on). Add a config_patcher unit test for both shapes. This is what lets the install be a single locked write.

- [ ] **Step 4: Run tests + ruff**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_slack_install_service.py -v && uv run ruff check core/services/slack_install_service.py core/containers/ecs_manager.py`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/slack_install_service.py apps/backend/tests/unit/services/test_slack_install_service.py apps/backend/core/containers/ecs_manager.py
git commit -m "feat(backend): slack_install_service — secret → row → task-def roll → http account block"
```

---

### Task 8: Events route — `POST /api/v1/slack/events`

**Files:**
- Create: `apps/backend/routers/slack_app.py` (events route only; OAuth routes come in Task 9)
- Modify: `apps/backend/main.py` — import + `app.include_router(slack_app.router, prefix="/api/v1/slack", tags=["slack"])` next to the `meta_webhook` mount (line ~888)
- Test: `apps/backend/tests/unit/routers/test_slack_app_events.py`

**Interfaces:**
- Consumes: `slack_signing.verify/sign` (Task 4), `platform_oauth_app_store.get("slack")` (Task 3), `webhook_dedup.record_event_or_skip`, `slack_installs_repo.get` (Task 2), `slack_install_service.revoke_install` (Task 7), `container_repo.get_by_owner_id`, `billing_repo.get_by_owner_id`, `provision_gate.has_always_on_entitlement`, `get_ecs_manager().discover_ip`, `GATEWAY_PORT`.
- Produces: route behaviour per the table in Step 3's docstring; module-level `_client_factory()` and `ecs_manager` for test patching (same idiom as `hooks_proxy`).

- [ ] **Step 1: Write the failing tests**

```python
"""POST /api/v1/slack/events — the platform Slack app's single Request URL.

Pins: the app signing secret is the only door in; a verified event lands on
the RIGHT owner's container, re-signed with THAT owner's hook_token, body
byte-identical; and every per-owner failure is a 200 (never lets one tenant
count against the shared app), while "no IP" is a 503 so Slack retries.
"""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app

APP_SECRET = "app-signing-secret"
HOOK_TOKEN = "owner-hook-token"
TEAM = "T0123ABC"
INSTALL = {"team_id": TEAM, "owner_id": "org_1", "agent_id": "agent_f", "bot_user_id": "U_BOT", "status": "active"}
CONTAINER = {"owner_id": "org_1", "hook_token": HOOK_TOKEN, "service_name": "openclaw-org-1-abc", "status": "running"}


@pytest.fixture
def client():
    return TestClient(app)


def _sig(secret: str, ts: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, secret: str = APP_SECRET, ts: str | None = None, ctype: str = "application/json"):
    ts = ts or str(int(time.time()))
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": _sig(secret, ts, body), "Content-Type": ctype}


def _event(event_id="Ev1", **event):
    e = {"type": "message", "user": "U_HUMAN", "text": "hi", "channel": "C1"}
    e.update(event)
    return json.dumps({"type": "event_callback", "team_id": TEAM, "event_id": event_id, "event": e}).encode()


def _app_store():
    return patch(
        "core.services.credentials.platform_oauth_app_store.get",
        AsyncMock(return_value={"client_id": "cid", "client_secret": "cs", "signing_secret": APP_SECRET}),
    )


def _dedup(result="recorded"):
    from core.services.webhook_dedup import WebhookDedupResult

    return patch(
        "routers.slack_app.webhook_dedup.record_event_or_skip",
        AsyncMock(return_value=WebhookDedupResult(result)),
    )


def _install(row=INSTALL):
    return patch("routers.slack_app.slack_installs_repo.get", AsyncMock(return_value=row))


def _container(row=CONTAINER):
    return patch("routers.slack_app.container_repo.get_by_owner_id", AsyncMock(return_value=row))


def _billing(status="active"):
    return patch(
        "routers.slack_app.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None if status is None else {"subscription_status": status}),
    )


def _ip(ip="10.0.42.17"):
    return patch("routers.slack_app.ecs_manager.discover_ip", return_value=ip)


class _Upstream(httpx.AsyncBaseTransport):
    def __init__(self, status=200, raise_exc=None):
        self.status, self.raise_exc, self.seen = status, raise_exc, []

    async def handle_async_request(self, request):
        self.seen.append(request)
        if self.raise_exc:
            raise self.raise_exc
        return httpx.Response(self.status, content=b"")


def _upstream(t: _Upstream):
    return patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=t))


def test_url_verification_echoes_challenge_when_signed(client):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    with _app_store():
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}


def test_bad_signature_is_401(client):
    body = _event()
    with _app_store():
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body, secret="wrong"))
    assert r.status_code == 401


def test_stale_timestamp_is_401(client):
    body = _event()
    with _app_store():
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body, ts=str(int(time.time()) - 400)))
    assert r.status_code == 401


def test_unseeded_app_is_503(client):
    body = _event()
    with patch("core.services.credentials.platform_oauth_app_store.get", AsyncMock(return_value=None)):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 503


def test_duplicate_event_is_200_and_not_forwarded(client):
    body = _event()
    t = _Upstream()
    with _app_store(), _dedup("already_seen"), _install(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and t.seen == []


def test_unknown_team_is_200(client):
    body = _event()
    with _app_store(), _dedup(), _install(None):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200


def test_own_bot_echo_is_dropped(client):
    body = _event(user="U_BOT")
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and t.seen == []


def test_unentitled_owner_is_200_no_retry_not_forwarded(client):
    body = _event()
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing("trial_local"), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and r.headers.get("x-slack-no-retry") == "1" and t.seen == []


def test_no_ip_is_503_with_retry_after(client):
    body = _event()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(None):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 503 and "Retry-After" in r.headers


def test_forward_is_byte_identical_and_re_signed_with_hook_token(client):
    body = _event()
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    (req,) = t.seen
    assert str(req.url) == f"http://10.0.42.17:18789/slack/events/{TEAM}"
    assert req.content == body
    ts = req.headers["x-slack-request-timestamp"]
    assert req.headers["x-slack-signature"] == _sig(HOOK_TOKEN, ts, body)
    assert req.headers["content-type"] == "application/json"
    assert "authorization" not in {k.lower() for k in req.headers}
    assert "x-forwarded-for" not in {k.lower() for k in req.headers}


def test_form_encoded_slash_payload_routes_by_team_id_field(client):
    body = b"token=x&team_id=" + TEAM.encode() + b"&command=%2Flightbulb&text=hello"
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post(
            "/api/v1/slack/events", content=body, headers=_headers(body, ctype="application/x-www-form-urlencoded")
        )
    assert r.status_code == 200 and t.seen[0].content == body


def test_form_encoded_interactive_payload_routes_by_team_id_in_payload(client):
    payload = json.dumps({"type": "block_actions", "team": {"id": TEAM}, "actions": []})
    body = b"payload=" + httpx.QueryParams({"p": payload})["p"].encode()
    from urllib.parse import quote

    body = b"payload=" + quote(payload).encode()
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post(
            "/api/v1/slack/events", content=body, headers=_headers(body, ctype="application/x-www-form-urlencoded")
        )
    assert r.status_code == 200 and t.seen[0].content == body


def test_upstream_timeout_is_200_with_metric(client):
    body = _event()
    t = _Upstream(raise_exc=httpx.ReadTimeout("slow"))
    with (
        _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t),
        patch("routers.slack_app.put_metric") as metric,
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    assert any(c.args and c.args[0] == "slack.forward_failed" for c in metric.call_args_list)


def test_upstream_5xx_is_200(client):
    body = _event()
    t = _Upstream(status=500)
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200


def test_app_uninstalled_revokes_and_is_200(client):
    body = json.dumps({"type": "event_callback", "team_id": TEAM, "event_id": "Ev9", "event": {"type": "app_uninstalled"}}).encode()
    with _app_store(), _dedup(), _install(), patch("routers.slack_app.slack_install_service.revoke_install", AsyncMock(return_value=True)) as rev:
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    rev.assert_awaited_once_with(TEAM)
```

(Delete the stray `httpx.QueryParams` line in the interactive test — keep only the `quote` version. It's there to show what NOT to do: never build the body with a helper that might re-encode differently from the assertion.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_slack_app_events.py -v`
Expected: FAIL — 404s (router not mounted) / import errors.

- [ ] **Step 3: Implement the router (events only)**

```python
"""The platform Slack app: ONE Request URL for every workspace, plus the OAuth
install rail (Task 9). Spec: docs/superpowers/specs/2026-09-04-slack-marketplace-app-design.md §3.1.

Ingress contract (Slack disables an app only at >95 % failed deliveries over
60 min, but one tenant's wedged box must never count against everyone):

| condition                              | response                         |
|----------------------------------------|----------------------------------|
| url_verification (signed)              | 200 {"challenge"}                |
| bad/missing/stale signature            | 401                              |
| platform app unseeded                  | 503 (config error, ours)         |
| duplicate event_id                     | 200                              |
| unknown team_id                        | 200 (log)                        |
| app_uninstalled / tokens_revoked       | revoke install, 200              |
| own-bot echo                           | 200 (never enqueue our own posts)|
| owner not always-on entitled           | 200 + x-slack-no-retry: 1        |
| no container IP                        | 503 + Retry-After (Slack retries)|
| forward ok (2xx)                       | upstream status                  |
| forward timeout / non-2xx              | 200 + metric slack.forward_failed|

Forward = raw body byte-identical, Content-Type preserved, X-Slack-Request-
Timestamp preserved, X-Slack-Signature RE-SIGNED with the owner's hook_token
(the container's Bolt receiver verifies against OPENCLAW_HOOK_TOKEN — the
app-wide signing secret never leaves this process). No Authorization header:
the plugin route is auth:"plugin". Never resolve_running_container here — its
5 s TCP probe eats Slack's whole 3 s budget.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Request, Response

from core.containers import get_ecs_manager
from core.containers.ecs_manager import GATEWAY_PORT
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, slack_installs_repo
from core.services import slack_install_service, slack_signing, webhook_dedup
from core.services.credentials import platform_oauth_app_store
from core.services.provision_gate import has_always_on_entitlement

logger = logging.getLogger(__name__)
router = APIRouter()

PLATFORM_APP = "slack"
FORWARD_TIMEOUT_S = 2.0
DEDUP_TTL_S = 3600
_REVOKE_EVENTS = {"app_uninstalled", "tokens_revoked"}

# Module-level so tests can patch (same idiom as hooks_proxy).
ecs_manager = get_ecs_manager()


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(FORWARD_TIMEOUT_S))


def _json_200(payload: dict, **headers: str) -> Response:
    return Response(status_code=200, content=json.dumps(payload).encode(), media_type="application/json", headers=headers)


def _parse(content_type: str, body: bytes) -> dict:
    """Slack sends events as JSON and slash/interactive payloads as form data
    (interactive carries a JSON string under ``payload``). Returns the dict we
    route on; ``{}`` when unparseable (the signature already proved it's Slack's)."""
    try:
        if content_type.startswith("application/x-www-form-urlencoded"):
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            if "payload" in form:
                inner = json.loads(form["payload"])
                return inner if isinstance(inner, dict) else {}
            return form
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _team_id(data: dict) -> str | None:
    if isinstance(data.get("team_id"), str):
        return data["team_id"]
    team = data.get("team")
    if isinstance(team, dict) and isinstance(team.get("id"), str):
        return team["id"]
    return None


@router.post("/events")
async def receive_slack_event(request: Request) -> Response:
    body = await request.body()
    app = await platform_oauth_app_store.get(PLATFORM_APP)
    if not app or not app.get("signing_secret"):
        return Response(status_code=503, content=b'{"detail":"slack_app_not_configured"}', media_type="application/json")
    if not slack_signing.verify(
        app["signing_secret"],
        request.headers.get("X-Slack-Request-Timestamp"),
        body,
        request.headers.get("X-Slack-Signature"),
    ):
        logger.warning("slack events: rejected unsigned delivery")
        return Response(status_code=401, content=b'{"detail":"invalid_signature"}', media_type="application/json")

    content_type = request.headers.get("Content-Type", "")
    data = _parse(content_type, body)
    if data.get("type") == "url_verification":
        return _json_200({"challenge": data.get("challenge", "")})

    team_id = _team_id(data)
    if not team_id:
        logger.warning("slack events: signed payload without a team id type=%s", data.get("type"))
        return Response(status_code=200)

    # Only event_callbacks carry event_id and get retried; interactive/slash
    # payloads are one-shot, so they skip the dedup table.
    event_id = data.get("event_id") if data.get("type") == "event_callback" else None
    if event_id:
        seen = await webhook_dedup.record_event_or_skip(f"slack:{team_id}:{event_id}", source="slack", ttl_seconds=DEDUP_TTL_S)
        if seen == webhook_dedup.WebhookDedupResult.ALREADY_SEEN:
            return Response(status_code=200)

    install = await slack_installs_repo.get(team_id)
    if not install or install.get("status") != slack_installs_repo.STATUS_ACTIVE:
        logger.info("slack events: no active install for team=%s", team_id)
        return Response(status_code=200)

    event = data.get("event") if isinstance(data.get("event"), dict) else {}
    if event.get("type") in _REVOKE_EVENTS:
        await slack_install_service.revoke_install(team_id)
        return Response(status_code=200)
    if event.get("bot_id") or (event.get("user") and event.get("user") == install.get("bot_user_id")):
        return Response(status_code=200)  # our own post echoing back

    owner_id = install["owner_id"]
    container = await container_repo.get_by_owner_id(owner_id)
    if not container or not container.get("hook_token"):
        logger.warning("slack events: owner=%s has no container/hook_token", owner_id)
        return Response(status_code=200)
    billing = await billing_repo.get_by_owner_id(owner_id)
    if not has_always_on_entitlement(billing):
        # Phase 1 is paid-only. Phase 2 replaces this with wake + templated reply.
        return Response(status_code=200, headers={"x-slack-no-retry": "1"})

    ip = ecs_manager.discover_ip(container["service_name"])
    if not ip:
        return Response(status_code=503, headers={"Retry-After": "30"}, content=b'{"detail":"container_not_running"}', media_type="application/json")

    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    headers = {
        "Content-Type": content_type or "application/json",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": slack_signing.sign(container["hook_token"], ts, body),
    }
    target = f"http://{ip}:{GATEWAY_PORT}/slack/events/{team_id}"
    try:
        async with _client_factory() as client:
            upstream = await client.post(target, headers=headers, content=body)
    except httpx.RequestError as exc:
        logger.warning("slack events: forward failed owner=%s err=%s", owner_id, exc)
        put_metric("slack.forward_failed", 1, {"reason": "request_error"})
        return Response(status_code=200)
    if upstream.status_code >= 300:
        logger.warning("slack events: container answered %s owner=%s", upstream.status_code, owner_id)
        put_metric("slack.forward_failed", 1, {"reason": f"upstream_{upstream.status_code // 100}xx"})
        return Response(status_code=200)
    return Response(status_code=upstream.status_code, content=upstream.content, media_type=upstream.headers.get("content-type", "application/json"))
```

Check `put_metric`'s signature with `grep -n "^def put_metric" core/observability/metrics.py` and match it (it may take `dimensions=` as a keyword). Mount in `main.py` after the `meta_webhook` include:

```python
# The platform Slack app: one Request URL for every workspace (events,
# interactivity, slash) + the OAuth install rail. See routers/slack_app.py.
app.include_router(slack_app.router, prefix="/api/v1/slack", tags=["slack"])
```

and add `slack_app,` to the `from routers import (...)` block.

- [ ] **Step 4: Run tests + ruff**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_slack_app_events.py -v && uv run ruff check routers/slack_app.py main.py`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/slack_app.py apps/backend/main.py apps/backend/tests/unit/routers/test_slack_app_events.py
git commit -m "feat(backend): Slack events ingress — verify, dedup, route by team_id, re-sign, forward to the owner's container"
```

---

### Task 9: OAuth start / callback / install status routes

**Files:**
- Modify: `apps/backend/routers/slack_app.py` (append)
- Test: `apps/backend/tests/unit/routers/test_slack_app_oauth.py`

**Interfaces:**
- Consumes: `oauth_state_repo.put(extra=)/consume` (Task 5), `platform_oauth_app_store.get("slack")`, `slack_install_service.apply_install/revoke_install/BOT_SCOPES` (Task 7), `slack_installs_repo.list_for_owner/get`, `get_current_user`, `require_org_admin`, `resolve_owner_id`, `settings.PUBLIC_API_BASE`, `settings.FRONTEND_URL`.
- Produces:
  - `GET /api/v1/slack/oauth/start?agent_id=` (auth, org admin) → `{"url": "<slack authorize url>"}`
  - `GET /api/v1/slack/oauth/callback?code=&state=` (no auth) → 302 to `{FRONTEND_URL}/chat?panel=agent-inspector&agent={agent_id}&inspectorTab=channels&slack=connected` (or `&slack=error:<code>`)
  - `GET /api/v1/slack/install` (auth) → `{"installs": [{"team_id","team_name","agent_id","status","installed_at"}]}`
  - `DELETE /api/v1/slack/install/{team_id}` (auth, org admin) → `{"status": "revoked"}`; 404 if not this owner's.
- Module constants: `SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"`, `SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"`, `_redirect_uri()` → `f"{settings.PUBLIC_API_BASE}/api/v1/slack/oauth/callback"`.

- [ ] **Step 1: Write the failing tests**

```python
"""The Slack OAuth install rail: start (signed-in admin) → Slack → callback."""

import os
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402

ADMIN = AuthContext(user_id="user_1", org_id="org_1", org_role="org:admin")


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def _app_store():
    return patch(
        "core.services.credentials.platform_oauth_app_store.get",
        AsyncMock(return_value={"client_id": "cid", "client_secret": "cs", "signing_secret": "ss"}),
    )


def test_start_returns_authorize_url_and_stores_state(client):
    with _app_store(), patch("routers.slack_app.oauth_state_repo.put", AsyncMock()) as put:
        r = client.get("/api/v1/slack/oauth/start?agent_id=agent_f")
    assert r.status_code == 200
    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert url.netloc == "slack.com" and url.path == "/oauth/v2/authorize"
    assert q["client_id"] == ["cid"]
    assert "chat:write" in q["scope"][0].split(",")
    assert q["redirect_uri"][0].endswith("/api/v1/slack/oauth/callback")
    state = q["state"][0]
    put.assert_awaited_once()
    assert put.call_args.args[0] == state
    assert put.call_args.kwargs["owner_id"] == "org_1"
    assert put.call_args.kwargs["extra"] == {"agent_id": "agent_f", "member_id": "user_1"}


def test_start_requires_org_admin(client):
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_2", org_id="org_1", org_role="org:member")
    with _app_store():
        r = client.get("/api/v1/slack/oauth/start?agent_id=agent_f")
    assert r.status_code == 403


class _SlackToken(httpx.AsyncBaseTransport):
    def __init__(self, ok=True):
        self.ok, self.seen = ok, []

    async def handle_async_request(self, request):
        self.seen.append(request)
        if not self.ok:
            return httpx.Response(200, json={"ok": False, "error": "invalid_code"})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-token",
                "scope": "chat:write,app_mentions:read",
                "bot_user_id": "U_BOT",
                "app_id": "A1",
                "team": {"id": "T1", "name": "Acme"},
                "authed_user": {"id": "U_ADMIN"},
            },
        )


def test_callback_exchanges_code_applies_install_and_redirects(client):
    t = _SlackToken()
    state_row = {"state": "st", "owner_id": "org_1", "provider": "slack", "agent_id": "agent_f", "member_id": "user_1"}
    with (
        _app_store(),
        patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=state_row)),
        patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=t)),
        patch("routers.slack_app.slack_install_service.apply_install", AsyncMock(return_value={"team_id": "T1"})) as apply,
    ):
        r = client.get("/api/v1/slack/oauth/callback?code=c0de&state=st", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].endswith("/chat?panel=agent-inspector&agent=agent_f&inspectorTab=channels&slack=connected")
    form = parse_qs(t.seen[0].content.decode())
    assert form["code"] == ["c0de"] and form["client_id"] == ["cid"] and form["client_secret"] == ["cs"]
    kw = apply.call_args.kwargs
    assert kw["owner_id"] == "org_1" and kw["agent_id"] == "agent_f" and kw["team_id"] == "T1"
    assert kw["bot_token"] == "xoxb-token" and kw["scopes"] == ["chat:write", "app_mentions:read"]
    assert kw["installer_slack_user_id"] == "U_ADMIN" and kw["installer_member_id"] == "user_1"


def test_callback_with_bad_state_redirects_with_error(client):
    with _app_store(), patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=None)):
        r = client.get("/api/v1/slack/oauth/callback?code=c&state=bad", follow_redirects=False)
    assert r.status_code == 302 and "slack=error:invalid_state" in r.headers["location"]


def test_callback_with_slack_error_redirects_with_error(client):
    t = _SlackToken(ok=False)
    state_row = {"state": "st", "owner_id": "org_1", "provider": "slack", "agent_id": "agent_f", "member_id": "user_1"}
    with (
        _app_store(),
        patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=state_row)),
        patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=t)),
    ):
        r = client.get("/api/v1/slack/oauth/callback?code=c&state=st", follow_redirects=False)
    assert r.status_code == 302 and "slack=error:invalid_code" in r.headers["location"]


def test_callback_conflict_redirects_with_error(client):
    from core.services.slack_install_service import SlackInstallConflict

    t = _SlackToken()
    state_row = {"state": "st", "owner_id": "org_1", "provider": "slack", "agent_id": "agent_f", "member_id": "user_1"}
    with (
        _app_store(),
        patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=state_row)),
        patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=t)),
        patch("routers.slack_app.slack_install_service.apply_install", AsyncMock(side_effect=SlackInstallConflict("T1"))),
    ):
        r = client.get("/api/v1/slack/oauth/callback?code=c&state=st", follow_redirects=False)
    assert r.status_code == 302 and "slack=error:workspace_already_connected" in r.headers["location"]


def test_list_installs_for_owner(client):
    rows = [{"team_id": "T1", "team_name": "Acme", "agent_id": "agent_f", "status": "active", "installed_at": "2026-09-04T00:00:00+00:00", "bot_user_id": "U"}]
    with patch("routers.slack_app.slack_installs_repo.list_for_owner", AsyncMock(return_value=rows)):
        r = client.get("/api/v1/slack/install")
    assert r.status_code == 200
    assert r.json() == {"installs": [{"team_id": "T1", "team_name": "Acme", "agent_id": "agent_f", "status": "active", "installed_at": "2026-09-04T00:00:00+00:00"}]}


def test_delete_install_revokes_only_own_team(client):
    with (
        patch("routers.slack_app.slack_installs_repo.get", AsyncMock(return_value={"team_id": "T1", "owner_id": "org_1"})),
        patch("routers.slack_app.slack_install_service.revoke_install", AsyncMock(return_value=True)) as rev,
    ):
        r = client.delete("/api/v1/slack/install/T1")
    assert r.status_code == 200 and r.json() == {"status": "revoked"}
    rev.assert_awaited_once_with("T1")
    with patch("routers.slack_app.slack_installs_repo.get", AsyncMock(return_value={"team_id": "T1", "owner_id": "org_other"})):
        r = client.delete("/api/v1/slack/install/T1")
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_slack_app_oauth.py -v`
Expected: FAIL — 404s.

- [ ] **Step 3: Append the OAuth + install routes to `routers/slack_app.py`**

Add imports at the top: `import secrets`, `from urllib.parse import urlencode`, `from fastapi import Depends, HTTPException` (extend the existing `fastapi` import), `from fastapi.responses import RedirectResponse`, `from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id`, `from core.config import settings`, `from core.repositories import oauth_state_repo`. Then:

```python
SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"


def _redirect_uri() -> str:
    return f"{settings.PUBLIC_API_BASE}/api/v1/slack/oauth/callback"


def _frontend_return(agent_id: str, outcome: str) -> str:
    """Back to the agent's Channels tab (the same deep link ChatLayout uses)."""
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/chat?panel=agent-inspector&agent={agent_id}&inspectorTab=channels&slack={outcome}"


@router.get("/oauth/start")
async def slack_oauth_start(agent_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Mint the Slack authorize URL for the signed-in org admin. The browser
    navigates to it (a bearer can't ride a navigation, so this is JSON)."""
    require_org_admin(auth)
    app = await platform_oauth_app_store.get(PLATFORM_APP)
    if not app:
        raise HTTPException(status_code=503, detail="slack_app_not_configured")
    owner_id = resolve_owner_id(auth)
    state = secrets.token_urlsafe(32)
    await oauth_state_repo.put(
        state,
        owner_id=owner_id,
        provider=PLATFORM_APP,
        verifier="",
        extra={"agent_id": agent_id, "member_id": auth.user_id},
    )
    query = urlencode(
        {
            "client_id": app["client_id"],
            "scope": ",".join(slack_install_service.BOT_SCOPES),
            "redirect_uri": _redirect_uri(),
            "state": state,
        }
    )
    return {"url": f"{SLACK_AUTHORIZE_URL}?{query}"}


@router.get("/oauth/callback")
async def slack_oauth_callback(code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    """Slack redirects here after the admin authorizes. Unauthenticated by
    nature — the single-use ``state`` row is the identity."""
    rec = await oauth_state_repo.consume(state) if state else None
    if not rec or rec.get("provider") != PLATFORM_APP:
        return RedirectResponse(_frontend_return("", "error:invalid_state"), status_code=302)
    agent_id = rec.get("agent_id") or ""
    if error or not code:
        return RedirectResponse(_frontend_return(agent_id, f"error:{error or 'missing_code'}"), status_code=302)

    app = await platform_oauth_app_store.get(PLATFORM_APP)
    if not app:
        return RedirectResponse(_frontend_return(agent_id, "error:slack_app_not_configured"), status_code=302)
    try:
        async with _client_factory() as client:
            resp = await client.post(
                SLACK_TOKEN_URL,
                data={
                    "client_id": app["client_id"],
                    "client_secret": app["client_secret"],
                    "code": code,
                    "redirect_uri": _redirect_uri(),
                },
            )
        token = resp.json()
    except (httpx.RequestError, ValueError) as exc:
        logger.warning("slack oauth: token exchange failed: %s", exc)
        return RedirectResponse(_frontend_return(agent_id, "error:exchange_failed"), status_code=302)
    if not token.get("ok"):
        return RedirectResponse(_frontend_return(agent_id, f"error:{token.get('error', 'exchange_failed')}"), status_code=302)

    try:
        await slack_install_service.apply_install(
            owner_id=rec["owner_id"],
            agent_id=agent_id,
            team_id=token["team"]["id"],
            team_name=token["team"].get("name") or "",
            bot_token=token["access_token"],
            bot_user_id=token.get("bot_user_id") or "",
            app_id=token.get("app_id") or "",
            scopes=[s for s in (token.get("scope") or "").split(",") if s],
            installer_slack_user_id=(token.get("authed_user") or {}).get("id") or "",
            installer_member_id=rec.get("member_id") or "",
        )
    except slack_install_service.SlackInstallConflict:
        return RedirectResponse(_frontend_return(agent_id, "error:workspace_already_connected"), status_code=302)
    return RedirectResponse(_frontend_return(agent_id, "connected"), status_code=302)


@router.get("/install")
async def list_slack_installs(auth: AuthContext = Depends(get_current_user)) -> dict:
    rows = await slack_installs_repo.list_for_owner(resolve_owner_id(auth))
    keys = ("team_id", "team_name", "agent_id", "status", "installed_at")
    return {"installs": [{k: r.get(k) for k in keys} for r in rows]}


@router.delete("/install/{team_id}")
async def delete_slack_install(team_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    require_org_admin(auth)
    row = await slack_installs_repo.get(team_id)
    if not row or row.get("owner_id") != resolve_owner_id(auth):
        raise HTTPException(status_code=404, detail="install_not_found")
    await slack_install_service.revoke_install(team_id)
    return {"status": "revoked"}
```

Note the `_client_factory` from Task 8 now serves both the container forward (2 s timeout) and the Slack token exchange; Slack's token endpoint answers well under that, so keep one factory.

- [ ] **Step 4: Run both router suites + ruff**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_slack_app_events.py tests/unit/routers/test_slack_app_oauth.py -v && uv run ruff check routers/slack_app.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/slack_app.py apps/backend/tests/unit/routers/test_slack_app_oauth.py
git commit -m "feat(backend): Slack OAuth install rail — start, callback, list, revoke"
```

---

### Task 10: Reprovision durability — carry bindings verbatim

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py:152-181` (`_provision_channel_accounts`) and the call at `~4122` / pass-through at `~4149`
- Modify: `apps/backend/core/containers/config.py:1481-1493` (`build_openclaw_config_dict` signature), `~2622-2650` (re-injection), `~2684/2737` (`write_openclaw_config` pass-through)
- Test: `apps/backend/tests/unit/containers/test_channel_accounts_carry.py` (new)

Why: the existing re-injection recreates bindings as `{agentId: account_id}`. For the platform app the account id is the TEAM id and the agent is Friday, so a reprovision would rewire the workspace to a non-existent agent. Carry the file's actual channel bindings instead and only synthesize when none was carried (legacy socket accounts).

**Interfaces:**
- `_provision_channel_accounts(owner_id) -> tuple[dict[str, dict[str, dict]], list[dict]]` — `(accounts_by_provider, channel_bindings)`; bindings = every top-level `bindings[]` entry whose `match.channel in CHANNEL_PROVIDER_IDS`.
- `build_openclaw_config_dict(..., channel_accounts=None, channel_bindings: list[dict] | None = None, ...)` and the same kwarg on `write_openclaw_config`.

- [ ] **Step 1: Write the failing tests**

```python
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers import config as cfg  # noqa: E402
from core.containers import ecs_manager as em  # noqa: E402


@pytest.mark.asyncio
async def test_provision_channel_accounts_returns_accounts_and_channel_bindings():
    on_disk = {
        "channels": {"slack": {"accounts": {"T1": {"mode": "http"}}}, "telegram": {"accounts": {"agent_a": {"botToken": "x"}}}},
        "bindings": [
            {"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}},
            {"type": "route", "agentId": "agent_a", "match": {"channel": "telegram", "accountId": "agent_a"}},
            {"type": "route", "agentId": "agent_x", "match": {"channel": "email", "accountId": "e"}},
        ],
    }
    with patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(return_value=on_disk)):
        accounts, bindings = await em._provision_channel_accounts("org_1")
    assert accounts == {"slack": {"T1": {"mode": "http"}}, "telegram": {"agent_a": {"botToken": "x"}}}
    assert bindings == on_disk["bindings"][:2]


@pytest.mark.asyncio
async def test_provision_channel_accounts_is_fail_safe():
    with patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(side_effect=RuntimeError("efs"))):
        assert await em._provision_channel_accounts("org_1") == ({}, [])


def _build(**kw):
    """Minimal build_openclaw_config_dict call — mirror an existing test in
    tests/unit/containers/ for the required positional args (gateway token,
    provider choice, user id) and pass the rest through kw."""
    from tests.unit.containers.helpers import minimal_build_kwargs  # see step 3 note

    return cfg.build_openclaw_config_dict(**{**minimal_build_kwargs(), **kw})


def test_carried_binding_wins_over_synthesized_one():
    out = _build(
        channel_accounts={"slack": {"T1": {"mode": "http"}}},
        channel_bindings=[{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}],
    )
    slack_bindings = [b for b in out["bindings"] if b["match"]["channel"] == "slack"]
    assert slack_bindings == [{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}]
    assert out["channels"]["slack"]["accounts"]["T1"] == {"mode": "http"}


def test_legacy_account_without_carried_binding_still_synthesizes():
    out = _build(channel_accounts={"telegram": {"agent_a": {"botToken": "x"}}}, channel_bindings=[])
    assert {"type": "route", "agentId": "agent_a", "match": {"channel": "telegram", "accountId": "agent_a"}} in out["bindings"]
```

For `_build`: look at how an existing test calls `build_openclaw_config_dict` (`grep -ln "build_openclaw_config_dict(" tests/unit/containers/*.py | head -1`), copy its minimal kwargs into a tiny helper `tests/unit/containers/helpers.py::minimal_build_kwargs()` (paid `bedrock_claude` provider so the trial skip does not fire), and import it as shown. If a helper with that role already exists, use it and delete the import line's comment.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_channel_accounts_carry.py -v`
Expected: FAIL — tuple unpack / unexpected kwarg.

- [ ] **Step 3: Implement**

(a) `ecs_manager._provision_channel_accounts` — return a tuple; after the accounts loop:

```python
        bindings = [
            b
            for b in (existing_cfg.get("bindings") or [])
            if isinstance(b, dict) and (b.get("match") or {}).get("channel") in CHANNEL_PROVIDER_IDS
        ]
        return out, bindings
    except Exception:  # noqa: BLE001 — best-effort; never fail provision over this.
        logger.exception("preserving channel accounts failed for %s; continuing", owner_id)
        return {}, []
```

Update its docstring: *"Returns ``(accounts_by_provider, channel_bindings)``; the bindings are carried VERBATIM because the platform Slack app keys accounts by team id and routes them to an agent — re-synthesizing ``agentId == accountId`` would rewire the workspace to a non-existent agent."* At the call site (~4122): `channel_accounts, channel_bindings = await _provision_channel_accounts(owner_id)` and pass `channel_bindings=channel_bindings,` into `write_openclaw_config`.

(b) `config.py` — add `channel_bindings: list[dict] | None = None,` after `channel_accounts` in BOTH `build_openclaw_config_dict` and `write_openclaw_config`, pass it through, and change the re-injection loop's binding step:

```python
            carried = [
                b for b in (channel_bindings or []) if (b.get("match") or {}).get("channel") == provider
            ]
            for b in carried:
                if b not in bindings:
                    bindings.append(b)
            carried_accounts = {(b.get("match") or {}).get("accountId") for b in carried}
            for account_id in accounts:
                if account_id in carried_accounts:
                    continue  # the carried binding already routes this account
                binding = {
                    "type": "route",
                    "agentId": account_id,
                    "match": {"channel": provider, "accountId": account_id},
                }
                if binding not in bindings:
                    bindings.append(binding)
```

Extend the comment above it: *"Bindings are carried verbatim (platform Slack app: account = team id → agent); the ``agentId == accountId`` synthesis is the legacy fallback for accounts whose binding was not carried."*

- [ ] **Step 4: Run the new tests + every existing test that touches channel_accounts**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_channel_accounts_carry.py -v && uv run pytest tests/unit -q -k "channel_accounts or channel_bindings or provision_channel" && uv run ruff check core/containers/`
Expected: new tests pass; fix any existing test that unpacked the old single return value (`grep -rn "_provision_channel_accounts" tests/`).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/core/containers/config.py apps/backend/tests/unit/containers/test_channel_accounts_carry.py apps/backend/tests/unit/containers/helpers.py
git commit -m "fix(backend): reprovision carries channel route bindings verbatim (team-keyed Slack accounts)"
```

---

### Task 11: Account wipe deletes install rows

**Files:**
- Modify: `apps/backend/core/services/account_wipe.py` — import `slack_installs_repo`; add a step right after `_s9i` (agent email, line ~1001); add the dry-run "would_delete" line near line ~631.
- Test: `apps/backend/tests/unit/services/test_account_wipe.py` (append; find the existing agent-email step assertion with `grep -n "agent_email_addresses" tests/unit/services/test_account_wipe.py` and mirror it)

- [ ] **Step 1: Write the failing test** — mirror the agent-email assertion: patch `core.services.account_wipe.slack_installs_repo.delete_all_for_owner` to return `["T1"]` and assert the report contains a step `system == "slack_installs"`, `action == "delete_all_for_owner"`, `status == "ok"`, `detail` containing `T1`. Also assert the dry-run report has a `would_delete` step for `slack_installs`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_account_wipe.py -v -k slack`
Expected: FAIL — no such step.

- [ ] **Step 3: Implement** — after `_s9i`'s `await _step(...)`:

```python
    async def _s9i2() -> str:
        teams = await slack_installs_repo.delete_all_for_owner(owner_id)
        return f"deleted {len(teams)} slack install(s): {', '.join(teams) or 'none'}"

    await _step(steps, system="slack_installs", action="delete_all_for_owner", fn=_s9i2)
```

The Secrets Manager entries (`integrations/{owner}/slack/*`) and the `integrations` status row are already swept by the owner-prefix steps that exist. Dry-run: next to the agent-email `would_delete` line add

```python
    install_rows = await slack_installs_repo.list_for_owner(owner_id)
    steps.append(WipeStep(system="slack_installs", action="delete_all_for_owner", status="would_delete", detail=", ".join(r["team_id"] for r in install_rows) or "none"))
```

- [ ] **Step 4: Run + ruff**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_account_wipe.py -v && uv run ruff check core/services/account_wipe.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/account_wipe.py apps/backend/tests/unit/services/test_account_wipe.py
git commit -m "feat(backend): account wipe deletes the owner's Slack install rows"
```

---

### Task 12: API Gateway route + own throttle bucket

**Files:**
- Modify: `apps/infra/lib/stacks/api-stack.ts` — after `HttpDefaultRoute` (line ~204) and in `HttpStage` (line ~222)
- Test: `apps/infra/test/api-stack.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
describe("ApiStack — Slack events route has its own throttle bucket", () => {
  const template: Template = API_TEMPLATE;

  test("explicit POST /api/v1/slack/events route exists", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
      RouteKey: "POST /api/v1/slack/events",
    });
  });

  test("stage carries route-level throttling for the Slack route", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      RouteSettings: Match.objectLike({
        "POST /api/v1/slack/events": Match.objectLike({
          ThrottlingBurstLimit: Match.anyValue(),
          ThrottlingRateLimit: Match.anyValue(),
        }),
      }),
    });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/infra && npx vitest run test/api-stack.test.ts -t "Slack events route"`
Expected: FAIL.

- [ ] **Step 3: Implement** — next to `THROTTLE_CONFIG` add

```ts
// Slack posts every workspace's events to ONE URL. Give that route its own
// bucket so a chatty tenant (or Slack's 3× retries) never eats the stage-wide
// budget every browser/desktop/mobile REST call shares — and vice versa.
const SLACK_ROUTE_KEY = "POST /api/v1/slack/events";
const SLACK_THROTTLE: Record<string, { burstLimit: number; rateLimit: number }> = {
  dev: { burstLimit: 50, rateLimit: 25 },
  prod: { burstLimit: 200, rateLimit: 100 },
};
```

after `HttpDefaultRoute`:

```ts
    // Explicit route so the stage can attach route-level throttling to it;
    // it forwards to the same ALB integration as $default.
    new apigatewayv2.CfnRoute(this, "HttpSlackEventsRoute", {
      apiId: httpApi.ref,
      routeKey: SLACK_ROUTE_KEY,
      target: `integrations/${httpIntegration.ref}`,
    });
```

and in `HttpStage` props, after `defaultRouteSettings`:

```ts
      routeSettings: {
        [SLACK_ROUTE_KEY]: {
          throttlingBurstLimit: (SLACK_THROTTLE[env] ?? SLACK_THROTTLE.dev).burstLimit,
          throttlingRateLimit: (SLACK_THROTTLE[env] ?? SLACK_THROTTLE.dev).rateLimit,
        },
      },
```

(`env` is the same variable `THROTTLE_CONFIG` is indexed by — reuse the existing `throttle` lookup's key.)

- [ ] **Step 4: Test, synth, drift check**

Run: `cd apps/infra && npx vitest run test/api-stack.test.ts && npx cdk synth > /dev/null && git -C ../.. status --short .github/workflows/deploy.yml`
Expected: PASS; stage `deploy.yml` if it changed.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/api-stack.ts apps/infra/test/api-stack.test.ts .github/workflows/deploy.yml
git commit -m "feat(infra): explicit Slack events route with its own API GW throttle bucket"
```

---

### Task 13: Frontend — Add-to-Slack card

**Files:**
- Create: `apps/frontend/src/components/channels/SlackInstallCard.tsx`
- Modify: `apps/frontend/src/components/control/panels/AgentChannelsSection.tsx` — in the Slack provider slot (lines ~93-142), render `<SlackInstallCard agentId={agentId} />` above the existing bot list, and label the existing "Add Slack bot" button "Use your own Slack app (advanced)".
- Test: `apps/frontend/src/components/channels/__tests__/SlackInstallCard.test.tsx`

**Interfaces:**
- Consumes: `GET /slack/install` → `{installs:[{team_id, team_name, agent_id, status, installed_at}]}`; `GET /slack/oauth/start?agent_id=` → `{url}`; `DELETE /slack/install/{team_id}`. Via `useApi()` (`api.get`, `api.del`).
- Reads `?slack=connected|error:<code>` from the URL once to show a toast/inline note.

- [ ] **Step 1: Write the failing test** (vitest + RTL; mock `@/lib/api` the way sibling tests under `src/components/**/__tests__` do — `grep -rl "vi.mock(\"@/lib/api\"" apps/frontend/src | head -1` and copy that mock shape):

```tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const api = { get: vi.fn(), del: vi.fn() };
vi.mock("@/lib/api", () => ({ useApi: () => api }));

import { SlackInstallCard } from "../SlackInstallCard";

describe("SlackInstallCard", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.del.mockReset();
  });

  it("shows Add to Slack when this agent has no install and navigates to the authorize url", async () => {
    api.get.mockImplementation(async (path: string) =>
      path.startsWith("/slack/install") ? { installs: [] } : { url: "https://slack.com/oauth/v2/authorize?x=1" },
    );
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { assign, search: "" }, writable: true });
    render(<SlackInstallCard agentId="agent_f" />);
    const btn = await screen.findByRole("button", { name: /add to slack/i });
    fireEvent.click(btn);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://slack.com/oauth/v2/authorize?x=1"));
    expect(api.get).toHaveBeenCalledWith("/slack/oauth/start?agent_id=agent_f");
  });

  it("shows the connected workspace and can disconnect", async () => {
    api.get.mockResolvedValue({
      installs: [{ team_id: "T1", team_name: "Acme", agent_id: "agent_f", status: "active", installed_at: "" }],
    });
    api.del.mockResolvedValue({ status: "revoked" });
    render(<SlackInstallCard agentId="agent_f" />);
    expect(await screen.findByText(/connected to acme/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /disconnect/i }));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/slack/install/T1"));
  });

  it("ignores installs bound to other agents", async () => {
    api.get.mockResolvedValue({
      installs: [{ team_id: "T1", team_name: "Acme", agent_id: "agent_other", status: "active", installed_at: "" }],
    });
    render(<SlackInstallCard agentId="agent_f" />);
    expect(await screen.findByRole("button", { name: /add to slack/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/channels/__tests__/SlackInstallCard.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";

type Install = { team_id: string; team_name: string; agent_id: string; status: string; installed_at: string };

/** The platform Slack app's card: one click installs Lightbulb into a
 * workspace and binds it to this agent. The BYO "create your own app"
 * wizard stays available below it as the advanced path. */
export function SlackInstallCard({ agentId }: { agentId: string }) {
  const api = useApi();
  const [installs, setInstalls] = useState<Install[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const res = (await api.get("/slack/install")) as { installs: Install[] };
    setInstalls(res.installs.filter((i) => i.status === "active"));
  }, [api]);

  useEffect(() => {
    void load();
    const flag = new URLSearchParams(window.location.search).get("slack");
    if (flag === "connected") setNote("Slack connected.");
    else if (flag?.startsWith("error:")) setNote(`Slack connection failed (${flag.slice(6)}).`);
  }, [load]);

  const start = async () => {
    setBusy(true);
    try {
      const res = (await api.get(`/slack/oauth/start?agent_id=${encodeURIComponent(agentId)}`)) as { url: string };
      window.location.assign(res.url);
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async (teamId: string) => {
    setBusy(true);
    try {
      await api.del(`/slack/install/${encodeURIComponent(teamId)}`);
      await load();
    } finally {
      setBusy(false);
    }
  };

  if (installs === null) return null;
  const mine = installs.filter((i) => i.agent_id === agentId);

  return (
    <div className="space-y-2">
      {note && <p className="text-xs text-[var(--midnight-muted)]">{note}</p>}
      {mine.length === 0 ? (
        <Button variant="outline" size="sm" onClick={start} disabled={busy}>
          Add to Slack
        </Button>
      ) : (
        mine.map((i) => (
          <div key={i.team_id} className="flex items-center gap-2 text-sm">
            <CheckCircle2 size={16} className="text-ok" />
            <span>Connected to {i.team_name || i.team_id}</span>
            <div className="flex-1" />
            <Button variant="ghost" size="sm" onClick={() => disconnect(i.team_id)} disabled={busy}>
              Disconnect
            </Button>
          </div>
        ))
      )}
    </div>
  );
}
```

In `AgentChannelsSection.tsx`, inside the provider map, render before the `!isSubscribed ? … : bots.length === 0 ? …` chain:

```tsx
            {provider === "slack" && isSubscribed && data.can_create_bots && (
              <SlackInstallCard agentId={agentId} />
            )}
```

(import `SlackInstallCard` from `@/components/channels/SlackInstallCard`), and change the existing `Add {PROVIDER_LABELS[provider]} bot` button label to render `provider === "slack" ? "Use your own Slack app (advanced)" : \`Add ${PROVIDER_LABELS[provider]} bot\``.

- [ ] **Step 4: Run tests + lint + typecheck**

Run: `cd apps/frontend && pnpm vitest run src/components/channels/__tests__/SlackInstallCard.test.tsx src/components/control && pnpm lint && pnpm tsc --noEmit`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/channels/SlackInstallCard.tsx apps/frontend/src/components/channels/__tests__/SlackInstallCard.test.tsx apps/frontend/src/components/control/panels/AgentChannelsSection.tsx
git commit -m "feat(frontend): Add-to-Slack card on the agent Channels tab; BYO wizard demoted to advanced"
```

---

### Task 14: Manifest + doc corrections

**Files:**
- Create: `apps/backend/scripts/slack-app-manifest.yaml`
- Modify: `CLAUDE.md` — the "Config Patching Notes" `env.vars` bullet (channel accounts are a third carried-forward block) and the Secrets Manager prefix mentions (`isol8/{env}` → `lightbulb/{env}`); the Manual dev testing section's `isol8/dev/clerk_secret_key` too.
- Modify: `apps/frontend/src/components/control/panels/ChannelsStatusPanel.tsx:114` — empty-state copy.
- Modify: `apps/infra/openclaw/skills/lightbulb/SKILL.md` — the channels line (~134): "Slack: one click from the agent's Channels tab (Add to Slack); Telegram/Discord: the bot wizard."
- Test: `apps/backend/tests/unit/services/test_slack_install_service.py` — add one test that the manifest's bot scopes equal `BOT_SCOPES`.

- [ ] **Step 1: Write the failing test** (append to `test_slack_install_service.py`):

```python
def test_manifest_scopes_match_service_scopes():
    import pathlib

    import yaml

    manifest = yaml.safe_load(pathlib.Path("scripts/slack-app-manifest.yaml").read_text())
    assert sorted(manifest["oauth_config"]["scopes"]["bot"]) == sorted(svc.BOT_SCOPES)
    assert manifest["settings"]["socket_mode_enabled"] is False
    assert manifest["settings"]["interactivity"]["is_enabled"] is True
    assert manifest["features"]["shortcuts"]
```

(`pyyaml` — check `grep -n pyyaml apps/backend/pyproject.toml`; if absent, `cd apps/backend && uv add --dev pyyaml`.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_slack_install_service.py -v -k manifest`
Expected: FAIL — file missing.

- [ ] **Step 3: Write the manifest** (`apps/backend/scripts/slack-app-manifest.yaml`). Replace `api-dev.flickmeon.ai` with `api.flickmeon.ai` when creating the prod app.

```yaml
# The platform Lightbulb Slack app. Create it ONCE per environment at
# api.slack.com/apps → "From a manifest". Bot scopes MUST equal
# core/services/slack_install_service.BOT_SCOPES (test enforces it).
# Events API, not Socket Mode — Socket Mode apps cannot be listed in the
# Slack Marketplace, and an app-level token cannot be minted by OAuth.
display_information:
  name: Lightbulb
  description: Your AI company, in Slack. Ask Friday anything.
  background_color: "#0b0d17"
features:
  bot_user:
    display_name: Lightbulb
    always_online: true
  app_home:
    home_tab_enabled: true
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  agent_view:
    agent_description: Friday runs your company's agents. Ask for a status, hand off a task, or approve work right here.
    suggested_prompts:
      - title: What's in flight?
        message: What is the team working on right now?
      - title: Draft a brief
        message: Draft a brief for the next thing we should ship.
      - title: Approvals
        message: Anything waiting on my approval?
  slash_commands:
    - command: /lightbulb
      url: https://api-dev.flickmeon.ai/api/v1/slack/events
      description: Talk to Friday
      usage_hint: "[what you need]"
      should_escape: false
  shortcuts:
    - name: Ask Friday about this
      type: message
      callback_id: lightbulb_ask_message
      description: Send this message to Friday
    - name: Ask Friday
      type: global
      callback_id: lightbulb_ask_global
      description: Start a conversation with Friday
oauth_config:
  redirect_urls:
    - https://api-dev.flickmeon.ai/api/v1/slack/oauth/callback
  scopes:
    bot:
      - app_mentions:read
      - assistant:write
      - channels:history
      - channels:read
      - chat:write
      - commands
      - files:read
      - files:write
      - groups:history
      - groups:read
      - im:history
      - im:read
      - im:write
      - mpim:history
      - mpim:read
      - mpim:write
      - reactions:read
      - reactions:write
      - users:read
settings:
  event_subscriptions:
    request_url: https://api-dev.flickmeon.ai/api/v1/slack/events
    bot_events:
      - app_home_opened
      - app_mention
      - app_context_changed
      - app_uninstalled
      - channel_rename
      - member_joined_channel
      - member_left_channel
      - message.channels
      - message.groups
      - message.im
      - message.mpim
      - reaction_added
      - reaction_removed
      - tokens_revoked
  interactivity:
    is_enabled: true
    request_url: https://api-dev.flickmeon.ai/api/v1/slack/events
    message_menu_options_url: https://api-dev.flickmeon.ai/api/v1/slack/events
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

- [ ] **Step 4: Doc edits** — CLAUDE.md "Config Patching Notes", last bullet: append *"Channel bot accounts (`channels.*.accounts`) and their route bindings are the third carried-forward block (`ecs_manager._provision_channel_accounts`) — the platform Slack app relies on it."* Replace every `isol8/{env}/…` Secrets Manager path in CLAUDE.md with `lightbulb/{env}/…` (`grep -n "isol8/{env}\|isol8/dev/" CLAUDE.md`). `ChannelsStatusPanel.tsx:114` → `"No channels configured. Connect Slack from an agent's Channels tab, or add a Telegram/Discord bot there."`. SKILL.md line per the file list above.

- [ ] **Step 5: Run the manifest test + frontend typecheck**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_slack_install_service.py -v -k manifest && cd ../frontend && pnpm tsc --noEmit`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/scripts/slack-app-manifest.yaml apps/backend/tests/unit/services/test_slack_install_service.py apps/backend/pyproject.toml apps/backend/uv.lock CLAUDE.md apps/frontend/src/components/control/panels/ChannelsStatusPanel.tsx apps/infra/openclaw/skills/lightbulb/SKILL.md
git commit -m "docs(slack): platform app manifest (scopes pinned to BOT_SCOPES) + CLAUDE.md/skill/panel copy corrections"
```

---

### Task 15: Dev verification (Claude-in-Chrome, after the branch deploys to dev)

Not a code task; the gate before any prod promotion (memory: E2E = Claude-in-Chrome on DEV).

- [ ] Operator has completed "Manual setup" for dev (Slack app created, secret seeded with `--with-signing-secret`, Request URL verified — the `url_verification` POST must have returned the challenge).
- [ ] Sign in to dev as a PAID org admin test account; open Friday → Channels tab; click **Add to Slack**; authorize in the dev Slack workspace; land back on the tab with `slack=connected`; card shows "Connected to <workspace>".
- [ ] `aws ecs describe-task-definition` on the owner's family shows a `secrets` entry `SLACK_BOT_TOKEN_T…`; `openclaw.json` on EFS shows `channels.slack.accounts.T…` with SecretRefs and NO plaintext token; `bindings[]` has `{agentId: <friday>, match: {channel: slack, accountId: T…}}`.
- [ ] In Slack: DM the bot → streamed reply in thread; @mention in a channel it's in → reply; `/lightbulb hi` → ephemeral ack + reply; click an approval button on an exec approval → decision applied. `channels.status` in the Channels panel shows `connected`.
- [ ] Reprovision via admin UI (PATCH) → account block + binding survive byte-for-byte.
- [ ] Remove the app from the Slack workspace → `app_uninstalled` arrives → install row `revoked`, account block gone, card shows "Add to Slack" again.
- [ ] CloudWatch: `slack.forward_failed` = 0 during the walk; API GW route metrics show the Slack route separately.

---

## Self-review

**Spec coverage (Phase 1 = spec §6 step 1):** transport §3.1 → Tasks 4, 8, 12; credentials §3.2 → Tasks 3, 6, 7; routing table §3.3 → Tasks 1, 2; config shape §3.4 → Task 7 (`account_block`), Task 10 (durability); install/uninstall lifecycle §3.5 (paid-owner path) → Tasks 5, 7, 9, 11; migration §3.8 → Task 13 (BYO path demoted, not removed) + Task 10 (mixed accounts carried); manifest §4 → Task 14. Deliberately NOT here: cold install / Clerk minting / trial admission / wake (§3.6–3.7, Phase 2), Grid, modals, agent faces, Lambda+SQS.

**Placeholder scan:** none — every code step is complete. Three places say "check the neighbour and match its signature" (`integrations_repo` status writer, `append_to_openclaw_config_list` param order, `put_metric` kwargs); those are one-grep verifications of existing code, not deferred design.

**Type consistency:** `slack_env_var_name` (Task 6) is used by Task 7's `account_block` and Task 8's tests via the literal `SLACK_BOT_TOKEN_T0123ABC`; `secret_provider` → `f"slack/{team_id}"` matches the `integration_secret_store.delete("org_1", "slack/T1")` assertion; `slack_installs_repo.STATUS_ACTIVE/STATUS_REVOKED` used in Tasks 7–8; `_provision_channel_accounts` returns a tuple from Task 10 onward and its single call site is updated in the same task; `route_binding(team_id, agent_id)` shape equals what Task 10's carry test expects.
