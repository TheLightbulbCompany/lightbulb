# Slack-parity messaging — Phase 3 (sidebar, room header, details) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sidebar the owner's (stars, mute, custom sections with drag reorder, unread-first sort, a row overflow menu, leave), give the room a real header (topic, facepile, star, bell, overflow), and turn the right panel into Details with Members / Pins / Files / Links / About — on five small additive backend endpoints.

**Architecture:** Five backend touches, all additive to the existing single-table canonical room contract and all in `routers/messaging.py` + `core/services/messaging.py` + `core/repositories/messaging_repo.py`. Per-user, per-room preferences (`starred`, `muted`, `sectionId`) are three new attributes on the existing `MEMBER#human#<id>` row; the owner's section list is a **new `PREF#<user>` item** on the existing `OWNER#<owner>` partition (open decision 2, resolved below); `topic` and `pinnedMessageIds` are new attributes on `META`. **No new table, no new GSI, no CDK change, so no `deploy.yml` regeneration.** On the frontend `RoomDirectory.tsx` sheds its grouping into a pure `roomSections.ts` plus a `RoomDirectoryRow.tsx`, and `RoomPanel.tsx` sheds its header and right panel into `RoomHeader.tsx` and `RoomDetailsPanel.tsx`.

**Tech Stack:** FastAPI + Pydantic v2 (`ContractModel`, `alias_generator=to_camel`, `extra="forbid"`), DynamoDB via `messaging_repo`, pytest + moto (`cd apps/backend && uv run pytest`); Next.js 16 / React 19, `radix-ui` (DropdownMenu, Dialog, Tooltip — installed), `@dnd-kit/core` + `@dnd-kit/sortable` + `@dnd-kit/utilities` (already in `apps/frontend/package.json`), Vitest + Testing Library (`cd apps/frontend && pnpm test -- <path>`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-11-slack-parity-messaging-design.md` — the "Sidebar", "Room header", "Details / thread panel" sections, the Phase 3 row of "Phases and acceptance", the Phase-3 rows of "Backend touches", "States and accessibility", "Coordination", and "Open decisions".

**Baseline:** `origin/main` at `0d3eb91b1`. Phases 0–1 shipped (`timeline.ts`, `RoomTimeline.tsx`, `RoomMessageItem.tsx`, `mentions.ts`, `EmojiPicker.tsx`, `messageLinks.ts`, permalinks); Phase 2 (composer) is being planned in parallel by another agent — see Coordination.

---

## Inputs considered — PR #2482 and PR #2483 (Forge / `Isol8Admin`)

Both were opened 2026-09-11 against an older `main`, and **#2483 supersedes #2482**: #2483 carries #2482's entire frontend change (three-way sidebar split, `canRename` split from `canManageMembers`, "Group chat" copy, `types.ts` label) *plus* a backend guard and its tests, and closes issue #2479. #2482 has no backend change at all.

**This plan SUPERSEDES both.** Concrete mismatches with the spec:

1. **Sidebar sections.** Both add a third top-level `"Group chats"` section. The spec enumerates the order exactly — "**Starred** (if any) · **Channels** · **Direct messages** · **Drafts** (if any) · **More**" — and its row anatomy puts group DMs *inside* Direct messages ("avatar for DM, **stacked avatars for group DM**"). Task 7 builds the spec's five groups; a fourth built-in group would then have to be deleted.
2. **Rename lives in the wrong place.** Both keep rename as a "Rename channel" button inside the Details body. The spec's header rule is "overflow (Copy link, **Rename**, Leave, Details)". Task 9 moves it; the PR's control would be removed in the same PR that merged it.
3. **Copy.** Both rewrite `roomKindLabel["group-dm"]` to `"Group chat"` and the create-dialog option to `"Group chat"`. The spec's vocabulary throughout is "group DM" (`roomKindLabel` today reads `"Group message"`). No open decision covers a vocabulary change, and "builder must not invent" applies — Phase 3 keeps `"Group message"`.
4. **Collision surface.** Between them the two PRs touch `RoomDirectory.tsx`, `RoomPanel.tsx`, `MessagingWorkspaceContext.tsx`, `types.ts`, `messaging.css` and `MessagesIntegration.test.tsx` — the exact five files Tasks 6–11 rewrite. Merging either buys one behaviour (Task 3 below) at the cost of a guaranteed rebase of every Phase 3 task.

**What is kept, with credit:** #2483's backend finding is real and correct — `patch_room` today lets a DM owner set a `name` on a DM, which `roomDisplayName` then renders instead of the counterpart's name, with no UI to clear it. Task 3, Step 1 reproduces that guard (`meta.get("kind") == "dm"` → `ROOM_FORBIDDEN`) and its two service tests, and keeps rename open to `group-dm`.

**Action for the controller (Task 12):** close **#2482** as superseded by #2483, and **#2483** as superseded by this plan's Task 3, linking the Phase 3 PR. Do not merge or modify either PR at any point.

---

## Decisions resolved (spec "Open decisions")

- **Open decision 2 — sections storage row: RESOLVED as a new `PREF#<user>` item**, not the user-stream row. `messaging_repo.ensure_user_stream` rewrites `OWNER#<owner>` / `USERSTREAM#<user>` inside a `_transact` whose reactivation arm is conditioned on `active = :f AND channelKey = :old`, and `deactivate_user_stream` flips both mappings on access revocation. A sections write on that row would race the delivery-key lifecycle and would ride on a row whose entire purpose is a rotating key. `PREF#<user>` sits in the same `OWNER#<owner>` partition (no new index, no new table, one extra `GetItem` that `list_rooms` already pays a partition read for) and is untouched by revocation.
- **Open decision 1** was resolved in the spec itself (`@Display Name`). **Open decision 3** (`⌘K` palette) belongs to Phase 2 and is out of scope here.

## Assumptions (flagged; spec defaults used)

1. **`room.updated` → `room.metadata.changed`.** The spec's pins row says "`room.updated` live event". No such event type exists; the room-metadata event in `LIVE_EVENT_TYPES` is `room.metadata.changed`, published by `_publish_room_metadata`. Task 4 reuses it rather than inventing a type. **Spec contradicted by code.**
2. **Sections are returned on `GET /messaging/rooms`, not a new `GET`.** The spec's backend table names only `PUT /messaging/me/sections`. A client that can write sections must read them; folding them into `RoomListResponse.sections` is one fewer round trip and keeps sections and the rooms that reference them atomic in one response. No new route.
3. **`sectionId` is not validated against the section list.** A room pointing at a deleted section falls back to its default group client-side (`roomSections.ts`). Server-side referential validation would cost a `PREF#` read on every star/mute write for no user-visible gain.
4. **`POST /leave` accepts non-owner humans in any room kind**, per the spec's backend table ("non-owner humans"); the spec's *sidebar* rule ("Leave (channels only, not owner)") governs where the menu item is **shown** (Task 8), not what the endpoint accepts.
5. **Collapse state and sort order are local**, under `lightbulb.messaging.sidebar.<ownerScopeId>`. The spec makes `starred`/`muted`/`sectionId` server-side and is silent on collapse/sort; they are per-device view state, and the Phase 3 acceptance ("a starred room survives a reload on another device") names only stars.
6. **Backward history paging (`beforeSeq`) is NOT built.** The spec makes it conditional — "added only if a real room exceeds ~500 messages". No founder room does.
7. **Drafts row is Phase 2's.** The spec's sidebar order lists Drafts; per-room drafts are a Phase 2 deliverable. Task 7 leaves the slot between Direct messages and More empty and Phase 2 fills it. **Coordinate before touching `MessagingWorkspaceContext`'s draft state.**

---

## Global Constraints

Every task's requirements implicitly include this section.

- **Design system** (`apps/frontend/.interface-design/system.md`, `DESIGN.md`): white/cream ground, **borders only, no shadows** except the shipped `.room-message-actions` one; **4px spacing rhythm**; radii 4–8px from `messaging.css`; Source Sans via `var(--font-sans)`.
- **No colour literals outside `globals.css`** — tokens only (`var(--foreground)`, `var(--muted-foreground)`, `var(--border)`, `var(--info)`, `var(--cream-deeper)`, …). `node scripts/brand-tokens-scan.mjs src` must print `0` (run from `apps/frontend`).
- **"Never colour alone":** every status keeps its word or icon. A state colour marks text, an icon, a border or a thin rule — **never a fill**.
- Every new control has hover, focus-visible, disabled, pending, and error states; every list has loading, empty, and error states. `.room-directory :is(button, input, select):focus-visible` and `.room-panel :is(button, …):focus-visible` already apply `outline: 2px solid var(--ring)`.
- Popovers and overflow menus are the shipped `@/components/ui/dropdown-menu` (radix) with roving focus and Escape. Touch targets ≥44px at ≤859px (the existing `@media (max-width: 859px)` rule covers `.room-panel button, .room-directory button`).
- `prefers-reduced-motion: reduce` already zeroes transitions/animations under `.room-panel *, .room-directory *`; any drag feedback must respect it.
- Screen readers: list groups are `<section aria-label>` + `<h3>`; the pinned count is text, not a colour; drag handles expose `aria-label` and the `@dnd-kit` keyboard sensor.
- **No new dependencies. No feature flags, no migration periods, no dual-write paths, no compatibility shims.**
- **Never use `sleep` or `tail` in bash.** Use `gh run watch` for CI and the Read tool for files.
- Backend tests: pytest under `apps/backend/tests` (`cd apps/backend && uv run pytest tests/... -v`). Repository tests use the `table` fixture in `tests/unit/repositories/test_messaging_repo.py` (moto, `test-messaging`, `by-member` GSI); service tests use `monkeypatch` + `AsyncMock` as in `tests/unit/services/test_messaging_service_review_findings.py`.
- Frontend tests: Vitest + Testing Library. Clock-dependent fixtures use `vi.useFakeTimers({ toFake: ["Date"] })` and **local-time constructors** (`new Date(2026, 8, 11, 10, 0)`), never UTC strings.
- `localStorage` keys live under the **`lightbulb.messaging.`** prefix (`RECENT_EMOJI_KEY = "lightbulb.messaging.recentEmoji"` is the shipped precedent). Every read and write is wrapped in try/catch.
- **Any CDK change must regenerate and commit `.github/workflows/deploy.yml` in the same PR** (`cd apps/infra && npx cdk synth`). **Phase 3 makes no CDK change** — the messaging table (`api-stack.ts:521`) is `pk`/`sk` with one `by-member` GSI projecting ALL, and every new attribute lives on an existing item shape. If a task finds itself editing `apps/infra/lib/`, stop and re-read this plan.
- `packages/shared` is consumed verbatim by **mobile**. After any change there, `pnpm --filter @lightbulb/mobile exec tsc --noEmit` must stay green.
- Never log message bodies, topics, or room names. Never send message text anywhere new.
- Existing tests must keep passing after every task: `apps/frontend/src/components/messaging/__tests__/`, `apps/frontend/src/components/workspace/__tests__/`, `apps/frontend/src/hooks/__tests__/useRooms.test.tsx`, `apps/backend/tests/unit/{repositories/test_messaging_repo.py,services/test_messaging_service_review_findings.py}`.
- Commits: conventional prefix (`feat(messaging): …`, `refactor(messaging): …`), ending with the attribution block from the session's system reminder.

---

## Coordination

- **`core/services/openclaw_work.py` is NOT touched by Phase 3.** Another agent owns an "ambient-turn cost fence" change in `openclaw_work.py` and `messaging_repo.py`. Phase 3's only shared file is `messaging_repo.py`, and every change there is **additive**: new module-level helpers (`pref_sk`, `get_user_prefs`, `put_user_sections`, `set_member_prefs`, `set_pin`) appended at the end of their existing sections, plus exactly two edits to existing functions — `patch_room_name` is **renamed to `patch_room_meta` with a generalised `updates` dict** (Task 3) and `mark_unread` **gains a keyword-only `from_seq` parameter with a `None` default** (Task 5). Neither is called from `openclaw_work.py` (verified: its only repo calls are `list_messages_before`, `get_agent_delivery`, `get_room`, `list_messages_between`, `get_member`, `advance_agent_delivery`, `list_members`, `get_operation`, `claim_operation`, `release_operation`, `complete_operation`). If a merge conflict appears in `messaging_repo.py`, it is textual, not semantic — take both sides.
- **Phase 2 (composer) is being planned in parallel** and owns `RoomComposer.tsx`, the draft state in `MessagingWorkspaceContext.tsx`, the Drafts sidebar row, and the `⌘K` switcher. Phase 3 touches `MessagingWorkspaceContext.tsx` only to thread new callbacks into `directoryProps` / `roomProps` (Task 6) and never touches `draft`, `mentions`, `threadDraft`, `threadMentions`, or `clientMessageId`.
- Files under `components/messaging/**`, `hooks/useRooms.ts`, `components/workspace/WorkspaceSidebar.tsx`, `packages/shared/src/{messaging,live}/**`, backend `routers/messaging.py`, `core/services/messaging.py`, `core/repositories/messaging_repo.py` are Claude-owned for this program.
- **This spec and this plan are DELETED when Phase 3 ships** (repo planning-docs policy: plans are disposable, git history and the PR body are the durable record). Phase 4 gets its own specs.

---

## File structure

| File | Responsibility |
|---|---|
| `apps/backend/core/repositories/messaging_repo.py` (modify) | `pref_sk`, `get_user_prefs`, `put_user_sections`, `set_member_prefs`, `patch_room_meta` (was `patch_room_name`), `set_pin`, `mark_unread(..., from_seq=)`. |
| `apps/backend/core/services/messaging.py` (modify) | `RoomSection`, `MemberPrefsRequest`, `SectionsRequest`, `MarkUnreadRequest`; `topic`/`starred`/`muted`/`sectionId` on `RoomSummary`, `pinnedMessageIds` on `RoomDetail`, `sections` on `RoomListResponse`; `patch_room_me`, `put_sections`, `leave_room`, `set_pin`; `room.member.updated` in `USER_LIVE_EVENT_TYPES`. |
| `apps/backend/routers/messaging.py` (modify) | `PATCH /rooms/{id}/me`, `PUT /me/sections`, `POST /rooms/{id}/leave`, `PUT|DELETE /rooms/{id}/pins/{messageId}`, body on `POST /rooms/{id}/unread`. |
| `packages/shared/src/messaging/{types,client}.ts` (modify) | New contract fields + `setMemberPrefs`, `putSections`, `leaveRoom`, `setPin`, `markUnread(roomId, fromSeq?)`. |
| `packages/shared/src/live/types.ts` (modify) | `room.member.updated` in `LiveEventType` / `LIVE_EVENT_TYPES` + `RoomMemberUpdated`. |
| `apps/frontend/src/hooks/useRooms.ts` (modify) | `sections`, `setRoomPrefs`, `saveSections`, `leaveRoom`, `setPin`, `markUnread(roomId, fromSeq?)`; `room.member.updated` → `mutateRooms()`. |
| `apps/frontend/src/components/messaging/roomSections.ts` (create) | Pure: `groupRooms(rooms, sections, options)` → ordered groups; `sortRooms`; `DEFAULT_GROUPS`. No React. |
| `apps/frontend/src/components/messaging/RoomDirectoryRow.tsx` (create) | One sidebar row: icon/avatar/stacked avatars, name, unread badge, mute styling, overflow menu. |
| `apps/frontend/src/components/messaging/sidebarPrefs.ts` (create) | `readSidebarPrefs(scope)` / `writeSidebarPrefs(scope, prefs)` over `lightbulb.messaging.sidebar.<scope>`. |
| `apps/frontend/src/components/messaging/RoomDirectory.tsx` (modify) | Composes groups + rows; sort control; collapse; `@dnd-kit` section reorder; Manage sections dialog. |
| `apps/frontend/src/components/messaging/RoomHeader.tsx` (create) | Kind icon, name, topic (click-to-edit), search slot, facepile, star, bell, overflow. |
| `apps/frontend/src/components/messaging/RoomDetailsPanel.tsx` (create) | Details tabs: Members · Pins · Files · Links · About. |
| `apps/frontend/src/components/messaging/RoomPanel.tsx` (modify) | Uses `RoomHeader` + `RoomDetailsPanel`; new props; pin + mark-unread-from-here in the message menu. |
| `apps/frontend/src/components/messaging/RoomMessageContent.tsx` (modify) | `menuExtras` prop so non-owners get a "more" menu. |
| `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (modify) | Threads the new callbacks into `directoryProps` / `roomProps`. |
| `apps/frontend/src/components/messaging/messaging.css` (modify) | Group header, star/bell, topic, facepile, tab bar, drag handle, pins list. |
| `apps/infra/openclaw/skills/lightbulb/references/surfaces.md` + `MAINTENANCE.md` (modify) | Sidebar (Task 8), header (Task 9), pins/details (Task 11). |

**Types reused everywhere.** Backend: `RoomSummary`, `RoomDetail`, `RoomMember`, `MemberRef`, `ApiProblem(code, message)`, `require_room_member(owner_id, user_id, room_id) -> (meta, member)`, `require_room_admin(...) -> (meta, member)`, `_room_detail(meta, user_id, member=None)`, `_publish_room_metadata(meta)`, `make_live_event(type, payload, room_id=..., room_revision=...)`, `get_live_event_publisher().publish_user(owner_id, user_id, event)`. Frontend: `RoomSummary` (`roomId, name, kind, roomRevision, lastMessageSeq, lastMessageAt, unreadCount, updatedAt`), `RoomMember` (`kind, id, displayName, mentionHandle, role, listenMode, joinedAt, joinedSeq`), `DirectoryRoom = RoomSummary & { participants }`, `RoomParticipant = RoomMember & { agentActivity? }`, `RoomMessage = CanonicalRoomMessage & { contextIds?, run? }`.

---

# Backend (Tasks 1–5) — all additive, no CDK change

### Task 1: Per-user room preferences — `starred`, `muted`, `sectionId`

*Suggested model: sonnet — scoped, one row, one endpoint, contract additions that later tasks depend on.*

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py` (append `set_member_prefs` after `upsert_member`, ~line 886)
- Modify: `apps/backend/core/services/messaging.py` (`USER_LIVE_EVENT_TYPES` L68, `RoomSummary` L115, `MemberPrefsRequest` near `PatchRoomRequest` L164, `_room_detail` L795, `list_rooms` L820, new `patch_room_me` after `patch_room` L895)
- Modify: `apps/backend/routers/messaging.py` (import + route after `patch_room`, L108)
- Test: `apps/backend/tests/unit/repositories/test_messaging_repo.py` (append), `apps/backend/tests/unit/services/test_messaging_service_review_findings.py` (append)

**Interfaces:**
- Produces (repo):
  ```python
  async def set_member_prefs(room_id: str, kind: str, member_id: str, updates: dict[str, Any]) -> dict
  ```
  `updates` keys are a subset of `{"starred", "muted", "sectionId"}`; returns the updated member row. Raises `RoomNotFound` when the member row is absent. Does **not** bump `META.revision` — a star is one person's view, and bumping the revision would churn `roomRevision` for every member and invalidate every client's room cache on every toggle.
- Produces (service):
  ```python
  class MemberPrefsRequest(ContractModel):
      starred: bool | None = None
      muted: bool | None = None
      section_id: uuid.UUID | None = None
  async def patch_room_me(auth: AuthContext, room_id: str, request: MemberPrefsRequest) -> RoomDetail
  ```
  `RoomSummary` gains `starred: bool`, `muted: bool`, `section_id: str | None` (wire: `starred`, `muted`, `sectionId`). Absent-field semantics use `request.model_fields_set`, so `{"sectionId": null}` clears the section and an omitted `sectionId` leaves it alone.
- Produces (router): `PATCH /messaging/rooms/{room_id}/me` → `RoomDetail`.

- [ ] **Step 1: Write the failing repository test**

Append to `apps/backend/tests/unit/repositories/test_messaging_repo.py`:

```python
@pytest.mark.asyncio
async def test_set_member_prefs_writes_only_named_fields_and_leaves_revision_alone(table):
    room_id, meta = await _room()

    updated = await repo.set_member_prefs(room_id, "human", USER_A, {"starred": True, "sectionId": "sec-1"})
    assert updated["starred"] is True
    assert updated["sectionId"] == "sec-1"
    assert updated.get("muted") in (None, False)

    only_mute = await repo.set_member_prefs(room_id, "human", USER_A, {"muted": True})
    assert only_mute["muted"] is True
    assert only_mute["starred"] is True
    assert only_mute["sectionId"] == "sec-1"

    cleared = await repo.set_member_prefs(room_id, "human", USER_A, {"sectionId": None})
    assert cleared["sectionId"] is None
    assert cleared["starred"] is True

    assert (await repo.get_room(room_id))["revision"] == meta["revision"]
    listed = await repo.list_rooms_for_member(OWNER, "human", USER_A)
    assert listed[0]["member"]["starred"] is True


@pytest.mark.asyncio
async def test_set_member_prefs_rejects_a_member_that_is_not_in_the_room(table):
    room_id, _ = await _room()
    with pytest.raises(repo.RoomNotFound):
        await repo.set_member_prefs(room_id, "human", USER_B, {"starred": True})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k set_member_prefs -q`
Expected: FAIL — `AttributeError: module 'core.repositories.messaging_repo' has no attribute 'set_member_prefs'`.

- [ ] **Step 3: Implement `set_member_prefs`**

Append to `apps/backend/core/repositories/messaging_repo.py` immediately after `upsert_member` (before `def _live_item`):

```python
_MEMBER_PREF_FIELDS = ("starred", "muted", "sectionId")


async def set_member_prefs(room_id: str, kind: str, member_id: str, updates: dict[str, Any]) -> dict:
    """Per-user, per-room view preferences on the member row.

    Deliberately does NOT bump ``META.revision``: starring a room is one
    person's view of it, and a revision bump would invalidate every other
    member's cached room on every toggle.
    """
    named = {k: v for k, v in updates.items() if k in _MEMBER_PREF_FIELDS}
    if not named:
        return await get_member(room_id, kind, member_id) or {}
    names = {f"#{k}": k for k in named}
    values = {f":{k}": _ddb_safe(v) for k, v in named.items()}
    expression = "SET " + ", ".join(f"#{k} = :{k}" for k in named)
    try:
        response = await run_in_thread(
            _table().update_item,
            Key={"pk": room_pk(room_id), "sk": member_sk(kind, member_id)},
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(sk)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RoomNotFound(f"{room_id}:{kind}:{member_id}") from exc
        raise
    return _plain(response["Attributes"])
```

- [ ] **Step 4: Run the repository test to verify it passes**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k set_member_prefs -q`
Expected: 2 passed.

- [ ] **Step 5: Write the failing service test**

Append to `apps/backend/tests/unit/services/test_messaging_service_review_findings.py`:

```python
@pytest.mark.asyncio
async def test_patch_room_me_writes_only_named_prefs_and_notifies_the_caller_only(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 4}
    member = {"kind": "human", "id": "user_owner", "role": "owner", "joinedSeq": 0}
    set_prefs = AsyncMock(return_value={**member, "muted": True})
    detail = AsyncMock(return_value="detail")
    publish_user = AsyncMock(return_value=True)
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, member)))
    monkeypatch.setattr(messaging.messaging_repo, "set_member_prefs", set_prefs)
    monkeypatch.setattr(messaging, "_room_detail", detail)
    monkeypatch.setattr(
        messaging, "get_live_event_publisher", lambda: type("P", (), {"publish_user": publish_user})()
    )

    result = await messaging.patch_room_me(auth, "room-1", messaging.MemberPrefsRequest(muted=True))

    assert result == "detail"
    set_prefs.assert_awaited_once_with("room-1", "human", "user_owner", {"muted": True})
    publish_user.assert_awaited_once()
    owner_arg, user_arg, event = publish_user.await_args.args
    assert (owner_arg, user_arg) == ("org_1", "user_owner")
    assert event["type"] == "room.member.updated"
    assert event["payload"] == {"roomId": "room-1"}


