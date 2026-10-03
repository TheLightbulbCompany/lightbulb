# Lightbulb in Google Meet (and later Zoom and Teams) — options and design

Status: draft for owner review (2026-10-02). Builds on Option D
(`2026-10-02-voice-agent-is-the-voice-design.md`): the agent is the voice. Delete when shipped.

Owner decisions this design keeps: Recall.ai for Meet now, our own bot only if cost or dependency
demands it; agents are summoned by name, never ambient.

## 0. Decisions of 2026-10-02 evening (these win wherever a later section differs)

Taken with the owner after the first fully recorded Live calls (routing v2, transcript persistence
and the fast cold restore were in prod by then). Each is a ruling, not a proposal.

1. **The door is a calendar invite to one team address: `agents@{slug}.thelightbulb.company`.**
   Not per-agent addresses (nobody uses agent mail yet; "rename can break it" is accepted), not a
   Settings page as the selector, not pasting links "willy nilly in chat". The address is reserved
   (an agent named "Agents" cannot take it). Chat/link invites remain only as an ad-hoc fallback.
   Mail to `agents@` without an invite is ordinary team mail: the CEO triages, as Jev picks a
   responder in #general. The invite's `.ics` (time, Meet link, organizer, attendees, title,
   description, SEQUENCE for updates and cancellations) creates the meeting; we RSVP "Accepted" from
   `agents@`, so the organizer and guests see the team on the invite before the meeting — the honest
   consent moment. This replaces §3's per-agent-identity analysis and §6's "paste a link" door.
2. **Everyone attends; no cap.** "Why not bring everyone?" — nothing technical prevents it: one bot,
   every agent eligible behind it, routing decides who speaks, one voice at a time. The CEO hosts
   (answers "hey Lightbulb" / unaddressed asks); the others speak when named or picked for an
   unclaimed question in their domain. "Lightbulb: only Friday" in the event description narrows.
   Cost of everyone is bounded by **memory**: each agent's session keeps its own turns plus the shared
   notes, never the whole transcript (§0.6).
3. **What differs from Live is three things, and only three** (the owner rejected a longer list):
   the **floor rule** (an agent speaks when named, or when a question to the room goes unclaimed for
   ~3.5 s [Bohus 2011]; never into human-to-human talk; Live's lone-speaker ⇒ someone-answers
   fallback is off); the **single tile** (the speaking agent's name and avatar, and an "about to
   speak" cue, on the bot's video [Skantze 2021]); and the **plumbing** (§7). Speed-vs-completeness
   is NOT a meeting rule: Live already has it ("a couple of quick tool steps, hand real work off");
   the 55 s board lookup was that rule unenforced, fixed in Live for both. Memory is NOT different:
   Hermes sessions on the meeting room's key plus AgentCore Memory, exactly as chat and Live.
4. **Meeting state is a hypothesis, not a decision.** The room experiment (D-class) showed a goal
   ledger buys nothing in rooms; the meeting literature says the opposite for meetings (capa: a
   prompt-only delegate missed 51.4% of its real chances on AMI; explicit state — topic, decisions,
   open questions, stances, floor — took that to 2.5% with 0.6% false contributions, and the ablation
   credits the state, not more transcript). So: build the **notes** regardless (they are the product
   output of a meeting), and gate "the router reads them" on our own measurement — replay
   meeting-style transcripts through Jev with state on and off, like the room bench, before wiring it.
   Use the papers' frameworks (Addlesee's G/AG/CG goal annotations as the schema; recall, not
   accuracy, as the metric [Inoue 2025]) and think of in-app Live and Meet together: a result that
   helps meetings is tried in multi-person Live too.
5. **Raised hand.** News that answers a person's own question: no hand, delivered at the next pause,
   to the room if the asker left or no pause comes within ~1 min. Unsolicited news: hand up and ONE
   short bid at a pause ("Pulse here — I have the Q3 numbers when you want them"), delivered on "go
   ahead", a tap or its name, to the room after a few minutes. In a meeting both behave like the bid,
   except a direct answer to the asker within seconds. The tile says what the hand is for (the
   request label). **P2 (owner):** a meeting also ends with work still running — add an intermediate
   state between "working" and "raised hand / done": "finishing — will post in the room", so people
   leaving know where the answer lands. (Live already posts unheard answers to the room, 10-02.)
6. **After the meeting: a room per meeting** ("Design review — Oct 3") with the notes (decisions,
   open questions, action items as Paperclip tasks), the transcript as an attachment (not as
   messages, so agents observe the notes, not every line), the attending agents as members; linked
   from the calendar event; a one-line reply from `agents@` to the organizer with the link.
   **Open:** this revises Option D's "nothing is stored" — we store notes and transcript in the
   inviting workspace; Recall keeps ZDR. The pinned consent chat (§6, §12) must say so.
7. **Admission.** v1: the bot knocks, the host admits (10-min lobby cap), tile named for who is
   there ("Acme agents · Lightbulb"). Upgrade for Google-connected organizers: the platform-owned
   bot Workspace (owner accepted that we own it — the Otter/Fireflies model) is put on their event
   through their own `gog` calendar (full `calendar` scope exists), so it enters directly. M365 is
   `Calendars.Read` only → lobby.
8. **Order of work:** fix what the recorded Live calls found first (done 10-02: empty-room race,
   mid-sentence switch, late answers to the room, holding line, prompt diet), then plan exactly what
   Meet changes in Live and in the app, then build Meet.

