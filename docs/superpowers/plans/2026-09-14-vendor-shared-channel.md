# Vendor Shared Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every customer workspace gets one platform-created channel shared with the Lightbulb workspace: all humans from both orgs plus the customer's CEO agent, in one customer-owned room.

**Architecture:** The room is an ordinary customer-owned room. Lightbulb humans hold ordinary member rows whose new `homeOwnerId` points at the vendor org, and the discovery index keys on that home. Every "room owner equals caller's workspace" check becomes "the caller's member row's home equals the caller's workspace". Private notices and push resolve the user stream by the member's home. The vendor room is recognised by its deterministic id, never by a stored flag.

**Tech Stack:** FastAPI + DynamoDB (boto3, moto in tests, pytest-asyncio), the messaging-projector Lambda (Python, unittest), AWS CDK (TypeScript, jest), Next.js + vitest, Expo + jest, `@lightbulb/shared`.

**Spec:** `docs/superpowers/specs/2026-09-14-vendor-shared-channel-design.md` (same branch). Read it first; the plan argues from it.

## Global Constraints

- `VENDOR_ORG_ID` empty ⇒ the feature is entirely off: no seed, no webhook fan-out, `shared` never true.
- Prod `VENDOR_ORG_ID` is `org_3H3viGAfq2SXc9dIdcatmGXg8Td` (the founder org).
- Vendor room id is `uuid5(NAMESPACE_URL, "lightbulb-vendor-room|<customerOrgId>")`; name `lightbulb-<customer org slug>`, fallback `lightbulb`; kind `channel`.
- Members: customer org `created_by` as `owner`; every other human of both orgs as `member`; the CEO (`starter_agent_ids["ceo"]`) as `member` with `listenMode: "mentions"`. Foreign humans carry `homeOwnerId = VENDOR_ORG_ID`.
- `MAX_ROOM_MEMBERS` stays 32; over the cap the order is owner, CEO, customer humans by user id, vendor humans by user id, cut at 32, metric `messaging.vendor_room.capped`.
- No new table, no new attribute on `META`. New attribute on member rows only: `homeOwnerId` (missing ⇒ the row's `ownerId`).
- Public JSON stays camelCase; new fields are exactly `workspaceId` and `shared` on `RoomSummary`/`RoomDetail`.
- Human add/remove/leave and rename on the vendor room return `ROOM_FORBIDDEN`.
- `require_room_member`: missing room → `ROOM_NOT_FOUND`; same-org human with no row → `ROOM_FORBIDDEN` with `reconcileFromSeq`; anything else → `ROOM_NOT_FOUND`.
- Every DynamoDB read stays `ConsistentRead=True`; the `by-member` GSI is discovery only.
- Never run subagents on Fable: Opus for judgment-heavy tasks (2, 3, 6), Sonnet for the rest.
- Work in the worktree `../lb-wt.nosync/vendor-shared-channel` on branch `feat/vendor-shared-channel` (already exists, holds the spec). Backend tests: `cd apps/backend && uv run pytest <path> -q`. Web: `cd apps/frontend && pnpm vitest run <path>`. Mobile: `cd apps/mobile && pnpm jest <path>`. Infra: `cd apps/infra && npx jest test/service-stack.test.ts`.
- Two PRs: Tasks 1–9 are the backend PR; Tasks 10–13 the client PR. The client PR deletes the spec and this plan.

---

### Task 1: The `VENDOR_ORG_ID` setting, threaded from CDK

**Files:**
- Modify: `apps/backend/core/config.py:379` (after `AGENT_EMAIL_DOMAIN`)
- Modify: `apps/infra/lib/stacks/service-stack.ts:183` (props) and `:1618` (env map)
- Modify: `apps/infra/lib/lightbulb-stage.ts:38` (per-env map) and `:254` (props pass)
- Test: `apps/infra/test/service-stack.test.ts`

**Interfaces:**
- Produces: `settings.VENDOR_ORG_ID: str` (empty = off), read by Tasks 4, 6, 7.

- [ ] **Step 1: Write the failing infra test**

Append to `apps/infra/test/service-stack.test.ts` inside the top-level `describe("ServiceStack — clean handoff / rolling deploy"`:

```ts
  test("stamps the vendor org id into the prod backend task and leaves it empty where unset", () => {
    buildServiceStack("prod").hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: "backend",
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "VENDOR_ORG_ID", Value: "org_3H3viGAfq2SXc9dIdcatmGXg8Td" }),
          ]),
        }),
      ]),
    });
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/infra && npx jest test/service-stack.test.ts -t "vendor org id"`
Expected: FAIL — no `VENDOR_ORG_ID` environment entry.

- [ ] **Step 3: Add the setting and the CDK plumbing**

`apps/backend/core/config.py`, directly after the `AGENT_EMAIL_DOMAIN` line:

```python
    # The Lightbulb workspace's Clerk org id. Every OTHER org workspace gets a
    # platform-managed channel shared with this org (core/services/messaging.py
    # ensure_vendor_room). Empty disables the feature entirely.
    VENDOR_ORG_ID: str = ""
```

`apps/infra/lib/stacks/service-stack.ts` props interface, after `agentMailConfigurationSet?: string;`:

```ts
  /**
   * The Lightbulb workspace's Clerk org id (lightbulb-stage.ts VENDOR_ORG_ID_BY_ENV).
   * Every other org gets a channel shared with it. Empty ⇒ feature off.
   */
  vendorOrgId?: string;
```

Same file, env map, after `AGENT_MAIL_CONFIGURATION_SET: ...`:

```ts
      VENDOR_ORG_ID: props.vendorOrgId ?? "",
```

`apps/infra/lib/lightbulb-stage.ts`, next to `STRIPE_CONNECT_CLIENT_ID_BY_ENV`:

```ts
// The Lightbulb workspace per environment — the org every customer's shared
// `lightbulb-<slug>` channel is shared with. Dev is the founder's dev org.
const VENDOR_ORG_ID_BY_ENV: Record<string, string> = {
  prod: "org_3H3viGAfq2SXc9dIdcatmGXg8Td",
  dev: "<paste the dev founder org id here — Step 4>",
};
```

and in the ServiceStack props, after `stripeConnectClientId: ...`:

```ts
      vendorOrgId: VENDOR_ORG_ID_BY_ENV[env],
```

- [ ] **Step 4: Resolve the dev founder org id and replace the placeholder**

```bash
aws sso login --profile isol8-admin
KEY=$(aws secretsmanager get-secret-value --profile isol8-admin --secret-id lightbulb/dev/clerk_secret_key --query SecretString --output text)
curl -s "https://api.clerk.com/v1/organizations?query=lightbulb&limit=10" -H "Authorization: Bearer $KEY" | python3 -c 'import json,sys; [print(o["id"], o["slug"], o["name"]) for o in json.load(sys.stdin)["data"]]'
```

Pick the org whose slug is the founder workspace (the one the founder's dev container belongs to; if in doubt, `aws dynamodb scan --table-name lightbulb-dev-containers --projection-expression owner_id` and cross-check). Put that `org_…` id into the `dev:` entry. If the lookup is not possible in this session, set `dev: ""` and say so in the PR body — the feature is off in dev until it is filled.

- [ ] **Step 5: Run the infra test and synth**

Run: `cd apps/infra && npx jest test/service-stack.test.ts -t "vendor org id" && npx cdk synth > /dev/null && git status --short .github/workflows/deploy.yml`
Expected: PASS; `deploy.yml` unchanged (an env var does not change the pipeline). If it did change, commit it in the same commit.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/config.py apps/infra/lib/stacks/service-stack.ts apps/infra/lib/lightbulb-stage.ts apps/infra/test/service-stack.test.ts .github/workflows/deploy.yml
git commit -m "feat(config): VENDOR_ORG_ID — the Lightbulb workspace, per environment"
```

---

### Task 2: Repo — member home owner, vendor room id, per-notice LIVE owner

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py:97-115` (key helpers), `:187` (after `deterministic_room_id`), `:444-472` (`list_rooms_for_member`), `:480-498` (`_member_item`), `:1088-1090` (`remove_member` live rows)
- Test: `apps/backend/tests/unit/repositories/test_messaging_repo.py`

**Interfaces:**
- Produces: `member_home(member: dict) -> str`; `vendor_room_id(owner_id: str) -> str`; member dicts may carry `homeOwnerId`; `remove_member(live_events=[{userId, channelKey, event, ownerId?}])`.
- `list_rooms_for_member(owner_id, kind, member_id)` now means "rooms where this member's HOME is owner_id".

- [ ] **Step 1: Write the failing tests**

Add `import uuid` to the imports of `apps/backend/tests/unit/repositories/test_messaging_repo.py` if absent, and append:

```python
VENDOR = "org_vendor"


def _foreign(user_id: str) -> dict:
    return {**_member("human", user_id), "homeOwnerId": VENDOR}


@pytest.mark.asyncio
async def test_foreign_member_is_indexed_by_home_owner(table):
    room_id, _ = await _room(members=[_member("human", USER_A, "owner"), _foreign("user_v")])
    row = await repo.get_member(room_id, "human", "user_v")
    assert row["memberKey"] == f"OWNER#{VENDOR}#human#user_v"
    assert row["ownerId"] == OWNER and repo.member_home(row) == VENDOR
    assert [r["meta"]["roomId"] for r in await repo.list_rooms_for_member(VENDOR, "human", "user_v")] == [room_id]
    assert await repo.list_rooms_for_member(OWNER, "human", "user_v") == []
    local = await repo.get_member(room_id, "human", USER_A)
    assert local["memberKey"] == f"OWNER#{OWNER}#human#{USER_A}" and repo.member_home(local) == OWNER


@pytest.mark.asyncio
async def test_upsert_keeps_the_members_home(table):
    room_id, _ = await _room()
    await repo.upsert_member(room_id=room_id, owner_id=OWNER, member=_foreign("user_v"))
    row = await repo.get_member(room_id, "human", "user_v")
    assert repo.member_home(row) == VENDOR and row["memberKey"] == f"OWNER#{VENDOR}#human#user_v"


@pytest.mark.asyncio
async def test_removal_notice_carries_its_own_owner(table):
    room_id, meta = await _room(members=[_member("human", USER_A, "owner"), _foreign("user_v")])
    event = {"eventId": str(uuid.uuid4()), "type": "room.access.removed", "payload": {}}
    await repo.remove_member(
        meta=meta, kind="human", member_id="user_v", new_delivery_key=str(uuid.uuid4()),
        live_events=[{"userId": "user_v", "channelKey": "k", "event": event, "ownerId": VENDOR}],
    )
    live = await repo._query(repo.room_pk(room_id), begins_with="LIVE#")
    assert [(r["userId"], r["ownerId"]) for r in live] == [("user_v", VENDOR)]


def test_vendor_room_id_is_deterministic_per_customer():
    assert repo.vendor_room_id(OWNER) == repo.vendor_room_id(OWNER)
    assert repo.vendor_room_id(OWNER) != repo.vendor_room_id(VENDOR)
    assert repo.vendor_room_id(OWNER) == str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-vendor-room|{OWNER}"))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_messaging_repo.py -q -k "foreign or upsert_keeps or removal_notice or vendor_room_id"`
Expected: FAIL — `member_home`/`vendor_room_id` not defined.

- [ ] **Step 3: Implement**

In `messaging_repo.py`, after `member_key` (line ~115):

```python
def member_home(member: dict) -> str:
    """The workspace a member belongs to. A foreign human (a shared room) carries
    ``homeOwnerId``; every other row's home is the room owner it was written with."""
    return member.get("homeOwnerId") or member["ownerId"]
```

After `deterministic_room_id`:

```python
def vendor_room_id(owner_id: str) -> str:
    """The one platform-managed channel a customer workspace shares with the vendor org."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-vendor-room|{owner_id}"))
```

Replace `_member_item`:

```python
def _member_item(room_id: str, owner_id: str, m: dict, joined_seq: int, now: str) -> dict:
    home = m.get("homeOwnerId") or owner_id
    return {
        "pk": room_pk(room_id),
        "sk": member_sk(m["kind"], m["id"]),
        "memberKey": member_key(home, m["kind"], m["id"]),
        "memberSort": room_pk(room_id),
        "roomId": room_id,
        "ownerId": owner_id,
        "homeOwnerId": home,
        "kind": m["kind"],
        "id": m["id"],
        "displayName": m.get("displayName") or m["id"],
        "mentionHandle": m.get("mentionHandle"),
        "role": m["role"],
        "listenMode": m.get("listenMode"),
        "joinedAt": now,
        "joinedSeq": joined_seq,
    }
```

In `list_rooms_for_member`, replace the final filter:

```python
    for r in rows:
        meta = metas.get(r["roomId"])
        if meta is not None and member_home(r) == owner_id:
            out.append({"member": r, "meta": meta})
    return out
```

In `remove_member`, replace the `for live in live_events or []:` line's body:

```python
    for live in live_events or []:
        items.append(
            _live_item(table_name, room_id, live.get("ownerId") or owner_id, live["userId"], live.get("channelKey"), live["event"])
        )
```

Update the key-registry docstring at the top of the file: on the `MEMBER#` line append `, homeOwnerId (foreign members of a shared room; memberKey is keyed on it)`.

- [ ] **Step 4: Run the whole repo suite**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_messaging_repo.py -q`
Expected: PASS, including the four new tests.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/messaging_repo.py apps/backend/tests/unit/repositories/test_messaging_repo.py
git commit -m "feat(messaging): member rows carry a home owner; vendor room id"
```

---

### Task 3: Service — authorization and notices follow the member's home

**Files:**
- Modify: `apps/backend/core/services/messaging.py:1028-1043` (`require_room_member`), `:1436-1467` (`_remove_member_with_notices`), `:1470-1482` (`revoke_human_membership`)
- Create: `apps/backend/tests/unit/services/test_messaging_shared_rooms.py`

**Interfaces:**
- Consumes: `messaging_repo.member_home` (Task 2).
- Produces: `require_room_member(owner_id, user_id, room_id)` with the Global-Constraints error rule; notices carry `ownerId`.

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/services/test_messaging_shared_rooms.py`:

```python
"""Shared (vendor) rooms: foreign members are authorised and notified by their HOME workspace."""

import uuid
from unittest.mock import AsyncMock, call

import pytest
from core.auth import AuthContext
from core.services import messaging
from core.services.messaging import ApiProblem

ROOM = str(uuid.uuid4())
CUSTOMER, VENDOR = "org_customer", "org_vendor"
META = {
    "roomId": ROOM, "ownerId": CUSTOMER, "kind": "channel", "revision": 2, "lastSeq": 7,
    "deliveryKey": str(uuid.uuid4()), "workspaceId": str(uuid.uuid4()), "updatedAt": "2026-09-14T00:00:00Z",
    "lastMessageAt": None, "createdAt": "2026-09-14T00:00:00Z",
}


def _human(user_id: str, home: str, role: str = "member") -> dict:
    return {"kind": "human", "id": user_id, "ownerId": CUSTOMER, "homeOwnerId": home, "role": role,
            "joinedSeq": 0, "displayName": user_id, "mentionHandle": None, "listenMode": None,
            "joinedAt": "2026-09-14T00:00:00Z"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member, owner_id, expected",
    [
        (_human("user_v", VENDOR), VENDOR, None),
        (_human("user_v", VENDOR), CUSTOMER, "ROOM_NOT_FOUND"),
        (None, CUSTOMER, "ROOM_FORBIDDEN"),
        (None, VENDOR, "ROOM_NOT_FOUND"),
    ],
)
async def test_require_room_member_follows_the_members_home(monkeypatch, member, owner_id, expected):
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=member))
    if expected is None:
        assert await messaging.require_room_member(owner_id, "user_v", ROOM) == (META, member)
        return
    with pytest.raises(ApiProblem) as exc:
        await messaging.require_room_member(owner_id, "user_v", ROOM)
    assert exc.value.error.code == expected
    assert (exc.value.error.reconcile_from_seq == 7) is (expected == "ROOM_FORBIDDEN")