@pytest.mark.asyncio
async def test_patch_room_me_clears_a_section_with_an_explicit_null(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 4}
    member = {"kind": "human", "id": "user_owner", "role": "owner", "joinedSeq": 0}
    set_prefs = AsyncMock(return_value=member)
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, member)))
    monkeypatch.setattr(messaging.messaging_repo, "set_member_prefs", set_prefs)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    monkeypatch.setattr(
        messaging, "get_live_event_publisher", lambda: type("P", (), {"publish_user": AsyncMock()})()
    )

    await messaging.patch_room_me(
        auth, "room-1", messaging.MemberPrefsRequest.model_validate({"sectionId": None})
    )
    set_prefs.assert_awaited_once_with("room-1", "human", "user_owner", {"sectionId": None})


@pytest.mark.asyncio
async def test_room_member_updated_is_a_recognised_user_live_event():
    assert "room.member.updated" in messaging.USER_LIVE_EVENT_TYPES
    assert "room.member.updated" in messaging.LIVE_EVENT_TYPES
    assert "room.member.updated" not in messaging.ROOM_LIVE_EVENT_TYPES
```

- [ ] **Step 6: Run it to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/services/test_messaging_service_review_findings.py -k "patch_room_me or room_member_updated" -q`
Expected: FAIL — `AttributeError: module 'core.services.messaging' has no attribute 'patch_room_me'`.

- [ ] **Step 7: Implement the service and contract changes**

In `apps/backend/core/services/messaging.py`:

(a) add `"room.member.updated",` to the `USER_LIVE_EVENT_TYPES` frozenset (L68–82) — it is delivered to one user's private stream, so it belongs there and **not** in `ROOM_LIVE_EVENT_TYPES`.

(b) extend `RoomSummary` (L115) with three fields, keeping the existing ones untouched:

```python
class RoomSummary(ContractModel):
    room_id: uuid.UUID
    name: str | None
    kind: RoomKind
    room_revision: int = Field(ge=1)
    last_message_seq: int = Field(ge=0)
    last_message_at: datetime | None
    unread_count: int = Field(ge=0)
    updated_at: datetime
    starred: bool = False
    muted: bool = False
    section_id: str | None = None
```

(c) add the request model beside `PatchRoomRequest` (L164):

```python
class MemberPrefsRequest(ContractModel):
    starred: bool | None = None
    muted: bool | None = None
    section_id: uuid.UUID | None = None

    def named_updates(self) -> dict[str, Any]:
        """Only the fields the caller actually sent — an omitted field is left
        alone, an explicit null clears it."""
        out: dict[str, Any] = {}
        if "starred" in self.model_fields_set:
            out["starred"] = bool(self.starred)
        if "muted" in self.model_fields_set:
            out["muted"] = bool(self.muted)
        if "section_id" in self.model_fields_set:
            out["sectionId"] = str(self.section_id) if self.section_id else None
        return out
```

(d) in `_room_detail` (L795), pass the member's prefs through — the `member` row is already loaded there:

```python
    return RoomDetail(
        room_id=meta["roomId"],
        name=meta.get("name"),
        kind=meta["kind"],
        room_revision=int(meta["revision"]),
        last_message_seq=int(meta["lastSeq"]),
        last_message_at=meta.get("lastMessageAt"),
        unread_count=max(0, int(meta["lastSeq"]) - floor),
        updated_at=meta["updatedAt"],
        starred=bool(member.get("starred")),
        muted=bool(member.get("muted")),
        section_id=member.get("sectionId"),
        delivery_key=meta["deliveryKey"],
        members=[_room_member(row) for row in members],
        last_read_seq=last_read,
        created_at=meta["createdAt"],
    )
```

(e) in `list_rooms` (L820), add the same three arguments to the `RoomSummary(...)` construction inside the loop, reading them off the `member` row that the loop already fetched (`starred=bool(member.get("starred")), muted=bool(member.get("muted")), section_id=member.get("sectionId"),`).

(f) add the command after `patch_room` (L895–903):

```python
async def patch_room_me(auth: AuthContext, room_id: str, request: MemberPrefsRequest) -> RoomDetail:
    """The caller's own view preferences for one room. Any member may set them."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    updates = request.named_updates()
    if updates:
        try:
            member = await messaging_repo.set_member_prefs(room_id, "human", auth.user_id, updates)
        except messaging_repo.RoomNotFound as exc:
            raise ApiProblem("ROOM_FORBIDDEN", "You no longer have access to this room.") from exc
        from core.services.live_events import get_live_event_publisher

        await get_live_event_publisher().publish_user(
            owner_id,
            auth.user_id,
            make_live_event("room.member.updated", {"roomId": meta["roomId"]}),
        )
    return await _room_detail(meta, auth.user_id, member)
```

- [ ] **Step 8: Add the route**

In `apps/backend/routers/messaging.py`, add `MemberPrefsRequest,` to the `from core.services.messaging import (...)` block (alphabetically beside `MessagePage`), and add after `patch_room` (L108):

```python
@router.patch("/messaging/rooms/{room_id}/me", response_model=RoomDetail)
async def patch_room_me(
    room_id: str,
    request: MemberPrefsRequest,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.patch_room_me(auth, room_id, request)
```

- [ ] **Step 9: Run the messaging backend suite**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py tests/unit/services/test_messaging_service_review_findings.py -q && uv run ruff check core/services/messaging.py core/repositories/messaging_repo.py routers/messaging.py`
Expected: all passed, ruff clean. (A pre-existing reaction-concurrency flake reruns green in isolation; rerun that one test alone before investigating.)

- [ ] **Step 10: Commit**

```bash
git add apps/backend/core/repositories/messaging_repo.py apps/backend/core/services/messaging.py apps/backend/routers/messaging.py apps/backend/tests/unit/repositories/test_messaging_repo.py apps/backend/tests/unit/services/test_messaging_service_review_findings.py
git commit -m "feat(messaging): per-user star, mute and section on the member row"
```

---

### Task 2: Owner sections — `PREF#<user>` row and `PUT /messaging/me/sections`

*Suggested model: sonnet — one new item shape, one endpoint, validation rules.*

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py` (`pref_sk` beside `userstream_sk` L127; `get_user_prefs` / `put_user_sections` in the "private user streams" section)
- Modify: `apps/backend/core/services/messaging.py` (`RoomSection`, `SectionsRequest`, `RoomListResponse`, `list_rooms`, `put_sections`)
- Modify: `apps/backend/routers/messaging.py`
- Test: both messaging test files (append)

**Interfaces:**
- Consumes: Task 1's `RoomSummary.section_id`.
- Produces (repo):
  ```python
  def pref_sk(user_id: str) -> str            # f"PREF#{user_id}"
  async def get_user_prefs(owner_id: str, user_id: str) -> dict | None
  async def put_user_sections(owner_id: str, user_id: str, sections: list[dict]) -> list[dict]
  ```
  `sections` entries are `{"id": str, "name": str}`, stored in order.
- Produces (service):
  ```python
  MAX_SECTIONS = 20
  SECTION_NAME_MAX_CHARS = 40
  class RoomSection(ContractModel):
      id: uuid.UUID
      name: str = Field(min_length=1, max_length=SECTION_NAME_MAX_CHARS)
  class SectionsRequest(ContractModel):
      sections: list[RoomSection] = Field(max_length=MAX_SECTIONS)
  async def put_sections(auth: AuthContext, request: SectionsRequest) -> RoomListResponse
  ```
  `RoomListResponse` gains `sections: list[RoomSection]`, so `GET /messaging/rooms` and `PUT /messaging/me/sections` both return the whole sidebar state (Assumption 2).
- Produces (router): `PUT /messaging/me/sections` → `RoomListResponse`.

- [ ] **Step 1: Write the failing repository test**

Append to `apps/backend/tests/unit/repositories/test_messaging_repo.py`:

```python
@pytest.mark.asyncio
async def test_user_sections_round_trip_preserve_order_and_replace_wholesale(table):
    assert await repo.get_user_prefs(OWNER, USER_A) is None

    first = await repo.put_user_sections(
        OWNER, USER_A, [{"id": "s-2", "name": "Ship"}, {"id": "s-1", "name": "Admin"}]
    )
    assert first == [{"id": "s-2", "name": "Ship"}, {"id": "s-1", "name": "Admin"}]
    assert (await repo.get_user_prefs(OWNER, USER_A))["sections"] == first

    replaced = await repo.put_user_sections(OWNER, USER_A, [{"id": "s-1", "name": "Admin"}])
    assert replaced == [{"id": "s-1", "name": "Admin"}]
    assert (await repo.get_user_prefs(OWNER, USER_A))["sections"] == replaced

    assert await repo.put_user_sections(OWNER, USER_A, []) == []


@pytest.mark.asyncio
async def test_user_sections_are_per_user_and_survive_stream_deactivation(table):
    await repo.put_user_sections(OWNER, USER_A, [{"id": "s-1", "name": "Admin"}])
    assert await repo.get_user_prefs(OWNER, USER_B) is None

    await repo.ensure_user_stream(OWNER, USER_A)
    await repo.deactivate_user_stream(OWNER, USER_A)
    assert (await repo.get_user_prefs(OWNER, USER_A))["sections"] == [{"id": "s-1", "name": "Admin"}]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k user_sections -q`
Expected: FAIL — no attribute `get_user_prefs`.

- [ ] **Step 3: Implement the repository side**

Beside `userstream_sk` (L127) in `apps/backend/core/repositories/messaging_repo.py`:

```python
def pref_sk(user_id: str) -> str:
    return f"PREF#{user_id}"
```

At the end of the "private user streams" section (after `deactivate_user_stream`):

```python
async def get_user_prefs(owner_id: str, user_id: str) -> dict | None:
    """The caller's sidebar preferences.

    A dedicated ``PREF#`` item rather than the ``USERSTREAM#`` row: that row's
    writes are conditioned on the delivery-key lifecycle (``ensure_user_stream``
    / ``deactivate_user_stream``), so a sections write there would race key
    rotation and be dropped on revocation.
    """
    return await _get(owner_pk(owner_id), pref_sk(user_id))


async def put_user_sections(owner_id: str, user_id: str, sections: list[dict]) -> list[dict]:
    """Replace the ordered section list wholesale (the client owns the order)."""
    stored = [{"id": str(section["id"]), "name": str(section["name"])} for section in sections]
    await run_in_thread(
        _table().put_item,
        Item={
            "pk": owner_pk(owner_id),
            "sk": pref_sk(user_id),
            "ownerId": owner_id,
            "userId": user_id,
            "sections": _ddb_safe(stored),
            "updatedAt": utc_now_iso(),
        },
    )
    return stored
```

- [ ] **Step 4: Run the repository test to verify it passes**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k user_sections -q`
Expected: 2 passed.

- [ ] **Step 5: Write the failing service test**

Append to `apps/backend/tests/unit/services/test_messaging_service_review_findings.py`:

```python
@pytest.mark.asyncio
async def test_put_sections_persists_and_returns_the_whole_sidebar(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    section_id = "33333333-3333-4333-8333-333333333333"
    put = AsyncMock(return_value=[{"id": section_id, "name": "Ship"}])
    monkeypatch.setattr(messaging.messaging_repo, "put_user_sections", put)
    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging.messaging_repo, "get_user_prefs", AsyncMock(return_value=None))

    result = await messaging.put_sections(
        auth, messaging.SectionsRequest(sections=[messaging.RoomSection(id=section_id, name="Ship")])
    )

    assert [str(section.id) for section in result.sections] == [section_id]
    assert result.rooms == []
    put.assert_awaited_once_with("org_1", "user_owner", [{"id": section_id, "name": "Ship"}])


def test_sections_request_rejects_too_many_sections_and_blank_names():
    section_id = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(ValueError):
        messaging.SectionsRequest(
            sections=[messaging.RoomSection(id=section_id, name="Ship")] * (messaging.MAX_SECTIONS + 1)
        )
    with pytest.raises(ValueError):
        messaging.RoomSection(id=section_id, name="")
    with pytest.raises(ValueError):
        messaging.RoomSection(id=section_id, name="x" * (messaging.SECTION_NAME_MAX_CHARS + 1))
```

- [ ] **Step 6: Run it to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/services/test_messaging_service_review_findings.py -k "put_sections or sections_request" -q`
Expected: FAIL — no attribute `put_sections`.

- [ ] **Step 7: Implement the service and contract changes**

In `apps/backend/core/services/messaging.py`, beside `ROOM_NAME_MAX_CHARS` (L37):

```python
MAX_SECTIONS = 20
SECTION_NAME_MAX_CHARS = 40
```

Above `RoomListResponse` (L133):

```python
class RoomSection(ContractModel):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=SECTION_NAME_MAX_CHARS)


class SectionsRequest(ContractModel):
    sections: list[RoomSection] = Field(max_length=MAX_SECTIONS)
```

Replace `RoomListResponse`:

```python
class RoomListResponse(ContractModel):
    rooms: list[RoomSummary]
    sections: list[RoomSection] = []
```

Add a shared reader above `list_rooms` (L820) and use it in both commands:

```python
async def _user_sections(owner_id: str, user_id: str) -> list[RoomSection]:
    prefs = await messaging_repo.get_user_prefs(owner_id, user_id)
    return [RoomSection(id=row["id"], name=row["name"]) for row in (prefs or {}).get("sections", [])]
```

At the end of `list_rooms`, change the return to `return RoomListResponse(rooms=summaries, sections=await _user_sections(owner_id, auth.user_id))`.

After `list_rooms`:

```python
async def put_sections(auth: AuthContext, request: SectionsRequest) -> RoomListResponse:
    """Replace the caller's ordered section list. Sections are per-user view
    state; a room's ``sectionId`` is not validated against them, so a deleted
    section simply drops its rooms back into their default group."""
    owner_id = resolve_owner_id(auth)
    await messaging_repo.put_user_sections(
        owner_id,
        auth.user_id,
        [{"id": str(section.id), "name": section.name} for section in request.sections],
    )
    return await list_rooms(auth)
```

- [ ] **Step 8: Add the route**

In `apps/backend/routers/messaging.py`, add `SectionsRequest,` to the service import block and add immediately after `list_rooms` (L89):

```python
@router.put("/messaging/me/sections", response_model=RoomListResponse)
async def put_sections(request: SectionsRequest, auth: AuthContext = Depends(get_current_user)):
    return await service.put_sections(auth, request)
```

> Route order matters: FastAPI matches in declaration order and `/messaging/me/sections` is a literal path with no `{room_id}` segment, so it cannot be shadowed by `/messaging/rooms/{room_id}`. Keep it beside the other `/messaging/…` literals, not under `/rooms`.

- [ ] **Step 9: Run and commit**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py tests/unit/services/test_messaging_service_review_findings.py -q && uv run ruff check core/services/messaging.py core/repositories/messaging_repo.py routers/messaging.py`
Expected: all passed, ruff clean.

```bash
git add apps/backend/core/repositories/messaging_repo.py apps/backend/core/services/messaging.py apps/backend/routers/messaging.py apps/backend/tests/unit/repositories/test_messaging_repo.py apps/backend/tests/unit/services/test_messaging_service_review_findings.py
git commit -m "feat(messaging): owner-defined sidebar sections on a PREF row"
```

---

### Task 3: Room `topic`, the DM-rename guard, and `POST /leave`

*Suggested model: sonnet — generalising one repo writer plus two guarded commands.*

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py:961-1000` (`patch_room_name` → `patch_room_meta`)
- Modify: `apps/backend/core/services/messaging.py` (`ROOM_TOPIC_MAX_CHARS`, `validate_room_topic`, `PatchRoomRequest` L164, `RoomSummary`, `_room_detail`, `list_rooms`, `patch_room` L895, new `leave_room`)
- Modify: `apps/backend/routers/messaging.py`
- Test: both messaging test files (append)

**Interfaces:**
- Consumes: Task 1's `RoomSummary` additions.
- Produces (repo):
  ```python
  async def patch_room_meta(room_id: str, updates: dict[str, Any]) -> dict
  ```
  Replaces `patch_room_name` (single caller). `updates` keys are a subset of `{"name", "topic"}`. CAS on `revision` exactly as before; mirrors `name` into the owner catalog row (`OWNER#<owner>` / `ROOM#<id>`) **only when `name` is in `updates`** — the catalog row feeds `list_rooms_for_member`'s sidebar join and has no topic column.
