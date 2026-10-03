# Google Meet via Recall — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An organizer adds `agents@{slug}.thelightbulb.company` to a Google Calendar event; the team accepts; at start time one "Acme agents · Lightbulb" bot joins the Meet, every agent is eligible behind it, people talk to agents by name exactly as in Live, and afterwards a room per meeting holds the notes and transcript.

**Architecture:** Recall.ai hosts the bot; its Output Media page is a LiveKit client that joins a per-meeting LiveKit room, so the existing `huddle-agent` worker, routing (`/route` addressing stage + Jev), per-agent GPT-Live voices, transcript persistence and late-answer posting serve a meeting unchanged. New: the reserved team address and `.ics` parsing on the SES path, a meeting row + scheduler, a `meeting_bots.py` adapter in front of Recall, a public ticket-authenticated page, a `setting: "meeting"` floor rule in the router, and the notes run at the end.

**Tech Stack:** Python/FastAPI backend (DynamoDB containers table, Secrets Manager, SES), the SES inbound Lambda, LiveKit Cloud (agents worker, rooms, data packets), Recall.ai Bot API (Output Media, status webhooks, participant/transcript events, chat), Next.js page, Hermes bridge (no change in Phase 1).

**Spec:** `docs/superpowers/specs/2026-10-02-google-meet-via-recall-design.md` — §0 (decisions of 10-02 evening) wins over any later section it contradicts. Research the rules come from: `.research/*` on this branch (Addlesee 2023/2024, capa, Bohus 2011, Skantze 2021, Eshghi & Healey 2016, Martinez & Kennedy 2020, Inoue 2025).

## Global Constraints

- Owner rulings (spec §0): ONE mailbox per workspace, `agents@{slug}` — per-agent addresses are removed (§0.10); everyone attends, no cap, CEO hosts; the only meeting-specific behaviours are the floor rule, the single tile with a holding cue, and the plumbing; meeting state is a hypothesis to measure, not built into routing in Phase 1; raised-hand rules per §0.5; a room per meeting, created when the invite arrives and carrying the meeting fields — there is NO meetings table and NO invite row; no sweeper — the bot is booked when the invite is parsed and Recall's webhooks drive the rest (§0.11); presence platform-paid, speech on the daily allowance (`voice_billing.FREE_SECONDS_PER_DAY = 600`).
- No bridge or plugin change in Phase 1. Skill TEXT changes (Task 1c) are baked into the image (`Dockerfile` `COPY skills/ /opt/hermes-skills/`), so they ship the normal way: `build-hermes-image` on merge → pin flip → `roll_hermes_runtimes.py`. `reapply_hermes_skills_dirs.py` does NOT carry skill text (it only patches `skills.external_dirs`). Runtimes on the old image keep working: the `/u/{owner}/address` and `/inbox` routes stay backward compatible.
- Recall: `recording_config.retention: null` (ZDR); `metadata` carries only `roomId` (the meeting room's id — an opaque uuid; the room META holds the owner and the UID); API key and webhook secret live in Secrets Manager (`lightbulb/{env}/recall_api_key`, `lightbulb/{env}/recall_webhook_secret`); never logged, never on a page or a runtime.
- Meeting URLs are secrets: validate `https://meet.google.com/xxx-xxxx-xxx`, never log in full (last 4 chars only), keep only in the room META's `meeting.meetingUrl`, never in a message. `RoomDetail`/`RoomSummary` are explicit contract models, so `meeting` is not projected today; any task that exposes `meeting` to a client or a live event projects it WITHOUT `meetingUrl`.
- Public page route `/meet-bot` bypasses Clerk (`middleware.ts`) and is authenticated by a signed, room-and-bot-bound ticket (Task 8) — nothing stored.
- CI is the test gate; run only the targeted test files named in each task locally. Codex is the only reviewer.

## Review Focus

1. An invite from someone outside the workspace to `agents@` must create no bot (money) and no room; it is stored as team mail and the CEO sees it. — pinned by Task 3's `test_a_strangers_invite_schedules_nothing`.
2. A rescheduled or cancelled event (`SEQUENCE` bump, `METHOD:CANCEL`) must move or cancel the bot, never create a second one. — Task 6's reschedule/leave tests.
3. The `/meet-bot` page ticket must be bound to one room and one bot and die with the meeting; a wrong or expired ticket gets nothing. — Task 8's `test_ticket_is_room_and_bot_bound`.
4. In a meeting, an unaddressed human question must wake nobody for 3.5 s and exactly one agent after; an addressed name still summons at once. — Task 9's `test_unclaimed_question_waits_then_routes_once` and Task 10's `test_meeting_setting_disables_the_lone_speaker_fallback`.
5. The daily allowance must meter agent speech in a meeting the same way as Live and never meter presence. — Task 11's `test_presence_is_not_billed_and_speech_draws_the_allowance`.

---

### Task 0: Recall account and spike (owner + manual, half a day)

**Files:** none in the repo.

- [ ] **Step 1:** Owner creates a Recall.ai pay-as-you-go account (5 free hours) and pastes the API key into the opened 600-mode file; the operator loads it with `aws secretsmanager create-secret --name lightbulb/prod/recall_api_key --secret-string file://<path>` (and `lightbulb/dev/...`), then `rm -P <path>`. Create the webhook signing secret the same way once the webhook endpoint exists (Task 7).
- [ ] **Step 2:** Ask Recall support (Slack) to enable **separate audio per participant** for the workspace (feature flag; spec [A1]) and apply for the startup rate if the owner says yes (§16.8).
- [ ] **Step 3:** With Task 8's page deployed to dev, create one ad-hoc bot by hand against a Meet the owner opens; measure: echo (does the page hear the bot's own voice?), round trip (say "Friday, what's today's date?" → first audio), CPU on `web_4_core`. Record the numbers in the spec §7 "measure" bullets.