@pytest.mark.asyncio
async def test_removal_notices_go_to_each_humans_home_stream(monkeypatch):
    members = [_human("user_c", CUSTOMER, "owner"), _human("user_v", VENDOR)]
    streams = AsyncMock(side_effect=lambda owner, user: f"key-{owner}-{user}")
    remove = AsyncMock(return_value={**META, "revision": 3})
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", streams)
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", remove)

    await messaging._remove_member_with_notices(META, "human", "user_v")

    assert streams.await_args_list == [call(CUSTOMER, "user_c"), call(VENDOR, "user_v")]
    notices = remove.await_args.kwargs["live_events"]
    assert [(n["userId"], n["ownerId"], n["channelKey"]) for n in notices] == [
        ("user_c", CUSTOMER, f"key-{CUSTOMER}-user_c"),
        ("user_v", VENDOR, f"key-{VENDOR}-user_v"),
    ]
    assert [n["event"]["type"] for n in notices] == ["room.subscription.rotated", "room.access.removed"]


@pytest.mark.asyncio
async def test_revoking_an_org_membership_only_touches_rooms_where_that_org_is_home(monkeypatch):
    foreign = _human("user_v", VENDOR)
    discovered = [{"meta": META, "member": foreign}]
    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=discovered))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=foreign))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "deactivate_user_stream", AsyncMock())
    removed = AsyncMock(return_value=META)
    monkeypatch.setattr(messaging, "_remove_member_with_notices", removed)

    await messaging.revoke_human_membership(VENDOR, "user_v")
    removed.assert_awaited_once_with(META, "human", "user_v")

    removed.reset_mock()
    await messaging.revoke_human_membership(CUSTOMER, "user_v")
    removed.assert_not_awaited()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py -q`
Expected: FAIL — the foreign-from-home case raises `ROOM_NOT_FOUND`; notices carry no `ownerId`.

- [ ] **Step 3: Implement**

Replace `require_room_member`:

```python
async def require_room_member(owner_id: str, user_id: str, room_id: str) -> tuple[dict, dict]:
    room_id = validate_uuid(room_id, label="room ID")
    meta, member = await asyncio.gather(
        messaging_repo.get_room(room_id),
        messaging_repo.get_member(room_id, "human", user_id),
    )
    if meta is None:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    if member is not None and messaging_repo.member_home(member) == owner_id:
        return meta, member
    # A human of the room's own workspace who lost access keeps the reconcile hint;
    # everyone else (a stranger, or a foreign member viewing from the wrong org) sees nothing.
    if member is None and meta.get("ownerId") == owner_id:
        raise ApiProblem(
            "ROOM_FORBIDDEN",
            "You no longer have access to this room.",
            reconcile_from_seq=int(meta.get("lastSeq", 0)),
        )
    raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