- Produces (service):
  ```python
  ROOM_TOPIC_MAX_CHARS = 250
  def validate_room_topic(value: str | None) -> str | None
  class PatchRoomRequest(ContractModel):
      name: str | None = None
      topic: str | None = None
  async def leave_room(auth: AuthContext, room_id: str) -> None
  ```
  `RoomSummary` gains `topic: str | None = None`.
- Produces (router): `POST /messaging/rooms/{room_id}/leave` → 204.

- [ ] **Step 1: Write the failing repository test**

Append to `apps/backend/tests/unit/repositories/test_messaging_repo.py`:

```python
@pytest.mark.asyncio
async def test_patch_room_meta_writes_name_topic_and_mirrors_only_the_name(table):
    room_id, meta = await _room(name="Launch crew")

    renamed = await repo.patch_room_meta(room_id, {"name": "Release crew"})
    assert renamed["name"] == "Release crew"
    assert renamed["revision"] == meta["revision"] + 1
    assert (await repo.get_room(room_id))["name"] == "Release crew"
    listed = await repo.list_rooms_for_member(OWNER, "human", USER_A)
    assert listed[0]["meta"]["name"] == "Release crew"

    topicked = await repo.patch_room_meta(room_id, {"topic": "Ship v2 by Friday"})
    assert topicked["topic"] == "Ship v2 by Friday"
    assert topicked["name"] == "Release crew"
    assert topicked["revision"] == renamed["revision"] + 1

    both = await repo.patch_room_meta(room_id, {"name": None, "topic": None})
    assert both["name"] is None and both["topic"] is None


@pytest.mark.asyncio
async def test_patch_room_meta_rejects_an_unknown_room(table):
    with pytest.raises(repo.RoomNotFound):
        await repo.patch_room_meta("11111111-1111-4111-8111-999999999999", {"topic": "x"})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k patch_room_meta -q`
Expected: FAIL — no attribute `patch_room_meta`.

- [ ] **Step 3: Replace `patch_room_name` with `patch_room_meta`**

Replace the whole of `patch_room_name` (`apps/backend/core/repositories/messaging_repo.py:961-1000`) with:

```python
_ROOM_META_FIELDS = ("name", "topic")


async def patch_room_meta(room_id: str, updates: dict[str, Any]) -> dict:
    """Update the caller-editable META fields under the usual revision CAS.

    ``name`` is mirrored into the owner catalog row because
    ``list_rooms_for_member`` joins the sidebar off it; ``topic`` is not in
    that projection and is never mirrored.
    """
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    named = {k: v for k, v in updates.items() if k in _ROOM_META_FIELDS}
    now = utc_now_iso()
    table_name = _table().name
    revision = int(meta["revision"]) + 1
    names = {f"#{k}": k for k in named}
    values = {f":{k}": _ddb_safe(v) for k, v in named.items()}
    sets = ", ".join(f"#{k} = :{k}" for k in named)
    items: list[dict] = [
        {
            "Update": {
                "TableName": table_name,
                "Key": {"pk": room_pk(room_id), "sk": "META"},
                "UpdateExpression": f"SET {sets + ', ' if sets else ''}revision = :rev, updatedAt = :now",
                "ConditionExpression": "revision = :expected",
                "ExpressionAttributeNames": names or None,
                "ExpressionAttributeValues": {**values, ":rev": revision, ":expected": revision - 1, ":now": now},
            }
        }
    ]
    if "name" in named:
        items.append(
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": owner_pk(meta["ownerId"]), "sk": room_pk(room_id)},
                    "UpdateExpression": "SET #name = :name, updatedAt = :now",
                    "ExpressionAttributeNames": {"#name": "name"},
                    "ExpressionAttributeValues": {":name": _ddb_safe(named["name"]), ":now": now},
                }
            }
        )
    items[0]["Update"] = {k: v for k, v in items[0]["Update"].items() if v is not None}
    await _transact(items)
    return {**meta, **named, "revision": revision, "updatedAt": now}
```

- [ ] **Step 4: Run the repository test to verify it passes**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -q`
Expected: all passed (the suite already includes `test_patch_room_name_persists_to_room_and_sidebar_catalog` if #2483 landed; if the name still appears, rename its call to `patch_room_meta({"name": ...})`).

- [ ] **Step 5: Write the failing service tests**

Append to `apps/backend/tests/unit/services/test_messaging_service_review_findings.py`:

```python
@pytest.mark.asyncio
async def test_patch_room_sets_topic_alone_and_rejects_renaming_a_dm(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    group_meta = {"roomId": "group", "ownerId": "org_1", "kind": "group-dm", "revision": 2}
    require_admin = AsyncMock(return_value=(group_meta, {"role": "admin"}))
    patch_meta = AsyncMock(return_value={**group_meta, "topic": "Ship v2"})
    monkeypatch.setattr(messaging, "require_room_admin", require_admin)
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_meta", patch_meta)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())

    assert await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(topic="Ship v2")) == "detail"
    patch_meta.assert_awaited_once_with("group", {"topic": "Ship v2"})

    require_admin.return_value = ({"roomId": "dm", "ownerId": "org_1", "kind": "dm"}, {"role": "owner"})
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, "dm", messaging.PatchRoomRequest(name="Private label"))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    assert "cannot be renamed" in exc.value.error.message
    assert patch_meta.await_count == 1


@pytest.mark.asyncio
async def test_patch_room_keeps_the_admin_check_in_front_of_persistence(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    monkeypatch.setattr(
        messaging,
        "require_room_admin",
        AsyncMock(side_effect=ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")),
    )
    patch_meta = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_meta", patch_meta)
    with pytest.raises(ApiProblem):
        await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(name="Denied"))
    patch_meta.assert_not_awaited()


def test_room_topic_validation_trims_and_caps():
    assert messaging.validate_room_topic("  Ship v2  ") == "Ship v2"
    assert messaging.validate_room_topic("   ") is None
    assert messaging.validate_room_topic(None) is None
    with pytest.raises(ValueError):
        messaging.PatchRoomRequest(topic="x" * (messaging.ROOM_TOPIC_MAX_CHARS + 1))


@pytest.mark.asyncio
async def test_leave_room_removes_a_non_owner_human_and_refuses_the_owner(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 3}
    remove = AsyncMock(return_value=meta)
    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "member"}))
    )
    monkeypatch.setattr(messaging, "_remove_member_with_notices", remove)

    await messaging.leave_room(auth, "room-1")
    remove.assert_awaited_once_with(meta, "human", "member")

    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "owner"}))
    )
    with pytest.raises(ApiProblem) as exc:
        await messaging.leave_room(auth, "room-1")
    assert exc.value.error.code == "INVALID_REQUEST"
    assert remove.await_count == 1
```

- [ ] **Step 6: Run to verify they fail**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/services/test_messaging_service_review_findings.py -k "topic or leave_room" -q`
Expected: FAIL — no attribute `validate_room_topic` / `leave_room`.

- [ ] **Step 7: Implement the service changes**

In `apps/backend/core/services/messaging.py`:

(a) beside `ROOM_NAME_MAX_CHARS` (L37): `ROOM_TOPIC_MAX_CHARS = 250`.

(b) beside `validate_room_name` (L493):

```python
def validate_room_topic(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > ROOM_TOPIC_MAX_CHARS:
        raise ValueError(f"topic exceeds {ROOM_TOPIC_MAX_CHARS} characters")
    return trimmed
```

(c) replace `PatchRoomRequest` (L164–171):

```python
class PatchRoomRequest(ContractModel):
    name: str | None = None
    topic: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return validate_room_name(value)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str | None) -> str | None:
        return validate_room_topic(value)

    def named_updates(self) -> dict[str, Any]:
        """Only the fields the caller sent, so a topic-only PATCH cannot blank
        the name (and vice versa) while an explicit null still clears one."""
        return {
            key: getattr(self, key) for key in ("name", "topic") if key in self.model_fields_set
        }
```

(d) add `topic: str | None = None` to `RoomSummary` (after `section_id`), pass `topic=meta.get("topic")` in `_room_detail`'s `RoomDetail(...)` and in `list_rooms`' `RoomSummary(...)`.

(e) replace `patch_room` (L895–903):

```python
async def patch_room(auth: AuthContext, room_id: str, request: PatchRoomRequest) -> RoomDetail:
    owner_id = resolve_owner_id(auth)
    meta, _ = await require_room_admin(owner_id, auth.user_id, room_id)
    updates = request.named_updates()
    # A DM's display name is derived from its counterpart; a stored name would
    # shadow it with nothing in the UI able to clear it. (Finding from #2483.)
    if "name" in updates and meta.get("kind") == "dm":
        raise ApiProblem("ROOM_FORBIDDEN", "Direct messages cannot be renamed.")
    if not updates:
        return await _room_detail(meta, auth.user_id)
    try:
        meta = await messaging_repo.patch_room_meta(room_id, updates)
    except messaging_repo.RoomNotFound as exc:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.") from exc
    await _publish_room_metadata(meta)
    return await _room_detail(meta, auth.user_id)
```

(f) add after `remove_member` (L932–943):

```python
async def leave_room(auth: AuthContext, room_id: str) -> None:
    """A member removes themselves. The owner must transfer or delete the room
    first — room deletion is out of scope, so this is a hard refusal."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    if member.get("role") == "owner":
        raise ApiProblem(
            "INVALID_REQUEST",
            "The room owner cannot leave. Make someone else an admin and transfer ownership first.",
        )
    await _remove_member_with_notices(meta, "human", auth.user_id)
```

- [ ] **Step 8: Add the route**

In `apps/backend/routers/messaging.py`, after `remove_room_member` (L128):

```python
@router.post("/messaging/rooms/{room_id}/leave", status_code=204)
async def leave_room(room_id: str, auth: AuthContext = Depends(get_current_user)):
    await service.leave_room(auth, room_id)
    return Response(status_code=204)
```

- [ ] **Step 9: Run the full messaging backend suite and commit**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py tests/unit/services/ tests/unit/routers/ -q && uv run ruff check core/services/messaging.py core/repositories/messaging_repo.py routers/messaging.py`
Expected: all passed, ruff clean.

```bash
git add apps/backend/core/repositories/messaging_repo.py apps/backend/core/services/messaging.py apps/backend/routers/messaging.py apps/backend/tests/unit/repositories/test_messaging_repo.py apps/backend/tests/unit/services/test_messaging_service_review_findings.py
git commit -m "feat(messaging): room topic, DM rename guard, and leave"
```

---

### Task 4: Pinned messages — `PUT` / `DELETE /messaging/rooms/{id}/pins/{messageId}`

*Suggested model: sonnet — one CAS writer with a cap and two guarded routes.*

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py` (append `set_pin` after `patch_room_meta`)
- Modify: `apps/backend/core/services/messaging.py` (`MAX_PINNED_MESSAGES`, `RoomDetail.pinned_message_ids`, `set_pin`)
- Modify: `apps/backend/routers/messaging.py`
- Test: both messaging test files (append)

**Interfaces:**
- Consumes: Task 3's `patch_room_meta` (the neighbouring META writer; `set_pin` follows its CAS shape).
- Produces (repo):
  ```python
  class PinLimitExceeded(MessagingRepoError): ...
  async def set_pin(room_id: str, message_id: str, pinned: bool) -> dict
  ```
  Appends/removes `message_id` in `META.pinnedMessageIds` (newest last), bumps `revision` under CAS, raises `PinLimitExceeded` past 50, and is idempotent (pinning an already-pinned message returns META unchanged with **no** revision bump).
- Produces (service):
  ```python
  MAX_PINNED_MESSAGES = 50
  async def set_pin(auth: AuthContext, room_id: str, message_id: str, pinned: bool) -> RoomDetail
  ```
  `RoomDetail` gains `pinned_message_ids: list[str] = []` (wire `pinnedMessageIds`). **Any member** may pin or unpin (spec). Publishes `room.metadata.changed` via `_publish_room_metadata` (Assumption 1).
- Produces (router): `PUT` / `DELETE /messaging/rooms/{room_id}/pins/{message_id}` → `RoomDetail`.

- [ ] **Step 1: Write the failing repository test**

Append to `apps/backend/tests/unit/repositories/test_messaging_repo.py`:

```python
@pytest.mark.asyncio
async def test_set_pin_appends_removes_is_idempotent_and_caps_at_fifty(table):
    room_id, meta = await _room()

    pinned = await repo.set_pin(room_id, "m-1", True)
    assert pinned["pinnedMessageIds"] == ["m-1"]
    assert pinned["revision"] == meta["revision"] + 1

    again = await repo.set_pin(room_id, "m-1", True)
    assert again["pinnedMessageIds"] == ["m-1"]
    assert again["revision"] == pinned["revision"]

    two = await repo.set_pin(room_id, "m-2", True)
    assert two["pinnedMessageIds"] == ["m-1", "m-2"]

    unpinned = await repo.set_pin(room_id, "m-1", False)
    assert unpinned["pinnedMessageIds"] == ["m-2"]

    missing = await repo.set_pin(room_id, "m-9", False)
    assert missing["pinnedMessageIds"] == ["m-2"]
    assert missing["revision"] == unpinned["revision"]

    for index in range(3, 52):
        await repo.set_pin(room_id, f"m-{index}", True)
    assert len((await repo.get_room(room_id))["pinnedMessageIds"]) == 50
    with pytest.raises(repo.PinLimitExceeded):
        await repo.set_pin(room_id, "m-over", True)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k set_pin -q`
Expected: FAIL — no attribute `set_pin`.

- [ ] **Step 3: Implement `set_pin`**

Beside the other exception classes (after `RoomNotFound`, L74–80) in `apps/backend/core/repositories/messaging_repo.py`:

```python
class PinLimitExceeded(MessagingRepoError):
    pass
```

Append after `patch_room_meta`:

```python
MAX_PINNED_MESSAGES = 50


async def set_pin(room_id: str, message_id: str, pinned: bool) -> dict:
    """Pin/unpin one message on META. Idempotent: a no-op change does not bump
    the revision, so repeated clicks never churn every client's room cache."""
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    current = [str(value) for value in meta.get("pinnedMessageIds", [])]
    if pinned and message_id in current:
        return meta
    if not pinned and message_id not in current:
        return meta
    if pinned and len(current) >= MAX_PINNED_MESSAGES:
        raise PinLimitExceeded(f"rooms hold at most {MAX_PINNED_MESSAGES} pinned messages")
    updated = [*current, message_id] if pinned else [value for value in current if value != message_id]
    now = utc_now_iso()
    revision = int(meta["revision"]) + 1
    await _transact(
        [
            {
                "Update": {
                    "TableName": _table().name,
                    "Key": {"pk": room_pk(room_id), "sk": "META"},
                    "UpdateExpression": "SET pinnedMessageIds = :pins, revision = :rev, updatedAt = :now",
                    "ConditionExpression": "revision = :expected",
                    "ExpressionAttributeValues": {
                        ":pins": _ddb_safe(updated),
                        ":rev": revision,
                        ":expected": revision - 1,
                        ":now": now,
                    },
                }
            }
        ]
    )
    return {**meta, "pinnedMessageIds": updated, "revision": revision, "updatedAt": now}
```

- [ ] **Step 4: Run the repository test to verify it passes**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k set_pin -q`
Expected: 1 passed.

- [ ] **Step 5: Write the failing service test**

Append to `apps/backend/tests/unit/services/test_messaging_service_review_findings.py`:

```python
@pytest.mark.asyncio
async def test_set_pin_allows_any_member_validates_the_message_and_publishes_metadata(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 2}
    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "member"}))
    )
    monkeypatch.setattr(
        messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value={"messageId": "m-1"})
    )
    pin = AsyncMock(return_value={**meta, "pinnedMessageIds": ["m-1"], "revision": 3})
    publish = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "set_pin", pin)
    monkeypatch.setattr(messaging, "_publish_room_metadata", publish)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))

    assert await messaging.set_pin(auth, "room-1", "m-1", True) == "detail"
    pin.assert_awaited_once_with("room-1", "m-1", True)
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_pin_rejects_a_foreign_message_and_maps_the_cap_to_a_problem(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 2}
    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "member"}))
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    with pytest.raises(ApiProblem) as missing:
        await messaging.set_pin(auth, "room-1", "m-9", True)
    assert missing.value.error.code == "MESSAGE_NOT_FOUND"

    monkeypatch.setattr(
        messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value={"messageId": "m-1"})
    )
    monkeypatch.setattr(
        messaging.messaging_repo,
        "set_pin",
        AsyncMock(side_effect=messaging.messaging_repo.PinLimitExceeded("too many")),
    )
    with pytest.raises(ApiProblem) as capped:
        await messaging.set_pin(auth, "room-1", "m-1", True)
    assert capped.value.error.code == "PIN_LIMIT"
```

- [ ] **Step 6: Run to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/services/test_messaging_service_review_findings.py -k set_pin -q`
Expected: FAIL — no attribute `set_pin`.

- [ ] **Step 7: Implement the service side**

In `apps/backend/core/services/messaging.py`:

(a) add `"PIN_LIMIT": 409,` and `"MESSAGE_NOT_FOUND": 404,` to `API_ERROR_STATUS` (L41) if either is absent.

(b) add `pinned_message_ids: list[str] = []` to `RoomDetail` (L126) and `pinned_message_ids=[str(value) for value in meta.get("pinnedMessageIds", [])],` to `_room_detail`'s construction. (`RoomSummary` deliberately does **not** carry pins — the sidebar never renders them and every room-list response would pay for them.)

(c) add after `set_reaction` (L1032–1060):

```python
async def set_pin(auth: AuthContext, room_id: str, message_id: str, pinned: bool) -> RoomDetail:
    """Pin or unpin a message. Any member may do either (spec)."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    if await messaging_repo.get_message_by_id(room_id, message_id) is None:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Message not found in this room.")
    try:
        meta = await messaging_repo.set_pin(room_id, message_id, pinned)
    except messaging_repo.PinLimitExceeded as exc:
        raise ApiProblem("PIN_LIMIT", f"A room can pin at most {MAX_PINNED_MESSAGES} messages.") from exc
    except messaging_repo.RoomNotFound as exc:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.") from exc
    await _publish_room_metadata(meta)
    return await _room_detail(meta, auth.user_id, member)
```

with `MAX_PINNED_MESSAGES = messaging_repo.MAX_PINNED_MESSAGES` beside `ROOM_TOPIC_MAX_CHARS`.

- [ ] **Step 8: Add the routes**

In `apps/backend/routers/messaging.py`, after `set_message_reaction` (L154):

```python
@router.put("/messaging/rooms/{room_id}/pins/{message_id}", response_model=RoomDetail)
async def pin_room_message(room_id: str, message_id: str, auth: AuthContext = Depends(get_current_user)):
    return await service.set_pin(auth, room_id, message_id, True)


@router.delete("/messaging/rooms/{room_id}/pins/{message_id}", response_model=RoomDetail)
async def unpin_room_message(room_id: str, message_id: str, auth: AuthContext = Depends(get_current_user)):
    return await service.set_pin(auth, room_id, message_id, False)
```

- [ ] **Step 9: Run and commit**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py tests/unit/services/test_messaging_service_review_findings.py -q && uv run ruff check core/services/messaging.py core/repositories/messaging_repo.py routers/messaging.py`
Expected: all passed, ruff clean.

```bash
git add apps/backend/core/repositories/messaging_repo.py apps/backend/core/services/messaging.py apps/backend/routers/messaging.py apps/backend/tests/unit/repositories/test_messaging_repo.py apps/backend/tests/unit/services/test_messaging_service_review_findings.py
git commit -m "feat(messaging): pin and unpin messages, capped at fifty"
```

---

### Task 5: Sequence-aware mark unread (`fromSeq`)

*Suggested model: haiku — one optional parameter threaded through three layers with an exact formula.*

The carried-over Phase 1 item: the message menu's "mark unread from here". #2475 shipped whole-room mark-unread (`POST /messaging/rooms/{id}/unread` → `messaging_repo.mark_unread`, which lands the cursor at `max(joined_seq, lastSeq - 1)`). This extends the **existing** endpoint rather than adding one.

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py:1045-1069` (`mark_unread`)
- Modify: `apps/backend/core/services/messaging.py:1240-1244` (`mark_room_unread`) + new `MarkUnreadRequest`
- Modify: `apps/backend/routers/messaging.py:210-212`
- Test: both messaging test files (append)

