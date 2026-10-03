# Google Meet via Recall — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An organizer adds `agents@{slug}.thelightbulb.company` to a Google Calendar event; the team accepts; at start time one "Acme agents · Lightbulb" bot joins the Meet, every agent is eligible behind it, people talk to agents by name exactly as in Live, and afterwards a room per meeting holds the notes and transcript.

**Architecture:** Recall.ai hosts the bot; its Output Media page is a LiveKit client that joins a per-meeting LiveKit room, so the existing `huddle-agent` worker, routing (`/route` addressing stage + Jev), per-agent GPT-Live voices, transcript persistence and late-answer posting serve a meeting unchanged. New: the reserved team address and `.ics` parsing on the SES path, a meeting row + scheduler, a `meeting_bots.py` adapter in front of Recall, a public ticket-authenticated page, a `setting: "meeting"` floor rule in the router, and the notes run at the end.

**Tech Stack:** Python/FastAPI backend (DynamoDB containers table, Secrets Manager, SES), the SES inbound Lambda, LiveKit Cloud (agents worker, rooms, data packets), Recall.ai Bot API (Output Media, status webhooks, participant/transcript events, chat), Next.js page, Hermes bridge (no change in Phase 1).

**Spec:** `docs/superpowers/specs/2026-10-02-google-meet-via-recall-design.md` — §0 (decisions of 10-02 evening) wins over any later section it contradicts. Research the rules come from: `.research/*` on this branch (Addlesee 2023/2024, capa, Bohus 2011, Skantze 2021, Eshghi & Healey 2016, Martinez & Kennedy 2020, Inoue 2025).

## Global Constraints

- Owner rulings (spec §0): one team address `agents@`, reserved; everyone attends, no cap, CEO hosts; the only meeting-specific behaviours are the floor rule, the single tile with a holding cue, and the plumbing; meeting state is a hypothesis to measure, not built into routing in Phase 1; raised-hand rules per §0.5; a room per meeting; presence platform-paid, speech on the daily allowance (`voice_billing.FREE_SECONDS_PER_DAY = 600`).
- Nothing new on the Hermes image in Phase 1 (no roll needed).
- Recall: `recording_config.retention: null` (ZDR); only `meetingId` in `metadata`; API key and webhook secret live in Secrets Manager (`lightbulb/{env}/recall_api_key`, `lightbulb/{env}/recall_webhook_secret`); never logged, never on a page or a runtime.
- Meeting URLs are secrets: validate `https://meet.google.com/xxx-xxxx-xxx`, never log in full, keep only on the meeting row.
- Public page route `/meet-bot` bypasses Clerk (`middleware.ts`) and is authenticated by a single-use 256-bit ticket.
- CI is the test gate; run only the targeted test files named in each task locally. Codex is the only reviewer.
- Container-table rows follow the existing `__kind__#id` key convention (`owner_id` partition key, no TTL attribute on this table — rows are deleted explicitly).

## Review Focus

1. An invite from someone outside the workspace to `agents@` must create no bot (money) and no room; it is stored as team mail and the CEO sees it. — pinned by Task 3's `test_a_strangers_invite_schedules_nothing`.
2. A rescheduled or cancelled event (`SEQUENCE` bump, `METHOD:CANCEL`) must move or cancel the bot, never create a second one. — Task 4's `test_a_sequence_bump_moves_the_bot_and_cancel_removes_it`.
3. The `/meet-bot` page ticket must be single-use and scoped to one LiveKit room; a replay or a wrong ticket gets nothing. — Task 8's `test_ticket_is_single_use_and_room_scoped`.
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

### Task 3: Invite parsing and the meeting row

**Files:**
- Create: `apps/backend/core/services/meeting_invites.py`
- Create: `apps/backend/core/repositories/meetings_repo.py`
- Modify: `apps/backend/routers/ses_inbound.py` (team-address branch before the per-agent recipient resolution)
- Test: `apps/backend/tests/unit/services/test_meeting_invites.py`

