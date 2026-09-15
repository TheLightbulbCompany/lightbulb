# Mobile Activity feed (server-backed) — design

Status: approved in chat 2026-09-15. Delete this file when the feature ships.

## What

The Activity tab lists **mentions of you** and **replies in threads you have
posted in**, newest first, each opening its message
(`2026-09-11-mobile-rooms-surface.md` §Tabs). Today the tab is a placeholder and
no server surface serves the data; the Home badge's mention count is in-memory
live events only.

## Write path — the messaging projector

`_notify_members` (apps/infra/lambda/messaging-projector/index.py) already
computes, per message, `mentioned` (human mention refs) and `participants`
(human authors in the thread). For each human member who passes its existing
membership / stream checks and is in either set and is not the author, it also
writes one activity row:

| attribute | value |
|---|---|
| `pk` | `ACTIVITY#<homeOwnerId>#<userId>` (home = member `homeOwnerId` or room owner, as the projector already resolves) |
| `sk` | `<createdAt>#<messageId>` — time-ordered, deterministic |
| `reason` | `mention` if in `mentioned`, else `thread_reply` |
| refs | `roomId`, `messageId`, `messageSeq`, `threadRootMessageId`, `roomOwnerId` |
| `expiresAt` | `createdAt + 30 days` (the table's TTL attribute) |

- Written with `UpdateItem` (`SET` of the same values), so a stream replay is a
  no-op. The projector already holds `dynamodb:UpdateItem` on the table; update
  the IAM comment in `api-stack.ts`, no new permission.
- Written regardless of read state and mute: activity is a record, not a
  notification. No message content in the row — content stays behind the
  authenticated read.
- A write failure is logged + metered and does not fail the record (the push
  and live publish must not regress). The row is best-effort; the canonical
  message is the truth.

## Read path — `GET /api/v1/messaging/activity`

Query `limit` (default 30, max 50) and `cursor` (the last `sk`).

1. `pk` from the authenticated caller only (resolved owner + user id) — never a
   client-supplied id.
2. `Query` the partition newest-first (`ScanIndexForward=False`).
3. For each row, **recheck membership** (`ROOM#<room> / MEMBER#human#<user>`
   exists, `messageSeq > joinedSeq`); drop rows that fail — a removed member
   stops seeing that room.
4. Hydrate the message from `ROOM#<room> / MSG#<seq>` through the same public
   projection room history uses (edits and deletes included), and the room's
   `kind` + display name.
5. `unread` = `messageSeq > READ#human#<user>.lastReadSeq`.

Response (camelCase, `schemas/`): `{ items: [{ activityId, reason, room:
{ roomId, kind, name }, message, unread }], nextCursor }`.

## Mobile

- `packages/shared`: `listActivity` on the messaging client + a `useActivity`
  hook in the existing hook style.
- `(tabs)/activity.tsx`: a `FlatList` of rows ("Aden mentioned you in #general" /
  "Aden replied in a thread in #general", snippet, relative time, unread dot),
  `contentInsetAdjustmentBehavior="automatic"`, pull-to-refresh, loading /
  empty / error states. Tap opens the message: room, or room then thread.
  Refetch on a `room.message.available` live event with `notify`.
- A 404 (backend not yet promoted) renders the empty state, not an error.

## Rollout

Backend + projector ship through `deploy.yml` (dev on merge, prod on the owner's
dispatch); mobile ships as an OTA after the prod promotion. The feed starts
empty and fills from new messages — no backfill.

## Tests

Projector: mention row, thread-reply row, mention wins over thread, author
excluded, non-member skipped, replay idempotent, write failure does not break
push. API: auth-derived partition, membership recheck drops removed rooms,
pagination cursor, unread flag, cross-workspace isolation. Mobile: rows,
empty/404, tap routing, refresh.
