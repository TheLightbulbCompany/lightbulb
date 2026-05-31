# Piece 1 — Owner-Keyed DynamoDB Fan-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make WebSocket event fan-out **owner-keyed via DynamoDB** so any backend instance can deliver to all of an owner's tabs/devices, with correct per-member demux for orgs — built clean, no migration scaffolding.

**Architecture:** Spec `docs/superpowers/specs/2026-05-30-multi-instance-backend-architecture-design.md` §3.0 + §3.1. `_forward_to_frontends` reads recipients from a new `by-owner-id` GSI on `ws-connections` (cached briefly), filters to `connectionType == "chat"`, demuxes per member, and pushes via the existing global Management API. DynamoDB is the **single source of truth** for fan-out recipients. N-agnostic: identical behavior at `desiredCount=1` (one task) and `>1`; **no flag, no guardrail, no mode switch.**

**Tech Stack:** FastAPI / Python 3.12 (`uv`), boto3 + `moto` for DDB tests, pytest; AWS CDK (TypeScript) + `aws-cdk-lib/assertions`.

---

## Scope decisions (read before implementing)

- **No migration, no fallback (decision 2026-05-31).** We run 1 prod + 2 dev containers, so we build the clean steady-state design directly. **This supersedes the spec's §3.0 migration-safety / backfill-sweep / dual-read / in-process-union text** — none of that is built. The single deploy that introduces `ownerId` + the GSI will drop whatever connections are open at that instant; those tabs reconnect. That's a refresh at our scale, not an incident.
- **GSI eventual consistency (~<1s) is accepted, not mitigated.** A brand-new tab's first sub-second of events could miss the GSI. At chat timescales (LLM round-trips are seconds) this is immaterial. Deliberate choice, consistent with "no fallbacks."
- **On a DDB query error, log + drop the event** (do not build an in-process-union fallback). The in-process `_frontend_connections` set still exists for connection-lifecycle/grace logic (untouched), so a fallback is a trivial future add if DDB ever proves flaky — but it is **not** built now.
- **Piece 1 only.** The single-owner **lease (Piece 2)** and the **relay-routing-at-N>1 (§6.1)** are separate plans. At N=1 the lease is a no-op, so Piece 1 is correct and complete on its own.

## Paperclip-safety checklist (verified; keep true)

- The **Teams broker is NOT changed** — it fans out via its own `_subscribers` set and only calls `delete_connection(connId)` (primary key). Don't touch it.
- **Operator + node connections must never enter the chat fan-out.** Today operator is excluded at `$connect` (`websocket_chat.py:179` `if not is_operator_relay`). The new `connectionType == "chat"` filter in fan-out enforces this for **both** operator and node, defensively, from the DDB side. Verify operator/node never receive `chunk`/`done`.
- Adding the GSI is **additive + online** (no table replacement); only the `$connect` writer touches the table for writes; no cross-stack export changes.

## File structure

- `apps/infra/lib/stacks/api-stack.ts` — add the `by-owner-id` GSI.
- `apps/infra/lambda/ws-connect/index.py` — write `ownerId` on the connect row.
- `apps/backend/core/services/connection_service.py` — write `ownerId` in `store_connection`; add `query_by_owner`.
- `apps/backend/routers/websocket_chat.py` — pass `owner_id` to `store_connection`.
- `apps/backend/core/gateway/connection_pool.py` — thread `owner_id` into `GatewayConnection`; migrate `_forward_to_frontends` to `query_by_owner`.
- Tests alongside each.

---

### Task 1: Add the `by-owner-id` GSI (CDK) + assertion test

**Files:**
- Modify: `apps/infra/lib/stacks/api-stack.ts` (the `ConnectionsTable` + GSI block, ~251-267)
- Create: `apps/infra/test/api-stack.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// apps/infra/test/api-stack.test.ts
import { Template, Match } from "aws-cdk-lib/assertions";
import { synthApiStack } from "./helpers"; // reuse the synth helper used by service-stack.test.ts (mirror its stage/stack wiring)

test("ws-connections has a by-owner-id GSI projecting userId + connectionType", () => {
  const template = synthApiStack();
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    GlobalSecondaryIndexes: Match.arrayWith([
      Match.objectLike({
        IndexName: "by-owner-id",
        KeySchema: [{ AttributeName: "ownerId", KeyType: "HASH" }],
        Projection: {
          ProjectionType: "INCLUDE",
          NonKeyAttributes: Match.arrayWith(["userId", "connectionType"]),
        },
      }),
    ]),
  });
});
```

