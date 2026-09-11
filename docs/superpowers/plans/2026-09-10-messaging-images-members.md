# Messaging continuation: images and channel members

Continue the approved native messaging architecture and deployed reference-style UI. User priorities after founder use: images and adding members to existing channels. No new approval/design cycle required.

## Global Constraints

- Reuse existing canonical messaging, upload, member authorization, and OpenClaw paths; do not build a parallel chat system.
- Preserve workspace/room isolation. Validate uploads server-side; do not expose public objects or trust client owner/path/MIME claims.
- User authorizes parallel worktrees, reviewed PR merges, production dispatch and founder production smoke tests. Focused unit checks and targeted type checks only; do not wait for broad CI/E2E/dev tests.
- Keep current messages/threads/mentions/send retries intact. No placeholder controls.
- Separate worktrees; no edits to dirty main. No new dependency unless existing/native capabilities cannot do the job.

### Task 1: Add people and agents to existing channels

Trace existing upsert-member endpoint, shared client, useRooms and workspace roster. Add an accessible add-members action in channel member/details UI, excluding current members and the current owner as appropriate. Use explicit wire fields only. Preserve server authorization and show useful pending/errors. Agents default to existing safe listen-mode behavior. Reuse current roster rather than a new endpoint. Update room state from response. Do not offer adding to one-to-one DMs. Focused tests cover selection, success, failure, duplicate exclusion and permission behavior. Implementer owns membership UI/provider/hook edits, not image composer/storage.

### Task 2: Real image attachments in room and thread messages

Trace existing upload storage and old chat image-to-OpenClaw support before choosing minimal reuse. Implement image selection/paste (drag/drop if existing component supports it), preview/remove, bounded upload with visible failures, persisted attachment rendering after refresh and full-size view. Cover room and thread composer. Agents must receive the actual supported image content/reference via the existing OpenClaw capability, not a filename-only illusion; report contract changes before implementing if needed. Use private authorized retrieval, allowlisted raster formats and existing size limits or a justified bounded limit. Block sends while pending uploads and preserve failed drafts/retry idempotency. Never inline untrusted SVG/HTML. Focused tests cover validation, attachment-only send, persisted retrieval/authorization and gateway forwarding. Own attachment contracts/backend/storage/composer and separate image renderer; coordinate any RoomPanel/provider overlap with Task 1 before editing.

### Task 3: Native image ingress adapter

Extend only the in-repo lightbulb-channel plugin; the pinned OpenClaw base already supports structured media. Canonical messages may carry `attachments` (default empty) with `{id,fileName,contentType,size}`. Validate UUIDs, raster content type, count <=4 and size <=5MiB; derive container path `/home/node/.openclaw/media/messaging/attachments/{roomId}/{id}{ext}` using png/jpeg/webp/gif -> .png/.jpg/.webp/.gif mapping, never accept client paths. This uses the existing native allowed media root. Pass current-message attachments as native `media` to buildChannelInboundEventContext. History accepts the metadata contract without accidentally reattaching historical images to the current turn. Preserve strict input parsing and receipt/idempotency behavior. Cover valid media forwarding and rejected path/metadata cases with focused tests. Rebuild extended image and update founder after review; no upstream base fork change.

## Review and release

Independent task reviews and combined integration review; fix material findings, merge ready slices and dispatch production. Verify a real founder channel membership add and a room/thread image upload, refresh rendering, and agent image understanding. Do not claim features shipped until observed. Keep unrelated reaction/project grouping scope deferred.