```

In `_remove_member_with_notices`, replace the loop body's first line and the notice append:

```python
    for human in humans:
        home = messaging_repo.member_home(human)
        key = await messaging_repo.ensure_user_stream(home, human["id"])
        removed = human["id"] == member_id
        event = make_live_event(
            "room.access.removed" if removed else "room.subscription.rotated",
            (
                {"roomId": meta["roomId"], "roomRevision": revision}
                if removed
                else {"roomId": meta["roomId"], "deliveryKey": new_key, "roomRevision": revision}
            ),
            room_id=meta["roomId"],
            room_revision=revision,
        )
        notices.append({"userId": human["id"], "ownerId": home, "channelKey": key, "event": event})
```

In `revoke_human_membership`, replace `if meta is None or meta.get("ownerId") != owner_id:` with:

```python
        if meta is None or messaging_repo.member_home(member) != owner_id:
```

- [ ] **Step 4: Run the new file and the existing messaging suites**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py tests/unit/services/test_messaging_service_review_findings.py tests/unit/services/test_messaging_telemetry.py tests/unit/routers/test_messaging_presence.py -q`
Expected: PASS. If an existing test built a member dict without `ownerId`, `member_home` raises `KeyError`; fix that test's fixture by adding `"ownerId"` (the row always carries it in production), never by loosening `member_home`.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/messaging.py apps/backend/tests/unit/services/test_messaging_shared_rooms.py
git commit -m "feat(messaging): authorise and notify room members by their home workspace"
```

---

### Task 4: Vendor room identity, managed-membership guard, `shared` + `workspaceId`

**Files:**
- Modify: `apps/backend/core/services/messaging.py` — imports (`:24`), `RoomSummary` (`:163`), `_room_summary_fields` (`:1065`), `patch_room` (`:1334`), `upsert_member` (`:1380`), `remove_member` (`:1410`), `leave_room` (`:1423`); new helpers next to `default_room_id` (`:1179`)
- Test: `apps/backend/tests/unit/services/test_messaging_shared_rooms.py`

**Interfaces:**
- Consumes: `settings.VENDOR_ORG_ID` (Task 1), `messaging_repo.vendor_room_id` (Task 2).
- Produces: `is_vendor_room(meta) -> bool`; `_deny_vendor_human_change(meta, kind)`; `RoomSummary.workspace_id`, `RoomSummary.shared`.

- [ ] **Step 1: Write the failing tests**

Append to `test_messaging_shared_rooms.py`:

```python
@pytest.fixture
def vendor(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", VENDOR)
    return VENDOR


def test_vendor_room_is_recognised_by_its_deterministic_id(vendor):
    room_id = messaging.messaging_repo.vendor_room_id(CUSTOMER)
    assert messaging.is_vendor_room({"roomId": room_id, "ownerId": CUSTOMER})
    assert not messaging.is_vendor_room({"roomId": ROOM, "ownerId": CUSTOMER})
    fields = messaging._room_summary_fields({**META, "roomId": room_id}, _human("user_c", CUSTOMER), 0)
    assert fields["shared"] is True and fields["workspace_id"] == META["workspaceId"]
    assert messaging._room_summary_fields(META, _human("user_c", CUSTOMER), 0)["shared"] is False


def test_feature_off_means_no_room_is_ever_shared(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "")
    assert not messaging.is_vendor_room({"roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER), "ownerId": CUSTOMER})


@pytest.mark.asyncio
async def test_vendor_room_membership_and_name_are_platform_managed(vendor, monkeypatch):
    meta = {**META, "roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER)}
    auth = AuthContext(user_id="user_c", org_id=CUSTOMER)
    monkeypatch.setattr(messaging, "require_room_admin", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER, "owner"))))
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER))))
    attempts = [
        messaging.upsert_member(auth, meta["roomId"], messaging.UpsertRoomMemberRequest(kind="human", id="user_x", role="member", listen_mode=None)),
        messaging.remove_member(auth, meta["roomId"], "human", "user_v"),
        messaging.leave_room(auth, meta["roomId"]),
        messaging.patch_room(auth, meta["roomId"], messaging.PatchRoomRequest(name="renamed")),
    ]
    for attempt in attempts:
        with pytest.raises(ApiProblem) as exc:
            await attempt
        assert exc.value.error.code == "ROOM_FORBIDDEN"


@pytest.mark.asyncio
async def test_vendor_room_still_lets_admins_change_the_agents_listen_mode(vendor, monkeypatch):
    meta = {**META, "roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER)}
    auth = AuthContext(user_id="user_c", org_id=CUSTOMER)
    agent = {"kind": "agent", "id": str(uuid.uuid4()), "role": "member", "listenMode": "mentions", "displayName": "CEO", "mentionHandle": "ceo"}
    monkeypatch.setattr(messaging, "require_room_admin", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER, "owner"))))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=agent))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": agent["id"], "name": "CEO"}]))
    monkeypatch.setattr(messaging, "_resolved_member", AsyncMock(return_value={**agent, "listenMode": "active"}))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[agent]))
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    result = await messaging.upsert_member(
        auth, meta["roomId"], messaging.UpsertRoomMemberRequest(kind="agent", id=agent["id"], role="member", listen_mode="active")
    )
    assert result == "detail"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py -q -k "vendor_room or feature_off"`
Expected: FAIL — `messaging.settings` / `is_vendor_room` missing.

- [ ] **Step 3: Implement**

Add to the imports of `messaging.py` (after `from core.auth import AuthContext, resolve_owner_id`):

```python
from core.auth import is_org_owner_id
from core.config import settings
```

In `RoomSummary`, after `last_read_seq: int = Field(ge=0, default=0)`:

```python
    # The ROOM's opaque AppSync segment: a shared channel lives in the owning
    # workspace, so a foreign member's client must subscribe with this, not its
    # own. None only on rows written before the segment was stamped.
    workspace_id: uuid.UUID | None = None
    # The platform-managed channel shared with the Lightbulb workspace.
    shared: bool = False
```

Next to `default_room_id`:

```python
def is_vendor_room(meta: dict) -> bool:
    """The one channel a customer shares with the vendor org — by id, never by a stored flag."""
    return bool(settings.VENDOR_ORG_ID) and meta.get("roomId") == messaging_repo.vendor_room_id(meta.get("ownerId") or "")


def _deny_vendor_human_change(meta: dict, kind: str) -> None:
    if kind == "human" and is_vendor_room(meta):
        raise ApiProblem("ROOM_FORBIDDEN", "Membership of this channel is managed by Lightbulb.")
```

In `_room_summary_fields`, add to the returned dict:

```python
        "workspace_id": meta.get("workspaceId"),
        "shared": is_vendor_room(meta),
```

In `patch_room`, after the DM rename guard:

```python
    if "name" in updates and is_vendor_room(meta):
        raise ApiProblem("ROOM_FORBIDDEN", "This channel's name is managed by Lightbulb.")
```

In `upsert_member`, right after `meta, actor = await require_room_admin(...)`:

```python
    _deny_vendor_human_change(meta, request.kind)
```

In `remove_member`, right after `meta, _ = await require_room_admin(...)`:

```python
    _deny_vendor_human_change(meta, kind)
```

In `leave_room`, right after `meta, member = await require_room_member(...)`:

```python
    _deny_vendor_human_change(meta, "human")
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py tests/unit/services/test_messaging_telemetry.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/messaging.py apps/backend/tests/unit/services/test_messaging_shared_rooms.py
git commit -m "feat(messaging): vendor room identity, managed membership, shared/workspaceId fields"
```

---

### Task 5: Attachments live in the room owner's workspace

**Files:**
- Modify: `apps/backend/routers/messaging.py:352-392` (both attachment routes)
- Test: `apps/backend/tests/unit/services/test_messaging_shared_rooms.py`

**Interfaces:**
- Consumes: `require_room_member` returning `(meta, member)` (Task 3).

- [ ] **Step 1: Write the failing test**

Append to `test_messaging_shared_rooms.py`:

```python
@pytest.mark.asyncio
async def test_attachment_download_reads_the_rooms_owner_workspace_not_the_callers(monkeypatch):
    from types import SimpleNamespace

    from routers import messaging as router

    seen: list[str] = []
    monkeypatch.setattr(router.service, "require_room_member", AsyncMock(return_value=(META, _human("user_v", VENDOR))))
    monkeypatch.setattr(
        router.service,
        "load_attachment",
        lambda owner, room, attachment_id: (seen.append(owner), (SimpleNamespace(content_type="image/png", file_name="a.png"), b"x"))[1],
    )
    response = await router.get_room_attachment(ROOM, str(uuid.uuid4()), AuthContext(user_id="user_v", org_id=VENDOR))
    assert response.status_code == 200 and seen == [CUSTOMER]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py -q -k attachment_download`
Expected: FAIL — `seen == ["org_vendor"]`.

- [ ] **Step 3: Implement**

In `upload_room_attachment`, replace the two lines `owner_id = resolve_owner_id(auth)` / `await service.require_room_member(owner_id, auth.user_id, room_id)` with:

```python
    # Files live with the ROOM's owner, so a foreign member's upload lands where the
    # room's own members can read it (and vice versa).
    meta, _ = await service.require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]