> First read `apps/infra/test/service-stack.test.ts` (created in the Piece-3 plan) to copy its exact synth helper. If api-stack needs sibling stacks to synth, mirror that wiring in a small `synthApiStack` in `helpers.ts`.

- [ ] **Step 2: Run, expect FAIL**

Run: `cd apps/infra && pnpm test -- api-stack`
Expected: FAIL — no `by-owner-id` GSI.

- [ ] **Step 3: Implement**

In `api-stack.ts`, immediately after the existing `by-user-id` GSI block (~line 267), add a second GSI. The table already declares `connectionId` (PK) and `userId`; `ownerId` is a new attribute (CDK infers it from the GSI key). Match the file's `dynamodb` import + `cdk.Duration` style:

```typescript
    connectionsTable.addGlobalSecondaryIndex({
      indexName: "by-owner-id",
      partitionKey: { name: "ownerId", type: dynamodb.AttributeType.STRING },
      // INCLUDE (not KEYS_ONLY): fan-out needs the member userId (demux) + connectionType (chat filter).
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["userId", "connectionType"],
    });
```

> CFN allows one GSI add per stack update — this is the only GSI added here, so it's safe + online (`PAY_PER_REQUEST`, no throughput). The existing `by-user-id` GSI is unchanged.

- [ ] **Step 4: Run, expect PASS**

Run: `cd apps/infra && pnpm test -- api-stack` → PASS. Then `pnpm -C apps/infra run build` (tsc) → exit 0.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/api-stack.ts apps/infra/test/api-stack.test.ts apps/infra/test/helpers.ts
git commit -m "feat(infra): add by-owner-id GSI to ws-connections (owner-keyed fan-out)"
```

---

### Task 2: Write `ownerId` on connect (service + Lambda)

**Files:**
- Modify: `apps/backend/core/services/connection_service.py` (`store_connection` ~61-90)
- Modify: `apps/backend/routers/websocket_chat.py` (the `store_connection` call ~169-175)
- Modify: `apps/infra/lambda/ws-connect/index.py` (the `put_item`)
- Test: `apps/backend/tests/unit/services/test_connection_service_errors.py` (extend) or a new `test_connection_service_owner.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_connection_service_owner.py
import boto3
import pytest
from moto import mock_aws

from core.services.connection_service import ConnectionService


@mock_aws
def test_store_connection_writes_owner_id_org():
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName="ws-conn-test",
        KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    svc = ConnectionService(table_name="ws-conn-test", region_name="us-east-1")
    # org member: ownerId must be the org id
    svc.store_connection(connection_id="c1", user_id="user_a", org_id="org_x", connection_type="chat")
    item = client.get_item(TableName="ws-conn-test", Key={"connectionId": {"S": "c1"}})["Item"]
    assert item["ownerId"]["S"] == "org_x"
    # personal: ownerId == userId
    svc.store_connection(connection_id="c2", user_id="user_b", org_id=None, connection_type="chat")
    item2 = client.get_item(TableName="ws-conn-test", Key={"connectionId": {"S": "c2"}})["Item"]
    assert item2["ownerId"]["S"] == "user_b"
```

- [ ] **Step 2: Run, expect FAIL**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_connection_service_owner.py -v`
Expected: FAIL — `ownerId` not written (`KeyError`).

- [ ] **Step 3: Implement**

In `connection_service.py` `store_connection`, derive and write `ownerId` in the item dict (after the existing `orgId` line ~87):

```python
            "ownerId": {"S": (org_id or user_id)},  # owner = org for org members, else the user
```