**Interfaces:**
- Produces (repo): `async def mark_unread(room_id, user_id, *, joined_seq: int, from_seq: int | None = None) -> int`. Target is `max(joined_seq, min(from_seq - 1, lastSeq))` when `from_seq` is given, otherwise the shipped `max(joined_seq, lastSeq - 1)`. The existing `attribute_exists(lastReadSeq) AND lastReadSeq > :n` condition is unchanged, so the cursor still only ever moves backwards.
- Produces (service): `class MarkUnreadRequest(ContractModel): from_seq: int | None = Field(default=None, ge=1)`; `async def mark_room_unread(auth, room_id, request: MarkUnreadRequest | None = None) -> ReadCursorResponse`. The default keeps every existing caller working.
- Produces (router): `POST /messaging/rooms/{room_id}/unread` accepts an optional body.

- [ ] **Step 1: Write the failing repository test**

Append to `apps/backend/tests/unit/repositories/test_messaging_repo.py`:

```python
@pytest.mark.asyncio
async def test_mark_unread_from_a_sequence_leaves_that_message_and_everything_after_unread(table):
    room_id, _ = await _room()
    for index in range(1, 6):
        await repo.create_message(
            owner_id=OWNER,
            room_id=room_id,
            author={"kind": "human", "id": USER_A, "displayName": USER_A},
            text=f"m{index}",
            mentions=[],
            client_message_id=f"c-{index}",
            user_id=USER_A,
        )
    await repo.set_read_cursor(room_id, USER_A, 5)
    assert await repo.get_read_cursor(room_id, USER_A) == 5

    assert await repo.mark_unread(room_id, USER_A, joined_seq=0, from_seq=3) == 2
    assert await repo.get_read_cursor(room_id, USER_A) == 2

    # Never moves the cursor forward: marking from a later message is a no-op.
    assert await repo.mark_unread(room_id, USER_A, joined_seq=0, from_seq=5) == 2
    # No from_seq keeps the shipped whole-room behaviour, floored at joined_seq.
    await repo.set_read_cursor(room_id, USER_A, 5)
    assert await repo.mark_unread(room_id, USER_A, joined_seq=0) == 4
    await repo.set_read_cursor(room_id, USER_A, 5)
    assert await repo.mark_unread(room_id, USER_A, joined_seq=3, from_seq=1) == 3
```

> `create_message`'s keyword names must match the shipped signature — read `messaging_repo.create_message` (L523) and adjust the call if it differs; the assertions are what matter.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k mark_unread_from_a_sequence -q`
Expected: FAIL — `mark_unread() got an unexpected keyword argument 'from_seq'`.

- [ ] **Step 3: Implement**

Replace the target line of `messaging_repo.mark_unread` (L1045–1069), leaving the rest of the function untouched:

```python
async def mark_unread(room_id: str, user_id: str, *, joined_seq: int, from_seq: int | None = None) -> int:
    """Leave the latest room message (or everything from ``from_seq`` on)
    unread without ever advancing the cursor."""
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    last_seq = int(meta["lastSeq"])
    ceiling = min(int(from_seq) - 1, last_seq) if from_seq is not None else last_seq - 1
    target = max(int(joined_seq), max(0, ceiling))
```

(the `try` / `update_item` / `except` body below is unchanged).

- [ ] **Step 4: Run the repository test to verify it passes**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py -k mark_unread -q`
Expected: all passed (including #2475's existing mark-unread tests).

- [ ] **Step 5: Write the failing service test**

Append to `apps/backend/tests/unit/services/test_messaging_service_review_findings.py`:

```python
@pytest.mark.asyncio
async def test_mark_room_unread_threads_from_seq_and_defaults_to_the_whole_room(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    member = {"kind": "human", "id": "member", "role": "member", "joinedSeq": 2}
    mark = AsyncMock(return_value=4)
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"roomId": "room-1"}, member)))
    monkeypatch.setattr(messaging.messaging_repo, "mark_unread", mark)

    await messaging.mark_room_unread(auth, "room-1", messaging.MarkUnreadRequest(from_seq=5))
    assert mark.await_args.kwargs == {"joined_seq": 2, "from_seq": 5}

    await messaging.mark_room_unread(auth, "room-1")
    assert mark.await_args.kwargs == {"joined_seq": 2, "from_seq": None}


def test_mark_unread_request_rejects_a_non_positive_sequence():
    with pytest.raises(ValueError):
        messaging.MarkUnreadRequest(from_seq=0)
```

- [ ] **Step 6: Run to verify it fails**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/services/test_messaging_service_review_findings.py -k mark_unread -q`
Expected: FAIL — no attribute `MarkUnreadRequest`.

- [ ] **Step 7: Implement the service and route**

In `apps/backend/core/services/messaging.py`, beside `ReadCursorRequest` (L293):

```python
class MarkUnreadRequest(ContractModel):
    from_seq: int | None = Field(default=None, ge=1)
```

Replace `mark_room_unread` (L1240–1244):

```python
async def mark_room_unread(
    auth: AuthContext, room_id: str, request: MarkUnreadRequest | None = None
) -> ReadCursorResponse:
    """Whole-room by default; ``fromSeq`` leaves that message and everything
    after it unread ("mark unread from here")."""
    _, member = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    value = await messaging_repo.mark_unread(
        room_id,
        auth.user_id,
        joined_seq=int(member.get("joinedSeq", 0)),
        from_seq=request.from_seq if request else None,
    )
    return ReadCursorResponse(room_id=room_id, last_read_seq=value)
```

In `apps/backend/routers/messaging.py`, add `MarkUnreadRequest,` to the service import block and replace the route (L210–212):

```python
@router.post("/messaging/rooms/{room_id}/unread", response_model=ReadCursorResponse)
async def mark_room_unread(
    room_id: str,
    request: MarkUnreadRequest | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.mark_room_unread(auth, room_id, request)
```

> The shipped client posts `{}`, which validates as `MarkUnreadRequest()` under `extra="forbid"`, so #2475's whole-room path is unchanged with no shim.

- [ ] **Step 8: Run the full backend messaging surface and commit**

Run: `cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/unit/repositories/test_messaging_repo.py tests/unit/services/ tests/unit/routers/ tests/contract -q && uv run ruff check core/services/messaging.py core/repositories/messaging_repo.py routers/messaging.py`
Expected: all passed, ruff clean.

```bash
git add apps/backend/core/repositories/messaging_repo.py apps/backend/core/services/messaging.py apps/backend/routers/messaging.py apps/backend/tests/unit/repositories/test_messaging_repo.py apps/backend/tests/unit/services/test_messaging_service_review_findings.py
git commit -m "feat(messaging): mark unread from a given message sequence"
```

---

# Frontend (Tasks 6–11)

### Task 6: Shared contract and `useRooms` wiring

*Suggested model: sonnet — mechanical but wide; every later frontend task consumes these exact names.*

**Files:**
- Modify: `packages/shared/src/messaging/types.ts`
- Modify: `packages/shared/src/messaging/client.ts`
- Modify: `packages/shared/src/live/types.ts:169-207`
- Modify: `apps/frontend/src/hooks/useRooms.ts`
- Test: `packages/shared/src/messaging/client.test.ts` (append), `apps/frontend/src/hooks/__tests__/useRooms.test.tsx` (append)

**Interfaces:**
- Consumes: Tasks 1–5's wire contract.
- Produces (`@lightbulb/shared`):
  ```ts
  export interface RoomSection { id: string; name: string }
  export interface RoomSummary { /* …existing… */ topic: string | null; starred: boolean; muted: boolean; sectionId: string | null }
  export interface RoomDetail extends RoomSummary { /* …existing… */ pinnedMessageIds: string[] }
  export interface RoomListResponse { rooms: RoomSummary[]; sections: RoomSection[] }
  export interface MemberPrefsRequest { starred?: boolean; muted?: boolean; sectionId?: string | null }
  export interface SectionsRequest { sections: RoomSection[] }
  export interface PatchRoomRequest { name?: string | null; topic?: string | null }
  // MessagingClient gains:
  setMemberPrefs(roomId: string, request: MemberPrefsRequest): Promise<RoomDetail>;
  putSections(request: SectionsRequest): Promise<RoomListResponse>;
  leaveRoom(roomId: string): Promise<void>;
  setPin(roomId: string, messageId: string, pinned: boolean): Promise<RoomDetail>;
  markUnread(roomId: string, fromSeq?: number): Promise<ReadCursorResponse>;   // signature change
  ```
  `LiveEventType` and `LIVE_EVENT_TYPES` gain `"room.member.updated"`; `export type RoomMemberUpdated = { roomId: string }`.
- Produces (`useRooms` return, added to the existing object):
  ```ts
  sections: RoomSection[];
  setRoomPrefs: (roomId: string, prefs: MemberPrefsRequest) => Promise<void>;
  saveSections: (sections: RoomSection[]) => Promise<void>;
  leaveRoom: (roomId: string) => Promise<void>;
  setPin: (messageId: string, pinned: boolean) => Promise<void>;
  markUnread: (roomId: string, fromSeq?: number) => Promise<void>;   // signature change
  ```

- [ ] **Step 1: Write the failing tests**