9. **Everyone gets meetings; presence is on us for now.** Ruling 10-02 23:05 EDT: "everyone
   should get meetings, we can eat that cost for now". Bot presence (Recall + LiveKit + STT,
   ~$1.5/h list) is platform-paid and not metered to the customer, on every plan including trial;
   what the customer's **daily 10-minute allowance** (`voice_billing.FREE_SECONDS_PER_DAY` = 600,
   then credits) covers is **agent speech**, in a meeting exactly as in Live — one counter, one
   card. Read this way so a free workspace's bot never walks out of a 30-minute meeting at minute
   ten; if the owner meant presence itself to draw down the allowance, that is the change. Revisit
   presence billing (§10) when bot-hours are a line item.
10. **One mailbox per workspace — per-agent addresses are removed** (ruling 10-03 ~00:10 EDT:
    "get rid of friday@acme… and keep agents@"). `agents@{slug}.thelightbulb.company` is the only
    address a workspace has. Every agent sends from it with its own display name ("Friday · Acme
    agents"); inbound goes to the CEO to triage, except a reply to a thread another agent started,
    which goes back to that agent. The address row's switches (`send_to_anyone`, `verified_senders`,
    `outbound_disabled`) become workspace-level; the Agents page shows the one address. Nothing is
    minted per agent any more, so decision 1's "reserved name" guard is moot, and the handful of
    existing per-agent rows are deleted by a one-off script (nobody uses them). This supersedes the
    per-agent half of §3 and §12.
11. **No sweeper; the bot is booked when the invite is parsed** (ruling 10-03: "why a sweeper, why
    not event based?"). Recall is already the scheduler — `join_at` more than 10 minutes out
    reserves a bot that joins itself, sooner is ad-hoc, a move is a PATCH, a cancel a DELETE — and
    its webhooks drive everything after. A failed booking marks the row `bot_failed` and says so in
    the meeting room; forwarding the invite again retries. This replaces §7's "scheduler sweep".

What the owner still has to settle: §16 items 7 (consent wording, now that notes are stored) and
8 (startup program); items 1, 2, 4 and 5 are answered above.

Every vendor claim cites a primary source. **(inferred)** marks reasoning that no source states;
**(measure)** marks something we must test before relying on it. Recall's docs were read on
2026-10-01/02 as raw Markdown (`https://docs.recall.ai/docs/<page>.md`).

---

## 1. Short answer

- **Recall.ai is the right tool to start with.** It is the only option that can, today and
  without us operating a browser fleet, join Meet, hear it, **speak** into it, show a video tile
  we draw, and reach Zoom and Teams with the same API. Its webpage-output model lets our existing
  LiveKit worker serve a meeting almost unchanged (section 4).
- **Keep an exit.** Attendee (open source, Elastic License 2.0, hosted at $0.35–0.50/h) has the
  same shape: a webpage voice agent, bidirectional websocket audio, and the same SAML signed-in
  Meet bots. Put Recall behind one small adapter so a switch is a week, not a rewrite.
- **One "Lightbulb" bot per meeting, not one bot per agent.** Its tile shows which agent is
  speaking; the voice switches per Option D.
- **Grow into:** calendar auto-join, then a signed-in bot pool on a dedicated Google Workspace
  (skips the lobby when invited), then Zoom and Teams. Per-agent Google identities are possible
  but cost a Workspace licence per agent and cannot use the agents' existing addresses (section
  3). Phone dial-in through LiveKit SIP is a cheap, ToS-clean fallback for meetings that refuse
  bots, but it has no tile, no names and narrowband audio.
- **Cost per meeting hour (Recall, list price):** about **$1.5/h** with the bot present and
  nobody summoning an agent, about **$2.1/h** with 10 minutes of agent speech, about **$4.6/h**
  if an agent talks the whole hour (section 9). Recall's startup rate cuts the Recall share by
  half.

---

## 2. Options compared

| | (a) Recall.ai | (b) Attendee / Meeting BaaS | (c) Our own headless Chrome | (d) Dial-in via LiveKit SIP | (e) Meet Media API |
|---|---|---|---|---|---|
| Hear: per person or mixed | Both: per-participant 16 kHz PCM over websocket (up to 16 speakers on Meet) [A1], or mixed audio inside the output page [A2] | Attendee: mixed or per-participant over websocket [B1]; Meeting BaaS: streaming in/out [B4] | Whatever we build (mixed from the tab, per person only by hooking WebRTC) | Mixed, telephone-band | Receive-only, exactly 3 "virtual" audio streams [E2] |
| Speak | Yes: Output Media webpage (audio + video) [A2] | Attendee: webpage voice agent or websocket audio out [B1][B2]; Meeting BaaS: Pipecat speaking bots [B5] | Yes, via a virtual mic we build | Yes (a phone caller) | **No** [E2] |
| Display identity | `bot_name` as an anonymous guest; signed-in bots take the Google account's name and photo [A3] | Same model (Attendee signed-in Meet via SAML) [B3] | Guest, or a Google account we sign in | A phone number; no tile, no name (inferred) | An app, not a participant |
| Admission | Guest knocks; anyone signed in from the host's Workspace can admit [A4]. Signed-in **and** on the Calendar invite enters Open/Trusted/Restricted meetings without knocking, unless the Waiting room is on or the org allows only its own users [A5] | Same as (a) | Same as (a), and we own the failure cases | Workspace-organized meetings only, with dial-in enabled; callers from another domain "might need" approval; joins muted when >5 people [D1] | Needs a consenting host-org participant present and host permission in Calendar [E1] |
| Cost per meeting hour | $0.50/h PAYG; $0.60/h with the 4-core variant output media needs; +$0.15/h for its transcription; startup program $0.25/h for the first 10,000 h [A6][A7][A8] | Attendee hosted $0.50 → $0.35/h, transcription included [B6]; Meeting BaaS $0.35–0.50/h + 0.10 token per stream direction [B4] | Fargate ~$0.08–0.10/h for 2 vCPU/4 GB [C3] plus our engineering | ~$0.44/h (LiveKit SIP $0.004/min + Twilio $0.0034/min) [D3][D4] | n/a |
| Engineering effort | Low: an API, a webpage, webhooks. MVP ~2–3 weeks (inferred) | Hosted: same as (a). Self-hosted: run their Django app + Postgres + Redis, Kubernetes for isolation [B7] | High and ongoing: Meet DOM automation, virtual audio devices, sign-in, bot detection | Low: a SIP trunk and `CreateSIPParticipant` with `dtmf` for the PIN [D2] | n/a |
| Reliability and ToS | Recall runs the fleet and absorbs Meet UI changes; Google can still flag or block a bot (`google_meet_bot_blocked`) [A9] | Smaller vendors. Attendee is very active (v1.82.2 released 2026-10-01; 745 stars) [B8] | Everything is ours, including Google's anti-automation risk | Plain PSTN; no automation of Google's UI; no ToS exposure (inferred) | Developer Preview, **not accepting new signups** [E1] |