```

Make the identical replacement in `get_room_attachment`.

- [ ] **Step 3b (controller ruling after the Task 3 review): every room-scoped write keys on the room owner**

`create_human_message`, `update_message`, and `set_reaction` in `core/services/messaging.py` still pass the CALLER's workspace into repo writes that stamp `ownerId` (the `MSG` row, the `WORK#` outbox and its FIFO group, the edit guard in `messaging_repo.update_message`), into `_touch_last_interactive_quietly`, `capture_first_workspace_message`, and the product events. With a foreign member that mis-keys a vendor human's send to the vendor's container and blocks editing their own message. Rule: the caller's workspace is used for `require_room_member` only; everything after it uses the room's owner.

In each of the three functions, replace the opening lines with this shape (keep everything below unchanged):

```python
    meta, member = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]  # room-scoped writes are keyed on the ROOM owner, never the caller's workspace
```

(`update_message` currently discards the tuple — capture `meta`; `set_reaction` already has `meta, _` — keep `meta`.) Do not touch `patch_room_me`, `list_rooms`, `put_sections`, or the user-stream/prefs calls: those are the caller's own rows and stay keyed on the caller's workspace.

Add this test to `test_messaging_shared_rooms.py`:

```python
@pytest.mark.asyncio
async def test_a_foreign_members_message_is_written_under_the_rooms_owner(monkeypatch):
    sender = _human("user_v", VENDOR)
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, sender)))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[sender]))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Vee"))
    stored = {**META, "messageId": str(uuid.uuid4()), "messageSeq": 8, "roomRevision": 2, "author": {"kind": "human", "id": "user_v", "displayName": "Vee"},
              "text": "hi", "mentions": [], "replyToMessageId": None, "threadRootMessageId": None, "createdAt": "2026-09-14T00:00:00Z", "runId": None, "dispatchTargets": []}
    create = AsyncMock(return_value=(stored, False))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    touched, captured = [], []
    monkeypatch.setattr(messaging, "_touch_last_interactive_quietly", lambda owner: touched.append(owner) or _noop())
    monkeypatch.setattr("core.services.onboarding_brief.capture_first_workspace_message", lambda owner, item: captured.append(owner) or _noop())
    await messaging.create_human_message(
        AuthContext(user_id="user_v", org_id=VENDOR), ROOM,
        messaging.CreateMessageRequest(client_message_id=uuid.uuid4(), text="hi", mentions=[], reply_to_message_id=None, thread_root_message_id=None),
    )
    assert create.await_args.kwargs["owner_id"] == CUSTOMER
    assert touched == [CUSTOMER] and captured == [CUSTOMER]


async def _noop() -> None:
    return None
```

If `create_human_message` awaits other collaborators the mock list above does not cover (read the function; e.g. `_validate_mentions`, `select_dispatch_targets`, telemetry), patch them with the smallest stub that lets the call reach `create_message` — the assertion that matters is the `owner_id` kwarg. `CreateMessageRequest` may require `attachment_ids`; pass `[]` if so.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py tests/unit/services/test_messaging_service_review_findings.py -q`
Expected: PASS (the existing review-findings attachment test still passes because it patches `require_room_member` to raise).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_messaging_shared_rooms.py
git commit -m "fix(messaging): room-scoped writes and attachments key on the room owner, not the caller"
```

---

### Task 6: `ensure_vendor_room` and its seat in `ensure_user_ready`

**Files:**
- Modify: `apps/backend/core/services/messaging.py` (new functions after `ensure_default_room`, `:1284`)
- Modify: `apps/backend/core/services/user_provision.py:362-369` (after the `_reconcile_owner_seed` try/except)
- Test: `apps/backend/tests/unit/services/test_messaging_shared_rooms.py`, `apps/backend/tests/unit/services/test_user_provision.py`