Append to `packages/shared/src/messaging/client.test.ts` (match the file's existing fake-`api` harness — read its first 30 lines and reuse the same `calls` recorder rather than inventing one):

```ts
it("addresses the phase 3 room commands", async () => {
  const calls: Array<[string, string, unknown]> = [];
  const api = {
    get: async (path: string) => { calls.push(["get", path, undefined]); return {}; },
    post: async (path: string, body?: unknown) => { calls.push(["post", path, body]); return {}; },
    put: async (path: string, body?: unknown) => { calls.push(["put", path, body]); return {}; },
    patch: async (path: string, body?: unknown) => { calls.push(["patch", path, body]); return {}; },
    del: async (path: string) => { calls.push(["del", path, undefined]); return {}; },
  } as never;
  const client = createMessagingClient(api);
  await client.setMemberPrefs("r 1", { starred: true });
  await client.putSections({ sections: [{ id: "s-1", name: "Ship" }] });
  await client.leaveRoom("r 1");
  await client.setPin("r 1", "m 1", true);
  await client.setPin("r 1", "m 1", false);
  await client.markUnread("r 1", 4);
  await client.markUnread("r 1");
  expect(calls).toEqual([
    ["patch", "/messaging/rooms/r%201/me", { starred: true }],
    ["put", "/messaging/me/sections", { sections: [{ id: "s-1", name: "Ship" }] }],
    ["post", "/messaging/rooms/r%201/leave", {}],
    ["put", "/messaging/rooms/r%201/pins/m%201", {}],
    ["del", "/messaging/rooms/r%201/pins/m%201", undefined],
    ["post", "/messaging/rooms/r%201/unread", { fromSeq: 4 }],
    ["post", "/messaging/rooms/r%201/unread", {}],
  ]);
});
```

Append to `packages/shared/src/live/types.test.ts` if it exists, otherwise to `packages/shared/src/messaging/client.test.ts`:

```ts
it("recognises the member-updated live event", () => {
  expect(LIVE_EVENT_TYPES.has("room.member.updated")).toBe(true);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd packages/shared && pnpm test -- src/messaging/client.test.ts`
Expected: FAIL — `client.setMemberPrefs is not a function`.

- [ ] **Step 3: Implement the shared changes**

`packages/shared/src/messaging/types.ts`:

```ts
export interface RoomSection {
  id: string;
  name: string;
}

export interface MemberPrefsRequest {
  starred?: boolean;
  muted?: boolean;
  sectionId?: string | null;
}

export interface SectionsRequest {
  sections: RoomSection[];
}
```

Extend `RoomSummary` with `topic: string | null; starred: boolean; muted: boolean; sectionId: string | null;`, `RoomDetail` with `pinnedMessageIds: string[];`, `RoomListResponse` with `sections: RoomSection[];`, and replace `PatchRoomRequest` with `{ name?: string | null; topic?: string | null }`.

`packages/shared/src/messaging/client.ts` — add to the `MessagingClient` interface and the returned object:

```ts
    setMemberPrefs: (roomId, request) => api.patch(`${roomPath(roomId)}/me`, request) as Promise<RoomDetail>,
    putSections: (request) => api.put("/messaging/me/sections", request) as Promise<RoomListResponse>,
    async leaveRoom(roomId) {
      await api.post(`${roomPath(roomId)}/leave`, {});
    },
    setPin: (roomId, messageId, pinned) => (pinned
      ? api.put(`${roomPath(roomId)}/pins/${encodeURIComponent(messageId)}`, {})
      : api.del(`${roomPath(roomId)}/pins/${encodeURIComponent(messageId)}`)) as Promise<RoomDetail>,
    markUnread: (roomId, fromSeq) => api.post(
      `${roomPath(roomId)}/unread`, fromSeq === undefined ? {} : { fromSeq },
    ) as Promise<ReadCursorResponse>,
```

(replace the shipped `markUnread` line; update the interface signature to `markUnread(roomId: string, fromSeq?: number): Promise<ReadCursorResponse>`.)

`packages/shared/src/live/types.ts` — add `| "room.member.updated"` to `LiveEventType` (after `"room.metadata.changed"`), `"room.member.updated",` to the `LIVE_EVENT_TYPES` set, and `export type RoomMemberUpdated = { roomId: string };` beside `RoomMetadataChanged`.

- [ ] **Step 4: Run the shared tests to verify they pass**

Run: `cd packages/shared && pnpm test && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Write the failing `useRooms` test**

Append to `apps/frontend/src/hooks/__tests__/useRooms.test.tsx` (reuse the file's existing wrapper/mock-messaging harness — read its top 60 lines; the assertions below are what matter):

```tsx
it("stars a room optimistically and revalidates the list", async () => {
  const setMemberPrefs = vi.fn().mockResolvedValue({ ...roomDetail, starred: true });
  const { result } = renderRooms({ setMemberPrefs });
  await act(async () => { await result.current.setRoomPrefs("room-1", { starred: true }); });
  expect(setMemberPrefs).toHaveBeenCalledWith("room-1", { starred: true });
  await waitFor(() => expect(result.current.rooms[0].starred).toBe(true));
});

it("passes fromSeq through to mark unread", async () => {
  const markUnread = vi.fn().mockResolvedValue({ roomId: "room-1", lastReadSeq: 2 });
  const { result } = renderRooms({ markUnread });
  await act(async () => { await result.current.markUnread("room-1", 3); });
  expect(markUnread).toHaveBeenCalledWith("room-1", 3);
});

it("revalidates the room list on a member-updated live event", async () => {
  const listRooms = vi.fn().mockResolvedValue({ rooms: [roomSummary], sections: [] });
  const { result, emit } = renderRooms({ listRooms });
  await waitFor(() => expect(result.current.rooms).toHaveLength(1));
  listRooms.mockClear();
  await act(async () => { emit({ type: "room.member.updated", payload: { roomId: "room-1" } }); });
  await waitFor(() => expect(listRooms).toHaveBeenCalled());
});
```

- [ ] **Step 6: Run to verify it fails**

Run: `cd apps/frontend && pnpm test -- src/hooks/__tests__/useRooms.test.tsx`
Expected: FAIL — `result.current.setRoomPrefs is not a function`.

- [ ] **Step 7: Implement the `useRooms` changes**

Add `type MemberPrefsRequest, type RoomSection,` to the `@lightbulb/shared` import. After `const rooms = roomsData?.rooms ?? NO_ROOMS;` add `const sections = roomsData?.sections ?? NO_SECTIONS;` with `const NO_SECTIONS: RoomSection[] = [];` beside `NO_ROOMS`.

Replace `markUnread` (L474–487) — keep the whole `manuallyUnreadRoom` / `pendingRead` body, only the signature and the call change:

```tsx
  const markUnread = useCallback(async (roomId: string, fromSeq?: number) => {
    if (activeRoomId === roomId) manuallyUnreadRoom.current = roomId;
    try {
      await pendingRead.current;
      await messaging.markUnread(roomId, fromSeq);
      await Promise.all([mutateRooms(), activeRoomId === roomId ? mutateRoom() : Promise.resolve()]);
    } catch (error) {
      if (manuallyUnreadRoom.current === roomId) manuallyUnreadRoom.current = null;
      throw error;
    }
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);
```

Add beside `updateRoom` (L553):

```tsx
  const setRoomPrefs = useCallback(async (roomId: string, prefs: MemberPrefsRequest) => {
    // Optimistic so a star or a bell flips under the cursor, then reconciled.
    await mutateRooms(
      (current) => current && { ...current, rooms: current.rooms.map((room) => room.roomId === roomId ? { ...room, ...prefs, sectionId: prefs.sectionId === undefined ? room.sectionId : prefs.sectionId } : room) },
      false,
    );
    try {
      const updated = await messaging.setMemberPrefs(roomId, prefs);
      if (roomId === activeRoomId) await mutateRoom(updated, false);
    } finally {
      await mutateRooms();
    }
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  const saveSections = useCallback(async (next: readonly RoomSection[]) => {
    const response = await messaging.putSections({ sections: [...next] });
    await mutateRooms(response, false);
  }, [messaging, mutateRooms]);

  const leaveRoom = useCallback(async (roomId: string) => {
    await messaging.leaveRoom(roomId);
    if (roomId === activeRoomId) setActiveRoomId(null);
    await mutateRooms();
  }, [activeRoomId, messaging, mutateRooms]);

  const setPin = useCallback(async (messageId: string, pinned: boolean) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const updated = await messaging.setPin(activeRoomId, messageId, pinned);
    await mutateRoom(updated, false);
  }, [activeRoomId, messaging, mutateRoom]);
```

In the `onEvent` handler that already switches on `event.type`, add beside the `room.metadata.changed` arm:

```tsx
      if (event.type === "room.member.updated") { void mutateRooms(); return; }
```

Add `sections, setRoomPrefs, saveSections, leaveRoom, setPin,` to the returned object (L590).

- [ ] **Step 8: Run, typecheck (web + mobile), commit**

Run: `cd apps/frontend && pnpm test -- src/hooks src/components/messaging && pnpm exec tsc --noEmit && cd ../.. && pnpm --filter @lightbulb/mobile exec tsc --noEmit`
Expected: all green. (Mobile only reads these types; new required fields are server-supplied, so mobile should not construct a `RoomSummary` — if it does, fix the fixture there in this commit.)

```bash
git add packages/shared/src/messaging/types.ts packages/shared/src/messaging/client.ts packages/shared/src/messaging/client.test.ts packages/shared/src/live/types.ts apps/frontend/src/hooks/useRooms.ts apps/frontend/src/hooks/__tests__/useRooms.test.tsx
git commit -m "feat(messaging): client and hook wiring for prefs, sections, pins and leave"
```

---

### Task 7: Sidebar groups — Starred, stars, mute, unread-first sort, collapse

*Suggested model: sonnet — pure helpers plus a row component; the shape is fully specified.*

**Files:**
- Create: `apps/frontend/src/components/messaging/roomSections.ts`
- Create: `apps/frontend/src/components/messaging/sidebarPrefs.ts`
- Create: `apps/frontend/src/components/messaging/RoomDirectoryRow.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomDirectory.tsx:133-155`
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Test: `apps/frontend/src/components/messaging/__tests__/roomSections.test.ts` (create), `__tests__/RoomDirectory.test.tsx` (append)

**Interfaces:**
- Consumes: Task 6's `DirectoryRoom` fields `starred`, `muted`, `sectionId`, and `RoomSection`.
- Produces:
  ```ts
  // roomSections.ts
  export type RoomGroup = { id: string; label: string; rooms: DirectoryRoom[]; custom: boolean };
  export type SidebarSort = "alpha" | "unread";
  export function groupRooms(
    rooms: readonly DirectoryRoom[],
    sections: readonly RoomSection[],
    options: { sort: SidebarSort; nameOf: (room: DirectoryRoom) => string },
  ): RoomGroup[];
  // sidebarPrefs.ts
  export interface SidebarPrefs { collapsed: string[]; sort: SidebarSort }
  export const SIDEBAR_PREFS_KEY_PREFIX = "lightbulb.messaging.sidebar.";
  export function readSidebarPrefs(scope: string): SidebarPrefs;
  export function writeSidebarPrefs(scope: string, prefs: SidebarPrefs): void;
  ```
  Group order: `starred` (only when non-empty) · every custom section **in `sections` order** (only when non-empty) · `channels` · `direct` (DMs **and** group DMs). A room with `starred: true` appears **only** in Starred. A room whose `sectionId` matches a section appears only there. Labels: `"Starred"`, the section's own `name`, `"Channels"`, `"Direct messages"`.
- Produces: `RoomDirectoryRow` props
  ```ts
  { room: DirectoryRoom; label: string; active: boolean; busy: boolean;
    onSelect: () => void; onToggleStar: () => void; onToggleMute: () => void;
    onMarkUnread: () => void; onCopyLink: () => void;
    onMoveToSection?: (sectionId: string | null) => void; sections: readonly RoomSection[];
    onLeave?: () => void }
  ```

- [ ] **Step 1: Write the failing helper tests**

`apps/frontend/src/components/messaging/__tests__/roomSections.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { groupRooms } from "../roomSections";
import type { DirectoryRoom } from "../types";

const base: DirectoryRoom = {
  roomId: "r", kind: "channel", name: "Launch", roomRevision: 1, lastMessageSeq: 0,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null,
  starred: false, muted: false, sectionId: null, participants: [],
};
const room = (id: string, extra: Partial<DirectoryRoom> = {}): DirectoryRoom => ({ ...base, roomId: id, name: id, ...extra });
const nameOf = (value: DirectoryRoom) => value.name ?? value.roomId;

describe("groupRooms", () => {
  it("orders starred, custom sections, channels, then direct messages", () => {
    const groups = groupRooms(
      [room("beta"), room("alpha", { starred: true }), room("dm-1", { kind: "dm" }), room("shipping", { sectionId: "s-1" })],
      [{ id: "s-1", name: "Ship" }],
      { sort: "alpha", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Starred", "Ship", "Channels", "Direct messages"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["alpha"]);
    expect(groups[1].rooms.map((value) => value.roomId)).toEqual(["shipping"]);
    expect(groups[2].rooms.map((value) => value.roomId)).toEqual(["beta"]);
    expect(groups[3].rooms.map((value) => value.roomId)).toEqual(["dm-1"]);
  });

  it("keeps group DMs inside Direct messages", () => {
    const groups = groupRooms([room("g", { kind: "group-dm" }), room("d", { kind: "dm" })], [], { sort: "alpha", nameOf });
    expect(groups.map((group) => group.label)).toEqual(["Direct messages"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["d", "g"]);
  });

  it("shows a room once — starred beats its section, which beats its kind", () => {
    const groups = groupRooms(
      [room("both", { starred: true, sectionId: "s-1" })],
      [{ id: "s-1", name: "Ship" }],
      { sort: "alpha", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Starred"]);
    expect(groups[0].rooms).toHaveLength(1);
  });

  it("drops empty groups and a section that no longer exists falls back to its kind", () => {
    const groups = groupRooms([room("orphan", { sectionId: "gone" })], [{ id: "s-1", name: "Ship" }], { sort: "alpha", nameOf });
    expect(groups.map((group) => group.label)).toEqual(["Channels"]);
  });

  it("sorts alphabetically by default and unread-first on request", () => {
    const rooms = [room("zulu", { unreadCount: 0 }), room("alpha", { unreadCount: 0 }), room("mike", { unreadCount: 3 })];
    expect(groupRooms(rooms, [], { sort: "alpha", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["alpha", "mike", "zulu"]);
    expect(groupRooms(rooms, [], { sort: "unread", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["mike", "alpha", "zulu"]);
  });

  it("never counts a muted room as unread for sorting", () => {
    const rooms = [room("alpha"), room("muted", { unreadCount: 9, muted: true })];
    expect(groupRooms(rooms, [], { sort: "unread", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["alpha", "muted"]);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/roomSections.test.ts`
Expected: FAIL — `Cannot find module '../roomSections'`.

- [ ] **Step 3: Implement `roomSections.ts`**

```ts
import type { RoomSection } from "@lightbulb/shared";
import type { DirectoryRoom } from "./types";

export type SidebarSort = "alpha" | "unread";
export interface RoomGroup {
  id: string;
  label: string;
  rooms: DirectoryRoom[];
  custom: boolean;
}

export function groupRooms(
  rooms: readonly DirectoryRoom[],
  sections: readonly RoomSection[],
  options: { sort: SidebarSort; nameOf: (room: DirectoryRoom) => string },
): RoomGroup[] {
  const known = new Set(sections.map((section) => section.id));
  const bucket = (room: DirectoryRoom) => room.starred ? "starred"
    : room.sectionId && known.has(room.sectionId) ? room.sectionId
      : room.kind === "channel" ? "channels" : "direct";
  const order: RoomGroup[] = [
    { id: "starred", label: "Starred", rooms: [], custom: false },
    ...sections.map((section) => ({ id: section.id, label: section.name, rooms: [], custom: true })),
    { id: "channels", label: "Channels", rooms: [], custom: false },
    { id: "direct", label: "Direct messages", rooms: [], custom: false },
  ];
  const byId = new Map(order.map((group) => [group.id, group]));
  for (const room of rooms) byId.get(bucket(room))?.rooms.push(room);
  // A muted room never carries a badge, so it never jumps the queue either.
  const unread = (room: DirectoryRoom) => room.muted ? 0 : room.unreadCount;
  for (const group of order) {
    group.rooms.sort((left, right) => (options.sort === "unread" && unread(right) !== unread(left)
      ? unread(right) - unread(left)
      : options.nameOf(left).localeCompare(options.nameOf(right))));
  }
  return order.filter((group) => group.rooms.length > 0);
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/roomSections.test.ts`
Expected: 6 passed.

- [ ] **Step 5: Implement `sidebarPrefs.ts`**

```ts
import type { SidebarSort } from "./roomSections";

export const SIDEBAR_PREFS_KEY_PREFIX = "lightbulb.messaging.sidebar.";
export interface SidebarPrefs { collapsed: string[]; sort: SidebarSort }
const EMPTY: SidebarPrefs = { collapsed: [], sort: "alpha" };

export function readSidebarPrefs(scope: string): SidebarPrefs {
  try {
    const raw = window.localStorage.getItem(`${SIDEBAR_PREFS_KEY_PREFIX}${scope}`);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<SidebarPrefs>;
    return {
      collapsed: Array.isArray(parsed.collapsed) ? parsed.collapsed.filter((value) => typeof value === "string") : [],
      sort: parsed.sort === "unread" ? "unread" : "alpha",
    };
  } catch {
    return EMPTY;
  }
}

export function writeSidebarPrefs(scope: string, prefs: SidebarPrefs): void {
  try {
    window.localStorage.setItem(`${SIDEBAR_PREFS_KEY_PREFIX}${scope}`, JSON.stringify(prefs));
  } catch {
    // Private windows and blocked site data: the sidebar simply forgets.
  }
}
```

- [ ] **Step 6: Write the failing directory test**

Append to `apps/frontend/src/components/messaging/__tests__/RoomDirectory.test.tsx`:

```tsx
const phase3 = { topic: null, starred: false, muted: false, sectionId: null };

it("renders Starred above Channels and stars a room from its overflow menu", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={onSetPrefs} onMarkUnread={vi.fn()} rooms={[
      { ...base, ...phase3, roomId: "plain", name: "Plain" },
      { ...base, ...phase3, roomId: "fav", name: "Favourite", starred: true },
    ]} />);
  expect(screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent)).toEqual(["Starred", "Channels"]);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star conversation" }));
  expect(onSetPrefs).toHaveBeenCalledWith("plain", { starred: true });
});

it("mutes a room, drops its badge, and says so without relying on colour", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={onSetPrefs} onMarkUnread={vi.fn()} rooms={[
      { ...base, ...phase3, roomId: "loud", name: "Loud", unreadCount: 4 },
      { ...base, ...phase3, roomId: "quiet", name: "Quiet", unreadCount: 7, muted: true },
    ]} />);
  expect(screen.getByLabelText("4 unread messages")).toBeInTheDocument();
  expect(screen.queryByLabelText("7 unread messages")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Quiet/ })).toHaveTextContent("Muted");
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Quiet" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Unmute conversation" }));
  expect(onSetPrefs).toHaveBeenCalledWith("quiet", { muted: false });
});

it("sorts unread first when asked and remembers the choice per owner scope", async () => {
  window.localStorage.clear();
  const rooms = [
    { ...base, ...phase3, roomId: "alpha", name: "Alpha" },
    { ...base, ...phase3, roomId: "zulu", name: "Zulu", unreadCount: 2 },
  ];
  const props = { ownerScopeId: "org", state: "ready" as const, participants: [], sections: [], rooms,
    onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs: vi.fn(), onMarkUnread: vi.fn() };
  const { unmount } = render(<RoomDirectory {...props} />);
  const names = () => screen.getAllByRole("button", { name: /^(Alpha|Zulu)/ }).map((node) => node.textContent?.slice(0, 5));
  expect(names()).toEqual(["Alpha", "Zulu "]);
  await userEvent.selectOptions(screen.getByLabelText("Sort conversations"), "unread");
  expect(names()).toEqual(["Zulu ", "Alpha"]);
  expect(window.localStorage.getItem("lightbulb.messaging.sidebar.org")).toContain('"sort":"unread"');
  unmount();
  render(<RoomDirectory {...props} />);
  expect(screen.getByLabelText("Sort conversations")).toHaveValue("unread");
});

it("collapses a group and keeps the collapse across a remount", async () => {
  window.localStorage.clear();
  const props = { ownerScopeId: "org", state: "ready" as const, participants: [], sections: [],
    rooms: [{ ...base, ...phase3, roomId: "plain", name: "Plain" }],
    onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs: vi.fn(), onMarkUnread: vi.fn() };
  const { unmount } = render(<RoomDirectory {...props} />);
  await userEvent.click(screen.getByRole("button", { name: "Collapse Channels" }));
  expect(screen.queryByRole("button", { name: /^Plain/ })).not.toBeInTheDocument();
  unmount();
  render(<RoomDirectory {...props} />);
  expect(screen.getByRole("button", { name: "Expand Channels" })).toBeInTheDocument();
});
```

- [ ] **Step 7: Run to verify it fails**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomDirectory.test.tsx`
Expected: FAIL — `Starred` heading absent.

- [ ] **Step 8: Implement `RoomDirectoryRow.tsx`**

```tsx
"use client";

import { Bell, BellOff, Hash, Link, LogOut, MailOpen, MessageCircle, MoreHorizontal, Star, Users } from "lucide-react";
import type { RoomSection } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { roomKindLabel, type DirectoryRoom } from "./types";

export interface RoomDirectoryRowProps {
  room: DirectoryRoom;
  label: string;
  active: boolean;
  busy: boolean;
  onSelect: () => void;
  onToggleStar: () => void;
  onToggleMute: () => void;
  onMarkUnread: () => void;
  onCopyLink: () => void;
  onMoveToSection?: (sectionId: string | null) => void;
  sections: readonly RoomSection[];
  onLeave?: () => void;
}

export function RoomDirectoryRow({ room, label, active, busy, onSelect, onToggleStar, onToggleMute, onMarkUnread, onCopyLink, onMoveToSection, sections, onLeave }: RoomDirectoryRowProps) {
  const Icon = room.kind === "channel" ? Hash : room.kind === "group-dm" ? Users : MessageCircle;
  const badge = !room.muted && room.unreadCount > 0;
  const faces = room.participants.slice(0, 2);
  return <li className="room-directory-row">
    <button type="button" className={`room-directory-entry ${badge ? "font-semibold" : ""} ${room.muted ? "room-directory-muted" : ""}`}
      aria-current={active ? "page" : undefined} onClick={onSelect}>
      {room.kind === "dm" && room.participants.length === 1
        ? <ParticipantAvatar participant={room.participants[0]} size="sm" />
        : room.kind === "group-dm" && faces.length > 0
          ? <span className="room-directory-faces" aria-hidden="true">{faces.map((participant) => <ParticipantAvatar key={`${participant.kind}:${participant.id}`} participant={participant} size="sm" />)}</span>
          : <Icon size={16} aria-hidden="true" />}
      <span className="min-w-0 flex-1">
        <span className="block truncate" title={label}>{label}</span>
        <span className="sr-only">{roomKindLabel[room.kind]}</span>
      </span>
      {room.starred && <Star size={12} aria-label="Starred" className="room-directory-star" />}
      {room.muted && <span className="room-directory-tag">Muted</span>}
      {badge && <span className="room-count" aria-label={`${room.unreadCount} unread messages`}>{room.unreadCount}</span>}
    </button>
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="room-directory-actions" aria-label={`Conversation actions for ${label}`} disabled={busy}><MoreHorizontal aria-hidden="true" /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={onToggleStar}><Star aria-hidden="true" />{room.starred ? "Unstar conversation" : "Star conversation"}</DropdownMenuItem>
        <DropdownMenuItem onSelect={onToggleMute}>{room.muted ? <Bell aria-hidden="true" /> : <BellOff aria-hidden="true" />}{room.muted ? "Unmute conversation" : "Mute conversation"}</DropdownMenuItem>
        <DropdownMenuItem onSelect={onMarkUnread}><MailOpen aria-hidden="true" />Mark unread</DropdownMenuItem>
        <DropdownMenuItem onSelect={onCopyLink}><Link aria-hidden="true" />Copy link</DropdownMenuItem>
        {onMoveToSection && <>
          <DropdownMenuSeparator />
          {sections.map((section) => <DropdownMenuItem key={section.id} disabled={room.sectionId === section.id} onSelect={() => onMoveToSection(section.id)}>Move to {section.name}</DropdownMenuItem>)}
          {room.sectionId && <DropdownMenuItem onSelect={() => onMoveToSection(null)}>Remove from section</DropdownMenuItem>}
        </>}
        {onLeave && <>
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={onLeave}><LogOut aria-hidden="true" />Leave channel</DropdownMenuItem>
        </>}
      </DropdownMenuContent>
    </DropdownMenu>
  </li>;
}
```

> `onMoveToSection` and `onLeave` arrive in Task 8; pass `undefined` for both in this task so the menu renders its first four items only.

- [ ] **Step 9: Rewire `RoomDirectory.tsx`**

Add to `RoomDirectoryProps`: `sections: readonly RoomSection[]; onSetPrefs?: (roomId: string, prefs: MemberPrefsRequest) => Promise<void>;`. Replace the group `.map` block (L133–155) with:

```tsx
    {groups.map((group) => {
      const collapsed = prefs.collapsed.includes(group.id);
      return <section key={group.id} aria-label={group.label}>
        <div className="room-directory-group-header">
          <h3 className="room-directory-group">{group.label}</h3>
          <Button variant="ghost" size="icon-sm" aria-label={`${collapsed ? "Expand" : "Collapse"} ${group.label}`}
            onClick={() => updatePrefs({ collapsed: collapsed ? prefs.collapsed.filter((id) => id !== group.id) : [...prefs.collapsed, group.id] })}>
            {collapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
          </Button>
        </div>
        {!collapsed && <ul>{group.rooms.map((room) => <RoomDirectoryRow key={room.roomId} room={room}
          label={roomDisplayName(room, room.participants)} active={room.roomId === currentRoomId}
          busy={markingUnread === room.roomId} sections={sections}
          onSelect={() => onSelectRoom(room.roomId)}
          onToggleStar={() => void onSetPrefs?.(room.roomId, { starred: !room.starred })}
          onToggleMute={() => void onSetPrefs?.(room.roomId, { muted: !room.muted })}
          onMarkUnread={() => void markRoomUnread(room.roomId, roomDisplayName(room, room.participants))}
          onCopyLink={() => void copyText(roomPermalink(room.roomId))} />)}</ul>}
      </section>;
    })}
```

with, above the `return`:

```tsx
  const [prefs, setPrefs] = useState(() => readSidebarPrefs(ownerScopeId));
  const updatePrefs = (patch: Partial<SidebarPrefs>) => {
    const next = { ...prefs, ...patch };
    setPrefs(next);
    writeSidebarPrefs(ownerScopeId, next);
  };
  const groups = groupRooms(rooms, sections, { sort: prefs.sort, nameOf: (room) => roomDisplayName(room, room.participants) });
```

and, in `.room-directory-heading-actions`, a sort control before the two icon buttons:

```tsx
        <label className="sr-only" htmlFor={`${id}-sort`}>Sort conversations</label>
        <select id={`${id}-sort`} className="room-listen-mode" value={prefs.sort}
          onChange={(event) => updatePrefs({ sort: event.target.value as SidebarSort })}>
          <option value="alpha">A–Z</option>
          <option value="unread">Unread first</option>
        </select>
```

Add `roomPermalink` to `messageLinks.ts` — one line beside `messagePermalink`, reusing its base:

```ts
export const roomPermalink = (roomId: string) => `${origin()}/chat?panel=chat&room=${encodeURIComponent(roomId)}`;
```

(read `messageLinks.ts` and mirror however `messagePermalink` builds its origin — do not duplicate the logic.)

Imports to add: `ChevronRight`, `Star` from `lucide-react`; `groupRooms`, `type SidebarSort` from `./roomSections`; `readSidebarPrefs`, `writeSidebarPrefs`, `type SidebarPrefs` from `./sidebarPrefs`; `RoomDirectoryRow` from `./RoomDirectoryRow`; `copyText`, `roomPermalink` from `./messageLinks`; `type MemberPrefsRequest`, `type RoomSection` from `@lightbulb/shared`. Delete the now-unused `Hash`, `MessageCircle`, `MailOpen`, `MoreHorizontal`, `DropdownMenu*`, `ParticipantAvatar` imports **only if** nothing else in the file still uses them.

- [ ] **Step 10: CSS**

Append to `apps/frontend/src/components/messaging/messaging.css`:

```css
.room-directory-group-header { display: flex; align-items: center; justify-content: space-between; gap: 4px; }
.room-directory-group-header .room-directory-group { margin-bottom: 4px; }
.room-directory-muted { color: var(--muted-foreground); }
.room-directory-star { flex: none; color: var(--muted-foreground); }
.room-directory-tag { flex: none; font-size: .75rem; color: var(--muted-foreground); }
.room-directory-faces { display: inline-flex; flex: none; }
.room-directory-faces > :nth-child(2) { margin-left: -8px; }
```

- [ ] **Step 11: Run, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging src/components/workspace && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`. (Existing `RoomDirectory`/`MessagesIntegration` tests need the four new `RoomSummary` fields on their fixtures and `sections={[]}` — update those fixtures in this commit.)

```bash
git add apps/frontend/src/components/messaging/roomSections.ts apps/frontend/src/components/messaging/sidebarPrefs.ts apps/frontend/src/components/messaging/RoomDirectoryRow.tsx apps/frontend/src/components/messaging/RoomDirectory.tsx apps/frontend/src/components/messaging/messageLinks.ts apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__ apps/frontend/src/components/workspace/__tests__
git commit -m "feat(messaging): starred group, mute, unread-first sort and collapsible sidebar groups"
```

---

### Task 8: Custom sections — create, rename, delete, drag-reorder, Move to section, Leave

*Suggested model: opus — `@dnd-kit` wiring plus a keyboard-equivalent reorder path and a destructive confirm; the hairiest frontend task in the phase.*

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomDirectory.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomDirectoryRow.tsx` (pass through `onMoveToSection` / `onLeave` — already in its props from Task 7)
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx:307-320` (`directoryProps`)
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Modify: `apps/infra/openclaw/skills/lightbulb/references/surfaces.md`, `apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md`
- Test: `__tests__/RoomDirectory.test.tsx` (append)

**Interfaces:**
- Consumes: Task 6's `useRooms.sections` / `saveSections` / `leaveRoom`, Task 7's `groupRooms` and `RoomDirectoryRow` props.
- Produces: `RoomDirectoryProps` gains
  ```ts
  onSaveSections?: (sections: RoomSection[]) => Promise<void>;
  onLeaveRoom?: (roomId: string) => Promise<void>;
  ```
  Sections are reordered by dragging the **group header** within the sidebar (`@dnd-kit` `DndContext` + `SortableContext` with `verticalListSortingStrategy`, `KeyboardSensor` + `PointerSensor`), and managed (create / rename / delete) in a "Manage sections" dialog. Deleting a section calls `onSaveSections` without it; its rooms fall back to their kind group via `groupRooms`' unknown-section branch (no per-room write needed — that is why Assumption 3 holds).

- [ ] **Step 1: Write the failing tests**

Append to `apps/frontend/src/components/messaging/__tests__/RoomDirectory.test.tsx`:

```tsx
const sectionProps = {
  ownerScopeId: "org", state: "ready" as const, participants: [],
  onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs: vi.fn().mockResolvedValue(undefined),
  onMarkUnread: vi.fn(), onSaveSections: vi.fn().mockResolvedValue(undefined),
  onLeaveRoom: vi.fn().mockResolvedValue(undefined),
};

it("creates a section, renames it, and deletes it from the manage dialog", async () => {
  const onSaveSections = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onSaveSections={onSaveSections} sections={[{ id: "s-1", name: "Ship" }]}
    rooms={[{ ...base, ...phase3, roomId: "plain", name: "Plain" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Manage sections" }));
  const dialog = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.type(within(dialog).getByLabelText("New section name"), "Admin");
  await userEvent.click(within(dialog).getByRole("button", { name: "Add section" }));
  expect(onSaveSections).toHaveBeenCalledWith([
    { id: "s-1", name: "Ship" },
    { id: expect.any(String), name: "Admin" },
  ]);

  onSaveSections.mockClear();
  await userEvent.clear(within(dialog).getByLabelText("Rename Ship"));
  await userEvent.type(within(dialog).getByLabelText("Rename Ship"), "Shipping");
  await userEvent.click(within(dialog).getByRole("button", { name: "Save sections" }));
  expect(onSaveSections).toHaveBeenLastCalledWith([{ id: "s-1", name: "Shipping" }]);

  onSaveSections.mockClear();
  await userEvent.click(within(dialog).getByRole("button", { name: "Delete Shipping" }));
  await userEvent.click(await screen.findByRole("button", { name: "Delete section" }));
  expect(onSaveSections).toHaveBeenLastCalledWith([]);
});

it("moves a room into a section and back out from the row menu", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onSetPrefs={onSetPrefs} sections={[{ id: "s-1", name: "Ship" }]}
    rooms={[{ ...base, ...phase3, roomId: "plain", name: "Plain" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Move to Ship" }));
  expect(onSetPrefs).toHaveBeenCalledWith("plain", { sectionId: "s-1" });
});

it("reorders sections with the keyboard and persists the new order", async () => {
  const onSaveSections = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onSaveSections={onSaveSections}
    sections={[{ id: "s-1", name: "Ship" }, { id: "s-2", name: "Admin" }]}
    rooms={[
      { ...base, ...phase3, roomId: "a", name: "A", sectionId: "s-1" },
      { ...base, ...phase3, roomId: "b", name: "B", sectionId: "s-2" },
    ]} />);
  expect(screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent)).toEqual(["Ship", "Admin"]);
  screen.getByRole("button", { name: "Reorder Ship" }).focus();
  await userEvent.keyboard("{ }{ArrowDown}{ }");
  await waitFor(() => expect(onSaveSections).toHaveBeenCalledWith([{ id: "s-2", name: "Admin" }, { id: "s-1", name: "Ship" }]));
});

it("offers Leave on a channel only, and confirms before leaving", async () => {
  const onLeaveRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onLeaveRoom={onLeaveRoom} sections={[]} rooms={[
    { ...base, ...phase3, roomId: "chan", name: "Channel" },
    { ...base, ...phase3, roomId: "dm", kind: "dm", name: "Direct", participants: [alice] },
  ]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Direct" }));
  expect(screen.queryByRole("menuitem", { name: "Leave channel" })).not.toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Channel" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Leave channel" }));
  const confirm = await screen.findByRole("dialog", { name: "Leave Channel?" });
  await userEvent.click(within(confirm).getByRole("button", { name: "Leave channel" }));
  expect(onLeaveRoom).toHaveBeenCalledWith("chan");
});

it("surfaces a failed section save without losing the edit", async () => {
  const onSaveSections = vi.fn().mockRejectedValue(new Error("Sections could not be saved."));
  render(<RoomDirectory {...sectionProps} onSaveSections={onSaveSections} sections={[]} rooms={[]} />);
  await userEvent.click(screen.getByRole("button", { name: "Manage sections" }));
  const dialog = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.type(within(dialog).getByLabelText("New section name"), "Admin");
  await userEvent.click(within(dialog).getByRole("button", { name: "Add section" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("Sections could not be saved.");
  expect(within(dialog).getByLabelText("New section name")).toHaveValue("Admin");
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomDirectory.test.tsx -t "section"`
Expected: FAIL — no "Manage sections" button.

- [ ] **Step 3: Implement the sortable group header**

In `RoomDirectory.tsx`, wrap the group list and make each **custom** group's header draggable. Non-custom groups (Starred / Channels / Direct messages) are fixed and are **not** in the `SortableContext` — their order is the spec's.

```tsx
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { GripVertical } from "lucide-react";

function SortableGroupHeader({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id });
  return <div ref={setNodeRef} className="room-directory-group-header" data-dragging={isDragging || undefined}
    style={{ transform: CSS.Transform.toString(transform), transition }}>
    <button type="button" className="room-directory-grip" aria-label={`Reorder ${label}`} {...attributes} {...listeners}>
      <GripVertical size={14} aria-hidden="true" />
    </button>
    <h3 className="room-directory-group">{label}</h3>
    {children}
  </div>;
}
```

In the component body:

```tsx
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const [sectionError, setSectionError] = useState<string>();
  async function persistSections(next: RoomSection[]) {
    setSectionError(undefined);
    try {
      await onSaveSections?.(next);
      return true;
    } catch (error) {
      setSectionError(error instanceof Error ? error.message : "Sections could not be saved.");
      return false;
    }
  }
  function onDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const ids = sections.map((section) => section.id);
    const from = ids.indexOf(String(active.id));
    const to = ids.indexOf(String(over.id));
    if (from < 0 || to < 0) return;
    const next = [...sections];
    next.splice(to, 0, ...next.splice(from, 1));
    void persistSections(next);
  }
```

Wrap the `groups.map(...)` in:

```tsx
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
      <SortableContext items={sections.map((section) => section.id)} strategy={verticalListSortingStrategy}>
        {groups.map((group) => { /* Task 7's body, but the header is SortableGroupHeader when group.custom */ })}
      </SortableContext>
    </DndContext>
```

and pass the two new row callbacks:

```tsx
          onMoveToSection={onSetPrefs && sections.length > 0 ? (sectionId) => void onSetPrefs(room.roomId, { sectionId }) : undefined}
          onLeave={onLeaveRoom && room.kind === "channel" ? () => setLeaving(room) : undefined}
```

- [ ] **Step 4: Implement the Manage sections dialog and the Leave confirm**

Add a "Manage sections" ghost icon button (`FolderPlus`, `aria-label="Manage sections"`) to `.room-directory-heading-actions`, rendered only when `onSaveSections` is present, and two dialogs at the end of the `<nav>`:

```tsx
    <Dialog open={managing} onOpenChange={setManaging}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md">
        <DialogHeader><DialogTitle>Manage sections</DialogTitle>
          <DialogDescription>Group conversations however you work. Deleting a section returns its conversations to Channels or Direct messages.</DialogDescription></DialogHeader>
        <ul className="grid gap-2">{drafts.map((section) => <li key={section.id} className="room-section-edit">
          <label className="sr-only" htmlFor={`${id}-section-${section.id}`}>Rename {section.name}</label>
          <input id={`${id}-section-${section.id}`} className="room-input" value={section.name} maxLength={40}
            onChange={(event) => setDrafts(drafts.map((row) => row.id === section.id ? { ...row, name: event.target.value } : row))} />
          <Button type="button" variant="ghost" size="icon" aria-label={`Delete ${section.name}`} onClick={() => setDeleting(section)}><Trash2 aria-hidden="true" /></Button>
        </li>)}</ul>
        <form className="room-section-edit" onSubmit={async (event) => {
          event.preventDefault();
          const name = newSection.trim();
          if (!name) return;
          const next = [...drafts, { id: crypto.randomUUID(), name }];
          if (await persistSections(next)) { setDrafts(next); setNewSection(""); }
        }}>
          <label className="sr-only" htmlFor={`${id}-new-section`}>New section name</label>
          <input id={`${id}-new-section`} className="room-input" value={newSection} maxLength={40} onChange={(event) => setNewSection(event.target.value)} />
          <Button type="submit" variant="outline" disabled={!newSection.trim()}>Add section</Button>
        </form>
        {sectionError && <p role="alert" className="text-sm text-err">{sectionError}</p>}
        <DialogFooter className="gap-2">
          <Button type="button" variant="outline" onClick={() => { setDrafts([...sections]); setManaging(false); }}>Cancel</Button>
          <Button type="button" onClick={async () => { if (await persistSections(drafts.filter((section) => section.name.trim()))) setManaging(false); }}>Save sections</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
```

plus a `deleting` confirm dialog (title `Delete section?`, confirm button `Delete section`, `variant="destructive"`, on confirm `persistSections(drafts.filter(s => s.id !== deleting.id))`) and a `leaving` confirm dialog (title `Leave ${label}?`, body "You will stop receiving its messages and lose access to its history. An admin can add you back.", confirm button `Leave channel`, on confirm `onLeaveRoom(leaving.roomId)` with pending + error states).

State: `const [managing, setManaging] = useState(false); const [drafts, setDrafts] = useState<RoomSection[]>([...sections]); const [newSection, setNewSection] = useState(""); const [deleting, setDeleting] = useState<RoomSection | null>(null); const [leaving, setLeaving] = useState<DirectoryRoom | null>(null);` — and reset `drafts` whenever `managing` flips true so a cancelled edit does not persist.

- [ ] **Step 5: Thread the callbacks through the context**

In `MessagingWorkspaceContext.tsx`, add to `directoryProps` (L307–320):

```tsx
    sections: rooms.sections,
    onSetPrefs: rooms.setRoomPrefs,
    onSaveSections: rooms.saveSections,
    onLeaveRoom: rooms.leaveRoom,
```

- [ ] **Step 6: CSS**

```css
.room-directory-grip { display: inline-flex; align-items: center; padding: 2px; color: var(--muted-foreground); cursor: grab; border-radius: 4px; }
.room-directory-grip:active { cursor: grabbing; }
.room-directory-group-header[data-dragging] { background: var(--cream-deeper); border-radius: 6px; }
.room-section-edit { display: flex; align-items: center; gap: 8px; }
@media (prefers-reduced-motion: reduce) { .room-directory-group-header { transition: none !important; } }
```

- [ ] **Step 7: Update the agent-facing surfaces doc**

In `apps/infra/openclaw/skills/lightbulb/references/surfaces.md`, add a row after "Message actions":

```
| Conversation list | flickmeon.ai/chat (left sidebar) | Conversations group as **Starred**, the owner's own named sections, **Channels**, then **Direct messages** (group DMs live there too). Each row's ⋯ menu holds Star, Mute, Mark unread, Copy link, Move to section, and — on channels the owner does not own — Leave. A muted conversation shows "Muted" and never badges or notifies; the server still delivers its events, so an agent's dispatch is never affected by mute. Sort is A–Z or unread-first, and sections reorder by dragging their header. Stars, mutes and section membership follow the owner to every device; collapse and sort are per-browser. |
```

In `apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md`, add the anchor line `  - Conversation list ....... references/surfaces.md` in the same block as `Message actions`.

- [ ] **Step 8: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging src/components/workspace && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src && pnpm exec tsc --noEmit`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging apps/frontend/src/components/workspace/__tests__ apps/infra/openclaw/skills/lightbulb/references/surfaces.md apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md
git commit -m "feat(messaging): custom sidebar sections, drag reorder and leave channel"
```

---

### Task 9: Room header — topic, facepile, star, bell, overflow

*Suggested model: sonnet — one extracted component with a clear contract, replacing existing markup.*

**Files:**
- Create: `apps/frontend/src/components/messaging/RoomHeader.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx:417-460` (header + tabs + the inline `room-members` panel), `:483-487` (drop the Details "Rename channel" section)
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx:321-363` (`roomProps`)
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Modify: `apps/infra/openclaw/skills/lightbulb/references/surfaces.md`
- Test: `__tests__/RoomHeader.test.tsx` (create), `__tests__/RoomPanel.test.tsx` (append)

**Interfaces:**
- Consumes: Task 6's `RoomSummary.topic/starred/muted`, `useRooms.setRoomPrefs` / `leaveRoom`, and Task 3's `PatchRoomRequest.topic`.
- Produces:
  ```ts
  export interface RoomHeaderProps {
    room?: RoomSummary;
    roomName: string;
    counterparts: readonly RoomParticipant[];
    participantCount: number;
    canEditRoom: boolean;          // owner/admin AND kind !== "dm"
    canLeave: boolean;             // kind === "channel" AND role !== "owner"
    actionable: boolean;
    pinnedCount: number;
    detailsOpen: boolean;
    search: ReactNode;             // the shipped <RoomMessageSearch />
    onOpenDetails: (tab?: "members" | "pins" | "files" | "links" | "about") => void;
    onSetPrefs?: (prefs: MemberPrefsRequest) => Promise<void>;
    onRenameRoom?: (name: string) => Promise<void>;
    onSetTopic?: (topic: string | null) => Promise<void>;
    onLeaveRoom?: () => Promise<void>;
  }
  ```
  `RoomPanelProps` gains `onSetPrefs?`, `onSetTopic?`, `onLeaveRoom?`, and `onOpenDetails` becomes internal state (`detailsTab`). **`onRenameRoom` stays on `RoomPanelProps`** but the context now supplies it for `channel` *and* `group-dm` (never `dm`).

- [ ] **Step 1: Write the failing tests**

`apps/frontend/src/components/messaging/__tests__/RoomHeader.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { RoomHeader } from "../RoomHeader";
import type { RoomParticipant, RoomSummary } from "../types";

const room: RoomSummary = {
  roomId: "room-1", name: "Roadmap", kind: "channel", roomRevision: 3, lastMessageSeq: 9,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: "Ship v2", starred: false, muted: false, sectionId: null,
};
const people: RoomParticipant[] = ["Aden", "Bo", "Cy", "Dee", "Eve"].map((name, index) => ({
  kind: "human", id: `u${index}`, displayName: name, mentionHandle: null, role: "member",
  listenMode: null, joinedAt: "2026-09-01T00:00:00Z", joinedSeq: 0,
}));
const props = {
  room, roomName: "Roadmap", counterparts: people, participantCount: 6, canEditRoom: true,
  canLeave: true, actionable: true, pinnedCount: 2, detailsOpen: false, search: <span />,
  onOpenDetails: vi.fn(),
};

it("shows the topic and at most four faces with the remaining count", () => {
  render(<RoomHeader {...props} />);
  expect(screen.getByText("Ship v2")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "View members, 6 in this conversation" })).toHaveTextContent("+2");
});

it("edits the topic and saves it", async () => {
  const onSetTopic = vi.fn().mockResolvedValue(undefined);
  render(<RoomHeader {...props} onSetTopic={onSetTopic} />);
  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  const field = screen.getByLabelText("Topic");
  await userEvent.clear(field);
  await userEvent.type(field, "Ship v3{Enter}");
  await waitFor(() => expect(onSetTopic).toHaveBeenCalledWith("Ship v3"));
});

it("clears the topic when the field is emptied and cancels on Escape", async () => {
  const onSetTopic = vi.fn().mockResolvedValue(undefined);
  render(<RoomHeader {...props} onSetTopic={onSetTopic} />);
  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  await userEvent.clear(screen.getByLabelText("Topic"));
  await userEvent.keyboard("{Enter}");
  await waitFor(() => expect(onSetTopic).toHaveBeenCalledWith(null));

  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  await userEvent.type(screen.getByLabelText("Topic"), "nope{Escape}");
  expect(onSetTopic).toHaveBeenCalledTimes(1);
});

it("offers an add-topic affordance when there is none", () => {
  render(<RoomHeader {...props} room={{ ...room, topic: null }} onSetTopic={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Add a topic" })).toBeInTheDocument();
});

it("toggles star and mute and keeps a word beside the icon state", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomHeader {...props} onSetPrefs={onSetPrefs} />);
  await userEvent.click(screen.getByRole("button", { name: "Star this conversation" }));
  expect(onSetPrefs).toHaveBeenCalledWith({ starred: true });
  rerender(<RoomHeader {...props} onSetPrefs={onSetPrefs} room={{ ...room, starred: true, muted: true }} />);
  expect(screen.getByRole("button", { name: "Unstar this conversation" })).toHaveAttribute("aria-pressed", "true");
  await userEvent.click(screen.getByRole("button", { name: "Unmute this conversation" }));
  expect(onSetPrefs).toHaveBeenLastCalledWith({ muted: false });
});

it("renames from the overflow menu and never offers it on a DM", async () => {
  const onRenameRoom = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomHeader {...props} onRenameRoom={onRenameRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
  const dialog = await screen.findByRole("dialog", { name: "Rename conversation" });
  await userEvent.clear(within(dialog).getByLabelText("Conversation name"));
  await userEvent.type(within(dialog).getByLabelText("Conversation name"), "Roadmap 2");
  await userEvent.click(within(dialog).getByRole("button", { name: "Save name" }));
  await waitFor(() => expect(onRenameRoom).toHaveBeenCalledWith("Roadmap 2"));

  rerender(<RoomHeader {...props} onRenameRoom={onRenameRoom} canEditRoom={false} room={{ ...room, kind: "dm", name: null }} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
  expect(screen.queryByRole("menuitem", { name: "Rename" })).not.toBeInTheDocument();
});

it("copies the room link and opens details from the overflow menu", async () => {
  const onOpenDetails = vi.fn();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  render(<RoomHeader {...props} onOpenDetails={onOpenDetails} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Copy link" }));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("room=room-1"));
  await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Details" }));
  expect(onOpenDetails).toHaveBeenCalledWith();
});
```

Append to `__tests__/RoomPanel.test.tsx`:

```tsx
it("labels the details tab and shows the pinned count on it", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, topic: null, starred: false, muted: false, sectionId: null }}
    pinnedMessageIds={["m-1", "m-2"]} />);
  expect(screen.getByRole("button", { name: /^Details/ })).toHaveTextContent("2");
  expect(screen.queryByRole("button", { name: "Context" })).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomHeader.test.tsx`
Expected: FAIL — `Cannot find module '../RoomHeader'`.

- [ ] **Step 3: Implement `RoomHeader.tsx`**

```tsx
"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Bell, BellOff, Hash, Link, LogOut, MessageCircle, MoreHorizontal, Pencil, Star, Users } from "lucide-react";
import type { MemberPrefsRequest } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { copyText, roomPermalink } from "./messageLinks";
import { ParticipantAvatar } from "./ParticipantAvatar";
import type { RoomParticipant, RoomSummary } from "./types";

const FACES = 4;

export interface RoomHeaderProps { /* exactly the Interfaces block above */ }

export function RoomHeader({ room, roomName, counterparts, participantCount, canEditRoom, canLeave, actionable, pinnedCount, detailsOpen, search, onOpenDetails, onSetPrefs, onRenameRoom, onSetTopic, onLeaveRoom }: RoomHeaderProps) {
  const id = useId();
  const [editingTopic, setEditingTopic] = useState(false);
  const [topicDraft, setTopicDraft] = useState(room?.topic ?? "");
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState(room?.name ?? "");
  const [leaving, setLeaving] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();
  const topicTrigger = useRef<HTMLButtonElement>(null);
  useEffect(() => { setTopicDraft(room?.topic ?? ""); }, [room?.topic]);

  async function run(action: () => Promise<void>, fallback: string, done?: () => void) {
    setPending(true);
    setError(undefined);
    try { await action(); done?.(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : fallback); }
    finally { setPending(false); }
  }

  const Icon = room?.kind === "channel" ? Hash : room?.kind === "group-dm" ? Users : MessageCircle;
  const faces = counterparts.slice(0, FACES);
  const overflow = Math.max(0, participantCount - faces.length);
  return <header className="room-header">
    <div className="room-title">
      {room?.kind === "dm" && counterparts.length === 1
        ? <ParticipantAvatar participant={counterparts[0]} size="sm" />
        : <Icon size={20} aria-hidden="true" />}
      <h2 className="font-semibold text-lg truncate" title={roomName}>{roomName}</h2>
      {editingTopic ? <form className="room-topic-form" onSubmit={(event) => {
        event.preventDefault();
        void run(async () => { await onSetTopic!(topicDraft.trim() || null); }, "The topic could not be saved.", () => setEditingTopic(false));
      }}>
        <label className="sr-only" htmlFor={`${id}-topic`}>Topic</label>
        <input id={`${id}-topic`} className="room-input" autoFocus maxLength={250} value={topicDraft} disabled={pending}
          onChange={(event) => setTopicDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setEditingTopic(false); setTopicDraft(room?.topic ?? ""); topicTrigger.current?.focus(); } }} />
      </form> : <>
        <span aria-hidden="true" className="room-topic-dot">·</span>
        {onSetTopic && canEditRoom
          ? <Button ref={topicTrigger} type="button" variant="ghost" className="room-topic" disabled={!actionable}
              aria-label={room?.topic ? "Edit topic" : "Add a topic"} onClick={() => setEditingTopic(true)}>
              {room?.topic ? <span className="truncate">{room.topic}</span> : <><Pencil size={12} aria-hidden="true" />Add a topic</>}
            </Button>
          : room?.topic ? <span className="room-topic truncate" title={room.topic}>{room.topic}</span> : null}
      </>}
    </div>
    <div className="room-audience">
      {search}
      <Button type="button" variant="ghost" className="room-facepile" aria-label={`View members, ${participantCount} in this conversation`}
        onClick={() => onOpenDetails("members")}>
        {faces.map((participant) => <ParticipantAvatar key={`${participant.kind}:${participant.id}`} participant={participant} size="sm" />)}
        {overflow > 0 && <span className="text-sm text-muted-foreground">+{overflow}</span>}
      </Button>
      {onSetPrefs && <Button type="button" variant="ghost" size="icon" disabled={!actionable || pending}
        aria-pressed={!!room?.starred} aria-label={room?.starred ? "Unstar this conversation" : "Star this conversation"}
        onClick={() => void run(async () => { await onSetPrefs({ starred: !room?.starred }); }, "The star could not be saved.")}>
        <Star aria-hidden="true" /></Button>}
      {onSetPrefs && <Button type="button" variant="ghost" size="icon" disabled={!actionable || pending}
        aria-pressed={!!room?.muted} aria-label={room?.muted ? "Unmute this conversation" : "Mute this conversation"}
        onClick={() => void run(async () => { await onSetPrefs({ muted: !room?.muted }); }, "The notification setting could not be saved.")}>
        {room?.muted ? <BellOff aria-hidden="true" /> : <Bell aria-hidden="true" />}</Button>}
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon" aria-label="Conversation options"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => void copyText(roomPermalink(room?.roomId ?? ""))}><Link aria-hidden="true" />Copy link</DropdownMenuItem>
          {onRenameRoom && canEditRoom && <DropdownMenuItem onSelect={() => { setNameDraft(room?.name ?? ""); setRenaming(true); }}><Pencil aria-hidden="true" />Rename</DropdownMenuItem>}
          <DropdownMenuItem onSelect={() => onOpenDetails()}>Details</DropdownMenuItem>
          {onLeaveRoom && canLeave && <><DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={() => setLeaving(true)}><LogOut aria-hidden="true" />Leave</DropdownMenuItem></>}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
    {error && <p role="alert" className="room-header-error text-sm text-err">{error}</p>}
    {/* Rename + Leave dialogs: same Dialog shape as RoomPanel's management dialog —
        title "Rename conversation" / "Leave {roomName}?", labelled input
        "Conversation name" (maxLength 200, required), confirm "Save name" /
        "Leave channel" (destructive), both wired through run(). */}
  </header>;
}
```

Write both dialogs out in full, mirroring `RoomPanel`'s shipped management dialog (`onOpenAutoFocus` / `onCloseAutoFocus` focus restoration, `disabled={!actionable || pending}`, pending labels "Saving…" / "Leaving…").

- [ ] **Step 4: Rewire `RoomPanel.tsx`**

Replace the `<header className="room-header">…</header>` block (L422–443) with `<RoomHeader … search={<RoomMessageSearch … />} />`, passing:

```tsx
        canEditRoom={visibleRoom?.kind !== "dm" && (viewer?.role === "owner" || viewer?.role === "admin")}
        canLeave={visibleRoom?.kind === "channel" && viewer?.role !== "owner"}
        pinnedCount={pinnedMessageIds.length}
        detailsOpen={!contextCollapsed}
        onOpenDetails={(tab) => { closeDetail(); setDetailsTab(tab ?? "members"); setContextCollapsed(false); }}
```

Delete the inline `membersOpen` state, the `room-members-trigger` button and the `{membersOpen && <section id={`${id}-members`} …>}` block (L440–441, L450–460) — the facepile replaces them and Task 11's Members tab is the single members surface. Delete the Details "Channel name" section (L483–487) — rename now lives in the header overflow.

In the tab bar (L444–449) rename the second tab to `Details` and append the pinned count:

```tsx
          onClick={() => { closeDetail(); setContextCollapsed(!contextCollapsed); }}>Details{pinnedMessageIds.length > 0 && <span className="room-count">{pinnedMessageIds.length}</span>}</button>
```

Add to `RoomPanelProps`: `pinnedMessageIds?: readonly string[]; onSetPrefs?: (prefs: MemberPrefsRequest) => Promise<void>; onSetTopic?: (topic: string | null) => Promise<void>; onLeaveRoom?: () => Promise<void>;` and destructure with `pinnedMessageIds = []`.

- [ ] **Step 5: Thread through the context**

In `MessagingWorkspaceContext.tsx`, replace the `rooms.activeRoom?.kind === "channel" && …` spread (L349–358) with:

```tsx
    pinnedMessageIds: rooms.activeRoom?.pinnedMessageIds ?? [],
    onSetPrefs: async (prefs) => { if (rooms.activeRoomId) await rooms.setRoomPrefs(rooms.activeRoomId, prefs); },
    onLeaveRoom: rooms.activeRoom?.kind === "channel" && viewerRole !== "owner"
      ? async () => { if (rooms.activeRoomId) await rooms.leaveRoom(rooms.activeRoomId); } : undefined,
    ...(rooms.activeRoom && rooms.activeRoom.kind !== "dm" && ["owner", "admin"].includes(viewerRole) ? {
      onRenameRoom: async (name: string) => { await rooms.updateRoom({ name }); },
      onSetTopic: async (topic: string | null) => { await rooms.updateRoom({ topic }); },
      ...(rooms.activeRoom.kind === "channel" ? {
        addMemberCandidates: directoryParticipants.filter((candidate) => !rooms.activeRoom!.members.some(
          (member) => member.kind === candidate.kind && member.id === candidate.id,
        )),
        onAddMember: addMember,
        onRemoveMember: rooms.removeMember,
      } : {}),
    } : {}),
```

with `const viewerRole = rooms.activeRoom?.members.find((member) => member.kind === "human" && member.id === userId)?.role ?? "";` above `roomProps`. (This is the one behaviour #2483 identified that Phase 3 keeps: member management stays channel-only, rename opens up to `group-dm`, and `dm` gets neither.)

- [ ] **Step 6: CSS**

```css
.room-topic { max-width: 32ch; gap: 4px; height: 28px; padding: 0 6px; color: var(--muted-foreground); font-size: .875rem; font-weight: 400; }
.room-topic-dot { color: var(--muted-foreground); }
.room-topic-form { flex: 1 1 200px; min-width: 0; }
.room-facepile { gap: 4px; height: 32px; padding: 0 6px; }
.room-facepile > :not(:first-child) { margin-left: -8px; }
.room-header-error { flex-basis: 100%; }
.room-tab .room-count { margin-left: 6px; }
```

- [ ] **Step 7: Surfaces doc**

In `surfaces.md`, append to the new "Conversation list" row's neighbour — add a **Room header** row:

```
| Room header | flickmeon.ai/chat (top of any conversation) | Kind icon, name, and a **topic** after a middle dot that owners and admins click to edit (channels and group DMs; a direct message has neither a name nor a topic). On the right: search, a member facepile that opens Details → Members, star, a bell that mutes, and a ⋯ menu with Copy link, Rename, Details and Leave. The tabs are Conversation · Details · Tasks, and Details carries the pinned count. |
```

Add `  - Room header ............. references/surfaces.md` to `MAINTENANCE.md`.

- [ ] **Step 8: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging src/components/workspace && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src && pnpm exec tsc --noEmit`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging apps/infra/openclaw/skills/lightbulb/references/surfaces.md apps/infra/openclaw/skills/lightbulb/MAINTENANCE.md
git commit -m "feat(messaging): room header with topic, facepile, star, bell and overflow"
```

---

### Task 10: Pin and "mark unread from here" in the message menu

*Suggested model: sonnet — one prop on a shipped component plus two wired menu items.*

The message toolbar shipped in #2476 as `.room-message-actions`, and its "more" menu lives inside `RoomMessageContent` and renders **only for the author's own messages**. Pin and mark-unread-from-here are available to every member, so the menu needs to exist for non-authors too.

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomMessageContent.tsx:10-17, 58-67`
- Modify: `apps/frontend/src/components/messaging/RoomMessageItem.tsx:18-58`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx:347-369` (`renderMessage`)
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (`roomProps`)
- Test: `__tests__/RoomMessageContent.test.tsx` (append), `__tests__/RoomPanel.test.tsx` (append)

**Interfaces:**
- Consumes: Task 4's `setPin` and Task 5's `markUnread(roomId, fromSeq)` via Task 6's hook.
- Produces:
  ```ts
  // RoomMessageContent
  menuExtras?: ReactNode;   // DropdownMenuItems rendered above Edit/Delete; the
                            // menu now renders when menuExtras is present even
                            // for a message the viewer did not write.
  // RoomMessageItem
  menuExtras?: ReactNode;   // passed straight through
  // RoomPanelProps
  onSetPin?: (messageId: string, pinned: boolean) => Promise<void>;
  ```

- [ ] **Step 1: Write the failing tests**

Append to `__tests__/RoomMessageContent.test.tsx`:

```tsx
it("opens the more menu for a message the viewer did not write when menuExtras exist", async () => {
  render(<RoomMessageContent message={{ ...message, author: { kind: "agent", id: "friday", displayName: "Friday" } }}
    currentUserId="me" actionable onUpdate={vi.fn()}
    menuExtras={<DropdownMenuItem onSelect={vi.fn()}>Pin to conversation</DropdownMenuItem>}><p>body</p></RoomMessageContent>);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  expect(await screen.findByRole("menuitem", { name: "Pin to conversation" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Edit message" })).not.toBeInTheDocument();
});

it("keeps edit and delete above nothing and extras above them on the viewer's own message", async () => {
  render(<RoomMessageContent message={message} currentUserId="me" actionable onUpdate={vi.fn()}
    menuExtras={<DropdownMenuItem onSelect={vi.fn()}>Pin to conversation</DropdownMenuItem>}><p>body</p></RoomMessageContent>);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  expect((await screen.findAllByRole("menuitem")).map((node) => node.textContent)).toEqual([
    "Pin to conversation", "Edit message", "Delete message",
  ]);
});
```

Append to `__tests__/RoomPanel.test.tsx`:

```tsx
it("pins and unpins a message from its menu", async () => {
  const onSetPin = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomPanel {...props} onSetPin={onSetPin} messages={[message]} pinnedMessageIds={[]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Pin to conversation" }));
  expect(onSetPin).toHaveBeenCalledWith(message.messageId, true);
  rerender(<RoomPanel {...props} onSetPin={onSetPin} messages={[message]} pinnedMessageIds={[message.messageId]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  expect(await screen.findByRole("menuitem", { name: "Unpin from conversation" })).toBeInTheDocument();
});

it("marks unread from a message using that message's sequence", async () => {
  const onMarkUnreadFrom = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} onMarkUnreadFrom={onMarkUnreadFrom} messages={[{ ...message, messageSeq: 7 }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Mark unread from here" }));
  expect(onMarkUnreadFrom).toHaveBeenCalledWith(7);
});

it("reports a failed pin without losing the message", async () => {
  const onSetPin = vi.fn().mockRejectedValue(new Error("A room can pin at most 50 messages."));
  render(<RoomPanel {...props} onSetPin={onSetPin} messages={[message]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Pin to conversation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("A room can pin at most 50 messages.");
  expect(screen.getByText(message.text)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomMessageContent.test.tsx src/components/messaging/__tests__/RoomPanel.test.tsx -t "menu"`
Expected: FAIL — the non-author menu trigger is absent.

- [ ] **Step 3: Implement `menuExtras`**

In `RoomMessageContent.tsx`, add `menuExtras?: ReactNode;` to the props type and destructure it. Replace the guard and the menu block (L58–67):

```tsx
    {!editing && (actions || menuExtras || (ownMessage && onUpdate)) && <div className="room-message-actions" role="group" aria-label="Message actions">
      {actions}
      {(menuExtras || (ownMessage && onUpdate)) && <DropdownMenu>
      <DropdownMenuTrigger asChild><Button ref={trigger} type="button" variant="ghost" size="icon-sm" disabled={!actionable || pending} aria-label="Message actions"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end" onEscapeKeyDown={(event) => event.stopPropagation()} onCloseAutoFocus={(event) => { if (request) event.preventDefault(); }}>
        {menuExtras}
        {ownMessage && onUpdate && <>
          <DropdownMenuItem onSelect={() => begin(message.text)}>Edit message</DropdownMenuItem>
          <DropdownMenuItem variant="destructive" onSelect={() => begin(null)}>Delete message</DropdownMenuItem>
        </>}
      </DropdownMenuContent>
    </DropdownMenu>}
    </div>}
```

In `RoomMessageItem.tsx`, add `menuExtras?: ReactNode;` to `RoomMessageItemProps`, destructure it, and pass `menuExtras={menuExtras}` to `<RoomMessageContent …>`.

- [ ] **Step 4: Wire it in `RoomPanel.tsx`**

Add to `RoomPanelProps`: `onSetPin?: (messageId: string, pinned: boolean) => Promise<void>; onMarkUnreadFrom?: (fromSeq: number) => Promise<void>;` and destructure. Add beside the other message-level error state:

```tsx
  const [messageActionError, setMessageActionError] = useState<string | null>(null);
  async function runMessageAction(action: () => Promise<void>, fallback: string) {
    setMessageActionError(null);
    try { await action(); }
    catch (error) { setMessageActionError(error instanceof Error ? error.message : fallback); }
  }
```

In `renderMessage`, add before `toolbarExtras`:

```tsx
      menuExtras={!message.deletedAt ? <>
        {onSetPin && <DropdownMenuItem onSelect={() => void runMessageAction(
          () => onSetPin(message.messageId, !pinnedMessageIds.includes(message.messageId)),
          "The pin could not be saved.",
        )}>{pinnedMessageIds.includes(message.messageId) ? "Unpin from conversation" : "Pin to conversation"}</DropdownMenuItem>}
        {onMarkUnreadFrom && !inThread && <DropdownMenuItem onSelect={() => void runMessageAction(
          () => onMarkUnreadFrom(message.messageSeq),
          "The conversation could not be marked unread.",
        )}>Mark unread from here</DropdownMenuItem>}
      </> : undefined}
```

and render the error once beside the copy-link status line (near L474):

```tsx
      {messageActionError && <p role="alert" className="room-typing text-sm text-err">{messageActionError}</p>}
```

Import `DropdownMenuItem` from `@/components/ui/dropdown-menu`.

> Mark-unread-from-here is **main-list only**: the read cursor is a room-level sequence, and a thread reply's `messageSeq` would silently mark the whole room unread from an arbitrary point in its history.

- [ ] **Step 5: Thread through the context**

In `MessagingWorkspaceContext.tsx`'s `roomProps`:

```tsx
    onSetPin: rooms.setPin,
    onMarkUnreadFrom: async (fromSeq: number) => { if (rooms.activeRoomId) await rooms.markUnread(rooms.activeRoomId, fromSeq); },
```

- [ ] **Step 6: Run, lint, scan, commit**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint src/components/messaging && node scripts/brand-tokens-scan.mjs src`
Expected: PASS; `0`.

```bash
git add apps/frontend/src/components/messaging
git commit -m "feat(messaging): pin and mark-unread-from-here in the message menu"
```

---

### Task 11: Details panel — Members · Pins · Files · Links · About

*Suggested model: sonnet — one extracted panel, five tabs, three of them derived from loaded history.*

**Files:**
- Create: `apps/frontend/src/components/messaging/RoomDetailsPanel.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx:480-527` (replace the Details `<aside>` body)
- Modify: `apps/frontend/src/components/messaging/messaging.css`
- Modify: `apps/infra/openclaw/skills/lightbulb/references/surfaces.md`
- Test: `__tests__/RoomDetailsPanel.test.tsx` (create)

**Interfaces:**
- Consumes: Task 4's `pinnedMessageIds`, Task 9's `detailsTab` state, Task 10's `onSetPin`.
- Produces:
  ```ts
  export type DetailsTab = "members" | "pins" | "files" | "links" | "about";
  export interface RoomDetailsPanelProps {
    room?: RoomSummary;
    roomName: string;
    tab: DetailsTab;
    onSelectTab: (tab: DetailsTab) => void;
    participants: readonly RoomParticipant[];
    currentUserId: string;
    actionable: boolean;
    messages: readonly RoomMessage[];
    pinnedMessageIds: readonly string[];
    activeRuns: readonly RoomRunView[];
    onOpenMessage: (messageId: string) => void;
    onUnpin?: (messageId: string) => Promise<void>;
    onLeaveRoom?: () => Promise<void>;
    members: ReactNode;          // the shipped Members section, lifted verbatim
  }
  export function extractLinks(messages: readonly RoomMessage[]): Array<{ href: string; label: string; messageId: string; at: string }>;
  ```
  **Pins** lists `pinnedMessageIds` resolved against `messages`, newest-pinned last, each row "Author · time" + first 120 chars, clicking jumps via `onOpenMessage`; an id not in loaded history renders "Message not loaded yet" with a Jump button that still calls `onOpenMessage`. **Files** lists every non-deleted message with `attachments?.length`. **Links** is `extractLinks` over non-deleted message text (`https?://` only, via the same `safeHref` the markdown renderer uses, deduped by href, newest first). **About** shows topic, created date, member count, and Leave.

- [ ] **Step 1: Write the failing tests**

`apps/frontend/src/components/messaging/__tests__/RoomDetailsPanel.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomDetailsPanel, extractLinks } from "../RoomDetailsPanel";
import type { RoomMessage, RoomSummary } from "../types";

const room: RoomSummary = {
  roomId: "room-1", name: "Roadmap", kind: "channel", roomRevision: 3, lastMessageSeq: 4,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: "Ship v2", starred: false, muted: false, sectionId: null,
};
const message = (id: string, extra: Partial<RoomMessage> = {}): RoomMessage => ({
  messageId: id, roomId: "room-1", messageSeq: Number(id.slice(1)), roomRevision: 1,
  author: { kind: "agent", id: "friday", displayName: "Friday" }, text: `body ${id}`,
  mentions: [], replyToMessageId: null, threadRootMessageId: null,
  createdAt: new Date(2026, 8, 11, 10, 0).toISOString(), runId: null, ...extra,
});
const props = {
  room, roomName: "Roadmap", tab: "pins" as const, onSelectTab: vi.fn(), participants: [],
  currentUserId: "me", actionable: true, pinnedMessageIds: [], activeRuns: [],
  onOpenMessage: vi.fn(), members: <p>Members go here</p>, messages: [] as RoomMessage[],
};

beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date(2026, 8, 11, 12, 0)); });
afterEach(() => { vi.useRealTimers(); });

it("lists pinned messages and unpins one", async () => {
  const onUnpin = vi.fn().mockResolvedValue(undefined);
  render(<RoomDetailsPanel {...props} messages={[message("m1"), message("m2")]} pinnedMessageIds={["m2"]} onUnpin={onUnpin} />);
  const pins = screen.getByRole("list", { name: "Pinned messages" });
  expect(within(pins).getAllByRole("listitem")).toHaveLength(1);
  expect(within(pins).getByText(/body m2/)).toBeInTheDocument();
  await userEvent.click(within(pins).getByRole("button", { name: "Unpin this message" }));
  expect(onUnpin).toHaveBeenCalledWith("m2");
});

it("keeps a pinned message that is not loaded yet reachable", async () => {
  const onOpenMessage = vi.fn();
  render(<RoomDetailsPanel {...props} messages={[]} pinnedMessageIds={["gone"]} onOpenMessage={onOpenMessage} />);
  expect(screen.getByText("Message not loaded yet")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Jump to message" }));
  expect(onOpenMessage).toHaveBeenCalledWith("gone");
});

it("shows an empty state per tab", () => {
  const { rerender } = render(<RoomDetailsPanel {...props} />);
  expect(screen.getByText("No pinned messages yet.")).toBeInTheDocument();
  rerender(<RoomDetailsPanel {...props} tab="files" />);
  expect(screen.getByText("No files shared yet.")).toBeInTheDocument();
  rerender(<RoomDetailsPanel {...props} tab="links" />);
  expect(screen.getByText("No links shared yet.")).toBeInTheDocument();
});

it("lists messages carrying attachments under Files", () => {
  render(<RoomDetailsPanel {...props} tab="files"
    messages={[message("m1"), message("m2", { attachments: [{ attachmentId: "a1", fileName: "plan.png", contentType: "image/png", byteSize: 10 }] })]} />);
  const files = screen.getByRole("list", { name: "Shared files" });
  expect(within(files).getAllByRole("listitem")).toHaveLength(1);
  expect(within(files).getByText("plan.png")).toBeInTheDocument();
});

it("shows the topic, created date and member count under About", () => {
  render(<RoomDetailsPanel {...props} tab="about" participants={[]} room={{ ...room }} />);
  expect(screen.getByText("Ship v2")).toBeInTheDocument();
  expect(screen.getByText("0 members")).toBeInTheDocument();
});

it("extracts, dedupes and orders links newest first", () => {
  const links = extractLinks([
    message("m1", { text: "see https://example.com/a and https://example.com/a again" }),
    message("m2", { text: "then https://example.com/b", createdAt: new Date(2026, 8, 11, 11, 0).toISOString() }),
    message("m3", { text: "javascript:alert(1) is not a link", deletedAt: null }),
    message("m4", { text: "https://example.com/c", deletedAt: new Date(2026, 8, 11, 11, 30).toISOString() }),
  ]);
  expect(links.map((link) => link.href)).toEqual(["https://example.com/b", "https://example.com/a"]);
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/RoomDetailsPanel.test.tsx`
Expected: FAIL — `Cannot find module '../RoomDetailsPanel'`.

- [ ] **Step 3: Implement `RoomDetailsPanel.tsx`**

```tsx
"use client";

import { useMemo, useState, type ReactNode } from "react";
import { Paperclip, PinOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import type { RoomMessage, RoomParticipant, RoomRunView, RoomSummary } from "./types";

export type DetailsTab = "members" | "pins" | "files" | "links" | "about";
const TABS: Array<{ id: DetailsTab; label: string }> = [
  { id: "members", label: "Members" }, { id: "pins", label: "Pins" },
  { id: "files", label: "Files" }, { id: "links", label: "Links" }, { id: "about", label: "About" },
];
const URL_PATTERN = /https?:\/\/[^\s<>()]+/g;

export function extractLinks(messages: readonly RoomMessage[]) {
  const seen = new Map<string, { href: string; label: string; messageId: string; at: string }>();
  for (const message of messages) {
    if (message.deletedAt) continue;
    for (const match of message.text.match(URL_PATTERN) ?? []) {
      const href = safeHref(match.replace(/[.,;:]+$/, ""));
      if (!href || seen.has(href)) continue;
      seen.set(href, { href, label: href.replace(/^https?:\/\//, ""), messageId: message.messageId, at: message.createdAt });
    }
  }
  return [...seen.values()].sort((left, right) => Date.parse(right.at) - Date.parse(left.at));
}
```

Then the component: a `<nav className="room-details-tabs" aria-label="Room details">` of five `aria-current` buttons calling `onSelectTab`, and a body switching on `tab`:

- `members` → `{members}` (the shipped Members section, passed in verbatim from `RoomPanel` so add/remove/listen-mode keep working).
- `pins` → `<ul aria-label="Pinned messages">`; for each id in `pinnedMessageIds` (reversed, newest pin first) find the message; a found one renders author, `toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })`, `text.slice(0, 120)`, a "Jump to message" button calling `onOpenMessage(id)`, and — when `onUnpin` — a `PinOff` icon button `aria-label="Unpin this message"`; a missing one renders `Message not loaded yet` plus the same Jump button. Empty: `No pinned messages yet.`
- `files` → `<ul aria-label="Shared files">` over `messages.filter((m) => !m.deletedAt && m.attachments?.length)`, each attachment's `fileName` with a `Paperclip` icon and a Jump button. Empty: `No files shared yet.`
- `links` → `<ul aria-label="Shared links">` over `extractLinks(messages)`, each an `<a href target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">` plus a Jump button. Empty: `No links shared yet.`
- `about` → topic (or `No topic yet.`), `new Date(room.updatedAt)`-independent created line only if the caller passes `createdAt` (it does not — render `{participants.length} members` and the topic, then the active-runs block lifted from `RoomPanel` L521-525, then Leave when `onLeaveRoom`).

Every list wraps in the shipped `.room-context-body` scroll container. Write the full component out — no placeholder branches.

- [ ] **Step 4: Rewire `RoomPanel.tsx`**

Replace the Details `<aside className="room-context" aria-label="Room details">` body (L480–527) with `<RoomDetailsPanel … members={<>{the existing Members <section> verbatim}</>} />`, keeping the `room-context-header` back button and `contextHeading` focus target untouched. Add `const [detailsTab, setDetailsTab] = useState<DetailsTab>("members");` and pass `tab={detailsTab} onSelectTab={setDetailsTab}`, `onUnpin={onSetPin ? (messageId) => onSetPin(messageId, false) : undefined}`, and `onOpenMessage={(messageId) => { setContextCollapsed(true); setSearchTarget({ messageId, threadRootMessageId: null, token: ++searchTargetToken.current }); }}` — which reuses the shipped scroll/highlight path rather than adding a second one.

- [ ] **Step 5: CSS**

```css
.room-details-tabs { display: flex; flex-wrap: wrap; gap: 12px; padding: 8px 16px; border-bottom: 1px solid var(--border); }
.room-details-tabs button { padding: 4px 2px; font-size: .875rem; color: var(--muted-foreground); border-bottom: 2px solid transparent; border-radius: 0; }
.room-details-tabs button[aria-current="page"] { color: var(--foreground); font-weight: 600; border-bottom-color: var(--foreground); }
.room-details-row { display: flex; align-items: flex-start; gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--border); }
.room-details-row p { min-width: 0; overflow-wrap: anywhere; }
```

- [ ] **Step 6: Surfaces doc**

In `surfaces.md`, append to the **Room header** row: " The Details tab holds Members, Pins, Files, Links and About — Pins are messages any member pinned (up to 50, and a pin is visible to everyone in the room); Files and Links are derived from the history already loaded, so they fill in as the owner scrolls back."

- [ ] **Step 7: Run the full frontend suite, lint, scan, build, commit**

Run: `cd apps/frontend && pnpm test && pnpm lint && node scripts/brand-tokens-scan.mjs src && pnpm exec tsc --noEmit && pnpm run build`
Expected: all green; `0`.

```bash
git add apps/frontend/src/components/messaging apps/infra/openclaw/skills/lightbulb/references/surfaces.md
git commit -m "feat(messaging): details panel with members, pins, files, links and about"
```

---

### Task 12 (controller): review, screenshots, ship Phase 3

*Suggested model: opus — judgement across three surfaces, a release decision, and two PR closures.*

**Files:** no new source; fixes land in the files Tasks 1–11 touched, plus the plan/spec deletions below.

- [ ] **Step 1: Whole-repo verification**

```bash
cd apps/backend && env -u ECS_CONTAINER_METADATA_URI_V4 ENVIRONMENT=test uv run pytest tests/ -q && uv run ruff check .
cd ../frontend && pnpm test && pnpm lint && node scripts/brand-tokens-scan.mjs src && pnpm exec tsc --noEmit && pnpm run build
cd ../.. && pnpm --filter @lightbulb/shared test && pnpm --filter @lightbulb/mobile exec tsc --noEmit
```
Expected: all green; the brand scan prints `0`. **Confirm no file under `apps/infra/lib/` was modified** (`git diff --stat origin/main -- apps/infra/lib`) — if anything is listed, `cd apps/infra && npx cdk synth` and commit the regenerated `.github/workflows/deploy.yml` in the same PR.

- [ ] **Step 2: Impeccable detect on the three surfaces**

```bash
impeccable detect --json --target apps/frontend/src/components/messaging
```
Triage against the design system: borders-only, 4px rhythm, no colour literals, focus-visible on every new control, ≥44px touch targets at ≤859px, `role`/`aria-label` on every new list and toggle. Fix material findings in one batch commit.

- [ ] **Step 3: Screenshot round on dev**

Sign in to `https://dev.flickmeon.ai/chat` (Clerk sign-in token per CLAUDE.md — never type a password or OTP). Capture at **1440×900** and **390×844**:
1. sidebar with a Starred group, one custom section, a muted room, unread-first sort, and a group collapsed;
2. the row overflow menu open;
3. the Manage sections dialog;
4. a channel header with a topic, facepile, star on, bell on, overflow open;
5. Details → Pins with two pins, and Details → Links;
6. the message menu showing Pin / Mark unread from here.

At 390 confirm the sidebar still collapses to `max-height: 36%`, the header wraps without clipping the name, and every menu is reachable by tap.

- [ ] **Step 4: Finish review**

Spawn `impeccable:impeccable-finish-reviewer` with the spec's "Sidebar", "Room header", "Details / thread panel" and "States and accessibility" sections as the contract, plus the Phase 3 acceptance line. Fix material findings in one batch.

- [ ] **Step 5: Acceptance check against the spec, by hand on dev**

- A starred room survives a reload **on another device** — star on desktop, reload the mobile web view signed in as the same user, confirm it is in Starred.
- A muted room **never notifies or badges** — send from a second account into a muted room; no toast, no native notification, no sidebar count. Confirm the agent still received it (an agent's dispatch never depends on mute).
- A pinned message **appears in Pins for every member** — pin as one member, open Details → Pins as the other.

- [ ] **Step 6: Delete the retired planning docs**

Per the repo's planning-docs policy, the spec and this plan are deleted when Phase 3 ships:

```bash
git rm docs/superpowers/specs/2026-09-11-slack-parity-messaging-design.md docs/superpowers/plans/2026-09-11-slack-parity-phase-3-sidebar-header-details.md
```

**Only if Phase 2 has already shipped.** If Phase 2 is still open, the spec is still its binding authority — delete this plan alone and leave the spec (and a one-line note in the PR body saying who deletes it). Phase 4 specs replace both.

- [ ] **Step 7: PR**

Tasks 1–5 may ship as one backend PR and 6–11 as one or two frontend PRs (the spec budgets 2–3 PRs); the backend PR must merge first. PR body:

```
## Phase 3 — sidebar, room header, details

Stars, mute, owner-defined sections with drag reorder, unread-first sort, a row
overflow menu and leave-channel in the sidebar; a real room header with topic,
facepile, star, bell and overflow; a Details panel with Members, Pins, Files,
Links and About; pin and "mark unread from here" in the message menu.

### Backend (five additive endpoints, no CDK change)
- `PATCH /messaging/rooms/{id}/me` — `starred`, `muted`, `sectionId` on the caller's MEMBER# row; `room.member.updated` to the caller's stream only.
- `PUT /messaging/me/sections` — ordered `{id,name}` list on a new `PREF#<user>` item (open decision 2).
- `PATCH /messaging/rooms/{id}` gains `topic` (≤250, owner/admin) and refuses renaming a DM.
- `PUT|DELETE /messaging/rooms/{id}/pins/{messageId}` — member-authorized, capped at 50.
- `POST /messaging/rooms/{id}/leave` — non-owner humans.
- `POST /messaging/rooms/{id}/unread` gains `fromSeq` for "mark unread from here".

No new table, no new GSI, no new attribute definition — every field lands on an
existing item shape in `lightbulb-{env}-messaging`, so `deploy.yml` is unchanged.

### Supersedes
Closes #2482 and #2483. Both split the sidebar into a third "Group chats"
section, which contradicts the spec's group order, and both leave Rename in the
Details body rather than the header overflow. #2483's DM-rename guard is kept —
see `patch_room` and `test_patch_room_sets_topic_alone_and_rejects_renaming_a_dm`.

### Verification
- backend `uv run pytest tests/ -q`; ruff clean
- frontend `pnpm test`, `pnpm lint`, `pnpm exec tsc --noEmit`, `pnpm run build`
- `node scripts/brand-tokens-scan.mjs src` → 0
- `pnpm --filter @lightbulb/mobile exec tsc --noEmit`
- impeccable `detect` clean; screenshots at 1440×900 and 390×844
- acceptance: starred room survives a reload on another device; a muted room never notifies or badges; a pinned message appears in Pins for every member
```

- [ ] **Step 8: Close the superseded PRs**

```bash
gh pr close 2482 --repo TheLightbulbCompany/lightbulb --comment "Superseded by #2483, and then by Phase 3 (<PR url>): the three-way sidebar split contradicts the spec's group order (Starred · Channels · Direct messages) and rename moves to the header overflow. No code from this PR is lost — the DM-rename guard ships in Phase 3."
gh pr close 2483 --repo TheLightbulbCompany/lightbulb --comment "Superseded by Phase 3 (<PR url>), which carries this PR's DM-rename guard verbatim. The sidebar split and the Details-body rename control are replaced by the spec's group order and header overflow."
```

Do not merge or modify either PR.

- [ ] **Step 9: Ship**

Merge (the `claude-review` job is the required check; CodeRabbit/Greptile are gone). Verify on dev. Then, and only when the deploy queue reads `0`:

```bash
gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 --json status --jq '[.[]|select(.status!="completed")]|length'
gh workflow run deploy.yml --ref main
```

Watch with `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status` (never `sleep`, never `tail`). Remember `main` is ahead of prod by default — confirm the promotion actually ran before telling the founder anything is live, then founder-check on `https://flickmeon.ai/chat`.

---

## Self-review

**Spec coverage — Sidebar:** order Starred · Channels · Direct messages · More → Task 7 (`groupRooms`; Drafts is Phase 2's, Assumption 7; "More" already exists in `WorkspaceSidebar`). Custom sections, collapsible, drag-reorderable → Tasks 7–8. Row anatomy (`#`, avatar, stacked avatars, muted styling with no badge, bold + count, active row `--cream-deeper`) → Task 7 `RoomDirectoryRow` + CSS (the lock icon is explicitly Phase 4; the draft pencil is Phase 2). Row overflow (Star, Mute, Mark unread, Move to section, Copy link, Leave) → Tasks 7–8. Sort alphabetical/unread-first → Task 7. `⌘K` quick switcher → **Phase 2**, out of scope (spec puts it in the Phase 2 acceptance line). Persistence on `MEMBER#` + sections on the user's prefs row → Tasks 1–2.

**Spec coverage — Room header:** kind icon + name + topic with click-to-edit for owner/admin → Task 9; `topic` on `PATCH /rooms/{id}` → Task 3; search · facepile · star · bell · overflow (Copy link, Rename, Leave, Details) → Task 9; tabs Conversation · Details · Tasks with the pinned count on Details → Task 9.

**Spec coverage — Details/thread:** Members (incl. add/remove/listen mode) → Task 11 (passed through verbatim, so the shipped behaviour is unchanged); Pins · Files · Links · About → Task 11; pins endpoints, member-authorized, cap 50 → Task 4; Pins tab fetching messages by id → Task 11 renders an explicit "Message not loaded yet" row with a working Jump rather than a second fetch path, because `useRooms.reconcile(0)` already loads a founder-scale room forward from seq 0. The thread panel's grouping and action bar shipped in Phase 1 and are untouched.

**Spec coverage — carried-over Phase 1 item:** "mark unread from here" → Tasks 5 + 10, extending #2475's endpoint and hook rather than adding a parallel one.

**Spec coverage — backend table:** all five Phase 3 rows are Tasks 1–4 plus Task 3's leave. Mute is client-side for badges and notifications only (Task 7's `groupRooms` zeroes a muted room's unread for sorting, `RoomDirectoryRow` suppresses the badge) and the server keeps sending events — asserted in Task 7's mute test and re-checked by hand in Task 12 Step 5.

**Spec coverage — states and accessibility:** hover/focus-visible/disabled/pending/error on every new control (Tasks 7–11 each assert an error path); loading/empty/error on every list (Task 11 asserts three empty states; `RoomDirectory`'s loading/error states are shipped and untouched); radix popovers throughout; reduced motion covered by the shipped rule plus Task 8's explicit drag rule; ≥44px touch targets from the shipped `@media (max-width: 859px)` rule; every status keeps a word (`Muted` tag, `aria-pressed` on star/bell, pinned count as text).

**Placeholder scan:** Task 9 Step 3 and Task 11 Step 3 each end with a prose spec for one dialog / one tab body plus an explicit "write it out in full — no placeholder branches" instruction and a test that fails if it is not written; every other code step is complete. Task 6 Step 5 and Task 8 Step 1 tell the implementer to match an existing test harness (`renderRooms`, the client `calls` recorder) rather than reproducing a harness this plan cannot see — the assertions are exact.

**Type consistency:** `set_member_prefs(room_id, kind, member_id, updates)` (Task 1 → 3 unaffected); `patch_room_meta(room_id, updates)` replaces `patch_room_name` at its single call site (Task 3, and Task 4's `set_pin` deliberately writes META directly rather than routing through it, because pins are a list-mutation under the same CAS); `mark_unread(..., from_seq=)` (Task 5 → 6 → 10); `MemberPrefsRequest` / `RoomSection` / `SectionsRequest` are the same names backend and frontend (Tasks 1, 2 → 6 → 7, 8); `groupRooms(rooms, sections, { sort, nameOf })` (Task 7 → 8); `RoomDirectoryRow` props are declared in full in Task 7 and only populated further in Task 8; `menuExtras` is the same prop name on `RoomMessageContent` and `RoomMessageItem` (Task 10); `DetailsTab` is defined once in Task 11 and imported by `RoomPanel` in the same task; `onSetPrefs` means "the active room" on `RoomPanelProps` and "(roomId, prefs)" on `RoomDirectoryProps` — deliberately different arities for different surfaces, and both are spelled out in their Interfaces blocks.

**Spec contradicted by code (one):** the pins row's `room.updated` live event does not exist; `room.metadata.changed` is reused (Assumption 1).