Others checked and ruled out:
- **Skribby**: real-time transcription and audio **receive** from $0.35/h [B9]. Its docs describe no
  way to speak into a meeting, so it fails our core need.
- **Nylas Notetaker**: records and transcribes after the meeting; nothing documents speaking
  [B10].
- **Vexa** (Apache-2.0, 2.8k stars): speaking is "proven live at the bot layer … not wired at the
  API layer" in v0.12 [B11]. Revisit later.

### (c) in detail: can AgentCore Browser be our bot?
Not as documented. The AgentCore Browser docs describe CDP automation (one automation stream per
session) and a DCV Live View that renders **video** [C1][C2]; nothing documents audio capture,
audio injection, or a microphone device, and we cannot add virtual audio devices or Chrome launch
flags to the managed browser. A page script injected over CDP that replaces `getUserMedia` and
taps `RTCPeerConnection` is conceivable but untested, and per our own rule we do not state a
vendor limit or capability we have not tried. An own bot would instead run Chromium plus virtual
audio in our own container (Fargate, or AgentCore Runtime, which supports WebRTC with a TURN relay
[C4]). That is option (c) proper: cheap per hour, expensive to own.

### (d) in detail: dial-in
- Google Meet dial-in exists "with any Google Workspace edition" for US and Canada numbers at no
  extra cost; other countries need the Meet Global Dialing subscription [D5]. It works only for
  meetings organized by a Workspace account, and an admin must turn it on [D1]. **Meetings
  organized from personal Gmail have no dial-in** [D1].
- Join flow: dial, then "enter the pin followed by the # key" [D1]; LiveKit's
  `CreateSIPParticipant` sends that as `dtmf` (`w` = 0.5 s pause) [D2]. The caller lands in our
  LiveKit room as an ordinary participant, so the worker needs no Recall integration at all.
- Number and PIN come from the calendar event's `conferenceData.entryPoints` (type `phone`, with a
  `pin`) [D6], which owners' connected Google accounts already let us read.
- Costs: the caller joins muted in meetings of more than 5 [D1], so it must unmute (Meet's `*6`
  over DTMF; **measure**). Whether a caller from outside the domain must be admitted is unclear
  [D1] (**measure**). Audio is narrowband and mixed, so speaker names are lost.
- Verdict: a fallback, not the product. It is worth one spike day because it costs almost nothing
  and has no vendor.

### (e) in detail: Meet Media API
Developer Preview, closed to new signups [E1], and receive-only: the client's offer must contain
"exactly 3 receive-only audio media descriptions" [E2]. It cannot speak, so it is out.

### Recommendation
Start with **(a) Recall**, using **Output Media with a page that is a LiveKit client** (section 4),
an anonymous guest bot, paste-a-link and pick-from-calendar. Hide Recall behind a
`meeting_bots.py` adapter of four calls (create, leave, status webhook, chat message), because
Attendee offers the same three primitives (webpage voice agent, bot status webhooks, SAML
signed-in Meet bots) [B2][B3]. Move to a signed-in "Lightbulb" bot pool once admission friction
shows up in usage. Reconsider self-hosted Attendee or our own bot when bot-hours reach thousands
per month and the ~$0.25–0.50/h difference is larger than a person's time.

---

## 3. Agent identity and calendar auto-join

### Can each agent sign in to Google as itself?
- A Google account can be created on a non-Gmail address [G1], so a consumer account on
  `nova@acme.thelightbulb.company` is possible in principle.
- **Neither vendor can sign that account into Meet.** Recall's signed-in Meet bots require "a
  dedicated, paid Google Workspace" (Starter is enough), with Google's **Legacy SSO** pointed at
  Recall as the SAML identity provider; bot accounts must be on that Workspace's **primary
  domain** ("a secondary domain or alias in your existing Workspace does not qualify") and must
  not be admins [A3]. Attendee does the same: SAML SSO, no password flow for Meet [B3].
  Automating a password login of a consumer account invites captchas (Recall even has a
  `google_meet_sign_in_captcha_failed` sub code [A9]) and Google ToS exposure (inferred).
- Our agent addresses use a **subdomain per customer** (`{agent}@{slug}.thelightbulb.company`,
  `AGENT_EMAIL_DOMAIN`). One Workspace has one primary domain, so per-customer addresses would
  need one Workspace per customer. Not viable.
- **Workable variant:** one dedicated Workspace with primary domain, say,
  `agents.thelightbulb.company`; one user per agent (`nova-acme@agents.thelightbulb.company`)
  whose name and photo are the agent's (Meet uses them and they override `bot_name` [A3]); MX for
  that domain points at **our SES**, not Gmail, so invitations still land in our pipeline; one
  Recall login group per agent so Recall picks that account (a group with several accounts picks
  any of them [A3]). Each login supports 30 concurrent sign-ins [A3].