(No signature change needed — `ownerId` is derived from the existing `org_id`/`user_id` params.)

The `$connect` router call already passes `org_id`/`user_id` (`websocket_chat.py:169-175`), so it needs **no change** for the service write — confirm it still passes `org_id=x_org_id`.

In `apps/infra/lambda/ws-connect/index.py`, the Lambda also writes the connect row; add `ownerId` to its `put_item` item so the row carries it even before the backend handler runs:

```python
    "ownerId": {"S": (org_id or user_id)},
```

(Match the Lambda's existing item-dict style and how it reads `user_id`/`org_id` from the authorizer context.)

- [ ] **Step 4: Run, expect PASS**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_connection_service_owner.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/connection_service.py apps/infra/lambda/ws-connect/index.py apps/backend/tests/unit/services/test_connection_service_owner.py
git commit -m "feat(backend): write ownerId (org_id or user_id) on ws-connection rows"
```

---

### Task 3: `query_by_owner` on `ConnectionService`

**Files:**
- Modify: `apps/backend/core/services/connection_service.py` (add method near `query_by_user_id` ~190)
- Test: `apps/backend/tests/unit/services/test_connection_service_owner.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to test_connection_service_owner.py
@mock_aws
def test_query_by_owner_returns_members_and_types():
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName="ws-conn-test",
        KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "connectionId", "AttributeType": "S"},
            {"AttributeName": "ownerId", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "by-owner-id",
            "KeySchema": [{"AttributeName": "ownerId", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["userId", "connectionType"]},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    svc = ConnectionService(table_name="ws-conn-test", region_name="us-east-1")
    svc.store_connection(connection_id="c1", user_id="m1", org_id="org_x", connection_type="chat")
    svc.store_connection(connection_id="c2", user_id="m2", org_id="org_x", connection_type="chat")
    svc.store_connection(connection_id="c3", user_id="m1", org_id="org_x", connection_type="node")
    rows = await_sync(svc.query_by_owner("org_x"))  # see helper note
    got = {(c, u, t) for (c, u, t) in rows}
    assert got == {("c1", "m1", "chat"), ("c2", "m2", "chat"), ("c3", "m1", "node")}
    assert await_sync(svc.query_by_owner("nobody")) == []
```

> `query_by_owner` is async (mirrors `query_by_user_id`). Use the repo's existing async-test convention (most service tests use `pytest.mark.asyncio` + `await`); write it that way rather than the `await_sync` placeholder — match `test_connection_service_query.py`.

- [ ] **Step 2: Run, expect FAIL** — `AttributeError: query_by_owner`.

- [ ] **Step 3: Implement**

Add `query_by_owner` mirroring `query_by_user_id` (paginated Query on the new GSI), returning `(connectionId, member_userId, connectionType)` tuples:

```python
    async def query_by_owner(self, owner_id: str) -> list[tuple[str, str, str]]:
        """Return (connectionId, member_userId, connectionType) for every active
        connection owned by *owner_id* (org_id for orgs, user_id for personal),
        via the by-owner-id GSI. Used by the gateway fan-out so any instance can
        reach all of an owner's connections and demux per member."""
        def _query() -> list[tuple[str, str, str]]:
            paginator = self._client.get_paginator("query")
            pages = paginator.paginate(
                TableName=self.table_name,
                IndexName="by-owner-id",
                KeyConditionExpression="ownerId = :o",
                ExpressionAttributeValues={":o": {"S": owner_id}},
                ProjectionExpression="connectionId, userId, connectionType",
            )
            out: list[tuple[str, str, str]] = []
            for page in pages:
                for item in page.get("Items", []):
                    out.append((
                        item["connectionId"]["S"],
                        item.get("userId", {}).get("S", ""),
                        item.get("connectionType", {}).get("S", "chat"),
                    ))
            return out
        try:
            return await asyncio.to_thread(_query)
        except ClientError as e:
            logger.error("query_by_owner failed for %s: %s", owner_id, e.response["Error"]["Message"])
            raise ConnectionServiceError(f"Failed to query connections for owner {owner_id}") from e
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/connection_service.py apps/backend/tests/unit/services/test_connection_service_owner.py
git commit -m "feat(backend): ConnectionService.query_by_owner via by-owner-id GSI"
```

---

### Task 4: Thread `owner_id` into `GatewayConnection`

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`GatewayConnection.__init__` ~70-81; `_create_connection` ~1503-1514)
- Test: `apps/backend/tests/unit/core/test_connection_pool.py` (extend)

> `_forward_to_frontends` needs the owner key to query. Today `GatewayConnection` is keyed/created per `user_id` (which IS the owner key — the pool's `_connections` dict is keyed by `owner_id`). Confirm by reading `_create_connection`: the key passed is the owner. Add an explicit `owner_id` attribute for clarity rather than overloading `self.user_id`.

- [ ] **Step 1: Write the failing test**

```python
# in test_connection_pool.py
def test_gateway_connection_exposes_owner_id():
    from core.gateway import connection_pool as cp
    conn = cp.GatewayConnection.__new__(cp.GatewayConnection)
    cp.GatewayConnection.__init__.__wrapped__ if False else None  # (construct via the real path in the existing fixture)
    # Use the existing connection-construction fixture/helper in this file; assert conn.owner_id == the key it was created under.
```

> Use this file's existing construction helper (it already builds `GatewayConnection`s). Assert the new `owner_id` attribute equals the owner key the pool created it under. Keep it minimal.

- [ ] **Step 2: Run, expect FAIL** — no `owner_id` attribute.

- [ ] **Step 3: Implement** — in `__init__`, set `self.owner_id = owner_id` (add the param, defaulting to `user_id` for back-compat), and in `_create_connection` pass the owner key explicitly.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(gateway): expose owner_id on GatewayConnection for owner-keyed fan-out"
```

---

### Task 5: Migrate `_forward_to_frontends` to owner-keyed DDB read

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_forward_to_frontends` ~596-637; add a small TTL cache; inject `ConnectionService`)
- Test: `apps/backend/tests/unit/core/test_connection_pool.py` + `test_connection_pool_org.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# in test_connection_pool.py — fan-out now reads query_by_owner, filters chat, demuxes per member
@pytest.mark.asyncio
async def test_forward_reads_owner_query_filters_chat_and_demuxes(monkeypatch):
    from core.gateway import connection_pool as cp
    conn = _make_conn_for_owner("org_x")  # existing-style helper; sets conn.owner_id="org_x"
    sent: list[str] = []
    conn._management_api.send_message = lambda cid, msg: (sent.append(cid) or True)
    # DDB returns: 2 chat members + 1 node (must be excluded) + member demux
    async def fake_query_by_owner(owner_id):
        assert owner_id == "org_x"
        return [("c_m1", "m1", "chat"), ("c_m2", "m2", "chat"), ("c_node", "m1", "node")]
    conn._conn_svc.query_by_owner = fake_query_by_owner

    # broadcast (target None): both chat members, NOT the node
    await conn._forward_to_frontends({"type": "chunk", "content": "x"}, target_member_id=None)
    assert set(sent) == {"c_m1", "c_m2"}

    # per-member demux: only member m1's chat connection (case-insensitive)
    sent.clear()
    await conn._forward_to_frontends({"type": "done"}, target_member_id="M1")
    assert sent == ["c_m1"]
```

```python
# also: DDB error => log + drop (no fallback, no crash)
@pytest.mark.asyncio
async def test_forward_on_query_error_drops_without_crashing(monkeypatch):
    conn = _make_conn_for_owner("user_p")
    async def boom(owner_id): raise RuntimeError("ddb down")
    conn._conn_svc.query_by_owner = boom
    sent = []
    conn._management_api.send_message = lambda cid, msg: (sent.append(cid) or True)
    await conn._forward_to_frontends({"type": "chunk", "content": "x"})  # must not raise
    assert sent == []  # dropped, logged
```

- [ ] **Step 2: Run, expect FAIL** — `_forward_to_frontends` still reads the in-process set and isn't async / doesn't query.

- [ ] **Step 3: Implement**

Make `_forward_to_frontends` async and source recipients from `query_by_owner` (with a short TTL cache to avoid per-chunk queries), filter `connectionType == "chat"`, demux per member (preserve the **case-insensitive** compare), push via the Management API, prune 410s. On query error: log + return (no fallback). Inject the `ConnectionService` (`self._conn_svc`) via the pool.

```python
    _RECIPIENT_CACHE_TTL_S = 5.0  # owner recipient set is stable across a streaming run

    async def _forward_to_frontends(self, message: dict, target_member_id: str | None = None) -> None:
        now = time.monotonic()
        cached = self._recipient_cache  # {"at": float, "rows": list[(conn,member,type)]}
        if cached and now - cached["at"] < self._RECIPIENT_CACHE_TTL_S:
            rows = cached["rows"]
        else:
            try:
                rows = await self._conn_svc.query_by_owner(self.owner_id)
            except Exception:
                logger.warning("[%s] fan-out: query_by_owner failed; dropping msg type=%s",
                               self.owner_id, message.get("type", "?"))
                return
            self._recipient_cache = {"at": now, "rows": rows}

        gone: list[str] = []
        for conn_id, member, ctype in rows:
            if ctype != "chat":            # exclude node/operator transparent pipes
                continue
            if target_member_id is not None and member.lower() != target_member_id.lower():
                continue
            try:
                if not self._management_api.send_message(conn_id, message):
                    gone.append(conn_id)
            except Exception:
                logger.warning("Failed to forward message to %s", conn_id)
                gone.append(conn_id)
        for conn_id in gone:
            # best-effort: drop the dead row so the GSI stops returning it
            try:
                self._conn_svc.delete_connection(conn_id)
            except Exception:
                pass
            self._recipient_cache = None  # invalidate so the next forward re-queries
            put_metric("gateway.frontend.prune")
```

> Callers (`event_name in ("agent","chat")` at ~1039/1095) already `self._forward_to_frontends(transformed, target_member)`; make those `await`. The in-process `_frontend_connections` + `add/remove_frontend_connection` stay for grace/idle lifecycle — **do not remove them** (out of scope), they're just no longer the fan-out source.

- [ ] **Step 4: Run, expect PASS** — both new tests + the existing `test_connection_pool*.py` (adjust any test that asserted the old in-process iteration to the new DDB-query path; the org test `test_multiple_members_share_owner_key` should now assert demux via the query result).

Run: `cd apps/backend && uv run pytest tests/unit/core/test_connection_pool.py tests/unit/core/test_connection_pool_org.py -v`

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(gateway): owner-keyed DDB fan-out (query_by_owner + chat filter + per-member demux)"
```

---

### Task 6: Full verification

- [ ] Backend full suite: `cd apps/backend && uv run pytest tests/ -q` — all green. **Run the whole suite, not just touched files** (Piece-3 lesson: per-task tests can pass while the integrated suite breaks). Pay attention to `test_teams_event_broker.py` (must be unaffected) and any contract tests.
- [ ] Infra suite: `cd apps/infra && pnpm test` — all green (incl. the new `by-owner-id` GSI assertion).
- [ ] Grep-verify the Paperclip-safety invariants: Teams broker untouched (`git diff` shows no `teams_event_broker.py` change); operator/node never reach `add_frontend_connection`; fan-out filters `connectionType=="chat"`.
- [ ] Manual sanity (dev): two members of one org chatting see only their own streams; a node/desktop connection receives no chat `chunk`/`done`.

## Out of scope (separate plans / explicitly not built)

- **Piece 2 — single-owner lease** (fencing, release-on-shutdown, `/internal/rpc-forward`). At N=1 it's a no-op; built next.
- **Relay routing at N>1 (§6.1)** — node-host/operator streaming forward.
- **Migration/backfill/dual-read/in-process-union** — deliberately NOT built (decision 2026-05-31; reconnect-on-introducing-deploy is acceptable at our scale).
- **DDB-error fallback to the in-process set** — not built; errors log + drop. Trivial future add since `_frontend_connections` remains.