---

### Task 1: The reserved team address `agents@{slug}`

**Files:**
- Modify: `apps/backend/core/services/agent_email.py`
- Modify: `apps/backend/core/repositories/agent_email_repo.py` (lookup of the team address)
- Test: `apps/backend/tests/unit/services/test_agent_email_team_address.py`

**Interfaces:**
- Produces: `agent_email.team_address(owner_id) -> str | None` (`agents@{slug}.{AGENT_EMAIL_DOMAIN}`, None when no slug/domain), `agent_email.RESERVED_LOCAL_PARTS = frozenset({"agents", "team", "lightbulb", "meetings"})`, and `mint_agent_email_address` refusing a reserved local part with `reason="reserved"`.
- Produces: `agent_email_repo.is_team_address(address) -> tuple[str, str] | None` → `(owner_id, slug)` by resolving the subdomain through `company_slug` (the existing slug table), without a per-agent row.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from unittest.mock import AsyncMock
from core.services import agent_email

@pytest.mark.asyncio
async def test_team_address_is_agents_at_the_company_subdomain(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(agent_email, "slug_for_owner", AsyncMock(return_value="acme"))
    assert await agent_email.team_address("org_1") == "agents@acme.thelightbulb.company"

@pytest.mark.asyncio
async def test_an_agent_named_agents_cannot_take_the_team_mailbox(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(agent_email, "slug_for_owner", AsyncMock(return_value="acme"))
    address, reason = await agent_email.mint_agent_email_address(owner_id="org_1", agent_id="a1", agent_name="Agents")
    assert address is None and reason == "reserved"
```

- [ ] **Step 2: Run them to see them fail** — `cd apps/backend && uv run pytest tests/unit/services/test_agent_email_team_address.py -q` → FAIL (`team_address` undefined).
- [ ] **Step 3: Implement** — in `agent_email.py` add `RESERVED_LOCAL_PARTS`, `team_address()` built from `build_agent_email_address(slug=slug, agent_name="agents", domain=domain)`, and in `mint_agent_email_address` return `(None, "reserved")` when `_agent_slug(agent_name) in RESERVED_LOCAL_PARTS`. In `agent_email_repo.py` add `is_team_address(address)` that splits `local@sub.domain`, requires `local == "agents"` and resolves `sub` to an owner through the company-slug repository (`company_slug_repo.get_owner_by_slug` — add if absent).
- [ ] **Step 4: Run the tests to see them pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(mail): reserved team address agents@{slug}"`.

---

### Task 1b: One mailbox per workspace — the backend

Owner ruling 2026-10-03 (spec §0.10): per-agent addresses are removed. `agents@{slug}` is the only
address; every agent sends from it with its own display name; inbound goes to the CEO unless it is
a reply to a thread another agent started. Survey of every per-agent assumption: the Explore
report of 10-03 (paths below are its findings).

**Files:**
- Modify: `apps/backend/core/services/agent_email.py` — delete `build_agent_email_address`, `_agent_slug`, `mint_agent_email_address`, `RESERVED_LOCAL_PARTS` and the per-agent `resolve_from_address`; keep `team_address`, `sender_allowed`, `resolve_company_name`, `resolve_agent_name`.
- Modify: `apps/backend/core/repositories/agent_email_repo.py` — `put` writes the one row (`address` = team address, `owner_id`, `verified_senders`, `send_to_anyone`, `status`, no `agent_id`); `is_team_address` stays for the inbound branch.
- Modify: `apps/backend/core/services/ses_email.py` — set `Message-ID` via `email.utils.make_msgid(domain=settings.AGENT_EMAIL_DOMAIN)` and return THAT (the RFC id) instead of the SES id (every caller only tests truthiness).
- Modify: `apps/backend/core/repositories/agent_mail_inbox_repo.py` — `put_sent(address, message_id, owner_id, agent_id, to, subject)` stores an item with `direction="out"` so the existing `by-message-id` GSI resolves a later `In-Reply-To`/`References` root to the writing agent; `list_messages` skips `direction == "out"`.
- Modify: `apps/backend/core/services/agent_mail_send.py`, `agent_mail_reply.py`, `agent_mail_owner_send.py`, `agent_mail_autoreply.py` — From becomes `agent_email.from_header(owner_id, agent_id)` = `email.utils.formataddr((f"{agent_name} · {company} agents", team_address))`; every policy flag is read from the one row; every send records `put_sent`.
- Modify: `apps/backend/routers/ses_inbound.py` — the team branch is the ONLY branch (delete the per-agent `get_by_address` loop); the target agent is the thread's writer (`find_by_message_id` on `References[0]` else `In-Reply-To`, item `direction == "out"` → its `agent_id`) else the CEO; the complaint handler disables the one row; `meeting_invites.team_row` becomes `ensure_team_row(owner_id, owner_email)` (create-if-absent).
- Modify: `apps/backend/routers/agent_mail.py` — `GET /agent-mail/address` and `PATCH /agent-mail/address` (workspace-level; the `{agent_id}` path segment goes); `POST /u/{owner}/address` returns the team address for any agent; `GET /u/{owner}/inbox?agent_id=` filters items by `agent_id`.
- Modify: `apps/backend/core/services/owner_facts.py` — `email_address` is the team address; drop `_lookup_email_address`'s oldest-row sort.
- Create: `apps/backend/scripts/delete_per_agent_mail_rows.py` — one-off: `scan_all()`, delete every row whose local part is not `agents` (`--dry-run` default, `--apply` to delete); run once per env after deploy, then DELETE the script (CLAUDE.md: spent one-shots go).
- Tests: rewrite `tests/unit/services/test_agent_email.py`, `test_agent_email_team_address.py`, `test_agent_mail_send.py`, `test_agent_mail_reply.py`, `test_owner_facts.py`, `tests/unit/repositories/test_agent_email_repo.py`, `tests/unit/routers/test_agent_mail.py`, `test_ses_inbound.py` (team tests); `test_hermes_inbound_contract.py` unchanged in contract.

**Interfaces:**
- Produces: `agent_email.ensure_team_address(owner_id, owner_email) -> tuple[str | None, str]` (reasons `ok | disabled | slug_allocation_failed | mint_failed`), `agent_email.from_header(owner_id, agent_id) -> str`, `agent_mail_inbox_repo.put_sent(...)`, `agent_mail_inbox_repo.writer_of(message_id) -> str | None` (agent id).
- Consumes: Task 1's `team_address`, `is_team_address`.

- [ ] **Step 1: Failing tests** — `from_header` formats `"Friday · Acme agents" <agents@acme.thelightbulb.company>`; a free send records `put_sent` with the returned Message-ID and the agent id; an inbound whose `In-Reply-To` matches a sent item is delivered to THAT agent, one without a match to the CEO; the one row's `outbound_disabled` blocks every agent; `GET /agent-mail/address` returns the single row; the cleanup script's dry run lists per-agent rows and deletes nothing.
- [ ] **Step 2–5:** implement, run only the files above, commit — `feat(mail): one mailbox per workspace, agents send as themselves from agents@`.

---

### Task 1c: One mailbox — skill text, app surface, cleanup

**Files:**
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/email.md` (lines 3–12, 27–57: "the team address `agents@<company-slug>.thelightbulb.company` is shared by every agent; you send as yourself from it; mail that replies to something you sent comes back to you, everything else goes to the CEO"), `references/company-and-plans.md:66-68`, `scripts/lightbulb-mail.sh` (`no_address` and the `Control → Agents → Email` wording → `Control → Agents → Team email`), the sibling `MAINTENANCE.md` anchor list.
- Modify: `apps/frontend/src/components/control/panels/AgentsPanel.tsx` — delete the per-agent `email` tab; add a "Team email" card at the top: the address with a copy button, "Invite the team to a meeting by adding this address to the calendar event", who-can-wake (verified senders), the open-email checkbox, re-enable after a complaint — the existing `AgentEmailSection` rewired to `GET/PATCH /agent-mail/address` with no `agentId`.
- Modify: `apps/frontend/src/content/docs/pages/browser-and-apps.md:21-23`, `skills-and-automations.md:22,40`.
- Tests: `AgentEmailSection.test.tsx` rewritten; `apps/infra/hermes/tests/test_agent_mail.py` wording.
- After merge: build the skills, run `scripts/reapply_hermes_skills_dirs.py` (no roll), run `delete_per_agent_mail_rows.py --apply` in dev then prod, then delete the script in a follow-up commit.

- [ ] **Step 1–5:** failing render test (card shows the address and copies it; no per-agent email tab) → implement → pass → commit — `feat(app): the team email card; agents' mail skill speaks of one shared address`.

---

### Task 2: SES Lambda extracts the calendar part

**Files:**
- Modify: `apps/infra/lambda/ses-inbound/index.py` (`_build_payload`)
- Test: `apps/infra/lambda/ses-inbound/test_calendar_part.py` (the Lambda's existing test file layout)

**Interfaces:**
- Produces: payload field `calendar: str | None` — the first `text/calendar` part's text, UTF-8, capped at 65536 bytes; `None` when absent. Nothing else in the payload changes.

- [ ] **Step 1: Failing test** — build a `multipart/alternative` message with `text/plain` + `text/calendar; method=REQUEST` (a minimal `BEGIN:VCALENDAR…END:VCALENDAR` body) and assert `_build_payload(ses, raw)["calendar"].startswith("BEGIN:VCALENDAR")`; a second message without the part → `calendar is None`; a 100 KB part → truncated to 65536 bytes.
- [ ] **Step 2: Run** → FAIL (`KeyError: 'calendar'`).
- [ ] **Step 3: Implement** — in `_build_payload`, `for part in msg.walk(): if part.get_content_type() == "text/calendar": calendar = part.get_content()[:65536]; break`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(ses-inbound): carry the first text/calendar part as payload.calendar`.

---

### Task 3: Invite parsing; the meeting ROOM is the record (no invite row)

Owner ruling 2026-10-03 ("why ddb invite row?"): there is no meetings table and no `__meeting__#`
row. A room per meeting is required anyway (spec §0.6), so the room is created when the invite is
parsed and carries the meeting fields; its id is derived from the calendar UID the way the vendor
room's is, so the next invite for the same UID finds it without an index.

**Files:**
- Create: `apps/backend/core/services/meeting_invites.py`
- Modify: `apps/backend/core/repositories/messaging_repo.py` (`meeting_room_id`, an `extra` META kwarg on `create_room`)
- Modify: `apps/backend/routers/ses_inbound.py` (team-address branch after the spam/DMARC gates)
- Test: `apps/backend/tests/unit/services/test_meeting_invites.py`

**Interfaces:**
- Produces: `meeting_invites.parse(calendar_text) -> Invite | None` with fields `uid, sequence: int, method ("REQUEST"|"CANCEL"), start: datetime, end: datetime | None, summary, description, organizer_email, attendee_emails: list[str], meeting_url: str | None` (from a validated `X-GOOGLE-CONFERENCE`, else the first `https://meet.google.com/[a-z]{3}-[a-z]{4}-[a-z]{3}` in `LOCATION`/`DESCRIPTION`). Pure, total (any malformed input → None, never raises), stdlib only; `RECURRENCE-ID` → None; Windows TZIDs → None (ponytail comment); UID > 512 chars → None; attendees capped at 100.
- Produces: `messaging_repo.meeting_room_id(owner_id, uid)` = `uuid5(NAMESPACE_URL, f"lightbulb-meeting|{owner_id}|{uid}")`; room META field `meeting = {uid, sequence, title, startAt, endAt, meetingUrl, organizer, createdBy, status, recallBotId?, botError?, rsvpSequence?}`, status `scheduled | bot_created | in_call | ended | cancelled | bot_failed`; updated with `patch_room_meta`.
- Produces: `meeting_invites.handle_team_mail(owner_id, payload, team) -> str | None` — runs AFTER the route's spam/DMARC gates; the DMARC-verified, `authenticated_sender` From must be a workspace member (Clerk org membership, primary email) or on the team row's `verified_senders` — the ICS organizer/attendees are sender-written and never trusted. A member's `REQUEST` → create the room (name `"{summary} — {Mon D}"`, kind `channel`, the From member as owner, other member attendees as members, every starter agent in `mentions`) with `meeting` on the META and one system line ("Agents will join **{title}** on {local time}."); a later `REQUEST` with a higher `SEQUENCE` → patch `meeting` + "moved to" line; `CANCEL` → status `cancelled` + line; stale `SEQUENCE` → `meeting_stale`; no room on `CANCEL` → `meeting_ignored`. Returns None ("this is mail") for no/unparseable calendar part, a REQUEST without a Meet URL, or a stranger — the route then delivers it as ordinary team mail to the CEO through the unchanged per-agent path.

- [ ] **Step 1: Failing tests** — Google and Outlook fixtures, UTC/date-only, cancel, stale, `RECURRENCE-ID`, `SEQUENCE:abc`, `TZID=America`, non-Meet `X-GOOGLE-CONFERENCE`, oversized UID; `test_a_strangers_invite_schedules_nothing` (ICS names a member as ORGANIZER and ATTENDEE while From is a stranger → no `create_room`, CEO mail); member From with an outsider ORGANIZER → scheduled; the room is created once with the deterministic id, the `meeting` META and the member set; a bump patches `startAt`; CANCEL patches `cancelled`.
- [ ] **Step 2–5:** implement, run, commit — `feat(meetings): the meeting room is the record; no invite rows`.

---

### Task 4: (folded into Task 3 and Task 6)

Sequence ordering, cancellation and stale handling shipped in Task 3; the bot reschedule/leave
hooks live in Task 6, which is where `meeting_bots` exists. No conditional write on `sequence`:
SES hands the Lambda one message per invocation and same-event updates arrive seconds apart;
add one if a concurrent-delivery reorder is ever observed.

---

### Task 5: RSVP from `agents@`

**Files:**
- Create: `apps/backend/core/services/meeting_rsvp.py` (uses `agent_mail_send`'s SES tenant and sender plumbing with `From: agents@{slug}`)
- Test: `apps/backend/tests/unit/services/test_meeting_rsvp.py`

**Interfaces:**
- Produces: `meeting_rsvp.accept(owner_id, invite, room_id)` → sends a `text/calendar; method=REPLY` with `ATTENDEE;PARTSTAT=ACCEPTED:mailto:agents@…` for the invite's `UID`/`SEQUENCE` to the organizer, plus a one-line `text/plain` ("Your Lightbulb agents will join. Notes will land in your Lightbulb workspace afterwards."). Sent by `handle_team_mail` after a `meeting_scheduled` outcome. Idempotent per `(uid, sequence)` via `meeting.rsvpSequence` on the room META (`patch_room_meta`). Transport: `ses_email.send_ses_email` gains one optional `calendar: str | None` kwarg → `msg.add_alternative(calendar, subtype="calendar", params={"method": "REPLY"})`; From is the team address with the owner's SES tenant (`agent_mail_tenant.ensure_tenant`), headers `AGENT_AUTHORED_HEADERS`; not counted against the agent daily send cap (one per invite, member-gated).

- [ ] **Step 1: Failing test** — the SES send is awaited once with a `REPLY` calendar part naming the UID and `PARTSTAT=ACCEPTED`; a second call with the same sequence sends nothing.
- [ ] **Step 2–5:** implement, run, commit — `feat(meetings): agents@ accepts the invite`.

---

### Task 6: `meeting_bots.py` — the Recall adapter, event-driven (no sweeper)

Owner ruling 2026-10-03: **no polling loop.** Recall is already the scheduler — a bot created
with a `join_at` more than 10 minutes out is reserved and joins itself; a sooner one is ad-hoc and
joins now; a move is a PATCH, a cancel a DELETE. So the bot is created the moment Task 3 parses the
invite, and Recall's webhooks (Task 7) drive everything after. State lives on the meeting room's
META (`meeting.*`, Task 3), never in a separate row.

**Files:**
- Create: `apps/backend/core/services/meeting_bots.py`
- Modify: `apps/backend/core/services/meeting_invites.py` (`handle_team_mail` calls `create` on `meeting_scheduled` with no `recallBotId`, `reschedule` when `startAt` changed and a `recallBotId` exists, `leave` on `meeting_cancelled` with a `recallBotId`; results written with `patch_room_meta`)
- Modify: `apps/backend/core/config.py` (`RECALL_API_BASE = "https://us-east-1.recall.ai"` — the region of the owner's account, `MEET_BOT_PAGE_URL`)
- Test: `apps/backend/tests/unit/services/test_meeting_bots.py`

**Interfaces:**
- Produces: `meeting_bots.create(meeting) -> str` (POST `/api/v1/bot/` with `meeting_url`, `bot_name=f"{company} agents · Lightbulb"`, `join_at` (start − 2 min, ISO; omitted when that is already past, so Recall joins now), `recording_config={"retention": None, "audio_separate_raw": {}, "realtime_endpoints": [...]}` only once the per-participant flag is on (Task 0), `output_media={"camera": {"kind": "webpage", "config": {"url": f"{MEET_BOT_PAGE_URL}#t={ticket}"}}}`, `variant={"google_meet": "web_4_core"}`, `automatic_leave={"waiting_room_timeout": 600, "noone_joined_timeout": 300}`, `chat={"on_bot_join": {"send_to": "everyone", "message": CONSENT_LINE, "pin": True}}`, `metadata={"roomId": room_id}`), `reschedule(bot_id, join_at)` (PATCH `/api/v1/bot/{id}/`), `leave(bot_id)` (DELETE `/api/v1/bot/{id}/` for a not-yet-joined bot, POST `/api/v1/bot/{id}/leave_call/` for a joined one), `verify_webhook(headers, body) -> bool` (Svix-style `webhook-id/-timestamp/-signature` HMAC per spec [A16]).
- META transitions (`patch_room_meta`): `create` ok → `meeting.status = bot_created`, `meeting.recallBotId`; any Recall failure (507 pool empty, 4xx, timeout) → `bot_failed` with `botError`, one `meeting.bot_failed{code}` metric, and a system line in the meeting room ("Couldn't book a bot for this meeting: <reason>. Forward the invite again to retry."). A re-sent invite (same UID, any SEQUENCE) on a `bot_failed` room retries `create`. The page URL carries Task 8's signed ticket, minted here from `(room_id, bot_id)` — nothing is stored for it.
- `CONSENT_LINE` (≤500 chars): "Lightbulb agents are in this meeting for <company>. They listen for their names; notes and a transcript go to <company>'s Lightbulb workspace. Recall.ai relays the call and stores nothing."

- [ ] **Step 1: Failing tests** — the create request body (URL, name, ZDR, metadata only `roomId`, page URL carries a ticket, no API key in logs via `caplog`); a meeting 12 min out carries `join_at`, one 1 min out carries none; 507 → `bot_failed` META, no raise; a SEQUENCE bump that moves `startAt` on a `bot_created` row calls `reschedule` once with the bot id; CANCEL on a `bot_created` row calls `leave` once; a re-sent invite on `bot_failed` calls `create` again; webhook signature verification accept/reject.
- [ ] **Step 2–5:** implement with `httpx.AsyncClient` and the key read once from Secrets Manager (`secrets_manager.get`), run, commit — `feat(meetings): Recall bots are booked when the invite arrives`.

---

### Task 7: Recall status webhook → LiveKit room and worker dispatch

**Files:**
- Create: `apps/backend/routers/meetings.py` (`POST /api/v1/webhooks/recall`, `DELETE /api/v1/internal/meetings/{roomId}` — the worker's idle leave)
- Modify: `apps/backend/core/services/livekit_huddles.py` (`meet_room_name(room_id) = f"meet-{env}-{room_id}"`, `dispatch_meet(room_id, metadata)`)
- Modify: `apps/backend/main.py` (mount the router)
- Test: `apps/backend/tests/unit/routers/test_meetings_webhook.py`

**Interfaces:**
- Every webhook names the room through `data.bot.metadata.roomId`; `messaging_repo.get_room(roomId).meeting` is the state; a webhook whose `bot_id` is not that room's `recallBotId` is a 200 no-op.
- On `in_call_recording` (or `in_call_not_recording`): dispatch the worker to `meet-{env}-{roomId}` with metadata `{roomId, ownerId, roomToken: room_token(roomId, ownerId), source: "meet", company: <name>}`; `meeting.status = in_call`; publish `room.meeting.changed` to the meeting room; call `voice_calls.prewake_owner(owner_id)` so the roster is warm before anyone speaks.
- On `call_ended`/`done`/`fatal`: `meeting.status = ended` (or `bot_failed` with `botError = sub_code` for `fatal`), close the LiveKit room, trigger the notes run (Task 12), metric `meeting.ended{sub_code}`.
- Unsigned or replayed webhooks → 401/200-noop (idempotent on `(bot_id, status)`).

- [ ] **Step 1: Failing tests** — a signed `in_call_recording` dispatches once with the right metadata and prewakes once; a replay dispatches nothing; `fatal` with `google_meet_knocking_disabled` sets `bot_failed` and posts a plain-language line into the meeting room; a bad signature is 401; a webhook for an unknown or mismatched bot is a 200 no-op.
- [ ] **Step 2–5:** implement, run, commit — `feat(meetings): Recall webhook drives the LiveKit room and worker`.

---

### Task 8: The `/meet-bot` page and its ticket exchange

**Files:**
- Create: `apps/frontend/src/app/meet-bot/page.tsx` (no Clerk; plain `livekit-client`)
- Modify: `apps/frontend/src/middleware.ts` (add `/meet-bot` to the public matcher)
- Create: `apps/backend/routers/meet_bot.py` (`POST /api/v1/meet-bot/session {ticket}` → `{serverUrl, roomName, token}`)
- Modify: `apps/backend/core/services/livekit_huddles.py` (`mint_meet_token(room_id)` — identity `meet:{room_id}`, grants: join + publish audio + publish data + subscribe, TTL 6 h; `meet_ticket(room_id, bot_id)` / `verify_meet_ticket(ticket)`)
- Test: `apps/backend/tests/unit/routers/test_meet_bot.py`, `apps/frontend/src/app/meet-bot/__tests__/page.test.tsx`

**Interfaces:**
- Ticket: stateless — `base64url(room_id|bot_id|exp)` + HMAC-SHA256 under the LiveKit API secret (`hmac.compare_digest` on verify), `exp` = meeting end + 6 h, minted by Task 6's `create` and put on the page URL. The exchange verifies the signature and expiry, loads the room, and refuses unless `meeting.recallBotId == bot_id` and `meeting.status in {bot_created, in_call}` — so the ticket dies with the meeting, a reconnecting bot (Recall reloads the page) can re-exchange, and nothing is stored or marked.
- The page: on load, `fetch('/api/v1/meet-bot/session', {ticket})` → `room.connect(serverUrl, token)` → `getUserMedia({audio: {echoCancellation: false}})` published as the mic track → subscribe to the worker's audio track and play it in an `<audio autoplay>` → render the tile from participant attributes `lightbulb.agent.<id>.status` (reuse `LiveStrip`'s status mapping; show the speaking agent's name and avatar large, "thinking" as the holding cue, "listening" otherwise) → forward Recall's transcript websocket lines (`wss://meeting-data.bot.recall.ai/api/v1/transcript`, available inside the Output Media page per spec [A2]) as data packets on topic `meet.speaker` `{name, text, final}`.

- [ ] **Step 1: Failing tests** — backend: a valid ticket returns a token scoped to `meet-{env}-{roomId}`; `test_ticket_is_room_and_bot_bound` (a ticket for another bot id, a tampered signature, an expired one, and one for a room whose status is `ended` → 401; a token's room grant equals the meet room; a second exchange while `in_call` succeeds). Frontend: renders the speaking agent's name from attributes; plays the subscribed track.
- [ ] **Step 2–5:** implement, run (`pnpm test -- meet-bot`), commit — `feat(meet-bot): the bot's page is a LiveKit client with a ticket`.

---

### Task 9: Worker meeting mode

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/agent.py` (read `source`, `roomId`, `company` from job metadata; `humans()` counts `meet:*` participants as humans)
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py` (`setting="meeting"`: unclaimed-question timer, no presence posts, `ROOM_IDLE_SECONDS` 60 min, idle action = `backend.leave_meeting()`; speaker labels from `meet.speaker` data packets; on leave, POST the whole transcript once with `final: true` — the worker holds it in memory, the backend never stores lines mid-call)
- Modify: `apps/huddle-agent/src/huddle_agent/backend.py` (`leave_meeting()` → `DELETE /internal/meetings/{roomId}`; `/route` body gains `setting`; `transcript(..., final=True)`)
- Test: `apps/huddle-agent/tests/test_meeting_mode.py`

**Interfaces:**
- Consumes: Task 7's metadata; Task 8's `meet.speaker` packets (`{name, text, final}`), used to label transcript lines ("Priya (Meet): …") and to tell `/route` who spoke; STT timing still from the worker's own STT on the `meet:*` track.
- Produces: `UNCLAIMED_QUESTION_S = 3.5` — a final line with no agent named, from a human, that `transcript.is_question()` (ends with `?` or starts with who/what/when/where/why/how/can/could/should/is/are/do/does) starts a timer; any human final before expiry cancels it; on expiry `/route` with `unclaimed: true`. Lines that are not questions wake nobody.

- [ ] **Step 1: Failing tests** — `test_unclaimed_question_waits_then_routes_once` (clock-driven; a second human line within 3.5 s cancels), `test_a_named_agent_still_answers_at_once`, `test_meeting_lines_carry_meet_speaker_names`, `test_meeting_idle_leaves_after_60_minutes_via_the_backend`.
- [ ] **Step 2–5:** implement, run `uv run pytest tests/test_meeting_mode.py tests/test_huddle.py -q`, commit — `feat(live): meeting mode — floor rule, speaker names, leave on idle`.

---

### Task 10: Router `setting: "meeting"`

**Files:**
- Modify: `apps/backend/core/services/voice_calls.py` (`route_live_line(..., setting)`), `apps/backend/core/services/jev.py` (`route(..., setting)`: with `setting == "meeting"` the lone-speaker fallback is off; `unclaimed` uses the existing `question_to_room` policy)
- Modify: `apps/backend/routers/huddles.py` (`RouteBody.setting: Literal["live","meeting"] = "live"`, `unclaimed: bool = False`)
- Test: `apps/backend/tests/unit/services/test_route_meeting_setting.py`

- [ ] **Step 1: Failing tests** — `test_meeting_setting_disables_the_lone_speaker_fallback` (one human, no name, Jev says none → nobody), `test_unclaimed_question_in_a_meeting_picks_at_most_one`, `test_a_name_anywhere_still_addresses` (addressing stage unchanged).
- [ ] **Step 2–5:** implement, run, commit — `feat(routing): meeting setting`.

---

### Task 11: Billing — presence platform-paid, speech on the allowance

**Files:**
- Modify: `apps/backend/core/billing/voice_billing.py` (no change to `FREE_SECONDS_PER_DAY`; `budget_seconds` already covers summons in any setting)
- Modify: `apps/backend/core/services/meeting_bots.py` (`meeting.presence.seconds` metric from `joining_call` to `done`, no Metronome event in Phase 1)
- Test: `apps/backend/tests/unit/services/test_meeting_billing.py`

- [ ] **Step 1: Failing test** — `test_presence_is_not_billed_and_speech_draws_the_allowance`: a 30-minute meeting with 3 minutes of agent speech → the daily counter rises by 180 s, no Metronome call, the presence metric is 1800 s.
- [ ] **Step 2–5:** implement, run, commit — `feat(meetings): presence metered to us, speech to the allowance`.

---

### Task 12: Transcript and notes in the meeting room

The room itself exists since Task 3 (created when the invite arrived, agents in `mentions`).

**Files:**
- Modify: `apps/backend/routers/huddles.py` (`/internal/huddles/{room}/transcript` with `final: true` on a meeting room → `meeting_notes.on_transcript(room_id, lines)`; interim batches keep today's debug logging only)
- Create: `apps/backend/core/services/meeting_notes.py` (`on_transcript`: upload `transcript.txt` as a room attachment via the existing attachment store; `on_ended` (Task 7): dispatch ONE CEO room run with the request "Write the meeting notes for <title>: decisions, open questions, action items as Paperclip tasks for their owners, two paragraphs max; the transcript is attached" using `openclaw_work._dispatch_message`'s follow-up path — idempotent on `meeting.notesRunId`; reply to the organizer from `agents@` with the room link)
- Test: `apps/backend/tests/unit/services/test_meeting_notes.py`

- [ ] **Step 1: Failing tests** — a final transcript attaches once; `on_ended` dispatches exactly one notes run even when called twice; the organizer reply carries the room URL and no meeting URL.
- [ ] **Step 2–5:** implement, run, commit — `feat(meetings): transcript and notes land in the meeting room`.

---

### Task 13: (folded into Task 1c — the team email card carries the "invite the team" copy)

---

### Task 14: Dev acceptance and the founder flag

- [ ] **Step 1:** Behind `MEETINGS_ENABLED` (CDK env, default `false` in prod, `true` in dev): `meeting_bots.create` and the webhook refuse when off (an invite still becomes a room and an RSVP is still sent — the flag gates money).
- [ ] **Step 2:** In dev (prod LiveKit project is shared; Recall is one account): invite `agents@<founder-dev-slug>.dev.thelightbulb.company` to a real Meet; verify RSVP, bot in lobby, admission, "Friday, what's today's date?" answered, a second person's question, an unclaimed question routed after 3.5 s, hang-up → notes in the meeting room, organizer reply. Receipts on the PR.
- [ ] **Step 3:** Flip `MEETINGS_ENABLED` for prod via the usual CDK env change once the owner has run one founder meeting.

---

## Self-review

- **Spec coverage:** §0.1 + §0.10 → Tasks 1, 1b, 1c, 2, 3, 5; §0.2 → Task 3 (everyone seated) + Task 9/10 (CEO hosts via the existing CEO rule); §0.3 floor rule → Tasks 9–10, tile → Task 8, plumbing → Tasks 6–8; §0.4 → notes in Task 12, state-for-routing deliberately absent (measure first); §0.5 raised hand → not in Phase 1 (Live PR first, shared); §0.6 → Tasks 3 and 12; §0.7 lobby → Task 6/7 (auto-admit upgrade is Phase 2); §0.9 → Task 11; §0.11 → Task 6; spec §11 security → Tasks 6, 8; §12 consent → `CONSENT_LINE` in Task 6; §14 failure modes → Task 7 (`fatal` codes) and Task 6 (`bot_failed`).
- **Placeholders:** none; every task names its files, interfaces and tests.
- **Type consistency:** the meeting room id (`messaging_repo.meeting_room_id(owner_id, uid)`) is the meeting's id everywhere after Task 3 — Recall `metadata.roomId`, `meet-{env}-{roomId}` the LiveKit room, `meet:{roomId}` the page participant, `DELETE /internal/meetings/{roomId}`; `meeting.*` on the room META is the only state; `setting` is `"live" | "meeting"` in both the worker's `/route` body and the backend.
- **Review Focus:** each of the five risks is pinned to a named test in Tasks 3, 6, 8, 9/10 and 11.