- Cost: Business Starter is $7.00/user/month after the current promotion [G2]. Six starter agents
  = $42/month per customer workspace, against plans priced $20–$150. Too much as a default.
- And it does not beat the hardest wall: "If an administrator permits only users from the host
  organization … signing in or adding the external account to the invitation does not override
  it" [A5].

**Recommendation:** a small pool of shared signed-in accounts named "Lightbulb" (one Recall login
group, 30 concurrent each [A3]) in phase 3, so invited bots skip the lobby. Per-agent Google
identities only as a later paid add-on if customers ask for them. The tile, not the Google name,
carries the agent's identity (section 5).

### Calendar auto-join by inviting the agent's email: what our SES path does today
Read on origin/main: `apps/infra/lambda/ses-inbound/index.py` (`_build_payload`,
`_process_attachments`) and `apps/backend/routers/ses_inbound.py`.
- The Lambda keeps only the `text/plain` body (`msg.get_body(preferencelist=("plain",))`) and
  every part `iter_attachments()` yields. The `text/calendar; method=REQUEST` part that calendar
  clients put inside `multipart/alternative` is a body candidate, so it is **neither parsed nor
  stored**. A separate `invite.ics` attachment, if present, is stored to S3 behind a one-hour
  presigned URL and appears only as a line in the agent's attachment manifest. Checked with the
  stdlib on a synthetic Google-shaped invite: body = `text/plain`, attachments =
  `[application/ics invite.ics]`. **Measure** with a real Google and Outlook invite.
- So invites reach the agent as an email to read; nothing schedules a bot. The Meet link is in the
  plain body ("Join with Google Meet: …"), which is fragile to scrape.

**Change needed (phase 2):** the Lambda extracts the first `text/calendar` part (capped at 64 KB)
into a new `calendar` field; the backend parses `METHOD`, `UID`, `SEQUENCE`, `DTSTART`, and the
conference URL (`X-GOOGLE-CONFERENCE`, else a Meet, Zoom or Teams URL in `LOCATION`/`DESCRIPTION`)
and creates, moves or cancels a scheduled bot keyed by `UID`. A bot costs money, so scheduling is
gated the way waking is: the organizer or an attendee must be a member of the agent's workspace (or
allowlisted). A stranger's invite is stored as mail and never dispatches a bot. Recall's own
Calendar V2 (free [A6]) is the alternative for users who connect a calendar; we already hold
owners' Google calendar access through `google_accounts`, so phase 2 reads it directly instead.

---

## 4. Recall facts the design depends on

1. **Create a bot**: `POST /api/v1/bot/` with `meeting_url`, `bot_name` (max 100 chars), `join_at`,
   `recording_config`, `output_media`, `automatic_leave` (`waiting_room_timeout`,
   `noone_joined_timeout` default 1200 s, `everyone_left_timeout` default 2 s), `metadata` [A10].
   `join_at` more than 10 minutes ahead reserves a machine; anything sooner is an **ad-hoc** bot
   drawn from a warm pool that can be empty (HTTP 507: retry every 30 s, up to 10 times) [A11].
2. **Admission on Meet**: unsigned bots must be admitted manually and are anonymous [A4]; Google
   labels anonymous joiners "may not be who they claim to be" and may block them
   (`google_meet_bot_blocked`); hosts who disable knocking get `google_meet_knocking_disabled` [A9].
3. **Output Media**: the bot loads our webpage and streams its audio and video into the meeting
   as its camera or screenshare; the page gets the meeting audio through `getUserMedia` with the
   microphone permission granted automatically, plus a transcript websocket at
   `wss://meeting-data.bot.recall.ai/api/v1/transcript` [A2]. Video is 1280×720 at 15 fps; output
   is always audio **and** video (no audio-only bot, camera cannot be turned off); it is exclusive
   with Output Audio [A2]. `web_4_core` is the recommended variant, $0.60/h pay-as-you-go [A2].
   Recall publishes **no latency figure** for Output Media [A2] (**measure**).
4. **Output Audio** (`POST /bot/{id}/output_audio/`, base64 MP3) is for short clips like greetings,
   not conversation; Output Media is "recommended for use cases with agents" [A12].
5. **Per-participant audio**: `recording_config.audio_separate_raw` with a websocket realtime
   endpoint subscribed to `audio_separate_raw.data`: base64 16 kHz mono S16LE, each chunk tagged
   with the participant's `id`, `name`, `is_host`; Meet, Zoom and Teams up to 16 concurrent
   speakers; no screenshare audio; no extra price [A1][A7]. Latency unpublished.
6. **Transcription**: `recallai_streaming` with `mode: "prioritize_low_latency"` and
   `language_code: "en"` arrives "typically 1–3 seconds after an utterance is finalized"; accuracy
   mode is delayed 3–10 minutes; $0.15 per hour [A13]. Third-party providers are also supported
   [A13].
7. **Participant events**: `participant_events.join`, `.leave`, `.update`, `.speech_on`,
   `.speech_off` and more over realtime endpoints [A14].
8. **Bot status webhooks**: `joining_call`, `in_waiting_room`, `in_call_not_recording`,
   `in_call_recording`, `call_ended` (with `sub_code`), `done`, `fatal` [A15]; requests are signed
   (`webhook-id`, `webhook-timestamp`, `webhook-signature`) [A16].
9. **Store nothing**: `recording_config.retention: null` is Zero Data Retention: media is
   processed in memory and never written to disk; it works with `recallai_streaming` only in
   low-latency English mode [A17]. Outside that scope Recall keeps the meeting URL for 14 days,
   custom `metadata` **indefinitely**, and diagnostic logs for 7 days [A18].
10. **Chat**: the bot can post a chat message, re-send it on every join (`on_participant_join`),
    and pin it on Meet (500-character limit) [A19].