**Interfaces:**
- Produces: `meeting_invites.parse(calendar_text) -> Invite | None` with fields `uid, sequence: int, method ("REQUEST"|"CANCEL"), start: datetime, end: datetime | None, summary, description, organizer_email, attendee_emails: list[str], meeting_url: str | None` (from `X-GOOGLE-CONFERENCE`, else the first `https://meet.google.com/[a-z]{3}-[a-z]{4}-[a-z]{3}` in `LOCATION`/`DESCRIPTION`). Pure functions, stdlib only (unfolded lines, `;`-parameters stripped, `TZID` handled via `zoneinfo`, `Z` as UTC).
- Produces: `meetings_repo.put_meeting(row)`, `get_meeting(owner_id, uid)`, `list_due(now, within_s)`, `set_status(...)`; key `owner_id = f"__meeting__#{owner_id}#{uid}"`; fields `ownerId, uid, sequence, title, startAt, endAt, meetingUrl, organizer, attendees, status ("scheduled"|"bot_created"|"in_call"|"ended"|"cancelled"|"refused"), recallBotId, roomId, ticketHash, createdBy`.
- Produces: `meeting_invites.handle_team_mail(owner_id, payload)` — invite with `method=REQUEST` from a workspace member → put/update the meeting row and schedule (Task 6), RSVP accepted (Task 5); `CANCEL` → cancel; no calendar part → `agent_mail_inbound.deliver` to the CEO (existing delivery, agent = `starter_agent_ids["ceo"]`).

- [ ] **Step 1: Failing tests** — `test_parse_google_invite` (a real Google-shaped ICS fixture: `X-GOOGLE-CONFERENCE`, `ORGANIZER;CN=…:mailto:…`, folded `DESCRIPTION`), `test_parse_outlook_invite` (Meet URL in `LOCATION`), `test_cancel_is_recognized`, `test_a_strangers_invite_schedules_nothing` (organizer not a member → `put_meeting` not awaited, `deliver` awaited once).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** parser (≈60 lines) + repo + `handle_team_mail`; in `ses_inbound.py`, before the per-agent recipient loop: `team = await agent_email_repo.is_team_address(candidate)` → `await meeting_invites.handle_team_mail(owner_id, body)` and return `_final("team_mail")`.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** — `feat(meetings): invites to agents@ become meeting rows; strangers' mail stays mail`.

---

### Task 4: Updates and cancellations are idempotent

**Files:** `meeting_invites.py`, `meetings_repo.py`, test file from Task 3.

- [ ] **Step 1: Failing test** — `test_a_sequence_bump_moves_the_bot_and_cancel_removes_it`: a second REQUEST with `SEQUENCE:1` and a new `DTSTART` → the row's `startAt` changes, `meeting_bots.reschedule` called once with the Recall bot id; a `CANCEL` → `meeting_bots.leave` + status `cancelled`; a stale REQUEST (`SEQUENCE:0` after `1`) → ignored.
- [ ] **Step 2–4:** implement with a conditional update on `sequence < :new` (DynamoDB `ConditionExpression`), run, pass.
- [ ] **Step 5: Commit** — `feat(meetings): sequence-ordered updates and cancellations`.

---

### Task 5: RSVP from `agents@`