**Interfaces:**
- Consumes: `clerk_admin.get_organization(org_id) -> dict|None` (raw Clerk org: `slug`, `created_by`), `company_slug_repo.get_by_owner(owner_id) -> dict|None` (`{"slug": ...}`, the platform's own company slug), `clerk_admin.list_organization_members(org_id) -> [{user_id, first_name, last_name, email, image_url, role}]`, `PaperclipRepo.resolve_owner_row`, `load_agent_roster`, `mention_handles`, `messaging_repo.create_room`, `messaging_repo.ensure_user_stream`.
- Produces: `ensure_vendor_room(owner_id: str) -> str | None`; helpers `_display_name_from_membership(row) -> str`, `_vendor_room_human(row, home_owner_id, *, role="member") -> dict` (reused by Task 7).

- [ ] **Step 1: Write the failing tests**

Append to `test_messaging_shared_rooms.py`:

```python
def _membership(user_id: str, role: str = "org:member", first: str = "", email: str = "") -> dict:
    return {"user_id": user_id, "first_name": first, "last_name": "", "email": email or f"{user_id}@example.com", "image_url": "", "role": role}


CEO = str(uuid.uuid4())


def _seed_env(monkeypatch, *, company_status="active", starter=None, room_exists=False, customer=None, vendor_humans=None):
    from types import SimpleNamespace

    company = SimpleNamespace(status=company_status, starter_agent_ids={"ceo": CEO} if starter is None else starter)
    repo_cls = lambda table_name: SimpleNamespace(resolve_owner_row=AsyncMock(return_value=company))  # noqa: E731
    monkeypatch.setattr("core.repositories.paperclip_repo.PaperclipRepo", repo_cls)
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value={"roomId": "x"} if room_exists else None))
    get_org = AsyncMock(return_value={"id": CUSTOMER, "slug": "acme-1789347059550170402", "created_by": "user_c"})
    monkeypatch.setattr("core.repositories.company_slug_repo.get_by_owner", AsyncMock(return_value={"slug": "acme"}))
    list_members = AsyncMock(side_effect=lambda org_id: (customer if customer is not None else [_membership("user_c", "org:admin", "Cal"), _membership("user_d")]) if org_id == CUSTOMER else (vendor_humans if vendor_humans is not None else [_membership("user_v", first="Vee")]))
    monkeypatch.setattr("core.services.clerk_admin.get_organization", get_org)
    monkeypatch.setattr("core.services.clerk_admin.list_organization_members", list_members)
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": CEO, "name": "CEO"}]))
    streams = AsyncMock(return_value="key")
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", streams)
    create = AsyncMock(return_value={**META, "roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER)})
    monkeypatch.setattr(messaging.messaging_repo, "create_room", create)
    monkeypatch.setattr(messaging, "_emit_room_created", lambda *a, **k: None)
    return SimpleNamespace(get_org=get_org, list_members=list_members, streams=streams, create=create)


@pytest.mark.asyncio
async def test_ensure_vendor_room_seeds_both_orgs_and_the_ceo(vendor, monkeypatch):
    env = _seed_env(monkeypatch)
    assert await messaging.ensure_vendor_room(CUSTOMER) == messaging.messaging_repo.vendor_room_id(CUSTOMER)
    kwargs = env.create.await_args.kwargs
    assert kwargs["owner_id"] == CUSTOMER and kwargs["creator_user_id"] == "user_c"
    assert kwargs["name"] == "lightbulb-acme" and kwargs["kind"] == "channel"
    assert kwargs["client_room_id"] == f"vendor-room-v1:{CUSTOMER}"
    members = kwargs["members"]
    assert [(m["kind"], m["id"], m["role"], m.get("homeOwnerId"), m.get("listenMode")) for m in members] == [
        ("human", "user_c", "owner", CUSTOMER, None),
        ("agent", CEO, "member", None, "mentions"),
        ("human", "user_d", "member", CUSTOMER, None),
        ("human", "user_v", "member", VENDOR, None),
    ]
    assert members[0]["displayName"] == "Cal" and members[3]["displayName"] == "Vee"
    assert env.streams.await_args_list == [call(CUSTOMER, "user_c"), call(CUSTOMER, "user_d"), call(VENDOR, "user_v")]


@pytest.mark.asyncio
async def test_ensure_vendor_room_falls_back_to_the_clerk_slug_without_its_suffix(vendor, monkeypatch):
    env = _seed_env(monkeypatch)
    monkeypatch.setattr("core.repositories.company_slug_repo.get_by_owner", AsyncMock(return_value=None))
    await messaging.ensure_vendor_room(CUSTOMER)
    assert env.create.await_args.kwargs["name"] == "lightbulb-acme"


@pytest.mark.asyncio
async def test_ensure_vendor_room_short_circuits_when_the_room_exists(vendor, monkeypatch):
    env = _seed_env(monkeypatch, room_exists=True)
    assert await messaging.ensure_vendor_room(CUSTOMER) == messaging.messaging_repo.vendor_room_id(CUSTOMER)
    env.get_org.assert_not_awaited()
    env.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id, status", [(VENDOR, "active"), ("user_personal", "active"), (CUSTOMER, "pending")])
async def test_ensure_vendor_room_waits_or_skips(vendor, monkeypatch, owner_id, status):
    env = _seed_env(monkeypatch, company_status=status)
    assert await messaging.ensure_vendor_room(owner_id) is None
    env.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_vendor_room_is_off_without_a_vendor_org(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "")
    env = _seed_env(monkeypatch)
    assert await messaging.ensure_vendor_room(CUSTOMER) is None
    env.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_vendor_room_is_humans_only_for_a_legacy_roster(vendor, monkeypatch):
    env = _seed_env(monkeypatch, starter={})
    await messaging.ensure_vendor_room(CUSTOMER)
    assert all(m["kind"] == "human" for m in env.create.await_args.kwargs["members"])


@pytest.mark.asyncio
async def test_ensure_vendor_room_never_seeds_from_a_short_clerk_roster(vendor, monkeypatch):
    env = _seed_env(monkeypatch, vendor_humans=[])
    assert await messaging.ensure_vendor_room(CUSTOMER) is None
    env.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_vendor_room_caps_customer_first(vendor, monkeypatch):
    customer = [_membership("user_c", "org:admin")] + [_membership(f"user_c{i:02d}") for i in range(20)]
    vendor_humans = [_membership(f"user_v{i:02d}") for i in range(20)]
    env = _seed_env(monkeypatch, customer=customer, vendor_humans=vendor_humans)
    metrics = []
    monkeypatch.setattr(messaging, "put_metric", lambda name, **kw: metrics.append(name))
    await messaging.ensure_vendor_room(CUSTOMER)
    members = env.create.await_args.kwargs["members"]
    assert len(members) == messaging.messaging_repo.MAX_ROOM_MEMBERS
    assert [m["id"] for m in members[:2]] == ["user_c", CEO]
    assert all(m.get("homeOwnerId") == CUSTOMER for m in members[2:22])
    assert [m["id"] for m in members[22:]] == [f"user_v{i:02d}" for i in range(10)]
    assert "messaging.vendor_room.capped" in metrics
```

And append to `apps/backend/tests/unit/services/test_user_provision.py`:

```python
@pytest.mark.asyncio
async def test_org_poll_on_a_running_container_reconciles_the_vendor_room():
    """A plain org MEMBER's poll reaches the vendor-room seat (spec §4) — the seat is not behind the admin-only container path."""
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()),
        patch.object(user_provision.container_repo, "get_by_owner_id", AsyncMock(return_value={"status": "running"})),
        patch.object(user_provision.wake_policy, "is_parked_trial", AsyncMock(return_value=False)),
        patch("core.services.user_provision._reconcile_owner_seed", new=AsyncMock()),
        patch("core.services.messaging.ensure_vendor_room", new=AsyncMock()) as vendor_room,
    ):
        await ensure_user_ready(_org_member_auth("user_m", "org_o", "m@example.com"))
    vendor_room.assert_awaited_once_with("org_o")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py -q -k ensure_vendor_room && uv run pytest tests/unit/services/test_user_provision.py -q -k vendor_room`
Expected: FAIL — `ensure_vendor_room` does not exist.

- [ ] **Step 3: Implement the seed**

In `messaging.py`, directly after `ensure_default_room`:

```python
def _display_name_from_membership(row: dict) -> str:
    """Same precedence as ``_resolve_human_display_name``, from the Clerk membership row
    we already hold — one roster read instead of one ``get_user`` per human."""
    name = " ".join(part.strip() for part in (row.get("first_name") or "", row.get("last_name") or "") if part.strip())
    if name:
        return name[:256]
    email = row.get("email") or ""
    return (email.split("@", 1)[0] if email else row["user_id"])[:256]


def _vendor_room_human(row: dict, home_owner_id: str, *, role: str = "member") -> dict:
    return {
        "kind": "human",
        "id": row["user_id"],
        "displayName": _display_name_from_membership(row),
        "mentionHandle": None,
        "role": role,
        "listenMode": None,
        "homeOwnerId": home_owner_id,
    }


async def ensure_vendor_room(owner_id: str) -> str | None:
    """Create the customer's channel shared with the vendor org, once (spec §4).

    Runs on every org-context poll of a running container; the steady-state cost is
    one consistent ``get_room``. Creation waits for an ACTIVE Paperclip company so a
    fresh workspace gets its CEO in the room. Returns the room id when it exists."""
    vendor = settings.VENDOR_ORG_ID
    if not vendor or owner_id == vendor or not is_org_owner_id(owner_id):
        return None
    room_id = messaging_repo.vendor_room_id(owner_id)
    if await messaging_repo.get_room(room_id) is not None:
        return room_id

    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.clerk_admin import get_organization, list_organization_members

    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id, consistent_read=True)
    if getattr(company, "status", None) != "active":
        return None
    org = await get_organization(owner_id)
    customer_humans = await list_organization_members(owner_id)
    vendor_humans = await list_organization_members(vendor)
    # list_organization_members returns [] on any Clerk failure; a half roster must never seed.
    if org is None or not customer_humans or not vendor_humans:
        return None
    by_id = {row["user_id"]: row for row in customer_humans}
    owner_user_id = org.get("created_by") if org.get("created_by") in by_id else next(
        (uid for uid in sorted(by_id) if by_id[uid].get("role") == "org:admin"), None
    )
    if owner_user_id is None:
        return None
    # The platform's own company slug (company hosts, agent mail) is the readable one;
    # Clerk org slugs here carry a timestamp suffix ("acme-1789347059550170402").
    from core.repositories import company_slug_repo

    slug_row = await company_slug_repo.get_by_owner(owner_id)
    slug = (slug_row or {}).get("slug") or re.sub(r"-\d{10,}$", "", org.get("slug") or "")
    try:
        name = validate_room_name(f"lightbulb-{slug}".rstrip("-"))
    except ValueError:
        name = "lightbulb"

    members = [_vendor_room_human(by_id[owner_user_id], owner_id, role="owner")]
    ceo_id = (getattr(company, "starter_agent_ids", None) or {}).get("ceo")
    if ceo_id:
        roster = await load_agent_roster(owner_id)
        agents = {row["id"]: row for row in roster}
        if ceo_id in agents:
            members.append({
                "kind": "agent",
                "id": ceo_id,
                "displayName": agents[ceo_id]["name"],
                "mentionHandle": mention_handles(roster)[ceo_id],
                "role": "member",
                "listenMode": "mentions",
            })
    members.extend(_vendor_room_human(by_id[uid], owner_id) for uid in sorted(by_id) if uid != owner_user_id)
    seen = {m["id"] for m in members}
    members.extend(
        _vendor_room_human(row, vendor)
        for row in sorted(vendor_humans, key=lambda r: r["user_id"])
        if row["user_id"] not in seen
    )
    if len(members) > messaging_repo.MAX_ROOM_MEMBERS:
        # ponytail: 32-member cap; raise MAX_ROOM_MEMBERS when a customer org exceeds ~25 humans
        put_metric("messaging.vendor_room.capped")
        logger.warning("vendor room capped at %d members for owner %s", messaging_repo.MAX_ROOM_MEMBERS, owner_id)
        members = members[: messaging_repo.MAX_ROOM_MEMBERS]
    for human in (m for m in members if m["kind"] == "human"):
        await messaging_repo.ensure_user_stream(human["homeOwnerId"], human["id"])
    try:
        await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=room_id,
            client_room_id=f"vendor-room-v1:{owner_id}",
            creator_user_id=owner_user_id,
            name=name,
            kind="channel",
            members=members,
            req_hash=messaging_repo.request_hash(name, "channel", [(m["kind"], m["id"]) for m in members]),
        )
    except messaging_repo.IdempotencyConflict:
        return room_id  # a concurrent poll created it with a different roster; it exists
    _emit_room_created(owner_id, owner_user_id, "channel", members[1:], by="system")
    return room_id
```

Metric dimensions stay low-cardinality (never an owner id): the owner goes in the log line.

- [ ] **Step 4: Implement the seat**

Controller ruling (2026-09-14): the seat is in `ensure_user_ready` itself, NOT inside `_provision_or_reconcile`. `_provision_or_reconcile` (and the `_reconcile_owner_seed` try/except inside it) runs only for personal owners and org ADMINS (`if not auth.is_org_context or auth.is_org_admin:`), so an org member's poll would never create the room. `ensure_vendor_room` needs no running container (it waits on an ACTIVE Paperclip company instead), so there is no container precondition either.

In `user_provision.py` `ensure_user_ready`, directly after the block

```python
    if not auth.is_org_context or auth.is_org_admin:
        await _provision_or_reconcile(auth, owner_id)
```

and before the nightly-CEO block, add:

```python
    # The customer's channel shared with the Lightbulb workspace. Any member's poll
    # reconciles it (not only the container owner's); its own first read is the
    # short-circuit, so the steady-state cost is one consistent read per poll.
    if auth.is_org_context:
        from core.services.messaging import ensure_vendor_room

        try:
            await ensure_vendor_room(owner_id)
        except Exception:
            put_metric("messaging.vendor_room.error")
            logger.exception("vendor room reconcile failed for owner %s", owner_id)
```

- [ ] **Step 5: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py tests/unit/services/test_user_provision.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/messaging.py apps/backend/core/services/user_provision.py apps/backend/tests/unit/services/test_messaging_shared_rooms.py apps/backend/tests/unit/services/test_user_provision.py
git commit -m "feat(messaging): seed the vendor shared channel from every org poll"
```

---

### Task 7: Webhook increments — new joiners on either side

**Files:**
- Modify: `apps/backend/core/services/messaging.py` (after `ensure_vendor_room`)
- Modify: `apps/backend/routers/webhooks.py:411-424` (end of `_handle_organization_membership_created`)
- Test: `apps/backend/tests/unit/services/test_messaging_shared_rooms.py`, `apps/backend/tests/unit/routers/test_webhooks.py`

**Interfaces:**
- Consumes: `_vendor_room_human`, `human_display_name`, `container_repo.scan_all()` (rows carry `owner_id`), `messaging_repo.upsert_member`.
- Produces: `add_vendor_room_member(owner_id, user_id, *, home_owner_id) -> bool`, `on_org_member_joined(org_id, user_id) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `test_messaging_shared_rooms.py`:

```python
@pytest.mark.asyncio
async def test_add_vendor_room_member_adds_a_missing_human_with_their_home(vendor, monkeypatch):
    room_id = messaging.messaging_repo.vendor_room_id(CUSTOMER)
    meta = {**META, "roomId": room_id}
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Vee"))
    streams = AsyncMock(return_value="key")
    upsert = AsyncMock(return_value={**meta, "revision": 3})
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", streams)
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())

    assert await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR) is True
    streams.assert_awaited_once_with(VENDOR, "user_v")
    member = upsert.await_args.kwargs["member"]
    assert (member["id"], member["role"], member["homeOwnerId"], member["displayName"]) == ("user_v", "member", VENDOR, "Vee")
    assert upsert.await_args.kwargs["owner_id"] == CUSTOMER


@pytest.mark.asyncio
async def test_add_vendor_room_member_is_a_no_op_without_a_room_or_when_present(vendor, monkeypatch):
    upsert = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=None))
    assert await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR) is False
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=_human("user_v", VENDOR)))
    assert await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR) is False
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_join_adds_to_that_customers_room(vendor, monkeypatch):
    add = AsyncMock(return_value=True)
    monkeypatch.setattr(messaging, "add_vendor_room_member", add)
    await messaging.on_org_member_joined(CUSTOMER, "user_d")
    add.assert_awaited_once_with(CUSTOMER, "user_d", home_owner_id=CUSTOMER)


@pytest.mark.asyncio
async def test_vendor_join_fans_out_to_every_org_owner(vendor, monkeypatch):
    add = AsyncMock(return_value=True)
    monkeypatch.setattr(messaging, "add_vendor_room_member", add)
    rows = [{"owner_id": CUSTOMER}, {"owner_id": "org_other"}, {"owner_id": "user_personal"}, {"owner_id": VENDOR}]
    monkeypatch.setattr(messaging.container_repo, "scan_all", AsyncMock(return_value=rows))
    await messaging.on_org_member_joined(VENDOR, "user_new")
    assert add.await_args_list == [
        call(CUSTOMER, "user_new", home_owner_id=VENDOR),
        call("org_other", "user_new", home_owner_id=VENDOR),
    ]


@pytest.mark.asyncio
async def test_joins_are_ignored_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "")
    add = AsyncMock()
    monkeypatch.setattr(messaging, "add_vendor_room_member", add)
    await messaging.on_org_member_joined(CUSTOMER, "user_d")
    add.assert_not_awaited()
```

Append to `apps/backend/tests/unit/routers/test_webhooks.py`:

```python
@pytest.mark.asyncio
async def test_membership_created_adds_the_member_to_the_vendor_room():
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {"user_id": "user_member", "identifier": "member@example.com"},
    }
    with (
        patch("routers.webhooks._renew_member_lifecycle", new=AsyncMock()),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()),
        patch("core.services.messaging.on_org_member_joined", new=AsyncMock()) as joined,
    ):
        from routers.webhooks import _handle_organization_membership_created

        await _handle_organization_membership_created(payload)
    joined.assert_awaited_once_with("org_xyz", "user_member")


@pytest.mark.asyncio
async def test_membership_created_survives_a_vendor_room_failure():
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {"user_id": "user_member", "identifier": "member@example.com"},
    }
    with (
        patch("routers.webhooks._renew_member_lifecycle", new=AsyncMock()),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()),
        patch("core.services.messaging.on_org_member_joined", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        from routers.webhooks import _handle_organization_membership_created

        await _handle_organization_membership_created(payload)  # must not raise: Clerk would retry the event
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py -q -k "add_vendor_room_member or join" && uv run pytest tests/unit/routers/test_webhooks.py -q -k vendor_room`
Expected: FAIL — functions missing.

- [ ] **Step 3: Implement**

In `messaging.py`, after `ensure_vendor_room`:

```python
async def add_vendor_room_member(owner_id: str, user_id: str, *, home_owner_id: str) -> bool:
    """Add one human to a customer's vendor room, keyed on THEIR home. Idempotent."""
    room_id = messaging_repo.vendor_room_id(owner_id)
    meta = await messaging_repo.get_room(room_id)
    if meta is None or await messaging_repo.get_member(room_id, "human", user_id) is not None:
        return False
    await messaging_repo.ensure_user_stream(home_owner_id, user_id)
    member = {
        "kind": "human",
        "id": user_id,
        "displayName": await human_display_name(user_id),
        "mentionHandle": None,
        "role": "member",
        "listenMode": None,
        "homeOwnerId": home_owner_id,
    }
    try:
        meta = await messaging_repo.upsert_member(room_id=room_id, owner_id=owner_id, member=member)
    except messaging_repo.MemberLimitExceeded:
        put_metric("messaging.vendor_room.capped")
        logger.warning("vendor room full; %s not added to owner %s", user_id, owner_id)
        return False
    await _publish_room_metadata(meta)
    return True


async def on_org_member_joined(org_id: str, user_id: str) -> None:
    """Clerk ``organizationMembership.created``: keep 'everyone in both workspaces' true."""
    vendor = settings.VENDOR_ORG_ID
    if not vendor:
        return
    if org_id != vendor:
        await add_vendor_room_member(org_id, user_id, home_owner_id=org_id)
        return
    # ponytail: full containers scan per vendor join; add a VENDORROOM# catalog row when org count makes this slow
    for row in await container_repo.scan_all():
        customer = row.get("owner_id")
        if isinstance(customer, str) and is_org_owner_id(customer) and customer != vendor:
            await add_vendor_room_member(customer, user_id, home_owner_id=vendor)
```

In `routers/webhooks.py`, at the end of `_handle_organization_membership_created` (after the `_ensure_paperclip_for_member` try/except):

```python
    from core.services.messaging import on_org_member_joined

    try:
        await on_org_member_joined(org_id, user_id)
    except Exception:
        logger.exception("membership.created: vendor room add failed org=%s user=%s", org_id, user_id)
        put_metric("messaging.vendor_room.error")
```

Also extend the handler's docstring sentence "Room admission remains explicit; …" with: "The one exception is the vendor shared channel, which `on_org_member_joined` keeps in step with org membership."

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_shared_rooms.py tests/unit/routers/test_webhooks.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/messaging.py apps/backend/routers/webhooks.py apps/backend/tests/unit/services/test_messaging_shared_rooms.py apps/backend/tests/unit/routers/test_webhooks.py
git commit -m "feat(messaging): org joins keep the vendor shared channel complete"
```

---

### Task 8: Projector notifies foreign members through their home stream

**Files:**
- Modify: `apps/infra/lambda/messaging-projector/index.py:489-499` (`_notify_members`)
- Test: `apps/infra/lambda/messaging-projector/test_index.py`

**Interfaces:**
- Consumes: member rows with `homeOwnerId` (Task 2).

- [ ] **Step 1: Write the failing test**

Add to `class ProjectorTests` in `test_index.py`:

```python
    @patch("index._publish")
    def test_foreign_member_gets_the_hint_on_their_home_stream(self, publish):
        vendor = "org_vendor"
        meta = {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": "dm", "workspaceId": WORKSPACE_ID}
        items = {
            (f"ROOM#{ROOM_ID}", "META"): meta,
            (f"ROOM#{ROOM_ID}", f"MEMBER#human#{BOB_ID}"): {"id": BOB_ID, "ownerId": OWNER_ID, "homeOwnerId": vendor, "joinedSeq": 0},
            (f"OWNER#{vendor}", f"USERSTREAM#{BOB_ID}"): {"active": True, "channelKey": BOB_KEY},
            (f"STREAM#{BOB_KEY}", "META"): {"active": True, "ownerId": vendor, "userId": BOB_ID, "workspaceId": WORKSPACE_ID},
        }
        index._ddb_client = FakeDdb(items)
        index._notify_members(message_item(), meta)
        self.assertEqual([c.args[0] for c in publish.call_args_list], [f"/ws/{WORKSPACE_ID}/users/{BOB_KEY}"])
        self.assertTrue(publish.call_args_list[0].args[1]["payload"]["notify"])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/infra/lambda/messaging-projector && python3 -m pytest test_index.py -q -k foreign_member`
Expected: FAIL — no publish (the stream was looked up under `OWNER#org_owner`).

- [ ] **Step 3: Implement**

In `_notify_members`, replace the two owner-keyed lookups:

```python
        home = member.get("homeOwnerId") or owner_id
        mapping = _get_item(f"OWNER#{home}", f"USERSTREAM#{user_id}")
        if not mapping or mapping.get("active") is not True:
            continue
        key = _require_uuid(mapping.get("channelKey"), "channelKey")
        reverse = _get_item(f"STREAM#{key}", "META")
        if not reverse or reverse.get("active") is not True or reverse.get("ownerId") != home or reverse.get("userId") != user_id:
            continue
```

Leave the preceding `member.get("ownerId") != owner_id` recheck as is: the row's `ownerId` is still the room owner.

- [ ] **Step 4: Run the projector suite**

Run: `cd apps/infra/lambda/messaging-projector && python3 -m pytest test_index.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lambda/messaging-projector/index.py apps/infra/lambda/messaging-projector/test_index.py
git commit -m "fix(projector): private hints and push resolve a member's HOME stream"
```

---

### Task 9: Skill reference + backend PR

**Files:**
- Modify: `apps/infra/openclaw/skills/lightbulb/references/surfaces.md` (surfaces table)
- Modify: `apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md` (anchor list)

- [ ] **Step 1: Add the surface row**

In `surfaces.md`, add this row to the table directly after the "Conversation list" row:

```markdown
| Lightbulb channel | flickmeon.ai/chat — the channel named `lightbulb-<workspace slug>` | A channel the platform creates in every workspace and shares with the Lightbulb team: every human in the owner's workspace, every human at Lightbulb, and you (the CEO) in **Mentions** mode. It carries a **Shared** marker. Lightbulb staff read and write there; treat it as the owner's line to the people who run the platform — product questions, bugs, requests. Membership and the name are managed by the platform (no add, remove, leave or rename), and nothing said there is copied anywhere else. You are not the support desk in it: answer when @mentioned, otherwise let the humans talk. |
```

- [ ] **Step 2: Add the maintenance anchor**

In `MAINTENANCE.md`, after the "Conversation list" anchor block, add:

```text
  - Lightbulb channel ........ references/surfaces.md
                              <- apps/backend/core/services/messaging.py (ensure_vendor_room, on_org_member_joined,
                                 is_vendor_room / _deny_vendor_human_change); core/config.py VENDOR_ORG_ID;
                                 apps/frontend/src/components/messaging/{RoomDirectoryRow,RoomHeader,RoomPanel}.tsx (Shared marker, gates)
```

- [ ] **Step 3: Run the full backend + infra test suites**

Run: `cd apps/backend && uv run pytest tests/ -q -x` and `cd apps/infra && npx jest`
Expected: PASS. Then `cd apps/backend && uv run ruff check . && uv run ruff format --check .` clean.

- [ ] **Step 4: Commit**

```bash
git add apps/infra/openclaw/skills/lightbulb/references/surfaces.md apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md
git commit -m "docs(skill): the Lightbulb shared channel"
```

Push and the PR are NOT part of this task (controller ruling): they happen at the finish, after the whole-branch review. PR title for then: "feat(messaging): vendor shared channel (backend)".

---

### Task 10: Clients subscribe with the room's workspace

**Files:**
- Modify: `packages/shared/src/messaging/types.ts:20-42` (`RoomSummary`), `packages/shared/src/messaging/useRooms.ts:50,244-255`
- Modify: `apps/frontend/src/hooks/useLiveEvents.tsx:28-31,83`
- Modify: `apps/mobile/src/providers/live-events.tsx:28-31,132-134`
- Test: `apps/frontend/src/hooks/__tests__/useLiveEvents.test.tsx`

Start a second branch for the client PR: `git switch -c feat/vendor-shared-channel-clients origin/main` in a fresh worktree (`git worktree add ../lb-wt.nosync/vendor-shared-channel-clients -b feat/vendor-shared-channel-clients origin/main`) — or, if the backend PR has not merged yet, branch from `feat/vendor-shared-channel` and rebase later.

**Interfaces:**
- Produces: `RoomSummary.workspaceId?: string | null`, `RoomSummary.shared?: boolean`; `ActiveRoomSubscription.workspaceId?: string | null` (web and mobile); `claimActiveRoomSubscription({ roomId, deliveryKey, workspaceId })`.

- [ ] **Step 1: Write the failing web test**

In `apps/frontend/src/hooks/__tests__/useLiveEvents.test.tsx`, find the test that asserts `shared.roomChannel` is `` `/ws/${WORKSPACE_ID}/rooms/room-a/key-a` `` and add a sibling test right after it, reusing that test's render/claim scaffolding verbatim but claiming:

```ts
      releaseFirstRoom = live.claimActiveRoomSubscription({ roomId: "room-c", deliveryKey: "key-c", workspaceId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" });
```

and asserting:

```ts
    await waitFor(() => expect(shared.roomChannel).toBe("/ws/cccccccc-cccc-4ccc-8ccc-cccccccccccc/rooms/room-c/key-c"));
```

Name it `"subscribes a shared room on ITS workspace segment, not the caller's"`.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/hooks/__tests__/useLiveEvents.test.tsx -t "ITS workspace"`
Expected: FAIL — path uses the config workspace (and a type error on `workspaceId`).

- [ ] **Step 3: Implement**

`packages/shared/src/messaging/types.ts`, append to `RoomSummary`:

```ts
  /**
   * The room's own AppSync workspace segment. A shared channel lives in the
   * owning workspace, so the live channel path must use this, not the caller's
   * live-config workspace. Absent on rows the backend has not stamped yet.
   */
  workspaceId?: string | null;
  /** The platform-managed channel shared with the Lightbulb team. */
  shared?: boolean;
```

`packages/shared/src/messaging/useRooms.ts`: change the dependency type to

```ts
  claimActiveRoomSubscription(room: { roomId: string; deliveryKey: string; workspaceId?: string | null }): () => void;
```

and the subscription block to

```ts
  const subscription = activeRoom && deliveryOverride?.roomId === activeRoom.roomId && deliveryOverride.roomRevision >= activeRoom.roomRevision
    ? { roomId: activeRoom.roomId, deliveryKey: deliveryOverride.deliveryKey, workspaceId: activeRoom.workspaceId ?? null }
    : activeRoom ? { roomId: activeRoom.roomId, deliveryKey: activeRoom.deliveryKey, workspaceId: activeRoom.workspaceId ?? null } : null;
  const subscriptionRoomId = subscription?.roomId ?? null;
  const subscriptionDeliveryKey = subscription?.deliveryKey ?? null;
  const subscriptionWorkspaceId = subscription?.workspaceId ?? null;

  useEffect(() => {
    if (!subscribeToRoom || accessRemoved || !subscriptionRoomId || !subscriptionDeliveryKey) return;
    return claimActiveRoomSubscription({
      roomId: subscriptionRoomId,
      deliveryKey: subscriptionDeliveryKey,
      workspaceId: subscriptionWorkspaceId,
    });
  }, [claimActiveRoomSubscription, subscriptionRoomId, subscriptionDeliveryKey, subscriptionWorkspaceId, accessRemoved, subscribeToRoom]);
```

`apps/frontend/src/hooks/useLiveEvents.tsx`:

```ts
export interface ActiveRoomSubscription {
  roomId: string;
  deliveryKey: string;
  /** The room's workspace segment; falls back to the caller's for rows without one. */
  workspaceId?: string | null;
}
```

and

```ts
  const roomChannel = config && activeRoom ? `/ws/${activeRoom.workspaceId ?? config.workspaceId}/rooms/${activeRoom.roomId}/${activeRoom.deliveryKey}` : null;
```

`apps/mobile/src/providers/live-events.tsx`: the same two edits (interface + `${activeRoom.workspaceId ?? config.workspaceId}`).

- [ ] **Step 4: Run the tests and typecheck**

Run: `cd apps/frontend && pnpm vitest run src/hooks/__tests__/useLiveEvents.test.tsx && pnpm tsc --noEmit -p .` and `cd packages/shared && pnpm vitest run src/messaging/useRooms.test.tsx` and `cd apps/mobile && pnpm tsc --noEmit`.
Expected: PASS. If `useRooms.test.tsx` asserts the claim's exact argument object, extend the expected object with `workspaceId: null`.

- [ ] **Step 5: Commit**

```bash
git add packages/shared/src/messaging/types.ts packages/shared/src/messaging/useRooms.ts apps/frontend/src/hooks/useLiveEvents.tsx apps/frontend/src/hooks/__tests__/useLiveEvents.test.tsx apps/mobile/src/providers/live-events.tsx packages/shared/src/messaging/useRooms.test.tsx
git commit -m "feat(live): subscribe a room on its own workspace segment"
```

---

### Task 11: Web "Shared" marker and managed-room gates

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomDirectoryRow.tsx:52`, `RoomHeader.tsx:86,129`, `RoomPanel.tsx:324,595,614`, `RoomDirectory.tsx:399`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomDirectory.test.tsx`, `__tests__/RoomHeader.test.tsx`

**Interfaces:**
- Consumes: `RoomSummary.shared` (Task 10).

- [ ] **Step 1: Write the failing tests**

Append to `RoomDirectory.test.tsx`:

```tsx
it("marks a shared channel and offers no Leave on it", async () => {
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={vi.fn()} onMarkUnread={vi.fn()} onLeaveRoom={vi.fn()} rooms={[
      { ...base, roomId: "own", name: "Launch" },
      { ...base, roomId: "ext", name: "lightbulb-acme", shared: true },
    ]} />);
  const shared = screen.getByRole("button", { name: /^lightbulb-acme/ });
  expect(shared).toHaveTextContent("Shared");
  expect(within(shared).getByText("Shared")).toHaveAttribute("title", "Shared between this workspace and Lightbulb");
  expect(screen.getByRole("button", { name: /^Launch/ })).not.toHaveTextContent("Shared");
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Launch" }));
  expect(await screen.findByRole("menuitem", { name: /Leave/ })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for lightbulb-acme" }));
  expect(screen.queryByRole("menuitem", { name: /Leave/ })).not.toBeInTheDocument();
});
```

(`within` is imported from `@testing-library/react` in that file already; add it to the import if not.) Check the exact accessible name of the Leave item in `RoomDirectoryRow.tsx:73-80` and match the regex to it.

Append to `RoomHeader.test.tsx`:

```tsx
it("marks a shared channel", () => {
  render(<RoomHeader {...props} room={{ ...room, shared: true }} />);
  expect(screen.getByText("Shared")).toHaveAttribute("title", "Shared between this workspace and Lightbulb");
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/frontend && pnpm vitest run src/components/messaging/__tests__/RoomDirectory.test.tsx src/components/messaging/__tests__/RoomHeader.test.tsx -t "shared"`
Expected: FAIL — no "Shared" text.

- [ ] **Step 3: Implement**

`RoomDirectoryRow.tsx`, after the `Muted` tag:

```tsx
      {room.shared && <span className="room-directory-tag" title="Shared between this workspace and Lightbulb">Shared</span>}
```

`RoomHeader.tsx`, after the `<h2 …>{roomName}</h2>`:

```tsx
      {room?.shared && <span className="room-directory-tag" title="Shared between this workspace and Lightbulb">Shared</span>}
```

and change the Rename item's condition to `onRenameRoom && canEditRoom && !room?.shared`.

`RoomPanel.tsx`:

```ts
  const canAddMembers = visibleRoom?.kind === "channel" && !!onAddMember && !visibleRoom.shared;
```

the Remove button condition becomes `canManageMembers && onRemoveMember && participant.role !== "owner" && !(visibleRoom?.shared && participant.kind === "human")`, and both `onLeaveRoom={onLeaveRoom && canLeaveRoom ? onLeaveRoom : undefined}` sites (header at `:541` passes `onLeaveRoom` bare — change it to the same gated expression) become `onLeaveRoom={onLeaveRoom && canLeaveRoom && !visibleRoom?.shared ? onLeaveRoom : undefined}`.

`RoomDirectory.tsx:399`: `onLeave={onLeaveRoom && room.kind === "channel" && !room.shared ? (trigger) => { … } : undefined}` (keep the existing arrow body).

- [ ] **Step 4: Run the messaging component suites**

Run: `cd apps/frontend && pnpm vitest run src/components/messaging && pnpm tsc --noEmit -p . && pnpm lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/messaging
git commit -m "feat(messaging-ui): Shared marker and platform-managed gates for the vendor channel"
```

---

### Task 12: Mobile marker

**Files:**
- Modify: `apps/mobile/src/components/room/RoomRow.tsx:105`, `apps/mobile/src/app/(app)/room/[roomId]/index.tsx:224-234`
- Create: `apps/mobile/src/components/room/__tests__/RoomRow.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `apps/mobile/src/components/room/__tests__/RoomRow.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react-native";
import type { RoomSummary } from "@lightbulb/shared";

import { RoomRow } from "../RoomRow";

const room = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "r1", name: "launch", kind: "channel", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null,
  unreadCount: 0, updatedAt: "2026-09-14T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null,
  members: [], lastReadSeq: 0, ...over,
});

it("tags a shared channel and nothing else", () => {
  render(<>
    <RoomRow room={room({ roomId: "ext", name: "lightbulb-acme", shared: true })} participants={[]} unread={0} muted={false} onPress={() => {}} />
    <RoomRow room={room({ roomId: "own" })} participants={[]} unread={0} muted={false} onPress={() => {}} />
  </>);
  expect(within(screen.getByTestId("room-row-ext")).getByText("Shared")).toBeTruthy();
  expect(within(screen.getByTestId("room-row-own")).queryByText("Shared")).toBeNull();
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/mobile && pnpm jest src/components/room/__tests__/RoomRow.test.tsx`
Expected: FAIL — no "Shared".

- [ ] **Step 3: Implement**

`RoomRow.tsx`, after the `Muted` tag:

```tsx
      {room.shared && <Text style={styles.mutedTag}>Shared</Text>}
```

`room/[roomId]/index.tsx`, inside the `headerTitle` view after the members-count `Text`:

```tsx
        {rooms.activeRoom?.shared ? (
          <Text style={styles.headerMeta}>Shared with Lightbulb</Text>
        ) : null}
```

- [ ] **Step 4: Run the mobile suites and typecheck**

Run: `cd apps/mobile && pnpm jest src/components/room src/app && pnpm tsc --noEmit && pnpm lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/room/RoomRow.tsx "apps/mobile/src/app/(app)/room/[roomId]/index.tsx" apps/mobile/src/components/room/__tests__/RoomRow.test.tsx
git commit -m "feat(mobile): Shared marker on the vendor channel"
```

---

### Task 13: Client PR, dev verification, cleanup

**Files:**
- Delete: `docs/superpowers/specs/2026-09-14-vendor-shared-channel-design.md`, `docs/superpowers/plans/2026-09-14-vendor-shared-channel.md` (if the backend branch was merged first they are on `main`; delete them in this PR)

- [ ] **Step 1: Delete the planning docs and open the PR**

```bash
git rm docs/superpowers/specs/2026-09-14-vendor-shared-channel-design.md docs/superpowers/plans/2026-09-14-vendor-shared-channel.md
git commit -m "docs: retire the vendor shared channel spec and plan (shipped)"
git push -u origin feat/vendor-shared-channel-clients
gh pr create --repo TheLightbulbCompany/lightbulb --title "feat(messaging): vendor shared channel (web + mobile)" --body-file - <<'EOF'
Client half of the vendor shared channel (backend: the previous PR).

- rooms subscribe on their OWN AppSync workspace segment (`RoomSummary.workspaceId`), falling back to live-config for unstamped rows — safe in either merge order
- "Shared" marker on the directory row, room header, and mobile row/header
- no add/remove/leave/rename controls on the shared channel (the server guards; this is courtesy)
- retires the spec + plan (disposable-plans rule)

Merging ships a mobile OTA (`mobile-ota.yml`).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_013yVPnu47Hgjo5N6gfmHXLu
EOF
```

- [ ] **Step 2: Verify on dev after both dev deploys are green**

With `VENDOR_ORG_ID` set for dev (Task 1 Step 4) and a second dev org (create one at https://dev.flickmeon.ai/onboarding with a test account, or reuse an existing non-founder dev org):

1. Sign in as a member of the second org; the sidebar shows `lightbulb-<slug>` with the Shared tag; the header has no Rename and no Leave; Details has no Add members.
2. Sign in as the dev founder (Clerk sign-in token, CLAUDE.md recipe); the same room is listed with the Shared tag; send a message; it appears for the customer.
3. As the customer, `@ceo …`; the CEO answers from the customer's container (run card then reply).
4. Redeem the second org's join link with a third account; that account appears in the room within a few seconds (webhook) without any reload.
5. Remove that account from the org in Clerk; the room drops it and the remaining members get a `room.subscription.rotated` (the room keeps working after a reload).

Record each step's outcome in the PR body.

- [ ] **Step 3: Promote**

Merge the client PR, watch its dev deploy, then dispatch prod once (`gh workflow run deploy.yml --ref main`) after confirming `gh run list --workflow deploy.yml --limit 10 --json status --jq '[.[]|select(.status!="completed")]|length'` reads `0`. Existing customers get the channel on their next org poll.