11. **Billing**: per second from `joining_call` to `done`; `fatal` bots are not billed; participant
    count does not change the price [A6][A20].

---

## 5. Agent presence under Option D

**Option 1: one bot per agent (one tile each).** Each bot costs $0.60/h for the whole meeting,
whether its agent speaks or not [A2], every bot knocks separately (N admissions for the host), and
signed-in per-agent identity needs the per-agent Workspace accounts from section 3. Six agents =
$3.60/h before anyone speaks. Rejected.

**Option 2: one "Lightbulb" bot whose voice switches (recommended).** One bot, one admission, one
tile. Its camera is our page, which already must render video [A2]: it shows the speaking agent's
name and avatar, and "joining…" / "working on it" states, driven by the worker's
`lightbulb.speaking_agent` attribute from Option D §7. The voice is that agent's own GPT-Live
voice. Bot name: "Lightbulb" (or "Lightbulb (Acme)"). People say "Nova, …" in the meeting exactly
as in Live.

---

## 6. The experience

- **Invite.** Add `agents@{slug}.thelightbulb.company` to the calendar event (§0.1). The team
  accepts; at start time the bot comes. Pasting a Meet link in a room is the ad-hoc fallback only.
- **Joining.** The room shows "Lightbulb is waiting to be let in" until the host admits it, then
  "In the meeting". Its Meet tile shows the Lightbulb mark and the team.