**Files:**
- Create: `apps/backend/core/services/meeting_rsvp.py` (uses `agent_mail_send`'s SES tenant and sender plumbing with `From: agents@{slug}`)
- Test: `apps/backend/tests/unit/services/test_meeting_rsvp.py`

**Interfaces:**
- Produces: `meeting_rsvp.accept(owner_id, invite)` → sends a `text/calendar; method=REPLY` with `ATTENDEE;PARTSTAT=ACCEPTED:mailto:agents@…` for the invite's `UID`/`SEQUENCE` to the organizer, plus a one-line `text/plain` ("Your Lightbulb agents will join. Notes will land in your Lightbulb workspace afterwards."). Idempotent per `(uid, sequence)` via a `rsvpSequence` field on the row.

- [ ] **Step 1: Failing test** — the SES send is awaited once with a `REPLY` calendar part naming the UID and `PARTSTAT=ACCEPTED`; a second call with the same sequence sends nothing.
- [ ] **Step 2–5:** implement, run, commit — `feat(meetings): agents@ accepts the invite`.

---

### Task 6: `meeting_bots.py` — the Recall adapter and the scheduler

**Files:**
- Create: `apps/backend/core/services/meeting_bots.py`
- Modify: `apps/backend/main.py` (register the `meetings_sweeper` in `BACKGROUND_TASKS`)
- Modify: `apps/backend/core/config.py` (`RECALL_API_BASE = "https://us-east-1.recall.ai"` — the region of the owner's account, `MEET_BOT_PAGE_URL`)
- Test: `apps/backend/tests/unit/services/test_meeting_bots.py`

**Interfaces:**
- Produces: `meeting_bots.create(meeting) -> str` (POST `/api/v1/bot/` with `meeting_url`, `bot_name=f"{company} agents · Lightbulb"`, `join_at` (start − 2 min, ISO), `recording_config={"retention": None, "audio_separate_raw": {}, "realtime_endpoints": [...]}` only once the per-participant flag is on (Task 0), `output_media={"camera": {"kind": "webpage", "config": {"url": f"{MEET_BOT_PAGE_URL}#t={ticket}"}}}`, `variant={"google_meet": "web_4_core"}`, `automatic_leave={"waiting_room_timeout": 600, "noone_joined_timeout": 300}`, `chat={"on_bot_join": {"send_to": "everyone", "message": CONSENT_LINE, "pin": True}}`, `metadata={"meetingId": uid}`), `reschedule(bot_id, join_at)`, `leave(bot_id)`, `verify_webhook(headers, body) -> bool` (Svix-style `webhook-id/-timestamp/-signature` HMAC per spec [A16]). On HTTP 507 (ad-hoc pool empty) raise `BotPoolEmpty`; the sweeper retries every 30 s up to 10 times.
- Produces: `meeting_bots.sweep(now)` — every 60 s: `list_due(now, within_s=15*60)` with status `scheduled` → `create` → status `bot_created`; store `recallBotId` and the hashed ticket (Task 8 mints it).
- `CONSENT_LINE` (≤500 chars): "Lightbulb agents are in this meeting for <company>. They listen for their names; notes and a transcript go to <company>'s Lightbulb workspace. Recall.ai relays the call and stores nothing."

- [ ] **Step 1: Failing tests** — the create request body (URL, name, ZDR, metadata only `meetingId`, page URL carries a ticket, no API key in logs via `caplog`), 507 → `BotPoolEmpty`, sweeper creates for a meeting 10 min out and not for one 2 h out, webhook signature verification accept/reject.
- [ ] **Step 2–5:** implement with `httpx.AsyncClient` and the key read once from Secrets Manager (`secrets_manager.get`), run, commit — `feat(meetings): Recall adapter and the bot scheduler`.

---

### Task 7: Recall status webhook → LiveKit room and worker dispatch

**Files:**
- Create: `apps/backend/routers/meetings.py` (`POST /api/v1/webhooks/recall`, `GET /api/v1/meetings?roomId=`, `DELETE /api/v1/meetings/{uid}`)
- Modify: `apps/backend/core/services/livekit_huddles.py` (`meet_room_name(uid) = f"meet-{env}-{uid}"`, `dispatch_meet(uid, metadata)`)
- Modify: `apps/backend/main.py` (mount the router)
- Test: `apps/backend/tests/unit/routers/test_meetings_webhook.py`

**Interfaces:**
- On `in_call_recording` (or `in_call_not_recording`): create the meeting room (Task 12 does the room creation helper; here call `meeting_rooms.ensure(owner_id, meeting)`), dispatch the worker to `meet-{env}-{uid}` with metadata `{roomId: <meeting room id>, ownerId, roomToken: room_token(roomId, ownerId), source: "meet", meetingId: uid, company: <name>}`; status `in_call`; publish `room.meeting.changed` to the meeting room.
- On `call_ended`/`done`/`fatal`: status `ended`/`refused` (with `sub_code`), close the LiveKit room, trigger the notes run (Task 12), metric `meeting.ended{sub_code}`.
- Unsigned or replayed webhooks → 401/200-noop (idempotent on `(bot_id, status)`).

- [ ] **Step 1: Failing tests** — a signed `in_call_recording` dispatches once with the right metadata; a replay dispatches nothing; `fatal` with `google_meet_knocking_disabled` sets `refused` and posts a plain-language line into the meeting room; a bad signature is 401.
- [ ] **Step 2–5:** implement, run, commit — `feat(meetings): Recall webhook drives the LiveKit room and worker`.

---

### Task 8: The `/meet-bot` page and its ticket exchange

**Files:**
- Create: `apps/frontend/src/app/meet-bot/page.tsx` (no Clerk; plain `livekit-client`)
- Modify: `apps/frontend/src/middleware.ts` (add `/meet-bot` to the public matcher)
- Create: `apps/backend/routers/meet_bot.py` (`POST /api/v1/meet-bot/session {ticket}` → `{serverUrl, roomName, token}`)
- Modify: `apps/backend/core/services/livekit_huddles.py` (`mint_meet_token(uid)` — identity `meet:{uid}`, grants: join + publish audio + publish data + subscribe, TTL 6 h)
- Test: `apps/backend/tests/unit/routers/test_meet_bot.py`, `apps/frontend/src/app/meet-bot/__tests__/page.test.tsx`

**Interfaces:**
- Ticket: 32 random bytes, base64url, minted by Task 6's sweeper, stored as `sha256` on the meeting row with `ticketUsedAt`; the exchange marks it used; a second exchange within the meeting's lifetime is allowed only from the same bot session (Recall reloads the page on reconnect — allow re-exchange while status is `in_call`, refuse after `ended`).
- The page: on load, `fetch('/api/v1/meet-bot/session', {ticket})` → `room.connect(serverUrl, token)` → `getUserMedia({audio: {echoCancellation: false}})` published as the mic track → subscribe to the worker's audio track and play it in an `<audio autoplay>` → render the tile from participant attributes `lightbulb.agent.<id>.status` (reuse `LiveStrip`'s status mapping; show the speaking agent's name and avatar large, "thinking" as the holding cue, "listening" otherwise) → forward Recall's transcript websocket lines (`wss://meeting-data.bot.recall.ai/api/v1/transcript`, available inside the Output Media page per spec [A2]) as data packets on topic `meet.speaker` `{name, text, final}`.

- [ ] **Step 1: Failing tests** — backend: a valid ticket returns a token scoped to `meet-{env}-{uid}` and marks it used; `test_ticket_is_single_use_and_room_scoped` (second exchange after `ended` → 401; a token's room grant equals the meet room). Frontend: renders the speaking agent's name from attributes; plays the subscribed track.
- [ ] **Step 2–5:** implement, run (`pnpm test -- meet-bot`), commit — `feat(meet-bot): the bot's page is a LiveKit client with a ticket`.

---

### Task 9: Worker meeting mode

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/agent.py` (read `source`, `meetingId`, `company` from job metadata; `humans()` counts `meet:*` participants as humans)
- Modify: `apps/huddle-agent/src/huddle_agent/huddle.py` (`setting="meeting"`: unclaimed-question timer, no presence posts, `ROOM_IDLE_SECONDS` 60 min, idle action = `backend.leave_meeting()`; speaker labels from `meet.speaker` data packets)
- Modify: `apps/huddle-agent/src/huddle_agent/backend.py` (`leave_meeting()` → `DELETE /internal/meetings/{uid}`; `/route` body gains `setting`)
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

### Task 12: The meeting room, transcript and notes

**Files:**
- Create: `apps/backend/core/services/meeting_rooms.py` (`ensure(owner_id, meeting) -> room_id`: a channel named `"{title} — {Mon D}"` with every roster agent as an `active` member and the inviting member (when a workspace member); idempotent on `meeting.roomId`)
- Modify: `apps/backend/routers/huddles.py` (`/internal/huddles/{room}/transcript` additionally appends lines to `meetings_repo` when the room is a meeting room — the transcript batches already arrive here)
- Create: `apps/backend/core/services/meeting_notes.py` (on `ended`: upload `transcript.txt` as a room attachment via the existing attachment store; dispatch one CEO room run with the request "Write the meeting notes for <title>: decisions, open questions, action items as Paperclip tasks for their owners, two paragraphs max; the transcript is attached" using `openclaw_work._dispatch_message`'s follow-up path; reply to the organizer from `agents@` with the room link)
- Test: `apps/backend/tests/unit/services/test_meeting_rooms_and_notes.py`

- [ ] **Step 1: Failing tests** — `ensure` creates once and seats every agent active; transcript lines accumulate on the row; `ended` attaches the transcript and dispatches exactly one notes run; the organizer reply carries the room URL.
- [ ] **Step 2–5:** implement, run, commit — `feat(meetings): a room per meeting with transcript and notes`.

---

### Task 13: Agents page shows the team address; meeting rooms in the sidebar

**Files:**
- Modify: `apps/frontend/src/components/control/AgentsPanel.tsx` (or the agents overview component found by `grep -rn "Hire" components/control`) — a card "Invite the team to a meeting: `agents@acme.thelightbulb.company`" with a copy button
- Modify: `apps/backend/routers/teams/agents.py` (`GET /teams/agents` response gains `teamAddress`)
- Test: `apps/frontend/src/components/control/__tests__/team-address.test.tsx`, backend router test

- [ ] **Step 1–5:** failing render test → implement → pass → commit — `feat(app): the team address, with a copy button`.

---

### Task 14: Dev acceptance and the founder flag

- [ ] **Step 1:** Behind `MEETINGS_ENABLED` (CDK env, default `false` in prod, `true` in dev): the sweeper and webhook refuse when off.
- [ ] **Step 2:** In dev (prod LiveKit project is shared; Recall is one account): invite `agents@<founder-dev-slug>.dev.thelightbulb.company` to a real Meet; verify RSVP, bot in lobby, admission, "Friday, what's today's date?" answered, a second person's question, an unclaimed question routed after 3.5 s, hang-up → notes in the meeting room, organizer reply. Receipts on the PR.
- [ ] **Step 3:** Flip `MEETINGS_ENABLED` for prod via the usual CDK env change once the owner has run one founder meeting.

---

## Self-review

- **Spec coverage:** §0.1 → Tasks 1–5; §0.2 → Task 12 (everyone seated) + Task 9/10 (CEO hosts via the existing CEO rule); §0.3 floor rule → Tasks 9–10, tile → Task 8, plumbing → Tasks 6–8; §0.4 → notes in Task 12, state-for-routing deliberately absent (measure first); §0.5 raised hand → not in Phase 1 (Live PR first, shared); §0.6 → Task 12; §0.7 lobby → Task 6/7 (auto-admit upgrade is Phase 2); §0.9 → Task 11; spec §11 security → Tasks 6, 8; §12 consent → `CONSENT_LINE` in Task 6; §14 failure modes → Task 7 (`fatal` codes) and Task 6 (507 retry).
- **Placeholders:** none; every task names its files, interfaces and tests.
- **Type consistency:** `meetings_repo` rows use `uid` as the meeting id everywhere; `meet-{env}-{uid}` is the LiveKit room; `meet:{uid}` the page participant; `setting` is `"live" | "meeting"` in both the worker's `/route` body and the backend.
- **Review Focus:** each of the five risks is pinned to a named test in Tasks 3, 4, 8, 9/10 and 11.