- **On join** it posts and pins one chat line (inside Meet's 500-character limit [A19]). Wording is
  open (§0.6): the notes and a transcript go to the inviting team's Lightbulb workspace; Recall
  itself keeps nothing.
- **Summon by name.** "Nova, what did we ship last week?" The tile switches to Nova, Nova answers
  in her own voice, and work goes to Nova's Hermes session exactly as in Live. "Lightbulb, …" means
  "ask the team" (Option D routing). Silence otherwise: agents never speak unprompted, except to
  deliver a result someone in this meeting asked for (Option D's proactive rule).
- **Leaving.** "Lightbulb, leave", a Leave button in the room, the host removing it, or everyone
  leaving (Recall's `everyone_left_timeout`).
- **Afterwards** the meeting's room holds the notes and transcript (§0.6); during the meeting the
  room shows the bot's status and, as of 10-02, any answer that finished after the floor moved on.

---

## 7. Architecture

### Data path
```
 Meet participants
      │  audio (Meet's own WebRTC)
      ▼
 Recall bot (in the meeting) ── runs our page: app.flickmeon.ai/meet-bot#t=<ticket>
      │  page: getUserMedia() = meeting audio (mixed)         [A2]
      │  page: LiveKit JS client joins room  meet-{env}-{sessionId}
      │        publishes the meeting audio as its mic track
      │        (phase 2: forwards Recall transcript lines with speaker names on a data topic)
      ▼
 LiveKit Cloud room meet-{env}-{sessionId}
      ▼
 huddle-agent worker (unchanged core): STT on the "meet" participant → wake word / routing
      │  → that agent's GPT-Live session (Option D) ──delegation──► backend /internal/huddles/*
      │                                                           ──► Hermes (bridge) ──► frames
      │  agent audio published on the worker's track
      ▼
 page subscribes to the worker's audio track → plays it in an <audio> element;
 page renders the speaking agent's tile from the worker's attributes
      ▼
 Recall streams the page's audio + video into Meet → attendees hear and see the agent
```

Why this shape: it needs **no inbound websocket on our side** (the page dials out to LiveKit, as
any browser does), and the worker sees the meeting as one more LiveKit participant, so wake word,
routing, GPT-Live sessions, delegation, billing and frames are today's code. The per-participant
websocket path [A1] would need a public, long-lived websocket server bridging into LiveKit; our
backend's rolling deploys and API Gateway's HTTP-only REST door make that the larger job, so it
waits until speaker-separated audio proves necessary.

Things to verify in the spike (**measure**):
- Whether the meeting audio the page receives excludes the bot's own output (Meet does not normally
  return a participant's own audio, so it should; inferred). If not, the worker hears its own agent.
  Request `echoCancellation: false` and gate STT while the agent speaks.
- Added latency: two extra hops (page ↔ LiveKit, Recall ↔ Meet). Target under 400 ms on top of
  Live (inferred estimate). Recall publishes none [A2].
- GPT-Live's duplex input is the whole meeting (mixed), so anyone can interrupt the agent. That is
  right for a meeting, but a noisy room will cut it off; may need a higher barge-in threshold.

### Separate LiveKit room per meeting
`meet-{env}-{sessionId}`, not the Lightbulb room's own Live room: Meet attendees did not agree to
be heard by room members who are not in the meeting. The worker is dispatched with metadata that
names the Lightbulb room (for team, persona, context and authorization) plus `source: "meet"`.

---

## 8. Backend routes

People (Clerk):
- `POST /api/v1/meetings` `{roomId, meetingUrl, joinAt?}` → gate (plan, credits, room membership,
  any-room kind with a listening agent), validate the URL (Meet `https://meet.google.com/xxx-xxxx-xxx`
  only in phase 1; Zoom/Teams hosts later), create the session row, mint a page ticket, create the
  Recall bot (`output_media.camera` = our page URL with the ticket, `variant.google_meet: web_4_core`,
  `recording_config: {retention: null}`, no transcript in phase 1, `automatic_leave`
  {`waiting_room_timeout`: 600, `noone_joined_timeout`: 300}, pinned consent chat,
  `metadata: {sessionId}` only). Returns `{sessionId, status}`. Ad-hoc 507 → retry in the
  background, status `starting`.
- `GET /api/v1/meetings?roomId=` → active sessions and status.
- `DELETE /api/v1/meetings/{sessionId}` → Recall "leave call".

Recall (signed):
- `POST /api/v1/webhooks/recall` → verify `webhook-signature` [A16], update the session,
  publish `room.meeting.changed` to the room; on `in_call_*` dispatch the worker to the meet room;
  on `call_ended`/`done`/`fatal` close and bill the session (idempotent, like `close_call`).

The page (public, ticket-authenticated):
- `POST /api/v1/meet-bot/session` `{ticket}` → single-use exchange for
  `{serverUrl, roomName, token}`; the LiveKit token grants only that room, publish audio + data,
  subscribe; short TTL with refresh by the same session. Route allowed through `middleware.ts`
  without Clerk.

Worker (existing `/internal/huddles/*`, worker secret + room token): the summon route must accept a
meet session as the speaker context. Today `start_huddle_call` 404s unless `speakerUserId` is a
room member; for meet sessions the backend uses the **inviting member** as the authorizing and
billing user and takes a display label ("Aden (Meet)") from the worker.

Storage: one row `__meet__#{sessionId}` in the containers table (status, Recall bot id, roomId,
ownerId, inviter, ticket hash, startedAt), TTL-deleted a day after the meeting. No audio, transcript
or meeting content is stored by us or by Recall.

---

## 9. Worker changes (small)

1. Job metadata `source: "meet"`, `sessionId`: skip `presence` publishing (the meet participant is
   not a room member), use the meet room token, and use a longer room-idle limit (60 min instead of
   15) with "leave the meeting" as the idle action (call `DELETE /meetings/{id}` via an internal
   route).
2. Treat the page participant (identity `meet:{sessionId}`) as a human for STT and floor purposes;
   its lines are labelled "Meet" until phase 2 names arrive.
3. Phase 2: consume `meet.speaker` data packets (Recall transcript lines with `participant.name`)
   to label the shared transcript ("Priya (Meet): …") and to tell the agent who asked. Wake word
   timing still comes from our own faster STT on the mixed track.
4. Expose `lightbulb.speaking_agent` (+ name, avatar URL, state) as attributes; the page reads them
   to draw the tile. Same attributes Option D already adds for Live tiles.

Everything else (routing, per-agent GPT-Live sessions, delegation, frames, billing per summon) is
Option D unchanged.

---

## 10. Billing

- **Voice minutes:** unchanged: each summon is a call row, GPT-Live seconds billed at $0.05/min
  provider cost × markup in credits mode, or from the free daily seconds (`voice_billing.py`).
- **Meeting presence (new):** the bot costs money for every second it is in the meeting, spoken
  or not. Bill it per second from `joining_call` to `done` (Recall's own billing window [A20]) as a
  Metronome event `meeting_bot`, provider cost = Recall rate + LiveKit agent session + mixed-track
  STT, at the managed-credit markup; hold credits for a capped duration at start (default cap 3 h,
  enforced by leaving the meeting).
- **Free and trial workspaces:** every plan gets meetings; presence is platform-paid for now and
  agent speech draws on the daily allowance (§0.9).
- Recall is paid by us from a prepaid balance with auto top-up; a negative balance interrupts new
  bots [A21], so alarm on the balance.

### Cost per meeting hour (list prices)

| Item | Rate | Bot present, no summons | 10 min of agent speech | Agent talks all hour |
|---|---|---|---|---|
| Recall bot, `web_4_core` | $0.60/h [A2] | $0.60 | $0.60 | $0.60 |
| Recall transcription (phase 2, names) | $0.15/h [A13] | $0.15 | $0.15 | $0.15 |
| LiveKit agent session (worker) | $0.01/min [D3] | $0.60 | $0.60 | $0.60 |
| LiveKit page participant | $0.0005/min [D3] | $0.03 | $0.03 | $0.03 |
| Our STT on the mixed track (`gpt-4o-mini-transcribe`) | $0.003/min [F1] | $0.18 | $0.18 | $0.18 |
| GPT-Live while an agent holds the floor | $0.05/min (`voice_billing.py`) | $0 | $0.50 | $3.00 |
| **Total provider cost** | | **$1.56** | **$2.06** | **$4.56** |
| Hermes inference per delegated turn | existing credits | — | extra | extra |

With Recall's startup program ($0.25/h for the first 10,000 hours [A6]) the Recall bot line is
$0.25, assuming it also covers the 4-core variant (open question). For comparison: dial-in replaces
the first two lines with ~$0.44 [D3][D4]; Attendee hosted replaces them with $0.35–0.50 [B6].

---

## 11. Security

- **Meeting URLs** are sensitive (anyone with an Open meeting's link can join). Validate the host
  and shape, never log them in full, keep them only on the session row, and remember Recall
  retains them 14 days [A18]. Put only `sessionId` in Recall `metadata` (kept indefinitely [A18]).
- **Page ticket**: 256-bit random, single use, stored hashed, valid for the session's lifetime
  cap; the page URL Recall holds is worthless once exchanged. The LiveKit token it yields is
  scoped to one meet room and short-lived.
- **Recall API key** in Secrets Manager (`lightbulb/{env}/recall_api_key`); webhook verification
  secret alongside. No Recall credential reaches the page or a runtime.
- **Who can send a bot**: room members of the owner's workspace only; shared-channel guests cannot
  (the Live join rule). Calendar-invite scheduling only for invites from workspace members.
- **Prompt injection**: anyone in the meeting can speak to the agent. The agent acts with the
  inviting member's authority, so the same approval rails as chat apply; voice never bypasses
  Needs-you approvals.
- **Signed-in accounts (phase 3)**: the SAML private key Recall signs with lives with Recall [A3];
  the dedicated Workspace's super-admin is a separate, hardware-key account.

---

## 12. Consent and recording law

- Recall's bot does not use Meet's own recording, so Meet's recording indicator does not appear
  (inferred; Recall documents no Meet-side indicator). Consent is ours to give: the pinned chat
  message re-sent to every joiner [A19], the tile text "Lightbulb — listening for its name, not
  recording", and the visible guest tile itself.
- We store nothing (ZDR [A17], no transcript kept, Option D posts nothing). Real-time processing of
  speech is still "interception" in all-party-consent jurisdictions (e.g. California); the notice
  above is the mitigation. Legal review is an open question.
- Zoom's own recording-consent disclaimer is configurable only per Zoom organization [A22], so on
  Zoom our chat notice is again the consent mechanism.

---

## 13. Zoom and Teams deltas

- **Zoom**: no setup needed for basic joins; the waiting room applies [A23]. Since February/March
  2026 Zoom requires Meeting SDK apps joining meetings outside their own account to carry an
  **OBF token** from a user who authorized our Zoom app and is present; when that user leaves,
  Zoom removes the bot [A24]. So: a Zoom Marketplace OAuth app, the inviter must authorize it, and
  the bot lives only while the inviter is in the meeting. Zoom dial-in is a fallback with the same
  DTMF mechanism.
- **Teams**: bots wait in the lobby by default; skipping it depends on the tenant's lobby setting,
  mostly requiring a signed-in bot in, or trusted by, the host's tenant [A25][A26]. Chat may be
  unavailable to the bot unless the tenant allows it, which breaks the pinned consent notice [A19].
  Registration-required meetings, Live Events and Town Halls are unsupported [A25].
- Output Media itself works on Zoom, Meet, Teams and Webex [A2].

---

## 14. Failure modes

| Failure | Signal | Behaviour |
|---|---|---|
| Nobody admits the bot | `in_waiting_room`, then `call_ended` / `call_ended_by_platform_waiting_room_timeout` [A9] | Room shows "Not let in"; offer retry. Cap the wait at 10 min. |
| Knocking disabled, org-only meeting, or Google blocks the bot | `fatal` with `google_meet_knocking_disabled` / `google_meet_organisation_restricted` / `google_meet_bot_blocked` [A9] | Plain message per code; suggest the host invite the bot or allow external guests; dial-in fallback later. Fatal bots are not billed [A20]. |
| Host removes the bot | `bot_kicked_from_call` [A9] | Close session; do not rejoin. |
| Ad-hoc pool empty | HTTP 507 [A11] | Retry every 30 s ×10 in the background; status "starting". |
| Page fails to load or LiveKit drops | No page participant in the meet room after N s | Worker posts the error; backend makes the bot leave so nobody pays for a mute tile. |
| Worker crash | Meet room without the worker | Re-dispatch, as Live's join does today (dedupe window). |
| Our backend deploy | none: page ↔ LiveKit ↔ worker never touch the backend except for turns, which already retry | — |
| Recall balance negative | Recall stops new bots [A21] | Auto top-up and a balance alarm. |
| Meeting runs past the cap | our timer | Agent says goodbye; leave call. |
| Everyone leaves | `everyone_left_timeout` [A10] | Recall leaves; webhook closes the session. |

---

## 15. Phasing

- **Phase 0, spike (3 days):** Recall PAYG account; one ad-hoc bot whose Output Media page joins a
  dev LiveKit room; measure echo, round-trip latency, CPU on `web_4_core`; same day try dial-in via
  LiveKit SIP + Twilio into a Workspace Meet.
- **Phase 1, MVP (per §0):** the reserved `agents@` address and the SES `text/calendar` change
  (section 3: the Lambda extracts the invite, the backend parses it, RSVPs, and schedules the bot
  with `join_at` ≥ 10 min [A11]); one anonymous "Acme agents · Lightbulb" bot; everyone attends,
  the CEO hosts; meeting floor rule; speaker names from Recall transcripts (needed for the
  addressing stage, so phase 1 not 2); agent tile with the holding cue; pinned consent chat; a room
  per meeting with notes and transcript; meeting billing; failure messages. Founder workspace first.
- **Phase 2:** auto-admit for Google-connected organizers via the platform bot Workspace (§0.7);
  the raised-hand bid and the "finishing" state (§0.5); meeting-state-for-routing if the measurement
  (§0.4) says so; paste-a-link fallback.
- **Phase 3:** signed-in "Lightbulb" account pool on a dedicated Workspace; Zoom (OBF) and Teams.
- **Phase 4 (only if asked):** per-agent Google identities; self-hosted Attendee or our own bot if
  bot-hours justify it; dial-in fallback for org-only meetings.

---

## 16. Open questions for the owner

1. **Who pays for presence?** Meeting bots cost ~$1.5/h before anyone speaks. Credits only, or a
   free allowance? Trial workspaces: none, or N minutes a day?
2. **Bot name**: "Lightbulb", or "Lightbulb (Acme)" so hosts know whose it is?
3. **Speaker names in phase 1 or 2?** Adds $0.15/h and English-only for ZDR [A17].
4. **Which room does a meeting belong to** when invited by calendar email: the agent's DM with the
   inviter, or `#general`?
5. **Signed-in pool (phase 3)**: OK to run a dedicated Google Workspace (`agents.thelightbulb.company`)
   with MX pointed at SES? Per-agent identities at $7/agent/month: never, add-on, or default on paid
   plans?
6. **Dial-in fallback**: worth building for meetings that block bots, given no tile and no names?
7. **Legal**: consent wording for all-party-consent jurisdictions; do we need a pre-meeting notice
   to attendees when the bot is scheduled?
8. **Recall startup program**: apply now ($0.25/h for the first 10,000 hours [A6])?

---

## Sources

Recall.ai
- [A1] Separate audio per participant (realtime): https://docs.recall.ai/docs/how-to-get-separate-audio-per-participant-realtime
- [A2] Output Media: https://docs.recall.ai/docs/stream-media
- [A3] Google Meet signed-in bots: https://docs.recall.ai/docs/google-meet-login-getting-started
- [A4] Google Meet overview (manual admission, anonymous): https://docs.recall.ai/docs/google-meet
- [A5] Google Meet: skip the waiting room: https://docs.recall.ai/docs/google-meet-skip-the-waiting-room
- [A6] Pricing: https://www.recall.ai/pricing
- [A7] Calculating usage (variants, no feature surcharges): https://docs.recall.ai/docs/calculating-usage
- [A8] Recall.ai transcription pricing: https://docs.recall.ai/docs/recallai-transcription
- [A9] Sub codes: https://docs.recall.ai/docs/sub-codes ; Meet FAQ: https://docs.recall.ai/docs/google-meet-faq
- [A10] Create Bot: https://docs.recall.ai/reference/bot_create
- [A11] Creating and scheduling bots (ad-hoc, 507): https://docs.recall.ai/docs/creating-and-scheduling-bots
- [A12] Output Audio: https://docs.recall.ai/docs/output-audio-in-meetings
- [A13] Recall.ai transcription modes and latency: https://docs.recall.ai/docs/recallai-transcription ; https://docs.recall.ai/docs/bot-real-time-transcription
- [A14] Real-time event payloads: https://docs.recall.ai/docs/real-time-event-payloads
- [A15] Bot status webhooks: https://docs.recall.ai/docs/bot-status-change-events
- [A16] Verifying requests from Recall: https://docs.recall.ai/docs/authenticating-requests-from-recallai
- [A17] Zero Data Retention: https://docs.recall.ai/docs/zero-data-retention
- [A18] Storage and data retention: https://docs.recall.ai/docs/storage-and-playback
- [A19] Sending chat messages: https://docs.recall.ai/docs/sending-chat-messages
- [A20] Usage billing window: https://docs.recall.ai/docs/calculating-usage
- [A21] Billing FAQ (credit balance): https://docs.recall.ai/docs/billing-faq
- [A22] Recall FAQ (Zoom consent disclaimer): https://docs.recall.ai/docs/faq
- [A23] Zoom overview: https://docs.recall.ai/docs/zoom-overview
- [A24] Zoom OBF: https://www.recall.ai/blog/zoom-obf ; Zoom: https://developers.zoom.us/blog/transition-to-obf-token-meetingsdk-apps/
- [A25] Microsoft Teams overview: https://docs.recall.ai/docs/microsoft-teams
- [A26] Bypass the Teams lobby: https://docs.recall.ai/docs/teams-bots-bypass-the-lobby

Alternatives
- [B1] Attendee realtime audio: https://docs.attendee.dev/guides/realtimeaudio
- [B2] Attendee voice agents: https://docs.attendee.dev/guides/voiceagents
- [B3] Attendee signed-in bots: https://docs.attendee.dev/guides/signedinbots
- [B4] Meeting BaaS pricing: https://www.meetingbaas.com/en/pricing
- [B5] Meeting BaaS speaking bots: https://docs.meetingbaas.com/llms/speaking-bots ; https://github.com/Meeting-Baas/speaking-meeting-bot
- [B6] Attendee pricing: https://attendee.dev/pricing
- [B7] Attendee README (self-hosting, ELv2): https://github.com/attendee-labs/attendee
- [B8] Attendee releases: https://github.com/attendee-labs/attendee/releases
- [B9] Skribby billing and realtime client: https://skribby.io/docs/guides/billing.md ; https://skribby.io/docs/ts-sdk/api-reference/RealtimeClient.md
- [B10] Nylas Notetaker: https://developer.nylas.com/docs/v3/notetaker/
- [B11] Vexa interactive bots: https://docs.vexa.ai/interactive-bots

Own bot
- [C1] AgentCore Browser features and limits: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-features.html ; https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html
- [C2] AgentCore Browser Live View (DCV): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-dcv-integration.html
- [C3] Fargate pricing: https://aws.amazon.com/fargate/pricing/
- [C4] AgentCore Runtime WebRTC: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-webrtc.html

Dial-in and Meet
- [D1] Join a Meet by phone: https://support.google.com/meet/answer/9518557
- [D2] LiveKit outbound SIP calls: https://docs.livekit.io/sip/outbound-calls/
- [D3] LiveKit Cloud pricing: https://livekit.com/pricing
- [D4] Twilio Elastic SIP Trunking US pricing: https://www.twilio.com/en-us/sip-trunking/pricing/us
- [D5] Meet dial-in by edition / Global Dialing: https://support.google.com/meet/answer/9683440 ; https://knowledge.workspace.google.com/admin/meet/compare-meet-features-across-google-workspace-editions
- [D6] Calendar API event `conferenceData.entryPoints`: https://developers.google.com/workspace/calendar/api/v3/reference/events
- [E1] Meet Media API overview: https://developers.google.com/workspace/meet/media-api/guides/overview
- [E2] Meet Media API concepts: https://developers.google.com/workspace/meet/media-api/guides/concepts

Google accounts and other
- [G1] Create a Google Account with an existing email: https://support.google.com/accounts/answer/27441
- [G2] Google Workspace pricing: https://workspace.google.com/pricing
- [F1] OpenAI pricing (`gpt-4o-mini-transcribe`): https://platform.openai.com/docs/pricing

Code read (origin/main): `apps/huddle-agent/src/huddle_agent/{agent,huddle,backend}.py`,
`apps/backend/routers/huddles.py`, `apps/backend/core/services/{livekit_huddles,voice_calls}.py`,
`apps/backend/core/billing/voice_billing.py`, `apps/infra/lambda/ses-inbound/index.py`,
`apps/backend/routers/ses_inbound.py`, `apps/backend/core/services/agent_mail_inbound.py`.
